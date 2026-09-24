#!/usr/bin/env python
"""Smoke test for bridge.lua on a ROM-free machine (pong).

Checks that MAME 0.289 accepts the socket file, the frame notifier and the
input field lookups. It does not need any ROM.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mame_env import MameBridge  # noqa: E402


def main():
    game = sys.argv[1] if len(sys.argv) > 1 else "pong"
    b = MameBridge(rom_dir=Path(__file__).resolve().parent / "roms", game=game, window=False, sound=False, probe=True,
                   extra_args=["-seconds_to_run", "6"])
    try:
        print("ready:", b.wait_ready(60))
        seq = 0
        for _ in range(3):
            st = b.wait_state(seq, timeout=5)
            seq = b.state_seq
            print("state:", {k: (v if k != "fields" else v[:200] + "...") for k, v in st.items()})
        b.send("P")
        time.sleep(0.5)
        print("events:", b.events)
    finally:
        b.close()
        print("--- mame stdout tail")
        print(open(b.work_dir / "mame_stdout.log").read()[-1500:])


if __name__ == "__main__":
    main()
