#!/usr/bin/env python
"""Measure attack reach and timing in FOOTSIES (headless).

p1 walks forward. p2 stands still and attacks nonstop. We record the
distance at which p1 first takes a hit, for the normal attack and for
the forward attack. The numbers go into the Jev rules text in play.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from footsiesgym.footsies.footsies_env import FootsiesEnv  # noqa: E402
from footsiesgym.footsies.game import constants  # noqa: E402

from play import ACTION_NAMES, free_port, launch_footsies  # noqa: E402


def run(env: FootsiesEnv, p2_action: int, label: str, p1_action: int = constants.EnvActions.FORWARD):
    env.reset()
    s = env.last_game_state
    moves = {}
    hits = 0
    prev_p1_action = None
    for step in range(900):
        # The attack button fires on press, so p2 taps: 1 frame on, 24 frames off.
        p2 = p2_action if step % 25 == 0 else constants.EnvActions.NONE
        _, _, term, trunc, _ = env.step({"p1": p1_action, "p2": p2})
        s = env.last_game_state
        p1s, p2s = s.player1, s.player2
        if p2s.current_action_id in (constants.ActionID.N_ATTACK, constants.ActionID.B_ATTACK, constants.ActionID.N_SPECIAL, constants.ActionID.B_SPECIAL):
            moves[ACTION_NAMES[p2s.current_action_id]] = p2s.current_action_frame_count
        if p1s.current_action_id != prev_p1_action and p1s.current_action_id in (constants.ActionID.DAMAGE, constants.ActionID.GUARD_STAND, constants.ActionID.GUARD_CROUCH, constants.ActionID.GUARD_M, constants.ActionID.GUARD_BREAK, constants.ActionID.DEAD):
            hits += 1
            dist = abs(p1s.player_position_x - p2s.player_position_x)
            print(f"{label}: hit {hits} -> p1 {ACTION_NAMES[p1s.current_action_id]} at distance {dist:.2f}, p1 guard={p1s.guard_health} vital={p1s.vital_health} dead={p1s.is_dead}; p2 move={ACTION_NAMES.get(p2s.current_action_id)} frame {p2s.current_action_frame}/{p2s.current_action_frame_count}")
        prev_p1_action = p1s.current_action_id
        if any(term.values()) or any(trunc.values()):
            print(f"{label}: round over at step {step}, p1 dead={p1s.is_dead} p2 dead={p2s.is_dead}")
            break
    print(f"{label}: moves seen {moves}; final distance {abs(s.player1.player_position_x - s.player2.player_position_x):.2f}")


def walk_in(env, target: float, max_steps: int = 400):
    """p1 walks toward p2 until the distance is below target."""
    for _ in range(max_steps):
        env.step({"p1": constants.EnvActions.FORWARD, "p2": constants.EnvActions.NONE})
        s = env.last_game_state
        if abs(s.player1.player_position_x - s.player2.player_position_x) <= target:
            return s
    return env.last_game_state


def report(label, s):
    p1, p2 = s.player1, s.player2
    print(
        f"{label}: dist {abs(p1.player_position_x - p2.player_position_x):.2f} | "
        f"p1 {ACTION_NAMES.get(p1.current_action_id)} guard={p1.guard_health} dead={p1.is_dead} | "
        f"p2 {ACTION_NAMES.get(p2.current_action_id)} f{p2.current_action_frame}/{p2.current_action_frame_count} charge={p2.special_attack_progress:.2f}"
    )


def special_ko(env):
    """p2 holds attack ~70 frames, then releases inside reach. Does p1 die?"""
    env.reset()
    walk_in(env, 1.1)
    env.step({"p1": constants.EnvActions.NONE, "p2": constants.EnvActions.SPECIAL_CHARGE})  # toggle hold on
    for i in range(70):
        env.step({"p1": constants.EnvActions.NONE, "p2": constants.EnvActions.NONE})
        if i % 20 == 0:
            report(f"special: holding {i}", env.last_game_state)
    env.step({"p1": constants.EnvActions.NONE, "p2": constants.EnvActions.SPECIAL_CHARGE})  # toggle hold off = release
    for i in range(40):
        _, _, term, _, _ = env.step({"p1": constants.EnvActions.NONE, "p2": constants.EnvActions.NONE})
        s = env.last_game_state
        if s.player2.current_action_id in (constants.ActionID.N_SPECIAL, constants.ActionID.B_SPECIAL) and i % 4 == 0:
            report(f"special: release+{i}", s)
        if any(term.values()):
            report("special: ROUND OVER", s)
            return
    report("special: end", env.last_game_state)


def cancel_ko(env):
    """p2 lands a normal, then presses attack again on the connect frame."""
    env.reset()
    walk_in(env, 1.1)
    env.step({"p1": constants.EnvActions.NONE, "p2": constants.EnvActions.ATTACK})
    pressed_again = False
    for i in range(60):
        s = env.last_game_state
        p2_act = constants.EnvActions.NONE
        if not pressed_again and s.player1.current_action_id == constants.ActionID.DAMAGE:
            p2_act = constants.EnvActions.ATTACK
            pressed_again = True
            report(f"cancel: p1 hit at +{i}, pressing attack again", s)
        _, _, term, _, _ = env.step({"p1": constants.EnvActions.NONE, "p2": p2_act})
        s = env.last_game_state
        if s.player2.current_action_id in (constants.ActionID.N_SPECIAL, constants.ActionID.B_SPECIAL) and i % 3 == 0:
            report(f"cancel: +{i}", s)
        if any(term.values()):
            report("cancel: ROUND OVER", s)
            return
    report("cancel: end (no KO)", env.last_game_state)


def guard_break(env):
    """p1 blocks four normals in a row."""
    env.reset()
    walk_in(env, 1.1)
    for i in range(160):
        p2 = constants.EnvActions.ATTACK if i % 30 == 0 else constants.EnvActions.NONE
        _, _, term, _, _ = env.step({"p1": constants.EnvActions.BACK, "p2": p2})
        s = env.last_game_state
        if s.player1.current_action_id in (constants.ActionID.GUARD_BREAK, constants.ActionID.GUARD_STAND, constants.ActionID.GUARD_M) and s.player1.current_action_frame == 1:
            report(f"block: +{i}", s)
        if any(term.values()):
            report("block: ROUND OVER", s)
            return
    report("block: end", env.last_game_state)


def main():
    port = free_port()
    server = launch_footsies(True, port)
    env = FootsiesEnv(config={"port": port, "headless": True, "launch_binaries": False, "action_delay": 0, "frame_skip": 1, "use_special_charge_action": True})
    try:
        env.reset()
        s = env.last_game_state
        print(f"start: p1 x={s.player1.player_position_x:.2f} p2 x={s.player2.player_position_x:.2f} guard={s.player1.guard_health} vital={s.player1.vital_health}")
        run(env, constants.EnvActions.ATTACK, "normal attack")
        run(env, constants.EnvActions.FORWARD_ATTACK, "forward attack")
        special_ko(env)
        cancel_ko(env)
        guard_break(env)
    finally:
        env.close()
        server.terminate()


if __name__ == "__main__":
    main()
