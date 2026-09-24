#!/usr/bin/env python
"""Jev plays Street Fighter III: 3rd Strike in MAME, in real time.

MAME runs the game at 60 fps. bridge.lua streams the state every frame.
Each player has a background thread that keeps asking Jev "which move?"
on the newest state. When an answer arrives, the move is sent to MAME as
a held input (walk, crouch, block) or a frame-exact sequence (specials).

Usage:
    python sf3/play_sf3.py                     # Jev (Ryu) vs Jev (Ken)
    python sf3/play_sf3.py --p2 idle           # p2 stands still
    python sf3/play_sf3.py --p1 ryu --p2 chun-li --headless
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
from collections import deque
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
load_dotenv(ROOT / ".env")

from jev_client import JevClient  # noqa: E402
from mame_env import CHARACTERS, MameBridge, view  # noqa: E402

PRICE_PER_MTOK = 0.042
MAX_HP = 160

# Moves: ("hold", keys) stays until the next decision. ("seq", steps) plays
# frame-exact inputs then returns to neutral. f/b are toward/away from the
# opponent; bridge.lua resolves them.
MOVES = {
    "stand": ("hold", ""),
    "walk_forward": ("hold", "f"),
    "walk_back": ("hold", "b"),
    "crouch": ("hold", "d"),
    "crouch_block": ("hold", "d+b"),
    "dash_forward": ("seq", "f:2;n:2;f:2"),
    "dash_back": ("seq", "b:2;n:2;b:2"),
    "jump_forward": ("seq", "u+f:3"),
    "jump_back": ("seq", "u+b:3"),
    "jab": ("seq", "LP:2"),
    "strong_punch": ("seq", "MP:2"),
    "fierce_punch": ("seq", "HP:2"),
    "short_kick": ("seq", "LK:2"),
    "forward_kick": ("seq", "MK:2"),
    "roundhouse": ("seq", "HK:2"),
    "crouch_short": ("seq", "d+LK:2"),
    "crouch_forward_kick": ("seq", "d+MK:2"),
    "sweep": ("seq", "d+HK:2"),
    "crouch_fierce": ("seq", "d+HP:2"),
    "throw": ("seq", "f+LP+LK:3"),
    "hadouken": ("seq", "d:2;d+f:2;f:2;f+MP:2"),
    "ex_hadouken": ("seq", "d:2;d+f:2;f:2;f+MP+HP:2"),
    "shoryuken": ("seq", "f:2;n:1;d:2;d+f+HP:2"),
    "tatsumaki": ("seq", "d:2;d+b:2;b:2;b+MK:2"),
    "super_art": ("seq", "d:2;d+f:2;f:2;n:1;d:2;d+f:2;f+HP:3"),
    "parry_high": ("seq", "f:2;n:2"),
    "parry_low": ("seq", "d:2;n:2"),
}

RULES = (
    "You control 'me' in Street Fighter III: 3rd Strike (Ryu / Ken style shoto). "
    f"Health starts at {MAX_HP}; the round ends when a player reaches 0 or the timer runs out. "
    "x is in pixels; about 40 px apart the fighters touch, jabs reach about 60 px, "
    "roundhouse and sweep about 90 px, and hadouken is a full-screen fireball. "
    "Block by walking back (highs) or crouch_block (lows and most attacks). "
    "Parry: tap forward (high) or down (low) exactly as an attack lands; on success "
    "the opponent is punishable. Throw needs about 40 px. Shoryuken is invincible on "
    "startup and beats jump-ins but whiffs are punished. Super art needs a super stock "
    "and does big damage; it can be canceled from a hit crouch_forward_kick. "
    "When 'opponent.attacking' is true and they are close, block or parry. When the "
    "opponent is in_recovery or busy and close, punish. When the opponent is airborne "
    "and close, use crouch_fierce or shoryuken. At long range a hadouken is fine once, "
    "but two fireballs only trade; after that walk in, dash in, or jump forward over "
    "the opponent's fireball and attack. 'me.last_moves' lists my recent moves, newest "
    "last: do not pick the same move three times in a row, the opponent adapts. "
    "Mix pokes, throws, blocks and specials like a strong human player. "
    "Choose the single best move to start right now."
)

MOVE_CRITERIA = {
    "stand": "Do nothing this moment; wait and watch.",
    "walk_forward": "Walk toward the opponent to close distance.",
    "walk_back": "Walk away. Also blocks high and mid attacks while walking back.",
    "crouch": "Crouch in place; ducks under some attacks.",
    "crouch_block": "Crouch and hold back: blocks low attacks and most normals. Safest option when the opponent attacks up close.",
    "dash_forward": "Quick forward dash to close a mid distance gap.",
    "dash_back": "Quick backdash to escape pressure.",
    "jump_forward": "Jump toward the opponent. Risky against shoryuken or crouch_fierce.",
    "jump_back": "Jump away, for example over a fireball.",
    "jab": "Fast light punch: safe poke up close, good for interrupting.",
    "strong_punch": "Medium punch: solid mid-range poke.",
    "fierce_punch": "Heavy punch: slow, high damage, punish tool up close.",
    "short_kick": "Light kick up close.",
    "forward_kick": "Medium kick: good range poke at about 70 px.",
    "roundhouse": "Heavy kick: long range, slow; punish or long-range poke.",
    "crouch_short": "Crouching light kick: fast low hit, starts combos.",
    "crouch_forward_kick": "Crouching medium kick: the best low poke; cancel into hadouken or super if it hits.",
    "sweep": "Crouching heavy kick: knocks down, punishable on block.",
    "crouch_fierce": "Crouching heavy punch: anti-air when the opponent jumps in.",
    "throw": "Grab the opponent when within about 40 px; beats blocking.",
    "hadouken": "Fireball: strong at long range, unsafe when the opponent is close or jumping.",
    "ex_hadouken": "Faster two-hit fireball; costs part of a super stock.",
    "shoryuken": "Dragon punch: invincible anti-air and reversal; heavy punish if it whiffs or is blocked.",
    "tatsumaki": "Hurricane kick: moves forward, hits standing opponents; whiffs on crouchers.",
    "super_art": "Super fireball (needs a super stock): huge damage, use to punish or on a confirmed hit.",
    "parry_high": "Tap forward to parry a high or mid attack that is about to land.",
    "parry_low": "Tap down to parry a low attack that is about to land.",
}

QUESTIONS = {
    "move": {"type": "choice", "instructions": RULES, "criteria": MOVE_CRITERIA},
    "danger": {"type": "noul", "instructions": "Is the opponent about to hit me within the next few frames?"},
}


class JevAgent:
    def __init__(self, name: str, player: int, client: JevClient, bridge: MameBridge, log, sample: bool = True, seed: int = 0):
        self.name, self.player, self.client, self.bridge, self.log = name, player, client, bridge, log
        self.sample = sample
        self.rng = random.Random(seed + player)
        self.history: deque[str] = deque(maxlen=4)
        self.latencies: list[float] = []
        self.input_tokens = 0
        self.errors = 0
        self.decisions = 0
        self.last_answer: dict | None = None
        self._state = None
        self._state_frame = -1
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def submit(self, state: dict):
        with self._cv:
            self._state, self._state_frame = state, state["frame"]
            self._cv.notify()

    def _run(self):
        last = -1
        while True:
            with self._cv:
                while not self._stop and self._state_frame == last:
                    self._cv.wait()
                if self._stop:
                    return
                state, last = self._state, self._state_frame
            self._evaluate(state)

    def _pick(self, move: dict) -> str:
        """Sample from Jev's distribution so play has variety; top choice otherwise."""
        probs = move.get("probabilities") or {}
        if not self.sample or not probs:
            return move["choice"]
        # drop the long tail, then sample proportionally
        items = [(k, p) for k, p in probs.items() if p >= 0.04 and k in MOVES]
        if not items:
            return move["choice"]
        names, weights = zip(*items)
        return self.rng.choices(names, weights=weights, k=1)[0]

    def _evaluate(self, state: dict):
        state["me"]["last_moves"] = list(self.history)
        opp = state["opponent"]
        opp["likely_fireball"] = bool(opp["attacking"] and state["distance"] > 110)
        try:
            answers, ms, usage = self.client.decide(state, QUESTIONS)
        except Exception as e:
            self.errors += 1
            if self.errors <= 3:
                print(f"[{self.name}] jev error: {e}", file=sys.stderr)
            return
        move = answers["move"]
        name = self._pick(move)
        self.history.append(name)
        kind, arg = MOVES[name]
        if kind == "hold":
            self.bridge.hold(self.player, arg)
        else:
            self.bridge.seq(self.player, arg)
        self.decisions += 1
        self.latencies.append(ms)
        self.input_tokens += usage.get("input_tokens", 0)
        self.last_answer = {"choice": name, "confidence": move.get("confidence"), "ms": ms,
                            "danger": answers.get("danger", {}).get("noul")}
        self.bridge.send(f"O {self.player} {name}|{ms:.0f}|{move.get('confidence') or 0:.2f}|{self.decisions}")
        self.log.write(json.dumps({
            "t": time.time(), "player": self.name, "frame": state["frame"], "machine_time": state.get("machine_time"),
            "latency_ms": round(ms, 1),
            "choice": name, "top": move["choice"], "confidence": move.get("confidence"), "probabilities": move.get("probabilities"),
            "danger": self.last_answer["danger"], "input_tokens": usage.get("input_tokens"),
        }) + "\n")


class IdleAgent:
    def __init__(self, player: int, bridge: MameBridge):
        self.player, self.bridge = player, bridge
        self.latencies, self.input_tokens, self.errors, self.decisions, self.last_answer = [], 0, 0, 0, None

    def start(self):
        self.bridge.hold(self.player, "")

    def stop(self):
        pass

    def submit(self, state):
        pass


def fmt_ms(values):
    if not values:
        return "n/a"
    s = sorted(values)
    return f"median {statistics.median(s):.0f} ms, p90 {s[int(0.9 * (len(s) - 1))]:.0f} ms, min {s[0]:.0f}, max {s[-1]:.0f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p1", default="ryu", help="character for player 1 (or 'idle:<char>')")
    ap.add_argument("--p2", default="ken", help="character for player 2 (or 'idle:<char>')")
    ap.add_argument("--rom-dir", default=str(HERE / "roms"))
    ap.add_argument("--game", default="sfiii3n", help="MAME set name: sfiii3n (990608 no CD) or sfiii3nr1 (990512 no CD)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument("--decision-frames", type=int, default=4, help="send a new state to Jev every N frames")
    ap.add_argument("--max-seconds", type=float, default=300)
    ap.add_argument("--model", default=os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest"))
    ap.add_argument("--argmax", action="store_true", help="always play Jev's top choice instead of sampling")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--record", metavar="FILE.avi", help="record the MAME screen with audio (MAME -aviwrite)")
    ap.add_argument("--snapshot-at", type=float, default=0, help="save a PNG this many seconds into the fight")
    ap.add_argument("--footer", default="Jev picks every move from the live game state. No screenshots, no vision model.")
    args = ap.parse_args()

    def parse(spec: str):
        idle = spec.startswith("idle:") or spec == "idle"
        char = spec.split(":", 1)[1] if ":" in spec else ("ryu" if idle else spec)
        if char not in CHARACTERS:
            ap.error(f"unknown character {char}; choose from {', '.join(CHARACTERS)}")
        return idle, char

    p1_idle, p1_char = parse(args.p1)
    p2_idle, p2_char = parse(args.p2)

    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    log_path = runs / time.strftime("sf3_decisions_%Y%m%d_%H%M%S.jsonl")
    log = open(log_path, "w", buffering=1)
    sys.stdout.reconfigure(line_buffering=True)

    client = None
    if not (p1_idle and p2_idle):
        client = JevClient(model=args.model)
        print(f"Jev warm-up: {client.warm_up():.0f} ms (model {client.model})")

    extra = ["-snapshot_directory", str(runs / "snap")]
    if args.record:
        extra += ["-aviwrite", str(Path(args.record).resolve())]
    bridge = MameBridge(args.rom_dir, game=args.game, window=not args.headless, sound=not args.no_sound, extra_args=extra)
    print("MAME:", bridge.wait_ready())
    bridge.auto_start(p1_char, p2_char)
    st = bridge.wait_fight()
    print(f"fight started: {p1_char} vs {p2_char}, frame {st.get('frame')}")
    bridge.send("T " + args.footer)
    snapshot_due = time.time() + args.snapshot_at if args.snapshot_at else None

    agents = {
        1: IdleAgent(1, bridge) if p1_idle else JevAgent("p1", 1, client, bridge, log, not args.argmax, args.seed),
        2: IdleAgent(2, bridge) if p2_idle else JevAgent("p2", 2, client, bridge, log, not args.argmax, args.seed),
    }
    for a in agents.values():
        a.start()

    t0 = time.time()
    seq = bridge.state_seq
    last_frame = -1
    last_print = 0
    wins = {1: 0, 2: 0}
    try:
        while time.time() - t0 < args.max_seconds:
            st = bridge.wait_state(seq, timeout=2.0)
            seq = bridge.state_seq
            frame = st.get("frame", 0)
            if frame == last_frame:
                continue
            last_frame = frame
            wins = {1: st.get("wins1", 0), 2: st.get("wins2", 0)}
            if st.get("fighting", 0) and frame % args.decision_frames == 0:
                for p, a in agents.items():
                    a.submit(view(st, p))
            if snapshot_due and time.time() >= snapshot_due:
                bridge.send("N")
                snapshot_due = None
            if time.time() - last_print >= 0.5:
                last_print = time.time()
                cells = []
                for p, a in agents.items():
                    la = a.last_answer
                    cell = f"p{p} hp={st.get(f'p{p}_hp', 0):3d} {'ATK' if st.get(f'p{p}_attacking') else '   '}"
                    if la:
                        cell += f" jev={la['choice']:<19} conf={la['confidence'] or 0:.2f} {la['ms']:.0f}ms"
                    cells.append(cell)
                print(f"f{frame:6d} t={st.get('timer', 0):3d} fight={st.get('fighting', 0)} wins={wins[1]}-{wins[2]} d={abs(st.get('p1_x', 0) - st.get('p2_x', 0)):3d} | " + " | ".join(cells))
            if wins[1] >= 2 or wins[2] >= 2:
                print(f"=== match over: p1 {wins[1]} - p2 {wins[2]} ===")
                break
    finally:
        for a in agents.values():
            a.stop()
        bridge.close()
        log.close()

    print("\n=== Match summary ===")
    print(f"rounds: p1 {wins[1]} - p2 {wins[2]}, wall time {time.time() - t0:.1f}s")
    total = 0
    for p, a in agents.items():
        if isinstance(a, JevAgent):
            total += a.input_tokens
            print(f"p{p}: {a.decisions} Jev decisions, {fmt_ms(a.latencies)}, errors {a.errors}")
    if total:
        print(f"input tokens: {total:,}  cost: ${total / 1e6 * PRICE_PER_MTOK:.4f}")
    print(f"decision log: {log_path}")


if __name__ == "__main__":
    main()
