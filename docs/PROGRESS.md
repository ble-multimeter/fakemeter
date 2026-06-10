# fake-ble-meter — progress log & handoff

A BLE peripheral emulator that impersonates multimeters so their official phone apps connect to it, letting us use the vendor app as a **hardware-free decode oracle** to verify the `uni-t-mmu-ble` BLE drivers. Primary current target: the **Voltcraft VC800/VC900** app (an OWON Flutter rebadge) to verify/fix `packages/protocol/src/drivers/voltcraft.ts` — in particular settling its suspected MSB-vs-LSB flag-bit-order bug via a live "bit-sweep".

## Status: SOLVED — the app shows a CORRECT, LIVE reading from the emulator

The emulator drives the Voltcraft series800 app (v1.2.3 on the bench phone)
through every gate AND streams measurement frames the app parses and displays
LIVE. Verified on-screen: setting `4.2 V_DC` shows `0004.2 V` (DC, bar-graph
auto-ranged); changing to `230.5 V` makes the display change to `0230.5 V`
(=> genuinely live, not a static template); `1.0 MΩ` shows `RES … MΩ`; the
`f hold` toggle lights the **HOLD** annunciator while keeping the value. The
full state-bit → annunciator map was then read off the live bit-sweep.

### Two bugs fixed (both required)
1. **Notifications were emitted off the GLib main-loop thread.** `gatt_server.py`
   re-pushed frames from a plain Python `threading.Thread` calling
   `Characteristic.set_value()`. bluezero turns that into a D-Bus
   `PropertiesChanged` signal, and dbus-python+GLib signal emission is NOT
   thread-safe — emitting off-thread did not reach BlueZ, so the phone
   subscribed but received ZERO frames (app stuck on `UL`). Fixed by driving the
   re-push with a `GLib.timeout_add` source and marshalling every `notify()` via
   `GLib.idle_add` onto the loop thread. After this, frames reached the app.
2. **The R10W frame layout was wrong** (the app then parsed our frames to
   garbage / `UL`). The decompiled `create24bits` was mis-read as 24-bit
   big-endian with even-keyed code tables. The TRUTH, confirmed by streaming raw
   frames and reading the display (see below), is LITTLE-endian words with
   CONSECUTIVE code tables. Fixed the encoder + oracle accordingly.

## What works (verified)
- **GATT**: service `0xFFF0`, notify `FFF4`, write `FFF3`, secure `FFF1`, info `FFF2`. Advertised name set via `--name`.
- **FFF2 device-info** (read on connect): 6 bytes `[seriesId, battery, fwMajor, fwMinor, fwPatch, flashFlag]`. Sending only 5 bytes → Dart `RangeError` "device not supported code:-2". REQUIRED to be ≥6.
- **Series gate**: the app maps `FFF2[0]` → model → `protocolType`. **Series 91 = VC915 → R10W parser** (15-byte records). Series 41 = OWON "B41" → **R2W** parser (6-byte records). Using 41 made the app slice our 15-byte frames as 6-byte garbage (the long red herring). Default series is now **91**.
- **FFF1 MD5 anti-counterfeit auth** (cracked): app writes 6 mixed coords (`mixed[i]=orig[i]+[200,100,50,20,10,5]`, padded to 16B); meter recovers `coord[i]=(mixed[i]-mixElems[i])&0xFF`, picks `s1[c]` (i<3) / `s2[c]` (i≥3) using the Java `UseMd5` tables, computes `md5(utf8(picked))`, and returns the **16 RAW DIGEST BYTES — not 32 ASCII hex** (that was the bug; the app hexifies each byte it reads). `code:3` = this compare failing. Implemented as auth scheme `'vc'` (default) in `voltcraft.py`.
- **Subscribe + stream**: app subscribes to FFF4; meter must **free-stream** frames (no `*READ?` command for live data — the SCPI `*READ?`/`*READ1?`/`*READlen?`/`*STOP` are offline-record only). Emulator re-pushes the last frame every 300ms while subscribed **on the GLib loop thread** (see bug #1 above — off-thread pushes silently never reached the app).
- **#TIMEsync**: after subscribe the app writes `#TIMEsync` + a 7-byte RTC datetime to FFF1 (`createSyncRTCParams`); it's a fire-and-forget clock-set, no reply needed. Harmless; we just log it.
- The app loads the **R10W VC915 UI** (rich button set: Select/Range/Hold/Rel/Max-Min/LPF/Compare/AC-DC/Motor/4~20mA/Display; 0–20000 bar graph) when series 91 is reported — confirms model detection.
- **Live measurement display** — the app shows our streamed value, changing it changes the display, the bar graph auto-ranges, and state bits light their annunciators. (The decisive empirical test rig: REPL `raw <hexbytes>` injects an arbitrary 15-byte frame so you can map any byte/bit against the on-screen result.)

## R10W measurement frame (CONFIRMED LIVE — authoritative)
15 bytes, five **24-bit LITTLE-endian** words `w = b[i] | b[i+1]<<8 | b[i+2]<<16`,
no markers, no checksum. (Words 0/1 = primary gear+value, 2/3 = secondary, 4 =
state. We send the secondary block zero and clear gear bit12, so only the primary
is parsed.)
- **bytes 0..2 gear word**: `decimals = g&7` (the field IS the #decimals: 0→0dp,
  1→1dp, …); `prefix = (g>>3)&7` → **0=p 1=n 2=µ 3=m 4=(none) 5=k 6=M 7=G — ALL
  EIGHT expressible**; `gear = (g>>6)&0x1f` (CONSECUTIVE: 0=V DC, 1=V AC, 2=A DC,
  3=A AC, 4=Ω, 5=CAP, 6=Hz, 7=DUTY, 8=°C, 9=°F, 10=DIODE, 11=CONT, 12=hFE,
  13=NCV); **bit12 = secondary-display active**.
- **bytes 3..5 value word**: `count = v&0x7FFFF`; `overRange = (v>>20)&7`
  (0=normal, **1=OL, 2=UL, 3=HI**); **sign = byte5 bit7** (= value-word bit23).
  `displayed value = count / 10**decimals` (then sign).
- **bytes 6..11**: secondary display (parsed only if gear bit12 set; we send zero).
- **bytes 12..14 state word** (annunciator bitmask, LSB-numbered): **HOLD=bit0,
  REL=1, AUTO=2, Bat=3, MIN=4, MAX=5, AVG=6, RMR=7, Loz=8, LPF=9, Peak=10,
  (bit11 blank), Cosφ=12, AC=13, DC=14, USB=15, Err=16, INRUSH=17, OSC=18**
  (bit18 switches the app into its oscilloscope view; bits≥19 blank).

Worked examples (exact bytes, all verified on-screen):
- `4.200 V DC` = `23 00 00 68 10 00 00 00 00 00 00 00 00 00 00`
- `OL (V AC)`  = `40 00 00 00 00 10 …`   (gear=V AC code1; over-range=1)
- `4.2 V DC + HOLD` = `21 00 00 2a 00 00 00 00 00 00 00 00 01 00 00`

## voltcraft.ts corrections (the original goal)
The driver `packages/protocol/src/drivers/voltcraft.ts` (DO NOT edit per task)
needs these fixes to match the real meter:
- **Endianness**: it reads the symbol/count words little-endian within 16-bit
  halves of a different layout; the real frame is 5×24-bit **little-endian**
  words at byte offsets 0/3/6/9/12. Re-derive offsets accordingly.
- **Flag bit ORDER**: the real state word is a **straight LSB-numbered bitmask**
  starting at **bit0 = HOLD** (then REL, AUTO, Bat, MIN, MAX, … as listed above).
  The driver's suspected **MSB-first** flag read is therefore WRONG — flags must
  be read LSB-first, and the named flags sit one bit LOWER than the old guess
  (HOLD is bit0, not bit1).
- **Gear/function table**: consecutive codes 0..13 as above (not even-keyed,
  not the third-party Windows-app table).
- **Prefix & decimals**: prefix codes 0..7 = p/n/µ/m/(none)/k/M/G; the dp field
  is literally the decimal-place count.

## INTERACTIVE control-button protocol (FFF3) — DISCOVERED + IMPLEMENTED (2026-06-10)
The emulator is now **interactive**: pressing a control button in the app writes a
command to **FFF3**, the emulator reacts by mutating the streamed reading, and the
app's display changes accordingly. ALL verified live on-screen (screenshots).

### Command format (captured live)
Every meter-screen button writes a **2-byte** frame to FFF3: `[opcode, 0x01]`. The
trailing `0x01` is the key-event flag (the app's `BuiltInBleMultimeter.pressKey(
code, bool)` sends `bool` as byte1; press = `0x01`). EVERY button is a real meter
command — there were NO app-local-only buttons in the Select…Display set.
(`pressKey` → `_writeCharacteristic("fff3", …)` confirmed in the decompile:
`asm/owon_imeter/device_manager/device/built_in_ble_multimeter.dart`.)

### Button → opcode map (each captured by pressing the button & reading the FFF3 write)
| Button   | FFF3 write | opcode | meter command? |
|----------|-----------|--------|----------------|
| Select   | `01 01`   | 0x01   | yes            |
| Range    | `02 01`   | 0x02   | yes            |
| Hold     | `03 01`   | 0x03   | yes            |
| Rel      | `04 01`   | 0x04   | yes            |
| Max/Min  | `06 01`   | 0x06   | yes            |
| LPF      | `07 01`   | 0x07   | yes            |
| 4~20mA   | `0a 01`   | 0x0a   | yes (special)  |
| Display  | `0c 01`   | 0x0c   | yes (special)  |
| Compare  | `0f 01`   | 0x0f   | yes (special)  |
| AC/DC    | `10 01`   | 0x10   | yes            |
| Motor    | `11 01`   | 0x11   | yes (special)  |
Unused opcodes (this model's button set): 0x05, 0x08, 0x09, 0x0b, 0x0d, 0x0e.

### Implemented reactions (in `voltcraft.py::command()`, all verified on-screen)
A single source of truth — module-level `_STATE` Reading + `_RUNTIME` dict — is
mutated by both the FFF3 commands and the REPL (`reset_state()` keeps them in sync).
Each command returns the freshly-encoded R10W frame; the GATT server streams it.
- **Hold (0x03)** → toggles HOLD: engaging freezes the current frame (`_RUNTIME
  ["held_frame"]`) and lights **HOLD**; pressing again resumes the live reading.
  VERIFIED: Mode box showed **HOLD**, value frozen at `01.111 V` while held.
- **Select (0x01)** → cycles the primary function through
  V_DC→V_AC→OHM→CAP→DIODE→CONT→HZ→DUTY→TEMP_C→A_DC→A_AC (wraps). Clears REL/MAX/MIN.
  VERIFIED: top-left changed DC→AC→**RES**(Ω)→**DIODE**, unit + bar-graph range tracked.
- **AC/DC (0x10)** → toggles the DC↔AC variant of the current V/A gear.
  VERIFIED: green gear label flipped **DC**↔**AC**.
- **Rel (0x04)** → toggles REL: engaging captures the value as baseline, zeroes the
  display, lights **REL**; pressing again restores. VERIFIED: **REL** lit, value `00000`.
- **Max/Min (0x06)** → cycles off→MAX→MIN→AVG→off, setting the MAX/MIN flags.
  VERIFIED: Mode box cycled **MAX**→**MIN**.
- **LPF (0x07)** → toggles the **LPF** annunciator (state bit 9; added as a named
  Reading flag). VERIFIED: **LPF** lit.
- **Range (0x02)** → cycles the decimal-place count 3→2→1→0 (manual range stepping
  moves the decimal point); turns AUTO off. VERIFIED: display `0004.2`→`004.20`.
- **Compare (0x0f) / Motor (0x11) / 4~20mA (0x0a) / Display (0x0c)** → captured &
  acknowledged but make **no primary-frame change** (handler returns None): these
  switch into special modes needing the secondary block / dedicated parsers
  (`parseRealTimeDataOnce_87Special`), out of scope for the primary-display
  verification. The writes are received without disconnecting the link.

### Wiring
`gatt_server.py::_on_write` (FFF3) now dispatches to `Profile.command_handler`
(mirrors how `auth_response`/`info_response` are wired); a non-None return is
pushed via `notify()` (which updates `_last_frame`, so the 300ms re-push keeps the
reaction on screen). `Profile.command_handler` + `Profile.current_frame` added to
`base.py`. The controller's `send()` routes through `voltcraft.reset_state()` so
manual REPL edits and button commands never fight.

## DEMO VALUE-WALK — the streamed reading gently drifts (2026-06-10)
To make the emulator feel like a live measurement (mirroring the webapp's demo
devices), the streamed numeric reading now **drifts** each stream tick instead of
sitting on a fixed value. `voltcraft.py::tick()` nudges `_RUNTIME["live_value"]`
by a few displayed counts of uniform jitter plus a 5% mean-reversion pull toward
`_RUNTIME["walk_center"]` (the nominal, re-centred whenever you `v`-set a value),
then writes the rounded result into `_STATE.value`. `gatt_server.py::_stream_tick`
calls `prof.tick()` on the GLib loop thread before pushing `prof.current_frame()`,
so the drift rides the existing 300ms re-push.
- **On by default**; `--no-walk` disables it (fixed reading, for precise
  byte-mapping); REPL `walk on|off` (bare `walk` toggles) flips it live.
- **Freezes under HOLD**: `tick()` is a no-op while `_RUNTIME["held_frame"]` is
  set (and for overload/underload/non-numeric / walk-off), so the held value stays
  put — `current_frame()` keeps streaming the frozen frame.
- **VERIFIED LIVE on the phone (2026-06-10)**: with the app on the VC915 meter
  screen, the displayed V_DC value drifted across consecutive ~2s screenshots
  (04.194 → 04.195 → 04.189 V, around the 4.2 V nominal); pressing HOLD froze it
  (04.193 V identical across a 3s gap, "HOLD" annunciator lit); releasing HOLD
  resumed the drift (04.197 → 04.194 V). Screenshot evidence captured.

## OPEN / minor caveats
- The **device-list card "disconnected" badge** stays red even while live data
  flows and the meter screen + bar graph update correctly (app v1.2.3 quirk; the
  decompile is v1.2.5). It does NOT block the live reading — both the card body
  and the meter screen show the correct live value. Not worth chasing.
- Gear codes ≥14 (Power/PF/4-20mA/AC+DC/Motor/Solar/etc.) were not swept; the
  5-bit field can hold them but they need the secondary block / special parsers
  (`parseRealTimeDataOnce_87Special`) — out of scope for the core verification.

## How to run / environment
- Emulator: `cd /home/mannes/projects/ai-slop/fake-ble-meter && source .venv/bin/activate && python -m fakemeter --profile voltcraft -v --name VC915` (defaults: series 91, R10W, auth `vc`). REPL: `r` resend, `p` presets, `v` set value/function/prefix, `f` toggle flag, `s` mode-word **bit-sweep**, **`raw <hexbytes>` inject an arbitrary 15-byte frame (the layout-mapping tool)**, `series <id>`, `auth <mode>`, `q` quit.
- **Persistence caveat**: the Claude harness SIGKILLs (exit 144) any bluez/D-Bus process spawned by a foreground/background Bash call when the call returns. `tmux` was NOT installed on this box — use **`screen`** instead: `screen -dmS fm bash -c '… exec python -u -m fakemeter … 2>&1 | tee /tmp/fm.log'`; drive it with `screen -S fm -p 0 -X stuff "cmd\n"` and read `/tmp/fm.log` (the `| tee` logfile is the reliable way to read REPL output; `screen -X hardcopy` only grabs the visible pane and the bluezero DEBUG "Char Prop Changed" spam scrolls prompts away — run WITHOUT `-v` to quiet it). The one-shot `--self-check` works inline.
- **Forcing a clean phone reconnect** (the app doesn't re-run GATT discovery on an already-open BLE link): `adb -s 995b6385 shell am force-stop com.voltcraft.series800` then relaunch + dismiss the SDK-compat dialog (`tap 888 1437`) + `+`/scan (`tap 955 210`, wait, `tap 540 636` on the VC915 row) + tap the card (`tap 540 600`) to open the meter. Force-stop clears the in-memory device list, guaranteeing a fresh add. `pm clear` also wipes saved devices but then re-grant BLE perms.
- Phone: **adb device `995b6385`, ROOTED** (`adb root` → uid 0; Magisk `su`). App `com.voltcraft.series800` (launcher `com.owon.imeter.MainActivity`), perms granted. Drive via `adb shell input` / `screencap` / `logcat`; read app memory as root if useful. Other installed apps: OWON BLE4.0 `com.owon.MultimeterBLE` (decompilable Java sibling), nRF Connect `no.nordicsemi.android.mcp`.
- Adapter: `hci0`, BD addr `44:AF:28:A5:53:1A`, BlueZ 5.72. The app strips non-alphanumerics from the advertised name for display ("VC900-FAKE"→"VC900FAKE").
- Decompiles: `/tmp/vc125-out` (blutter dump of VC arm64 v1.2.5, Dart 3.9.2 — `asm/imeter_base/…`), `/tmp/owon-ble-apk` (OWON Java), `/tmp/vc-xapk` (VC armeabi-v7a apk). jadx at `/tmp/jadx/bin/jadx`.

## REFACTOR — reusable layering for the driver fan-out (2026-06-10)
The voltcraft profile carried a lot of logic that is generic to ALL meters
(interactive state machine, HOLD/REL/walk) or shared by the whole OWON family
(FFF1 auth, FFF2 info, FFF3 button commands). That was extracted into reusable
layers so every BLE driver gets a cheap profile. **voltcraft behaviour is
unchanged** (48 tests green — the original 35 + 13 new; live re-validated on the
phone: connect → live R10W reading → value-walk drift 04.202→04.223→04.206 V →
HOLD froze it at 004.21 V across a 3s gap → release resumed the drift).

New module layout (bottom → top):
- `fakemeter/profiles/base.py` — `Profile`/`Reading`. **Added the `interaction`
  seam**: `'stream'` (free-stream on subscribe; OWON/bdm/ai-care) vs `'polled'`
  (request/response — silent until a write, then one reply frame; UNI-T AB-CD).
- `fakemeter/meter_core.py` — **NEW, METER-GENERIC**: `InteractiveMeter` owns the
  single source-of-truth `Reading` (`state`) + runtime dict, and the generic
  reactions (`toggle_hold`/`select_next`/`range_next`/`acdc_toggle`/`rel_toggle`/
  `maxmin_next`/`toggle_flag`) + the demo value-walk `tick()` + `reset_state`/
  `current_frame`. Parameterised by `InteractiveConfig` (family encoder + cycle
  tables). Any numeric profile gets HOLD/REL/walk for free.
- `fakemeter/profiles/owon_base.py` — **NEW, OWON-FAMILY shared**: the FFF1 MD5
  auth (`OwonAuth`, s1/s2 tables, raw-16-byte digest), the FFF2 6-byte series-info
  responder, the FFF3 button-opcode dispatch (+ `#TIMEsync` ignore), and the
  shared FFF0 GATT UUIDs. A profile builds an `OwonProfile` config + `OwonMeter`
  and calls `make_profile()`. Carries the `CMD_*` opcode constants now.
- `fakemeter/profiles/voltcraft.py` — now ONLY the R10W-specific bits: the
  15-byte LITTLE-endian `encode()`, gear/prefix/state tables, the Select gear
  cycle / AC-DC pairs / range cycle, series 91. Wires them via `owon_base`.
  Re-exports the old names (`CMD_*`, `_S1/_S2`, `_AUTH`/`_RUNTIME` proxies,
  `auth_response`/`info_response`/`command`/`current_frame`/`tick`/`reset_state`/
  `set_*`) so the REPL + tests are unchanged.
- `fakemeter/gatt_server.py` — `MeterServer` now honours `Profile.interaction`:
  the re-push stream loop only runs for `'stream'` profiles; `'polled'` ones stay
  silent on subscribe and only answer writes (the `_on_write` → `command_handler`
  → notify path already does write→response-frame; polled mode generalises it).
- `fakemeter/__main__.py` — CLI/REPL controller (unchanged behaviour; uses the
  voltcraft back-compat proxies).
- `tests/` — `decode_voltcraft.py` (R10W oracle), `test_voltcraft_encoder.py`
  (round-trip + auth + FFF3 reactions), **`test_meter_core.py` NEW** (the extracted
  `InteractiveMeter` + `owon_base` auth/info/dispatch directly). **48/48 pass.**
- `docs/adding-a-profile.md` — **NEW**: the add-a-profile template + the layering +
  the driver→base map for the next fan-out. `docs/owon-voltcraft-handshake.md`,
  `docs/voltcraft-measurement-protocol.md` unchanged.

### Driver → base map (next fan-out)
| driver(s) | base | interaction | auth/info |
|---|---|---|---|
| voltcraft (DONE) | `owon_base` | stream | FFF1+FFF2 |
| owon-plus, owon-old | `owon_base` | stream | FFF1+FFF2 |
| bdm | `meter_core` (direct Profile) | stream | none |
| ai-care | `meter_core` (direct Profile, FFB0) | stream | none |
| uni-t, ut60bt/161, ut117c, ut171, ut181a, ut202bt, ut219p | `uni_t_base`* | polled | per-model |

\* `uni_t_base` not built — only the `interaction='polled'` seam + the plan in
`docs/adding-a-profile.md` (reuse `InteractiveMeter`; add the AB-CD codec + per-
model command map; the server already supports polled).

## Code map (fake-ble-meter)
- `fakemeter/__main__.py` — CLI/REPL controller. Added `raw <hexbytes>` command. `send()` now routes voltcraft readings through `voltcraft.reset_state()` so manual edits share the interactive state.
- `fakemeter/gatt_server.py` — BlueZ GATT server + advertisement (`MeterServer`), notify streaming, FFF1/FFF2/FFF3 callbacks. **Drives the re-push + every `notify()` on the GLib main-loop thread.** **`_on_write` (FFF3) dispatches to `Profile.command_handler`** and streams the returned reaction frame. **Honours `Profile.interaction` (stream vs polled).**
- `fakemeter/meter_core.py` — **meter-generic interactive engine + value-walk** (see refactor note above).
- `fakemeter/profiles/base.py` — `Profile`/`Reading` interface. Has `Reading.lpf`, `Profile.command_handler`, `Profile.current_frame`, `Profile.tick`, **`Profile.interaction`**.
- `fakemeter/profiles/owon_base.py` — **OWON-family shared handshake** (FFF1 auth / FFF2 info / FFF3 dispatch / GATT / free-stream); `make_profile()` builds a wired `Profile`.
- `fakemeter/profiles/voltcraft.py` — the VC profile, now only R10W-specific (encoder + tables + series 91) on top of `owon_base`/`meter_core`; back-compat names re-exported.
- `tests/` — `decode_voltcraft.py` (R10W oracle), `test_voltcraft_encoder.py` (round-trip + auth + FFF3 reactions), `test_meter_core.py` (extracted core + owon_base). **48/48 pass.**
- `docs/adding-a-profile.md` (the template), `docs/owon-voltcraft-handshake.md`, `docs/voltcraft-measurement-protocol.md`.

## Driver-verification takeaways for `voltcraft.ts` (RESOLVED — see "voltcraft.ts corrections" above; do NOT edit the driver)
- The real R10W frame is **24-bit LITTLE-endian** words at byte offsets 0/3/6/9/12.
- Function/gear table is CONSECUTIVE codes 0..13 (0=V DC … 13=NCV); prefix codes
  0..7 = p/n/µ/m/(none)/k/M/G; dp field = #decimals; over-range 1/2/3 = OL/UL/HI.
- **Flag-bit-order question SETTLED LIVE**: the state word is a straight
  **LSB-numbered** bitmask with **HOLD=bit0** (REL=1, AUTO=2, Bat=3, MIN=4, MAX=5,
  AVG=6, RMR=7, Loz=8, LPF=9, Peak=10, Cosφ=12, AC=13, DC=14, USB=15, Err=16,
  INRUSH=17, OSC=18). The driver's MSB-first read is WRONG; read LSB-first.

_Nothing here is committed. Per repo memory: don't git-commit markdown unless asked._
