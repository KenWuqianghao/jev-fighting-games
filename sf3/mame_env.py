"""Run MAME with bridge.lua and talk to it over a local TCP socket.

The game runs on its own clock (real time). Python reads the newest state
and sends input commands. Nothing here blocks the emulator.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "bridge.lua"
ROM = "sfiii3n"

CHARACTERS = {
    "gill": 0, "alex": 1, "ryu": 2, "yun": 3, "dudley": 4, "necro": 5, "hugo": 6,
    "ibuki": 7, "elena": 8, "oro": 9, "yang": 10, "ken": 11, "sean": 12,
    "urien": 13, "gouki": 14, "chun-li": 16, "makoto": 17, "q": 18,
    "twelve": 19, "remy": 20,
}
CHARACTER_NAMES = {v: k for k, v in CHARACTERS.items()}

POSTURES = {
    0x00: "standing", 0x08: "walking_back", 0x06: "walking_forward", 0x20: "crouching",
    0x16: "jumping", 0x14: "jumping_forward", 0x18: "jumping_back", 0x1A: "high_jump",
    0x26: "knocked_down",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MameBridge:
    def __init__(
        self,
        rom_dir: str | os.PathLike,
        *,
        game: str = ROM,
        mame: str | None = None,
        window: bool = True,
        sound: bool = True,
        probe: bool = False,
        extra_args: list[str] | None = None,
        work_dir: str | os.PathLike | None = None,
    ):
        self.mame = mame or shutil.which("mame") or "mame"
        self.game = game
        self.rom_dir = str(Path(rom_dir).resolve())
        self.port = free_port()
        self.probe = probe
        self.work_dir = Path(work_dir or HERE.parent / "runs" / "mame").resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, int] = {}
        self.state_seq = 0
        self.events: list[str] = []
        self._cv = threading.Condition()
        self._stop = False
        self.proc: subprocess.Popen | None = None
        self.conn: socket.socket | None = None
        self._log = open(self.work_dir / "mame_stdout.log", "w")

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.port))
        self._server.listen(1)

        args = [
            self.mame, game,
            "-rompath", self.rom_dir,
            "-autoboot_script", str(BRIDGE),
            "-autoboot_delay", "1",
            "-skip_gameinfo",
            "-nvram_directory", str(self.work_dir / "nvram"),
            "-cfg_directory", str(self.work_dir / "cfg"),
            "-diff_directory", str(self.work_dir / "diff"),
        ]
        args += ["-window"] if window else ["-video", "none"]
        if not sound:
            args += ["-sound", "none"]
        args += extra_args or []
        env = dict(os.environ, SF3_BRIDGE_PORT=str(self.port), SF3_PROBE="1" if probe else "0")
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT, env=env)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------ transport
    def _read_loop(self):
        self._server.settimeout(90)
        try:
            self.conn, _ = self._server.accept()
        except socket.timeout:
            with self._cv:
                self.events.append("error: MAME never connected (see runs/mame/mame_stdout.log)")
                self._cv.notify_all()
            return
        self.conn.settimeout(None)
        buf = b""
        while not self._stop:
            try:
                chunk = self.conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle(line.decode("ascii", "replace"))
        with self._cv:
            self.events.append("disconnected")
            self._cv.notify_all()

    def _handle(self, line: str):
        if line.startswith("S "):
            st = {}
            for kv in line[2:].split(" "):
                k, _, v = kv.partition("=")
                if not k:
                    continue
                try:
                    st[k] = int(v)
                except ValueError:
                    try:
                        st[k] = float(v)
                    except ValueError:
                        st[k] = v
            with self._cv:
                self.state = st
                self.state_seq += 1
                self._cv.notify_all()
        elif line.startswith("E "):
            with self._cv:
                self.events.append(line[2:])
                self._cv.notify_all()

    def send(self, line: str):
        if self.conn is None:
            raise RuntimeError("bridge not connected")
        self.conn.sendall((line + "\n").encode("ascii"))

    # ------------------------------------------------------------ api
    def wait_ready(self, timeout: float = 90.0) -> str:
        deadline = time.time() + timeout
        with self._cv:
            while time.time() < deadline:
                for e in self.events:
                    if e.startswith("ready") or e.startswith("error"):
                        return e
                self._cv.wait(0.5)
        raise TimeoutError("bridge.lua did not report ready; see runs/mame/mame_stdout.log")

    def wait_state(self, min_seq: int, timeout: float = 5.0) -> dict:
        """Block until a state newer than min_seq arrives."""
        deadline = time.time() + timeout
        with self._cv:
            while self.state_seq <= min_seq and time.time() < deadline:
                self._cv.wait(0.05)
            return dict(self.state)

    def hold(self, player: int, keys: str = ""):
        self.send(f"H {player} {keys}")

    def seq(self, player: int, seq: str):
        self.send(f"Q {player} {seq}")

    def auto_start(self, p1: str = "ryu", p2: str = "ken", super_art: int = 1):
        self.send(f"A {CHARACTERS[p1]} {CHARACTERS[p2]} {super_art - 1}")

    def wait_fight(self, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        seq = self.state_seq
        while time.time() < deadline:
            st = self.wait_state(seq, timeout=2.0)
            seq = self.state_seq
            if st.get("fighting", 0) != 0 and st.get("p1_valid") == 1 and st.get("p2_valid") == 1:
                return st
        raise TimeoutError(f"fight did not start; last state {self.state}")

    def close(self):
        self._stop = True
        try:
            if self.conn is not None:
                self.send("X")
        except OSError:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        for s in (self.conn, self._server):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        self._log.close()


def view(st: dict, me: int) -> dict:
    """State from one player's point of view, ready to send to Jev."""
    opp = 3 - me

    def player(i: int) -> dict:
        p = f"p{i}_"
        y = st.get(p + "y", 0)
        return {
            "character": CHARACTER_NAMES.get(st.get(p + "char", -1), "?"),
            "x": st.get(p + "x", 0),
            "height": y,
            "airborne": y > 0,
            "hp": st.get(p + "hp", 0),
            "posture": POSTURES.get(st.get(p + "posture", 0), f"0x{st.get(p + 'posture', 0):02X}"),
            "attacking": st.get(p + "attacking", 0) != 0,
            "busy": st.get(p + "busy", 0) != 0,
            "in_recovery": st.get(p + "recovery", 0) != 0,
            "blocking": st.get(p + "blocking", 0) != 0,
            "freeze_frames": st.get(p + "freeze", 0),
            "being_thrown": st.get(p + "thrown", 0) != 0,
            "super_stocks": st.get(p + "stocks", 0),
            "super_gauge": st.get(p + "gauge", 0),
            "stun": st.get(p + "stun", 0),
            "stunned": st.get(p + "stun_timer", 0) > 0,
            "action_id": st.get(p + "action", 0),
        }

    mx, ox = st.get(f"p{me}_x", 0), st.get(f"p{opp}_x", 0)
    return {
        "frame": st.get("frame", 0),
        "machine_time": st.get("mt", 0.0),
        "timer": st.get("timer", 0),
        "round_wins": {"me": st.get(f"wins{me}", 0), "opponent": st.get(f"wins{opp}", 0)},
        "distance": abs(mx - ox),
        "opponent_side": "right" if ox > mx else "left",
        "me": player(me),
        "opponent": player(opp),
    }
