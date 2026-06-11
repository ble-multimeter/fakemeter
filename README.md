# fakemeter — a fake BLE multimeter

A **BLE peripheral emulator** that impersonates Bluetooth multimeters so their
official phone apps (and our own Web-Bluetooth web app) connect to it and render
frames *we* craft. It is a **hardware-free black-box oracle** for verifying the
`uni-t-mmu-ble` BLE decode drivers: we push known bytes on the notify
characteristic and a human reads how the app decodes / displays them.

```
   ┌─────────────────────────┐         BLE         ┌──────────────────────────┐
   │  fakemeter (this tool)  │  advertise 0xFFF0   │   phone                  │
   │  ───────────────────    │ ──────────────────▶ │   ┌────────────────────┐ │
   │  craft bytes for a      │                     │   │ vendor app / our   │ │
   │  KNOWN reading, e.g.    │   notify (0xFFF4)   │   │ Web-Bluetooth app  │ │
   │  "4.200 V DC"           │ ──── frame bytes ──▶│   │ DECODES + DISPLAYS │ │
   │                         │                     │   └────────────────────┘ │
   │  (you know the input)   │ ◀── writes (0xFFF3) │   (human reads output)   │
   └─────────────────────────┘     logged          └──────────────────────────┘
            input  ───────────────────────────────────────▶  output
            If the app shows what we encoded, the decode logic is right.
            If it differs, we've found a bug (e.g. flag-bit order).
```

Each profile's frame *encoder* is the exact inverse of the matching decoder in
the driver repo, so a profile both (a) lets the vendor app act as an oracle, and
(b) surfaces driver bugs the oracle reveals. The original goal — settling the
**voltcraft flag-bit-order bug** — is **done** (it was reversed; see below); the
project has since fanned out to the whole driver family.

> This repo is a standalone sibling of the driver repo (`uni-t-mmu-ble`). The
> driver repo is **read-only reference** for the byte layouts — nothing here
> modifies it. Driver fixes this work surfaced live on a branch there.

## Profiles & verification status

Eleven profiles across four families. "Live-validated" = an actual reading was
read off the real vendor app's screen; "byte-verified" = the encoder round-trips
bit-exact against a Python port of the app's own decoder, but it hasn't been put
on-screen yet.

| Profile      | Family / format            | Vendor app (Android pkg)                 | Status | Notes / quirks |
| ------------ | -------------------------- | ---------------------------------------- | ------ | -------------- |
| `voltcraft`  | OWON R10W, 15-byte LE      | Voltcraft VC800/900 (`com.voltcraft.series800`, OWON iMeter rebadge) | ✅ **live-validated** | Flag order settled (LSB-first). Interactive buttons + value-walk + HOLD all on-screen. ⚠️ device-card shows a red **"disconnected"** badge even while live data flows — app quirk, does NOT block the reading. |
| `owon-plus`  | OWON 6-byte binary (R2W)   | OWON **iMeter** (`com.owon.imeter`)       | ✅ **live-validated** + real-HW corroborated | The OWON workhorse. Confirmed against a physical **B35T+**. The "+" meters (B35T+/B41T+) are this binary format. Use **iMeter** (writes the CCCD), not BLE4.0. |
| `owon-old`   | OWON 14-byte ASCII (B35)   | OWON BLE4.0 (`com.owon.MultimeterBLE`)    | ⚠️ **byte-verified, LEGACY** | 31/31 round-trip green, but no live oracle and no real hardware exists in the wild — every real meter is binary. **Flagged for likely removal** (see its module docstring). |
| `bdm`        | YSCoCo XOR-scrambled       | **Bluetooth DMM** (`com.yscoco.wyboem`), AN9002 | ✅ **live-validated** | Needed a device-type-byte fix (descrambled `byte[2]=0x03`, AB_300) to render the right unit. Advert name must be **exactly** `Bluetooth DMM` or `ZY`. |
| `ai-care`    | AiCare self-addressing (FFB0) | INTELLIGENT MULTIMETER (`aicare.net.cn.iMultimeter`) | ✅ **live-validated** | Scan gate is **manufacturer-data, not name** — the emulator advertises `AC FF <mac-reversed>` so the app lists it (see below). The big readout only updates **after you tap the green "Start" button** in the app. |
| `uni-t`      | UNI-T AB-CD, 19-byte (polled) | UNI-T **Smart Measure** (`com.uni_t.multimeter`), as UT60BT | ✅ **live-validated** | Handshake-then-stream. Needed a range-index unit fix (range 0 = mV, not V). Advert name must be **exactly** a supported model (e.g. `UT60BT`). |
| `ut202bt`    | UNI-T (shares `uni_t.encode`) | Smart Measure                          | 🟡 **inherits uni-t fix** | Same encoder as `uni-t`, so it inherits the range fix; not separately put on-screen. |
| `ut117c`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified** | Own encoder; per-model unit/range sweep against its app still owed. |
| `ut171`      | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified** | Own encoder; not live-swept. |
| `ut181a`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified, partial** | MAIN value block only; secondary block + datalog deferred (need a HW capture). |
| `ut219p`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified, partial** | Standard live-data frame only; `daoPos`→param dispatch + battery-gate handshake deferred. |

**Tests: `262 passed`** — encoder round-trips through per-profile decoder ports,
the FFF1 auth math, the interactive reactions, the meter-core engine, and the
unsolicited-injection PDU layout.

## Install

Needs Linux + BlueZ (tested on BlueZ 5.72) and a working BLE adapter. The `bluezero`
library sits on top of the distro's `python3-dbus` and `python3-gi` (PyGObject),
which are painful to pip-build — so create the venv with `--system-site-packages`
so those are visible:

```bash
sudo apt install python3-dbus python3-gi python3-venv   # if not already present

cd fake-ble-meter
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt        # bluezero + pytest
```

Verify the adapter is up first:

```bash
hciconfig -a            # hci0 should be UP RUNNING
bluetoothctl list       # should list your controller
```

## Run

```bash
. .venv/bin/activate
python -m fakemeter --profile voltcraft --adapter hci0
```

Pick any profile id from the table above with `--profile`. The OWON family
publishes GATT service `0xFFF0` (notify `0xFFF4`, write `0xFFF3`, secure `0xFFF1`,
info `0xFFF2`); UNI-T uses the ISSC `49535343-…` service; ai-care uses `0xFFB0`.

You then get a keyboard REPL:

```
  p   list + play a preset pattern
  v   set a live reading (value / function / prefix)
  f   toggle an annunciator flag (hold/rel/auto/bat/min/max)
  r   re-send the current frame
  raw <hexbytes>   inject an arbitrary frame (the byte-mapping tool)
  s   start the MODE-WORD BIT SWEEP   (the flag-order test — voltcraft)
  series <id> / auth <mode> / walk on|off
  ?   help     q   quit
```

Flags:

- `--adapter hciN` — which BlueZ adapter (default `hci0`). Also accepts a BD address.
- `--name NAME` — override the advertised local name. **Many vendor apps filter
  the scan list by exact model name** (see "Exact-name scan filters" below), so the
  per-profile `*-FAKE` default will not list in those apps — pass the real model name.
- `--self-check` — publish, verify advertisement + GATT registered, run encoder
  round-trips, then exit. No phone needed; confirms the host is sane.
- `--no-walk` — disable the demo value drift (fixed reading, for precise byte-mapping).
- `--no-unsolicited` — disable the raw-HCI no-CCCD injection path (stream profiles).
- `-v` — debug logging (logs every notify / write, including the FFF1 challenge).

## How a phone-side check goes

1. Start the tool on the target profile (advertise the **exact** model name the
   app expects — see below).
2. On the phone, open the vendor app (or our Web-Bluetooth app / nRF Connect).
3. Connect to the advertised device; it should immediately show the initial
   reading (default `4.200 V DC`).
4. Play presets (`p`), set values (`v 230.5 V`), toggle flags (`f hold`) — confirm
   each renders correctly. Changing the value on-screen proves it's genuinely live,
   not a static template. Use `raw <hexbytes>` to map any byte/bit to the display.

## Cross-cutting mechanisms & gotchas

These bit every OWON/Java-app validation and are the hard-won part of the project.

### The CCCD wall (and the no-CCCD injection workaround)
A bluezero/BlueZ peripheral only emits notifications to a client that **subscribed
via the 0x2902 CCCD**. Some apps don't write it:
- **Newer/Flutter apps write the CCCD** → normal BlueZ path works: Voltcraft
  series800, OWON **iMeter**, UNI-T Smart Measure, Bluetooth DMM, ai-care, nRF.
- **The OWON BLE4.0 *Java* app NEVER writes the CCCD for FFF4** (vestigial sample
  code only writes it for the heart-rate UUID) — it relies on the real chip emitting
  *unsolicited* notifications. BlueZ won't route to that link, so the app sits on
  "No input".

`fakemeter/unsolicited.py` solves this at the delivery layer: it injects ATT
Handle-Value-Notification PDUs **directly onto the ACL link via a raw HCI socket**,
bypassing BlueZ's CCCD gate — exactly like a real meter chip. On by default for
stream profiles (`--no-unsolicited` to disable); the moment a real CCCD write lands
it stops and BlueZ's path takes over, so well-behaved apps are unaffected. The
mechanism is **proven over-air** (no-CCCD notification displayed on nRF). It
**requires `CAP_NET_RAW`** to deliver: `sudo setcap cap_net_raw+ep "$(readlink -f .venv/bin/python)"`.

### Dual-mode BR/EDR wall (OWON apps)
The OWON apps `connectGatt(AUTO)`; a dual-mode `hci0` makes Android connect over
Bluetooth *Classic* and auto-bond, blocking LE GATT. Fix: make the adapter LE-only
(`sudo btmgmt --index 0 bredr off`, reversible) **and** clear the phone's cached
device record so AUTO re-resolves to LE.

### Scan filters (each app gates differently)
Several vendor apps' add/scan screens filter what they'll list — the *model
identity* lives in the frame, not the advert name:
- **Bluetooth DMM** lists only devices named exactly `Bluetooth DMM` or `ZY` (the
  AN9002 identity is the device-type byte `0x03`, not the name).
- **UNI-T Smart Measure** matches `equalsIgnoreCase` against its supported-model set
  (`UT60BT`, `UT202BT`, `UT219P`, `UT117C`, …) — `UT60BT-FAKE` is filtered out.
- **ai-care INTELLIGENT MULTIMETER** ignores the name entirely and gates on
  **advertised manufacturer-specific data**: it lists a device only if the advert
  carries service `FFB0` **and** a manufacturer field `AC FF <device-MAC reversed>`.
  The `ai-care` profile advertises this automatically (via a `manufacturer_data`
  advert hook), so any `--name` works.

So advertise the real model name with `--name`, not the `*-FAKE` default.

### FFF1 MD5 anti-counterfeit auth (OWON family)
OWON meters gate the app UI on an MD5 *identity* challenge on `FFF1`: the app writes
6 "mixed coordinate" bytes, then reads back the meter's response. `fakemeter`
reproduces the responder (`auth_table="vc"`, default) — recover `orig = mixed -
[200,100,50,20,10,5]`, map through the app's `s1[]`/`s2[]` char tables, and return
the **16 raw MD5 digest bytes** (the app hex-encodes them itself; returning ASCII
hex double-hexes and fails). Confirmed live against both the Java and Flutter apps.

### Interactive control buttons (FFF3)
Pressing a meter-screen button writes `[opcode, 0x01]` to FFF3; the emulator mutates
the streamed reading and the app's display follows. Hold/Select/AC-DC/Rel/Max-Min/
LPF/Range are all implemented and verified on-screen for voltcraft.

### Value-walk demo drift
On by default: the streamed numeric reading gently drifts each stream tick (jitter +
mean-reversion toward the nominal) so it feels live. Freezes under HOLD. `--no-walk`
or REPL `walk off` pins it for precise byte-mapping.

## The voltcraft flag-order question — SETTLED

The driver's `voltcraft.ts` read the mode-flags word **MSB-first**. The live
bit-sweep (menu `s`) on the real app settled it: the state word is a straight
**LSB-numbered bitmask** with **HOLD = bit0** (then REL=1, AUTO=2, Bat=3, MIN=4,
MAX=5, …). The driver's MSB-first read is **wrong** — flags must be read LSB-first,
matching the same fix already applied to the sibling `owon-plus`. Full confirmed
R10W layout is in `docs/PROGRESS.md` and `docs/voltcraft-measurement-protocol.md`.

## Tests

```bash
. .venv/bin/activate
pytest -q        # 262 passed
```

Each profile round-trips readings through its encoder and a minimal Python port of
the matching decoder (`tests/decode_*.py`), asserting value / unit / decimal point /
sign survive, plus the FFF1 auth math, the interactive reactions, the meter-core
engine, and the unsolicited-injection PDU byte layout.

## Two radios / two instances

The design is fully instance-scoped (the adapter is threaded through, no globals),
so with a second BLE dongle you can run a second independent emulator on `hci1`.
Documented config, exercised lightly — only one adapter is usually present here.

## Privilege / D-Bus notes

- The GATT server + advertisement **register fine as a normal user** on this host
  (BlueZ 5.72) — `bluetoothctl` already works for the user. `--self-check` confirms
  registration via `LEAdvertisingManager1.ActiveInstances`.
- **`CAP_NET_RAW`** is needed only for the no-CCCD raw-HCI injection path (above)
  and for `btmon` on-air capture. The emulator's normal (CCCD) path needs no extra
  privilege.
- **Persistence gotcha**: the Claude/agent harness SIGKILLs (exit 144) any
  bluez/D-Bus process it spawns when the spawning call returns. For long-lived live
  runs, launch outside the harness (a real terminal, `screen`, or `setsid … &
  disown` which reparents under `systemd --user` so it survives). Drive a detached
  instance via a FIFO and read its `tee` logfile.
- **Bond-clear on restart**: restarting the emulator invalidates a phone's stored
  LTK ("Couldn't pair / incorrect PIN"). Clear the bond on **both** sides first
  (`bluetoothctl remove <phone-addr>` + toggle the phone's Bluetooth) then re-add.

## Further docs

- `docs/PROGRESS.md` — full chronological log: every profile's validation session,
  the protocol analysis, and the open items.
- `docs/adding-a-profile.md` — the add-a-profile template + the layering map
  (`base` → `meter_core` → `owon_base`/`uni_t_base` → per-model).
- `docs/voltcraft-measurement-protocol.md`, `docs/owon-voltcraft-handshake.md` —
  the confirmed R10W frame + the FFF1/FFF2 handshake.

## Status summary

- ✅ **Live-validated on the real vendor app**: `voltcraft`, `owon-plus`
  (+ real B35T+ hardware), `bdm` (as AN9002), `uni-t` (as UT60BT), `ai-care`
  (INTELLIGENT MULTIMETER).
- 🟡 **Byte-verified, not yet on-screen**: `ut202bt` (inherits uni-t),
  `ut117c`/`ut171`/`ut181a`/`ut219p` (own encoders, per-model app sweep owed;
  ut181a/ut219p are partial-frame).
- ⚠️ **Legacy**: `owon-old` — byte-correct but no live oracle / no real hardware;
  flagged for likely removal.
- The driver-repo fixes this surfaced (voltcraft R10W rewrite + LSB flag order,
  owon-old byte6/nano, bdm AB_300) live on a branch in `uni-t-mmu-ble`, pending
  review/merge.
