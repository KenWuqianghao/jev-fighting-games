#!/usr/bin/env python
"""Turn a recorded match (MAME AVI + Jev decision log) into a demo video.

MAME's recorder captures only the raw game pixels, so the overlay is drawn
here. Decisions are synced by MAME screen frame number: AVI frame i is
screen frame i.

    python sf3/make_video.py runs/demo.avi runs/sf3_decisions_*.jsonl -o runs/demo.mp4
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
GW, GH = 384, 224
SCALE = 2.3
GX, GY = int((W - GW * SCALE) / 2), 84  # game placement
CYAN = (0, 229, 255)
WHITE = (245, 245, 245)
DIM = (150, 150, 150)
RED = (255, 80, 80)
BG = (10, 10, 12)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]
MONO_CANDIDATES = ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf"]


def font(size: int, mono: bool = False):
    for p in (MONO_CANDIDATES if mono else FONT_CANDIDATES) + FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


F_TITLE, F_BIG, F_MED, F_MOVE, F_SMALL, F_MONO = font(34), font(44), font(24), font(22), font(18), font(22, mono=True)


def probe(avi: Path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames,duration", "-of", "json", str(avi)
    ])
    s = json.loads(out)["streams"][0]
    num, den = (int(x) for x in s["r_frame_rate"].split("/"))
    fps = num / den
    n = int(s.get("nb_frames") or float(s["duration"]) * fps)
    return fps, s["r_frame_rate"], n


def load_decisions(paths: list[Path], fps: float, game_frame_offset: int | None = None):
    rows = []
    for p in paths:
        rows += [json.loads(l) for l in open(p) if l.strip()]
    for r in rows:
        if game_frame_offset is not None:
            # fallback sync: AVI frame = game frame counter + offset
            r["screen_frame"] = r["frame"] + game_frame_offset
        elif r.get("machine_time"):
            # AVI frame i is written at emulated time i / fps
            r["screen_frame"] = int(round(r["machine_time"] * fps))
    rows = [r for r in rows if r.get("screen_frame")]
    rows.sort(key=lambda r: r["screen_frame"])
    return rows


class Panel:
    """One player's overlay state, replayed decision by decision."""

    def __init__(self, name: str, char: str, rows: list[dict]):
        self.name, self.char, self.rows = name, char, rows
        self.i = 0
        self.current: dict | None = None
        self.count = 0
        self.lat: list[float] = []
        self.flash_until = -1

    def advance(self, frame: int):
        while self.i < len(self.rows) and self.rows[self.i]["screen_frame"] <= frame:
            self.current = self.rows[self.i]
            self.count += 1
            self.lat.append(self.current["latency_ms"])
            self.flash_until = frame + 6
            self.i += 1

    def draw(self, d: ImageDraw.ImageDraw, x: int, frame: int, right: bool):
        w = GX - 16
        x0 = 8 if not right else W - w - 8
        y0 = GY
        d.rounded_rectangle([x0, y0, x0 + w, y0 + 300], radius=10, fill=(20, 20, 26), outline=CYAN, width=2)
        w = w  # panel inner width is used for wrapping below
        d.text((x0 + 12, y0 + 10), f"JEV  ·  {self.name}", font=F_SMALL, fill=CYAN)
        d.text((x0 + 12, y0 + 32), self.char.upper(), font=F_MED, fill=WHITE)
        if self.current is None:
            d.text((x0 + 12, y0 + 80), "waiting", font=F_SMALL, fill=DIM)
            return
        c = self.current
        flash = frame <= self.flash_until
        d.text((x0 + 12, y0 + 78), "move", font=F_SMALL, fill=DIM)
        words = c["choice"].split("_")
        # wrap the move name onto two lines when it does not fit the panel
        lines, cur = [], ""
        for wd in words:
            trial = (cur + " " + wd).strip()
            if d.textlength(trial, font=F_MOVE) > w - 24 and cur:
                lines.append(cur)
                cur = wd
            else:
                cur = trial
        lines.append(cur)
        for k, line in enumerate(lines[:2]):
            d.text((x0 + 12, y0 + 98 + 26 * k), line, font=F_MOVE, fill=CYAN if flash else WHITE)
        d.text((x0 + 12, y0 + 156), "decision time", font=F_SMALL, fill=DIM)
        ms = c["latency_ms"]
        d.text((x0 + 12, y0 + 174), f"{ms:.0f} ms", font=F_BIG, fill=RED if ms > 400 else WHITE)
        bw = w - 24
        d.rectangle([x0 + 12, y0 + 230, x0 + 12 + bw, y0 + 238], fill=(40, 40, 48))
        d.rectangle([x0 + 12, y0 + 230, x0 + 12 + int(bw * min(1.0, ms / 500)), y0 + 238], fill=RED if ms > 400 else CYAN)
        d.text((x0 + 12, y0 + 248), f"confidence {c.get('confidence') or 0:.2f}", font=F_SMALL, fill=DIM)
        med = statistics.median(self.lat) if self.lat else 0
        d.text((x0 + 12, y0 + 272), f"#{self.count}  ·  median {med:.0f}ms", font=F_SMALL, fill=DIM)


def render(avi: Path, rows: list[dict], out: Path, chars: tuple[str, str], title: str, footer: str,
           start_s: float | None, end_s: float | None, title_s: float, end_card_s: float,
           hold_last_s: float = 0.0):
    fps, fps_str, nframes = probe(avi)
    p1 = Panel("P1", chars[0], [r for r in rows if r["player"] == "p1"])
    p2 = Panel("P2", chars[1], [r for r in rows if r["player"] == "p2"])
    first = min(r["screen_frame"] for r in rows)
    last = max(r["screen_frame"] for r in rows)
    start_f = int(start_s * fps) if start_s is not None else max(0, first - int(1.0 * fps))
    end_f = min(nframes, int(end_s * fps)) if end_s is not None else min(nframes, last + int(3.0 * fps))
    all_lat = [r["latency_ms"] for r in rows]
    tokens = sum(r.get("input_tokens") or 0 for r in rows)
    print(f"fps {fps:.3f}, frames {start_f}..{end_f} ({(end_f - start_f) / fps:.1f}s), {len(rows)} decisions")

    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(avi), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE)
    delay_ms = int(title_s * 1000)
    enc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", fps_str, "-i", "-",
        "-ss", f"{start_f / fps:.3f}", "-i", str(avi),
        "-map", "0:v", "-map", "1:a?",
        # apad keeps silence running under the freeze and the end card; -shortest then follows the video
        "-af", f"adelay={delay_ms}|{delay_ms},volume=0.9,apad",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", "60",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out),
    ], stdin=subprocess.PIPE)

    frame_bytes = GW * GH * 3

    def emit(img: Image.Image):
        enc.stdin.write(img.tobytes())

    # title card
    card = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(card)
    d.text((W / 2, H / 2 - 40), title, font=F_TITLE, fill=WHITE, anchor="mm")
    d.text((W / 2, H / 2 + 12), "Street Fighter III: 3rd Strike  ·  both fighters controlled by Jev", font=F_MED, fill=CYAN, anchor="mm")
    d.text((W / 2, H / 2 + 52), footer, font=F_SMALL, fill=DIM, anchor="mm")
    for _ in range(int(title_s * fps)):
        emit(card)

    idx = 0
    last_canvas = None
    while True:
        buf = dec.stdout.read(frame_bytes)
        if len(buf) < frame_bytes or idx >= end_f:
            break
        if idx >= start_f:
            game = Image.frombuffer("RGB", (GW, GH), buf, "raw", "RGB", 0, 1)
            game = game.resize((int(GW * SCALE), int(GH * SCALE)), Image.NEAREST)
            canvas = Image.new("RGB", (W, H), BG)
            canvas.paste(game, (GX, GY))
            d = ImageDraw.Draw(canvas)
            d.rectangle([GX - 2, GY - 2, GX + game.width + 1, GY + game.height + 1], outline=(60, 60, 70), width=2)
            d.text((W / 2, 24), title, font=F_MED, fill=WHITE, anchor="mm")
            d.text((W / 2, 52), "live game state from emulator memory  →  Jev  →  controller input, every 4 frames", font=F_SMALL, fill=DIM, anchor="mm")
            p1.advance(idx)
            p2.advance(idx)
            p1.draw(d, 0, idx, right=False)
            p2.draw(d, 0, idx, right=True)
            d.text((W / 2, H - 34), footer, font=F_SMALL, fill=DIM, anchor="mm")
            emit(canvas)
            last_canvas = canvas
        idx += 1

    # freeze the last game frame (e.g. the KO) before the end card
    if last_canvas is not None:
        for _ in range(int(hold_last_s * fps)):
            emit(last_canvas)

    # end card
    med = statistics.median(all_lat)
    p90 = sorted(all_lat)[int(0.9 * (len(all_lat) - 1))]
    lines = [
        (f"{len(rows)} decisions by Jev in one match", F_BIG, WHITE),
        (f"median {med:.0f} ms  ·  p90 {p90:.0f} ms  (about 200 ms of that is network round trip)", F_MED, CYAN),
        (f"{tokens:,} input tokens  ·  ${tokens / 1e6 * 0.042:.3f}", F_MED, WHITE),
        ("no vision model, no screenshots: the state comes straight from the game", F_SMALL, DIM),
    ]
    card = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(card)
    y = H / 2 - 80
    for text, f, col in lines:
        d.text((W / 2, y), text, font=f, fill=col, anchor="mm")
        y += 56
    for _ in range(int(end_card_s * fps)):
        emit(card)

    enc.stdin.close()
    dec.kill()
    enc.wait()
    print("wrote", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("avi")
    ap.add_argument("decisions", nargs="+")
    ap.add_argument("-o", "--out", default="runs/demo.mp4")
    ap.add_argument("--p1", default="ryu")
    ap.add_argument("--p2", default="ken")
    ap.add_argument("--title", default="Jev plays 3rd Strike in real time")
    ap.add_argument("--footer", default="Jev by TypeSafe AI · no vision model · state read from MAME memory")
    ap.add_argument("--start", type=float, default=None, help="start second in the AVI (default: 1 s before the first decision)")
    ap.add_argument("--end", type=float, default=None, help="end second in the AVI (default: 3 s after the last decision)")
    ap.add_argument("--title-seconds", type=float, default=2.5)
    ap.add_argument("--end-seconds", type=float, default=4.0)
    ap.add_argument("--hold-last", type=float, default=0.0, help="freeze the last game frame for this many seconds")
    ap.add_argument("--game-frame-offset", type=int, default=None,
                    help="sync by the game's frame counter instead of screen_frame: AVI frame = game frame + offset")
    args = ap.parse_args()
    fps, _, _ = probe(Path(args.avi))
    rows = load_decisions([Path(p) for p in args.decisions], fps, args.game_frame_offset)
    if not rows:
        raise SystemExit("no decisions with sync info; pass --game-frame-offset")
    render(Path(args.avi), rows, Path(args.out), (args.p1, args.p2), args.title, args.footer,
           args.start, args.end, args.title_seconds, args.end_seconds, args.hold_last)


if __name__ == "__main__":
    main()
