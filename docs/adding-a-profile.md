# Adding a meter profile — the layering & template

This is the template the next group of agents follow to add a cheap profile for
each BLE driver in `uni-t-mmu-ble`. The emulator is now layered so a new profile
supplies almost nothing but its **frame encoder** + a small config.

## The layers (bottom → top)

```
fakemeter/profiles/base.py        Profile / Reading dataclasses + the `interaction` seam
fakemeter/meter_core.py           METER-GENERIC: interactive state machine, value-walk,
                                  HOLD-freeze / REL / Max-Min / Select / AC-DC / Range
fakemeter/profiles/owon_base.py   OWON-FAMILY shared: FFF1 MD5 auth, FFF2 series gate,
                                  FFF3 button-opcode dispatch, free-stream wiring
fakemeter/profiles/<name>.py      PER-PROFILE: the frame encoder + tables + series id
fakemeter/gatt_server.py          BlueZ GATT server; honours Profile.interaction
```

* **`meter_core.InteractiveMeter`** owns the single source-of-truth `Reading`
  (`state`) + the runtime dict (HOLD frozen frame, REL baseline, Max/Min cycle,
  the demo value-walk). It exposes generic reactions (`toggle_hold`, `select_next`,
  `range_next`, `acdc_toggle`, `rel_toggle`, `maxmin_next`, `toggle_flag`) and
  `tick()` (the walk). It is parameterised by an `InteractiveConfig` carrying the
  family encoder + the Select/AC-DC/range cycle tables. **Any numeric profile gets
  HOLD/REL/walk for free.**
* **`owon_base`** carries the entire OWON-shared handshake. A profile builds an
  `OwonProfile` config and an `OwonMeter`, then calls `owon_base.make_profile()`.

## Interaction modes (the `Profile.interaction` seam)

* `interaction='stream'` (default) — the meter **free-streams** frames on the
  notify char as soon as the app subscribes. The server runs a 300ms re-push loop
  and FFF3 button writes mutate state + re-stream. OWON / bdm / ai-care families.
* `interaction='polled'` — the meter is **request/response**: silent until the app
  WRITES a command, then replies with ONE frame. The server does NOT run the
  stream loop; it only answers writes (`_on_write` pushes `command_handler`'s
  return on the notify char). **UNI-T AB-CD family.** A polled profile supplies
  `command_handler(request_bytes) -> response_frame | None` and need not set
  `current_frame`/`tick`. (The button-command path already does write→response-
  frame; polled mode just generalises it and skips the free-stream loop.)

## Recipe A — an OWON-family profile (voltcraft / owon-plus / owon-old)

These three are the SAME GATT peripheral; they differ ONLY in the measurement
frame encoder and the FFF2 series id. Copy `voltcraft.py` and replace:

1. **The encoder** `encode(reading) -> bytes` — the exact INVERSE of the driver's
   decoder in `packages/protocol/src/drivers/<name>.ts`. Mirror its gear/prefix/
   state tables. (Port the driver decoder to `tests/decode_<name>.py` as the
   oracle; round-trip `encode → decode` in a test before any phone time.)
2. **The series id** (FFF2 byte0) — the value the app maps to this model/parser:
   * `voltcraft` → 91 (VC915) → protocolType 1 → R10W 15-byte parser
   * `owon-plus` → an R2W series id (e.g. 41 "B41") → protocolType 0 → 6-byte parser
   * `owon-old` → the older OWON id → the 14-byte ASCII parser
3. **The Select gear cycle / AC-DC pairs / range cycle** — the family's gear set.
4. **The presets** — a couple of representative readings + the bit-sweep.

Then wire it exactly like `voltcraft.py`'s bottom section:

```python
_CFG = owon_base.OwonProfile(id=..., series=..., encode=encode,
                             select_cycle=..., acdc_toggle=...,
                             function_codes=sorted(FUNCTION_CODES), presets=...)
_METER = owon_base.OwonMeter(_CFG)
profile = owon_base.make_profile(_CFG, _METER)
```

`owon_base` supplies FFF1 auth, FFF2 info, FFF3 dispatch, HOLD/REL/walk — free,
plus the profile-agnostic REPL hooks (reset_state / set_walk / set_series /
set_auth / function_codes) so the controller drives any OWON profile by capability.

**`OwonProfile` config surface** (everything a sibling can override):
- `encode`, `series` — the only two REQUIRED per-family bits.
- `select_cycle`, `acdc_toggle`, `range_dp_cycle` — the gear cycles for the
  Select / AC-DC / Range buttons (the family's gear set).
- `function_codes` — labels surfaced by the REPL `v` hint.
- `use_auth` / `use_info` (default True) — set **False** to DROP the FFF1 / FFF2
  characteristic. The Voltcraft Flutter app gates on both; an OWON sibling whose
  app streams with no auth (e.g. the Windows-derived owon-plus path) sets them
  False and still reuses the shared GATT + FFF3 dispatch + walk.
- `controls` — opcode→control-name map; defaults to `DEFAULT_CONTROLS` (the
  captured Voltcraft opcodes). Override only if a sibling uses different opcodes.
  Control names must be keys of `owon_base.CONTROL_ACTIONS`
  (hold/select/range/acdc/rel/maxmin/lpf/special).
- `auth_table`, `battery`, `fw`, `flash`, GATT UUIDs — sensible OWON defaults.

## Recipe B — bdm / ai-care (generic core, no OWON auth)

* **bdm**: service FFF0, an XOR-scrambled free-stream, **NO** FFF1 auth / FFF2 info.
* **ai-care**: service **FFB0**, self-addressing nibbles, free-stream, no OWON auth.

Build the `Profile` directly (don't use `owon_base`): set `service_uuid` /
`notify_uuid` / `write_uuid` (and leave `secure_uuid` / `info_uuid` = None),
`encode=...`, `interaction='stream'`. For HOLD/REL/walk, reuse
`meter_core.InteractiveMeter` yourself: construct one with an `InteractiveConfig`,
expose `current_frame`/`tick` from it, and (optionally) a `command_handler` that
maps the family's button bytes onto its generic actions.

## Recipe C — UNI-T family (polled AB-CD, HANDSHAKE-THEN-STREAM) — BUILT

Targets: uni-t / ut60bt (161) / ut117c / ut171 / ut181a / ut202bt / ut219p.
`uni_t_base` IS now built (mirrors `owon_base`): ISSC GATT UUIDs, the AB-CD
frame builder/parser (`build_frame_len8`/`len16`, `be16/le16_checksum`,
`parse_opcode_*`), the `UniTProfile`/`UniTMeter`/`make_profile` wiring, and the
`meter_core.InteractiveMeter` reuse for value-walk + HOLD/REL. A new model is just an
encoder + a `UniTProfile` config (see `ut202bt.py` for the minimal example —
it reuses `uni_t.encode` wholesale).

**The model is HANDSHAKE-THEN-STREAM, not reply-per-poll** (corrected 2026-06-10;
verified against the driver + confirmed live with the Smart Measure app — see PROGRESS.md):
  - **Different request opcodes → different frame KINDS.** The app writes `GET_NAME`
    and waits for a *control/name* frame, THEN writes `GET_DATA` and waits for a
    *measurement* frame. `UniTMeter.command` branches on the opcode: GET_NAME → name
    frame, GET_DATA → arm streaming + first measurement frame.
  - **GET_DATA GATES a free-stream, it does not pull one frame.** On GET_DATA the
    meter sets `_streaming=True` and thereafter `tick()` self-pushes a fresh
    measurement frame every tick (the periodic stream the app's SamplingManager
    reads), plus an occasional re-arm nudge (the 9-byte `…AA AA…` type-request /
    7-byte `…FF 00…` data-request the driver's `onRequest` answers).

**The polled-stream SEAM `gatt_server.py`/`base.py` must provide** (flagged, not yet
landed — base.py/gatt_server.py are owned elsewhere): the self-push needs the server
to (1) hand the profile a push fn and (2) drive `tick` on a timer even in polled mode.
`make_profile` already attaches `meter.on_start` to the profile instance. The owning
agent adds: a `Profile.on_start` field; a `profile.on_start(self.notify)` call after
`_resolve_chars`; and installing the GLib `_start_stream` timeout for polled-with-tick
profiles on subscribe (`tick` is a no-op until GET_DATA arms it). Until then, only the
FIRST GET_DATA frame is delivered (the `_on_write → command_handler → notify` path).

## Driver → base map (for the fan-out)

| driver(s)                                            | base            | interaction | auth/info |
|------------------------------------------------------|-----------------|-------------|-----------|
| voltcraft (DONE)                                     | `owon_base`     | stream      | FFF1+FFF2 |
| owon-plus                                            | `owon_base`     | stream      | app-dependent † |
| owon-old                                             | `owon_base`     | stream      | app-dependent † |
| bdm                                                  | `meter_core`    | stream      | none      |
| ai-care                                              | `meter_core`    | stream      | none (FFB0) |
| uni-t, ut60bt/161, ut117c, ut171, ut181a, ut202bt, ut219p | `uni_t_base`   | polled (handshake-then-stream) | none (ISSC) |

`uni_t_base` is BUILT (handshake-then-stream, see Recipe C). The periodic self-push
awaits the `on_start` + polled-`tick` seam in gatt_server.py/base.py (flagged above).

† **Decide which app you emulate first.** The driver `decode` for owon-plus/owon-old
was ported from the *Windows* app (`owonPlusTypeDecode`), which streams with NO
FFF1/FFF2 gate. The OWON *Android* app (and the Voltcraft Flutter app) DO gate.
The emulator impersonates a *phone app*, so confirm on the bench whether the target
Android app demands auth/info; set `use_auth` / `use_info` accordingly. Start with
both True (the Voltcraft default) and drop them only if the app connects without.

## Reading flags — match the driver's `Reading.flags` superset

`Reading` carries `max, min, hold, rel, auto, low_battery, hv_warning, peak_max,
peak_min` (the exact driver-repo `Reading.flags` set) plus `lpf` (voltcraft R10W).
A profile's encoder packs whichever flags its frame can express into the family
state word via a per-family `STATE_BITS` table (like `voltcraft.STATE_BITS`); the
rest stay False. Keep the names in sync with the driver so the table is 1:1.

## Checklist for a new profile

- [ ] Port the driver decoder to `tests/decode_<name>.py` (the oracle).
- [ ] Write `encode()` as its inverse; round-trip test green (value/unit/dp/sign/
      over-range/flags). Mirror the driver's gear/prefix + `STATE_BITS` tables.
- [ ] OWON family → `OwonProfile` + `OwonMeter` + `make_profile` (set `use_auth`/
      `use_info`/`controls` as the app demands); else build the `Profile` directly
      and reuse `InteractiveMeter`.
- [ ] Register the module in `profiles/__init__.py` (`PROFILES`).
- [ ] `python -m pytest -q tests/` green; `python -m fakemeter --profile <name>
      --self-check` registers.
- [ ] Live-confirm against the vendor app (connect, live reading, walk, HOLD) —
      HELD until the multi-device run.

## Hindsight gaps (flag these if you hit them)

Seams designed against one profile (voltcraft) that the second wave may stress:
- **`write` is a single UUID.** The driver's `gatt.write` is a LIST (UNI-T has a
  primary + fallback ISSC write char). `Profile.write_uuid` is one string. Fine for
  the emulator (expose one), but if an app probes the fallback, widen to a list.
- **Polled self-push — NEEDED, SPECIFIED (UNI-T handshake-then-stream).** A polled
  meter that free-streams after a GET_DATA write must self-push periodic frames, but
  in `'polled'` mode the server's stream loop is OFF and the profile has no ref to
  `MeterServer.notify`. The required seam (Recipe C): add `Profile.on_start(notify_cb)`,
  have the server call it after `_resolve_chars`, and install the GLib tick timer for
  polled-with-tick profiles on subscribe. `uni_t_base.make_profile` already attaches
  `meter.on_start`; the server just needs to call it + drive `tick`.
- **`function`/gear vocabulary differs per family.** voltcraft uses `V_DC/OHM/…`;
  the bdm/owon decoders emit range-independent keys (`ACV/DCV/OHM/…`). The encoder
  owns its own vocabulary (the `Reading.function` string is opaque to the core), so
  this is fine — but DON'T assume voltcraft's `FUNCTION_CODES` labels elsewhere.
- **`STATE_BITS` is per-profile, not shared.** Each family's state-word bit layout
  differs (voltcraft R10W ≠ owon-plus R2W ≠ bdm). The `Reading` flag NAMES are
  shared; the bit POSITIONS are not. Keep each profile's `STATE_BITS` local.
