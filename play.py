#!/usr/bin/env python
"""Jev plays FOOTSIES in real time.

The game runs at 60 fps. Every `frame_skip` frames the loop sends the
current game state to Jev in a background thread and applies the newest
answer. Jev's latency shows up as reaction time, exactly like a human.

Usage:
    python play.py                     # Jev vs Jev, windowed, 3 rounds
    python play.py --p2 bot            # Jev vs a scripted bot
    python play.py --headless --rounds 1
    python play.py --sync              # wait for Jev every step (turn based)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

import footsiesgym  # noqa: E402
from footsiesgym.binary_manager import get_binary_manager  # noqa: E402
from footsiesgym.footsies.footsies_env import FootsiesEnv  # noqa: E402
from footsiesgym.footsies.game import constants  # noqa: E402

from jev_client import JevClient  # noqa: E402

FPS = 60
ACTION_NAMES = {v: k for k, v in constants.FOOTSIES_ACTION_IDS.items()}
ENV_ACTIONS = {
    "none": constants.EnvActions.NONE,
    "back": constants.EnvActions.BACK,
    "forward": constants.EnvActions.FORWARD,
    "attack": constants.EnvActions.ATTACK,
    "back_attack": constants.EnvActions.BACK_ATTACK,
    "forward_attack": constants.EnvActions.FORWARD_ATTACK,
    # The env toggles "holding the attack button" on these three.
    "hold_attack": constants.EnvActions.SPECIAL_CHARGE,
    "release_special": constants.EnvActions.SPECIAL_CHARGE,
    "release_forward_special": constants.EnvActions.FORWARD_SPECIAL_CHARGE,
}
ATTACK_IDS = {
    constants.ActionID.N_ATTACK,
    constants.ActionID.B_ATTACK,
    constants.ActionID.N_SPECIAL,
    constants.ActionID.B_SPECIAL,
}
PRICE_PER_MTOK = 0.042
NORMAL_REACH = 2.0  # measured by calibrate.py: N_ATTACK first hits at 1.98
LONG_REACH = 2.0  # B_ATTACK first hits at 2.02

RULES = (
    "You control 'me' in FOOTSIES, a 2D fighting game with one attack button. "
    "There is no health bar. You lose the round ONLY when a SPECIAL move hits you. "
    "Guard: each normal attack that touches me (hit or block) costs 1 guard_health "
    "(3 total). At 0 guard, the next normal that touches me causes GUARD_BREAK: "
    "I am stuck and the opponent gets a free special. "
    f"Normal attacks reach about {NORMAL_REACH} units. N_ATTACK is the standing kick "
    "(22 frames, hits on frame 4). B_ATTACK is the knee done while walking "
    "(forward_attack / back_attack, 21 frames, hits on frame 3); it is the whiff-punish tool. "
    "A whiffed normal leaves me stuck for the rest of its frames and can be punished. "
    "Specials: hold the attack button for 60 frames (special_charge goes 0 to 1), "
    "then release it. Pressing the button to start the hold also throws one normal. "
    "The special is 44 frames and hits on frame 11; it wins the round on hit. "
    "Cancel: when my normal attack connects (opponent shows DAMAGE or GUARD), pressing "
    "attack again cancels into the special and wins the round. Land a normal, then press "
    "attack: this is the fastest way to win. "
    "Every action has 'action_frame' out of 'action_total_frames'; an opponent late in "
    "an attack is recovering and is safe to punish. Stage x runs from -4 to 4. "
    "Choose the single best input for the next 4 frames."
)

BASE_CRITERIA = {
    "none": "Stand still. Wait for the opponent to whiff or bait an attack.",
    "back": "Walk backward. This also BLOCKS if the opponent's attack reaches me. Use when the opponent is attacking inside their reach, or to keep distance.",
    "forward": "Walk forward to close distance. Unsafe if the opponent can attack me right now.",
    "attack": (
        f"Press attack: standing kick, reach {NORMAL_REACH}, hits on frame 4. Use when the opponent is inside "
        "reach and is recovering, walking in, or in hit stun. If my normal just connected (opponent in DAMAGE "
        "or GUARD), this cancels into the special and wins."
    ),
    "back_attack": f"Knee while stepping back: reach {LONG_REACH}, hits on frame 3. Good when the opponent walks into reach.",
    "forward_attack": f"Knee while stepping in: reach {LONG_REACH}, hits on frame 3. Use to punish a whiff or to catch an approach.",
}
HOLD_CRITERIA = {
    "hold_attack": (
        "Press and hold the attack button: throws one normal now, then charges a special over 60 frames. "
        "Good when the opponent is out of reach, or as a normal that can be released as a special later."
    ),
}
HOLDING_CRITERIA = {
    "none": "Keep holding the attack button and stand still.",
    "back": "Keep holding and walk backward (still blocks).",
    "forward": "Keep holding and walk forward.",
    "release_special": (
        "Release the button: neutral SPECIAL, hits on frame 11, wins the round on hit. "
        "Use when special_ready is true and the opponent is inside reach and not blocking. "
        "If special_ready is false this only drops the charge."
    ),
    "release_forward_special": (
        "Release while stepping in: advancing SPECIAL, wins on hit. Use when special_ready is true and the "
        "opponent is just outside reach or walking in."
    ),
}
DANGER_QUESTION = {
    "type": "noul",
    "instructions": "Is the opponent about to hit me within the next few frames?",
}


def build_questions(holding: bool) -> dict:
    criteria = dict(HOLDING_CRITERIA) if holding else {**BASE_CRITERIA, **HOLD_CRITERIA}
    return {
        "move": {"type": "choice", "instructions": RULES, "criteria": criteria},
        "danger": DANGER_QUESTION,
    }


# ---------------------------------------------------------------------------
# Game launch
# ---------------------------------------------------------------------------
def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def launch_footsies(headless: bool, port: int, wait_s: float = 60.0) -> subprocess.Popen:
    system = platform.system().lower()
    if system == "darwin":
        binary_platform = "mac"
    elif system == "linux":
        binary_platform = "linux"
    else:
        raise RuntimeError("Only macOS and Linux are supported")
    bm = get_binary_manager()
    root = Path(footsiesgym.__file__).resolve().parent
    subdir = "footsies_binaries_headless" if headless else "footsies_binaries_windowed"
    exe = root / "binaries" / subdir / bm.executable_relpath(binary_platform, headless)
    if not exe.exists():
        ok = bm.ensure_binaries_extracted(
            binary_platform, target_dir=str(root / "binaries"), headless=headless
        )
        if not ok or not exe.exists():
            raise FileNotFoundError(f"Footsies binary missing at {exe}")
    os.chmod(exe, 0o755)
    cmd = [str(exe), "-batchmode", "--grpc", "--port", str(port)]
    if binary_platform == "mac":
        sign_target = exe if headless else exe.parents[1]
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(sign_target)],
            check=False,
            capture_output=True,
        )
        cmd = ["arch", "-x86_64"] + cmd  # Grpc.Core plugin is x86_64 only
    log_path = HERE / "runs" / "footsies_server.log"
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if proc.poll() is not None:
            raise RuntimeError(f"Footsies exited early, see {log_path}")
        if port_is_open(port):
            print(f"Footsies ready on port {port} after {time.time() - t0:.1f}s")
            return proc
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"Footsies did not open port {port} in {wait_s}s")


# ---------------------------------------------------------------------------
# State description for Jev
# ---------------------------------------------------------------------------
def player_view(p) -> dict:
    return {
        "x": round(p.player_position_x, 2),
        "velocity_x": round(p.velocity_x, 2),
        "action": ACTION_NAMES.get(p.current_action_id, str(p.current_action_id)),
        "action_frame": p.current_action_frame,
        "action_total_frames": p.current_action_frame_count,
        "is_attacking": p.current_action_id in ATTACK_IDS,
        "guard_health": p.guard_health,
        "in_hit_stun": p.is_in_hit_stun,
        "special_charge": round(p.special_attack_progress, 2),
        "frame_advantage": p.current_frame_advantage,
    }


def describe(me, opp, frame: int, holding: bool) -> dict:
    dist = abs(me.player_position_x - opp.player_position_x)
    mine = player_view(me)
    mine["holding_attack_button"] = holding
    mine["special_ready"] = holding and me.special_attack_progress >= 1.0
    return {
        "frame": frame,
        "distance": round(dist, 2),
        "opponent_side": "right" if opp.player_position_x > me.player_position_x else "left",
        "in_short_reach": dist <= NORMAL_REACH,
        "in_long_reach": dist <= LONG_REACH,
        "me": mine,
        "opponent": player_view(opp),
    }


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
class JevAgent:
    """Background thread. Always evaluates the newest state it was given."""

    def __init__(self, name: str, client: JevClient, log):
        self.name = name
        self.client = client
        self.log = log
        self.action = constants.EnvActions.NONE
        self.last_answer: dict | None = None
        self.latencies: list[float] = []
        self.input_tokens = 0
        self.errors = 0
        self._state = None
        self._state_frame = -1
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def submit(self, state: dict):
        with self._cv:
            self._state = state
            self._state_frame = state["frame"]
            self._cv.notify()

    def decide_now(self, state: dict) -> int:
        """Synchronous decision, for --sync mode."""
        self._evaluate(state)
        return self.action

    def _run(self):
        last_frame = -1
        while True:
            with self._cv:
                while not self._stop and self._state_frame == last_frame:
                    self._cv.wait()
                if self._stop:
                    return
                state = self._state
                last_frame = self._state_frame
            self._evaluate(state)

    def _evaluate(self, state: dict):
        questions = build_questions(state["me"]["holding_attack_button"])
        try:
            answers, ms, usage = self.client.decide(state, questions)
        except Exception as e:  # keep playing on transient errors
            self.errors += 1
            if self.errors <= 3:
                print(f"[{self.name}] jev error: {e}", file=sys.stderr)
            return
        move = answers["move"]
        self.action = ENV_ACTIONS[move["choice"]]
        self.last_answer = {
            "choice": move["choice"],
            "confidence": move.get("confidence"),
            "danger": answers.get("danger", {}).get("noul"),
            "ms": ms,
        }
        self.latencies.append(ms)
        self.input_tokens += usage.get("input_tokens", 0)
        self.log.write(
            json.dumps(
                {
                    "t": time.time(),
                    "player": self.name,
                    "frame": state["frame"],
                    "latency_ms": round(ms, 1),
                    "choice": move["choice"],
                    "confidence": move.get("confidence"),
                    "probabilities": move.get("probabilities"),
                    "danger": self.last_answer["danger"],
                    "input_tokens": usage.get("input_tokens"),
                }
            )
            + "\n"
        )


class ScriptedBot:
    """Simple footsies bot: approach, poke in range, block when threatened."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.action = constants.EnvActions.NONE
        self.last_answer = None
        self.latencies: list[float] = []
        self.input_tokens = 0
        self.errors = 0

    def start(self):
        pass

    def stop(self):
        pass

    def submit(self, state: dict):
        self.action = self.decide_now(state)

    def decide_now(self, state: dict) -> int:
        d = state["distance"]
        opp = state["opponent"]
        if opp["is_attacking"] and d < 2.2:
            return constants.EnvActions.BACK
        if d <= 1.2:
            return constants.EnvActions.ATTACK if self.rng.random() < 0.7 else constants.EnvActions.BACK
        if d <= 1.9 and self.rng.random() < 0.25:
            return constants.EnvActions.FORWARD_ATTACK
        if d > 1.9:
            return constants.EnvActions.FORWARD if self.rng.random() < 0.85 else constants.EnvActions.NONE
        return constants.EnvActions.NONE


class IdleBot(ScriptedBot):
    def decide_now(self, state: dict) -> int:
        return constants.EnvActions.NONE


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def make_agent(kind: str, name: str, client: JevClient | None, log, rng):
    if kind == "jev":
        return JevAgent(name, client, log)
    if kind == "bot":
        return ScriptedBot(rng)
    if kind == "idle":
        return IdleBot(rng)
    raise ValueError(kind)


def fmt_ms(values: list[float]) -> str:
    if not values:
        return "n/a"
    s = sorted(values)
    p90 = s[min(len(s) - 1, int(0.9 * len(s)))]
    return f"median {statistics.median(s):.0f} ms, p90 {p90:.0f} ms, min {s[0]:.0f}, max {s[-1]:.0f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p1", choices=["jev", "bot", "idle"], default="jev")
    ap.add_argument("--p2", choices=["jev", "bot", "idle"], default="jev")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--frame-skip", type=int, default=4, help="frames per decision step")
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 = real time, 0.5 = half speed")
    ap.add_argument("--sync", action="store_true", help="wait for Jev on every step")
    ap.add_argument("--max-seconds", type=float, default=90.0, help="round time limit")
    ap.add_argument("--model", default=os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest"))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    port = args.port or free_port()
    runs = HERE / "runs"
    runs.mkdir(exist_ok=True)
    log_path = runs / time.strftime("decisions_%Y%m%d_%H%M%S.jsonl")
    log = open(log_path, "w", buffering=1)  # line buffered: readable while running
    sys.stdout.reconfigure(line_buffering=True)

    client = None
    if "jev" in (args.p1, args.p2):
        client = JevClient(model=args.model)
        print(f"Jev warm-up: {client.warm_up():.0f} ms (model {client.model})")

    server = launch_footsies(args.headless, port)
    env = FootsiesEnv(
        config={
            "port": port,
            "headless": args.headless,
            "launch_binaries": False,
            "action_delay": 0,
            "frame_skip": args.frame_skip,
            "max_t": int(args.max_seconds * FPS / args.frame_skip),
            "use_special_charge_action": True,
        }
    )
    agents = {
        "p1": make_agent(args.p1, "p1", client, log, rng),
        "p2": make_agent(args.p2, "p2", client, log, rng),
    }
    for a in agents.values():
        a.start()

    wins = {"p1": 0, "p2": 0, "draw": 0}
    step_dt = args.frame_skip / FPS / max(args.speed, 1e-6)
    t_match = time.time()
    try:
        for rnd in range(1, args.rounds + 1):
            env.reset()
            state = env.last_game_state
            frame = 0
            t_round = time.time()
            next_tick = time.perf_counter()
            while True:
                holding = env._holding_special_charge
                views = {
                    "p1": describe(state.player1, state.player2, frame, holding["p1"]),
                    "p2": describe(state.player2, state.player1, frame, holding["p2"]),
                }
                actions = {}
                for pid, agent in agents.items():
                    if args.sync:
                        actions[pid] = agent.decide_now(views[pid])
                    else:
                        agent.submit(views[pid])
                        actions[pid] = agent.action
                    # A toggle answer computed for an older hold state must not
                    # flip the hold the wrong way.
                    if actions[pid] in (
                        constants.EnvActions.SPECIAL_CHARGE,
                        constants.EnvActions.FORWARD_SPECIAL_CHARGE,
                        constants.EnvActions.BACK_SPECIAL_CHARGE,
                    ):
                        la = getattr(agent, "last_answer", None)
                        wants_release = bool(la and la["choice"].startswith("release"))
                        if wants_release != holding[pid]:
                            actions[pid] = constants.EnvActions.NONE
                        else:
                            agent.action = constants.EnvActions.NONE  # toggle once
                _, _, term, trunc, _ = env.step(actions)
                state = env.last_game_state
                frame += args.frame_skip

                if frame % (FPS // 2 // args.frame_skip * args.frame_skip) == 0:
                    line = []
                    for pid, agent in agents.items():
                        la = agent.last_answer
                        p = state.player1 if pid == "p1" else state.player2
                        tag = f"{pid} {ACTION_NAMES.get(p.current_action_id, '?'):<12} g{p.guard_health}"
                        if la:
                            tag += f" jev={la['choice']:<14} conf={la['confidence'] if la['confidence'] is not None else 0:.2f} {la['ms']:.0f}ms"
                        line.append(tag)
                    print(f"r{rnd} f{frame:4d} d={views['p1']['distance']:.2f} | " + " | ".join(line))

                if any(term.values()) or any(trunc.values()):
                    break
                if not args.sync:
                    next_tick += step_dt
                    sleep = next_tick - time.perf_counter()
                    if sleep > 0:
                        time.sleep(sleep)
                    else:
                        next_tick = time.perf_counter()

            p1_dead, p2_dead = state.player1.is_dead, state.player2.is_dead
            if p1_dead and not p2_dead:
                winner = "p2"
            elif p2_dead and not p1_dead:
                winner = "p1"
            else:
                winner = "draw"
            wins[winner] += 1
            print(f"=== Round {rnd}: {winner} wins after {frame} frames ({time.time() - t_round:.1f}s) ===")
    finally:
        for a in agents.values():
            a.stop()
        env.close()
        server.terminate()
        log.close()

    total_tokens = sum(a.input_tokens for a in agents.values())
    print("\n=== Match summary ===")
    print(f"rounds: p1 {wins['p1']} - p2 {wins['p2']} (draws {wins['draw']}), wall time {time.time() - t_match:.1f}s")
    for pid, agent in agents.items():
        if isinstance(agent, JevAgent):
            n = len(agent.latencies)
            print(f"{pid}: {n} Jev decisions, {fmt_ms(agent.latencies)}, errors {agent.errors}")
    if total_tokens:
        print(f"input tokens: {total_tokens:,}  cost: ${total_tokens / 1e6 * PRICE_PER_MTOK:.4f}")
    print(f"decision log: {log_path}")


if __name__ == "__main__":
    main()
