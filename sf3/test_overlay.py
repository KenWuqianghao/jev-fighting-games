#!/usr/bin/env python
"""Draw a fake overlay in 3rd Strike and save a snapshot. No Jev calls."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mame_env import MameBridge  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent / "runs"


def main():
    record = "--record" in sys.argv
    extra = ["-snapshot_directory", str(RUNS / "snap")]
    if record:
        extra += ["-aviwrite", str(RUNS / "overlay_test.avi")]
    b = MameBridge(HERE / "roms", window=True, sound=False, extra_args=extra)
    try:
        print(b.wait_ready())
        b.auto_start("ryu", "ken")
        b.wait_fight()
        b.send("T Jev picks every move from the live game state. No screenshots, no vision model.")
        b.send("O 1 crouch_forward_kick|283|0.62|41")
        b.send("O 2 shoryuken|412|0.35|40")
        time.sleep(3)
        b.send("N")
        time.sleep(2)
    finally:
        b.close()


if __name__ == "__main__":
    main()
