-- MAME bridge for Street Fighter III: 3rd Strike (sfiii3n).
--
-- Run:  mame sfiii3n -autoboot_script sf3/bridge.lua -autoboot_delay 1
-- Env:  SF3_BRIDGE_PORT  TCP port on 127.0.0.1 where the Python side listens
--       SF3_PROBE=1      no memory reads; only list inputs and send heartbeats
--
-- Every frame the script sends one line "S k=v k=v ..." with the game state
-- and reads commands. Commands (one per line):
--   H <p> <keys>      hold these keys until the next H/Q for that player
--   Q <p> <seq>       play a sequence: "keys:frames;keys:frames" then go back to H
--   A <c1> <c2> <sa>  auto-start a 2 player match with these character ids
--   O <p> <move>|<ms>|<conf>|<count>   update the on-screen overlay for a player
--   T <text>          set the footer text of the overlay ("" hides it)
--   N                 save a snapshot PNG (MAME snapshot directory)
--   X                 exit MAME
-- Keys: up down left right f b n LP MP HP LK MK HK start coin. Combined keys
-- use "+" (e.g. "d+f+HP"). "f"/"b" resolve to right/left from the facing.
-- Memory map from Grouflon/3rd_training_lua and modal-projects/sf3.

local PORT = tonumber(os.getenv("SF3_BRIDGE_PORT") or "5555")
local PROBE = os.getenv("SF3_PROBE") == "1"

local machine = manager.machine

-- ---------------------------------------------------------------- socket
local sock = emu.file("", 3)  -- read | write; no CREATE, so MAME connects as a client
local open_err = sock:open("socket.127.0.0.1:" .. PORT)
if open_err ~= nil and open_err ~= false then
  print("sf3 bridge: socket open error: " .. tostring(open_err))
end
local function send(line)
  sock:write(line .. "\n")
end

-- ---------------------------------------------------------------- inputs
local fields = {}
for tag, port in pairs(machine.ioport.ports) do
  for name, field in pairs(port.fields) do
    fields[name] = field
  end
end

local NAMES = {
  [1] = { up = "P1 Up", down = "P1 Down", left = "P1 Left", right = "P1 Right",
          LP = "P1 Jab Punch", MP = "P1 Strong Punch", HP = "P1 Fierce Punch",
          LK = "P1 Short Kick", MK = "P1 Forward Kick", HK = "P1 Roundhouse Kick",
          start = "1 Player Start", coin = "Coin 1" },
  [2] = { up = "P2 Up", down = "P2 Down", left = "P2 Left", right = "P2 Right",
          LP = "P2 Jab Punch", MP = "P2 Strong Punch", HP = "P2 Fierce Punch",
          LK = "P2 Short Kick", MK = "P2 Forward Kick", HK = "P2 Roundhouse Kick",
          start = "2 Players Start", coin = "Coin 2" },
}
local KEY_ORDER = { "up", "down", "left", "right", "LP", "MP", "HP", "LK", "MK", "HK", "start", "coin" }

local missing = {}
for p = 1, 2 do
  for _, k in ipairs(KEY_ORDER) do
    if fields[NAMES[p][k]] == nil then missing[#missing + 1] = NAMES[p][k] end
  end
end
if #missing > 0 then
  print("sf3 bridge: missing input fields: " .. table.concat(missing, ", "))
  local all = {}
  for name, _ in pairs(fields) do all[#all + 1] = name end
  table.sort(all)
  print("sf3 bridge: available fields: " .. table.concat(all, " | "))
end

-- Per player: the held key set, the running sequence, and what is pressed now.
local players = {
  [1] = { hold = {}, seq = nil, seq_i = 1, seq_left = 0, pressed = {} },
  [2] = { hold = {}, seq = nil, seq_i = 1, seq_left = 0, pressed = {} },
}

local function parse_keys(s)
  local set = {}
  if s == nil or s == "" or s == "n" then return set end
  for k in string.gmatch(s, "[^+,]+") do
    if k ~= "n" then set[k] = true end
  end
  return set
end

local function parse_seq(s)
  local steps = {}
  for step in string.gmatch(s, "[^;]+") do
    local keys, frames = string.match(step, "^(.-):(%d+)$")
    if keys == nil then keys, frames = step, "2" end
    steps[#steps + 1] = { keys = parse_keys(keys), frames = tonumber(frames) }
  end
  return steps
end

-- ---------------------------------------------------------------- memory
local mem = nil
if not PROBE then
  local cpu = machine.devices[":maincpu"]
  if cpu ~= nil then mem = cpu.spaces["program"] end
  if mem == nil then print("sf3 bridge: no :maincpu program space") end
end

local BASE = { 0x02068C6C, 0x02069104 }
local ADDR = {
  frame = 0x02007F00,
  fighting = 0x02011389,
  timer = 0x02011377,
  wins = { 0x02011383, 0x02011385 },
  match_state = 0x020154A7,
  menu_state = 0x0201546B,
  char_id = { 0x02011387, 0x02011388 },
  sel_state = { 0x0201553D, 0x02015545 },
  sel_sa = { 0x020154D3, 0x020154D5 },
  hp = { 0x02068D0A, 0x020691A2 },
  gauge = { 0x020695B5, 0x020695E1 },
  stocks = { 0x020695BF, 0x020695EB },
  stun_timer = { 0x020695F9, 0x0206960D },
  stun_bar = { 0x020695FD, 0x02069611 },
  combo = { 0x020696C5, 0x0206961D },
}

local function r8(a) return mem:read_u8(a) end
local function r16(a) return mem:read_u16(a) end
local function r16s(a) return mem:read_i16(a) end
local function r32(a) return mem:read_u32(a) end

local function read_player(i)
  local b = BASE[i]
  return {
    x = r16s(b + 0x64),
    y = r16s(b + 0x68),
    flip = r8(b + 0x0A),           -- 0 = sprite faces left, else faces right
    hp = r16s(ADDR.hp[i]),
    posture = r8(b + 0x20E),
    action = r32(b + 0xAC),
    anim = r16(b + 0x202),
    busy = r16(b + 0x3D1),
    recovery = r8(b + 0x3B),
    freeze = r8(b + 0x45),
    attacking = r8(b + 0x428),
    blocking = r8(b + 0x3D3),
    thrown = r8(b + 0x3CF),
    standing = r8(b + 0x297),
    char = r16(b + 0x3C0),
    valid = r32(b + 0x2A0) ~= 0,
    gauge = r8(ADDR.gauge[i]),
    stocks = r8(ADDR.stocks[i]),
    stun_timer = r8(ADDR.stun_timer[i]),
    stun = r32(ADDR.stun_bar[i]) >> 24,
    combo = r8(ADDR.combo[i]),
  }
end

local function state_line(frame_no)
  local parts = { "S", "n=" .. frame_no }
  if mem == nil then return table.concat(parts, " ") end
  parts[#parts + 1] = "frame=" .. r32(ADDR.frame)
  -- emulated seconds since MAME started; the AVI recorder starts at 0 too
  parts[#parts + 1] = string.format("mt=%.4f", machine.time:as_double())
  parts[#parts + 1] = "fighting=" .. r8(ADDR.fighting)
  parts[#parts + 1] = "timer=" .. r8(ADDR.timer)
  parts[#parts + 1] = "match=" .. r8(ADDR.match_state)
  parts[#parts + 1] = "menu=" .. r8(ADDR.menu_state)
  parts[#parts + 1] = "sel1=" .. r8(ADDR.sel_state[1])
  parts[#parts + 1] = "sel2=" .. r8(ADDR.sel_state[2])
  parts[#parts + 1] = "wins1=" .. r8(ADDR.wins[1])
  parts[#parts + 1] = "wins2=" .. r8(ADDR.wins[2])
  for i = 1, 2 do
    local p = read_player(i)
    local k = "p" .. i .. "_"
    parts[#parts + 1] = k .. "x=" .. p.x
    parts[#parts + 1] = k .. "y=" .. p.y
    parts[#parts + 1] = k .. "flip=" .. p.flip
    parts[#parts + 1] = k .. "hp=" .. p.hp
    parts[#parts + 1] = k .. "posture=" .. p.posture
    parts[#parts + 1] = k .. "action=" .. p.action
    parts[#parts + 1] = k .. "anim=" .. p.anim
    parts[#parts + 1] = k .. "busy=" .. p.busy
    parts[#parts + 1] = k .. "recovery=" .. p.recovery
    parts[#parts + 1] = k .. "freeze=" .. p.freeze
    parts[#parts + 1] = k .. "attacking=" .. p.attacking
    parts[#parts + 1] = k .. "blocking=" .. p.blocking
    parts[#parts + 1] = k .. "thrown=" .. p.thrown
    parts[#parts + 1] = k .. "standing=" .. p.standing
    parts[#parts + 1] = k .. "char=" .. p.char
    parts[#parts + 1] = k .. "valid=" .. (p.valid and 1 or 0)
    parts[#parts + 1] = k .. "gauge=" .. p.gauge
    parts[#parts + 1] = k .. "stocks=" .. p.stocks
    parts[#parts + 1] = k .. "stun_timer=" .. p.stun_timer
    parts[#parts + 1] = k .. "stun=" .. p.stun
    parts[#parts + 1] = k .. "combo=" .. p.combo
  end
  return table.concat(parts, " ")
end

-- ---------------------------------------------------------------- facing
local function facing_right(i)
  if mem == nil then return i == 1 end
  local other = 3 - i
  return r16s(BASE[i] + 0x64) <= r16s(BASE[other] + 0x64)
end

local function resolve(i, keys)
  local out = {}
  local right = facing_right(i)
  for k, _ in pairs(keys) do
    if k == "f" then out[right and "right" or "left"] = true
    elseif k == "b" then out[right and "left" or "right"] = true
    elseif k == "u" then out.up = true
    elseif k == "d" then out.down = true
    else out[k] = true end
  end
  return out
end

local function apply_inputs(i)
  local st = players[i]
  local want
  if st.seq ~= nil then
    local step = st.seq[st.seq_i]
    want = resolve(i, step.keys)
    st.seq_left = st.seq_left - 1
    if st.seq_left <= 0 then
      st.seq_i = st.seq_i + 1
      if st.seq_i > #st.seq then
        st.seq = nil
      else
        st.seq_left = st.seq[st.seq_i].frames
      end
    end
  else
    want = resolve(i, st.hold)
  end
  for _, k in ipairs(KEY_ORDER) do
    local field = fields[NAMES[i][k]]
    if field ~= nil then
      if want[k] and not st.pressed[k] then
        field:set_value(1)
        st.pressed[k] = true
      elseif not want[k] and st.pressed[k] then
        field:clear_value()
        st.pressed[k] = false
      end
    end
  end
end

-- ---------------------------------------------------------------- auto start
-- Coins in, both starts, lock the characters, confirm with jab. Modeled on
-- modal-projects/sf3 (memory lock) with coins instead of the service menu.
local auto = nil

local function auto_start(c1, c2, sa)
  auto = { c1 = c1, c2 = c2, sa = sa, t = 0, phase = "coin", fight_frames = 0 }
end

local function auto_step()
  if auto == nil or mem == nil then return end
  auto.t = auto.t + 1
  local fighting = r8(ADDR.fighting)
  local s1, s2 = r8(ADDR.sel_state[1]), r8(ADDR.sel_state[2])

  -- keep the chosen characters and super arts while selecting
  if s1 >= 2 or s2 >= 2 then
    if fighting == 0 then auto.fight_frames = 0 else auto.fight_frames = auto.fight_frames + 1 end
    if auto.fight_frames <= 8 then
      mem:write_u8(ADDR.char_id[1], auto.c1)
      mem:write_u8(ADDR.char_id[2], auto.c2)
      if s1 >= 3 then mem:write_u8(ADDR.sel_sa[1], auto.sa) end
      if s2 >= 3 then mem:write_u8(ADDR.sel_sa[2], auto.sa) end
    end
  end

  local function tap(p, key, on)
    if on then players[p].hold = { [key] = true } else players[p].hold = {} end
  end

  if auto.phase == "coin" then
    -- 4 coin taps each, 6 frames on, 14 off
    local cycle = auto.t % 20
    tap(1, "coin", cycle < 6); tap(2, "coin", cycle < 6)
    if auto.t >= 80 then auto.phase = "start"; auto.t = 0 end
  elseif auto.phase == "start" then
    local cycle = auto.t % 30
    tap(1, "start", cycle < 6); tap(2, "start", cycle < 6)
    if s1 >= 2 and s2 >= 2 then auto.phase = "select"; auto.t = 0; tap(1, "start", false); tap(2, "start", false) end
    if auto.t > 600 then auto.phase = "coin"; auto.t = 0 end
  elseif auto.phase == "select" then
    -- press jab every 20 frames while a player sits on the character (2) or super art (4) screen
    local cycle = auto.t % 20
    local press = cycle < 4
    tap(1, "LP", press and (s1 == 2 or s1 == 4))
    tap(2, "LP", press and (s2 == 2 or s2 == 4))
    if fighting ~= 0 then
      tap(1, "LP", false); tap(2, "LP", false)
      auto.phase = "done"
      send("E started")
    end
  elseif auto.phase == "done" then
    if auto.fight_frames > 8 then auto = nil end
  end
end

-- ---------------------------------------------------------------- overlay
local scr = nil
for _, s in pairs(machine.screens) do scr = s; break end

local overlay = { [1] = nil, [2] = nil }
local footer = ""
local CYAN, WHITE, DIM, RED = 0xFF00E5FF, 0xFFFFFFFF, 0xFFB0B0B0, 0xFFFF5050

local function draw_overlay()
  if scr == nil then return end
  local W, H = scr.width, scr.height
  local bw, bh = 152, 36
  for p = 1, 2 do
    local o = overlay[p]
    if o ~= nil then
      local x0 = (p == 1) and 4 or (W - bw - 4)
      local y0 = 26
      scr:draw_box(x0, y0, x0 + bw, y0 + bh, 0xC0000000, CYAN)
      scr:draw_text(x0 + 4, y0 + 3, string.format("JEV P%d  %s", p, o.move), CYAN)
      scr:draw_text(x0 + 4, y0 + 13, string.format("%d ms  conf %.2f  n=%d", o.ms, o.conf, o.count), WHITE)
      local w = math.max(1, math.min(bw - 8, (o.ms / 500) * (bw - 8)))
      scr:draw_box(x0 + 4, y0 + 25, x0 + 4 + w, y0 + 31, o.ms < 350 and CYAN or RED, 0)
      scr:draw_box(x0 + 4, y0 + 25, x0 + bw - 4, y0 + 31, 0, DIM)
    end
  end
  if footer ~= "" then
    scr:draw_box(0, H - 12, W, H, 0xC0000000, 0)
    scr:draw_text("center", H - 10, footer, DIM)
  end
end

-- ---------------------------------------------------------------- commands
local inbuf = ""

local function handle(line)
  local cmd, rest = string.match(line, "^(%S+)%s*(.*)$")
  if cmd == "H" then
    local p, keys = string.match(rest, "^(%d)%s*(.*)$")
    p = tonumber(p)
    if p then players[p].hold = parse_keys(keys); players[p].seq = nil end
  elseif cmd == "Q" then
    local p, seq = string.match(rest, "^(%d)%s+(.+)$")
    p = tonumber(p)
    if p and seq then
      local steps = parse_seq(seq)
      if #steps > 0 then
        players[p].seq = steps; players[p].seq_i = 1; players[p].seq_left = steps[1].frames
      end
    end
  elseif cmd == "A" then
    local c1, c2, sa = string.match(rest, "^(%d+)%s+(%d+)%s*(%d*)$")
    auto_start(tonumber(c1) or 2, tonumber(c2) or 11, tonumber(sa) or 0)
  elseif cmd == "O" then
    local p, move, ms, conf, count = string.match(rest, "^(%d)%s+([^|]*)|([^|]*)|([^|]*)|(.*)$")
    p = tonumber(p)
    if p then
      overlay[p] = { move = move, ms = tonumber(ms) or 0, conf = tonumber(conf) or 0, count = tonumber(count) or 0 }
    end
  elseif cmd == "T" then
    footer = rest
  elseif cmd == "N" then
    machine.video:snapshot()
  elseif cmd == "X" then
    machine:exit()
  elseif cmd == "P" then
    send("E pong")
  end
end

local function poll_commands()
  while true do
    local chunk = sock:read(4096)
    if chunk == nil or #chunk == 0 then break end
    inbuf = inbuf .. chunk
  end
  while true do
    local nl = string.find(inbuf, "\n", 1, true)
    if nl == nil then break end
    local line = string.sub(inbuf, 1, nl - 1)
    inbuf = string.sub(inbuf, nl + 1)
    if #line > 0 then handle(line) end
  end
end

-- ---------------------------------------------------------------- per frame
local frame_no = 0
local function on_frame()
  frame_no = frame_no + 1
  poll_commands()
  auto_step()
  apply_inputs(1)
  apply_inputs(2)
  if PROBE then
    if frame_no % 30 == 1 then
      local all = {}
      for name, _ in pairs(fields) do all[#all + 1] = name end
      table.sort(all)
      send("S n=" .. frame_no .. " probe=1 fields=" .. table.concat(all, "|"))
    end
  else
    send(state_line(frame_no))
  end
end

-- keep the subscription in a global so it is not garbage collected
sf3_bridge_frame_sub = emu.add_machine_frame_notifier(on_frame)
-- on-screen drawing only shows when done from the frame-done callback
emu.register_frame_done(draw_overlay)
send("E ready port=" .. PORT .. " probe=" .. tostring(PROBE) .. " mem=" .. tostring(mem ~= nil))
print("sf3 bridge: connected to 127.0.0.1:" .. PORT)
