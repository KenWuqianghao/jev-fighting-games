# Jev plays fighting games

Jev controls fighters in real time. The game state comes from the engine or the
emulator memory. No vision model. No screenshots.

Two games:

| Game | Source of state | Status |
|---|---|---|
| FOOTSIES | Unity build over gRPC (`footsies-gym`) | Works. 3-round Jev vs Jev match done. |
| Street Fighter III: 3rd Strike | MAME memory over a Lua bridge | Works. Jev vs Jev matches done (Ryu vs Ken). |

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python footsies-gym requests python-dotenv
echo "TYPESAFE_API_KEY=..." > .env
brew install mame          # for 3rd Strike
```

macOS needs Rosetta 2 for the Footsies binary.

## FOOTSIES

```bash
.venv/bin/python play.py                  # Jev vs Jev, windowed, 3 rounds
.venv/bin/python play.py --p2 bot         # Jev vs scripted bot
.venv/bin/python play.py --headless --rounds 1
.venv/bin/python calibrate.py             # measure attack reach
```

## Street Fighter III: 3rd Strike

1. Put `sfiii3n.zip` (the MAME set "Street Fighter III 3rd Strike, Japan 990512, NO CD")
   in `sf3/roms/`. You must supply this file.
2. Check it:

```bash
mame -rompath sf3/roms -verifyroms sfiii3n
```

3. Play:

```bash
.venv/bin/python sf3/play_sf3.py                     # Jev (Ryu) vs Jev (Ken)
.venv/bin/python sf3/play_sf3.py --p2 idle:ken       # p2 stands still
.venv/bin/python sf3/play_sf3.py --p1 ryu --p2 chun-li --no-sound
```

`sf3/probe.py` runs the bridge on `pong` (no ROM) to check the MAME Lua API.

### Overlay and demo video

The bridge draws a live overlay on the MAME screen (move, latency bar, decision
count for each player, plus a footer). MAME's recorder captures only the raw game
pixels, so the video overlay is composited afterwards:

```bash
.venv/bin/python sf3/play_sf3.py --record runs/demo.avi
.venv/bin/python sf3/make_video.py runs/demo.avi runs/sf3_decisions_<stamp>.jsonl -o runs/demo.mp4
```

`make_video.py` syncs decisions to frames by MAME machine time, upscales the game
2.3x with nearest-neighbour into a 1280x720 frame, adds side panels, a title card
and an end card with the match stats, and keeps the game audio.
`--start`/`--end` cut the clip in seconds.

By default the loop samples a move from Jev's probability distribution (moves with
at least 4% probability). `--argmax` always plays the top choice; in tests that
produced a pure fireball war, because `hadouken` wins every long-range comparison.
Jev also sees its last 4 moves and a rule against repeating a move 3 times.

### How the bridge works

- `sf3/bridge.lua` runs inside MAME (`-autoboot_script`). Every frame it reads the
  player structs (positions, health, action, meter, stun, attack and block flags)
  and writes one `S key=value ...` line to a TCP socket. It reads commands from
  the same socket.
- `sf3/mame_env.py` starts MAME, owns the socket, and keeps the newest state.
- `sf3/play_sf3.py` sends the state to Jev every 4 frames in a background thread
  and turns the answer into a held input (`H 1 d+b`) or a frame-exact sequence
  (`Q 1 d:2;d+f:2;f:2;f+MP:2` is a hadouken). The Lua side resolves forward and
  back from the facing and presses the buttons frame by frame.
- Match start is automatic: coins, both starts, then the character ids are written
  to memory during the select screen and confirmed with jab.

Memory addresses come from Grouflon/3rd_training_lua and modal-projects/sf3.

## Logs

Each run writes `runs/*decisions_*.jsonl` with one line per Jev decision:
latency, choice, confidence, probabilities and token count.
