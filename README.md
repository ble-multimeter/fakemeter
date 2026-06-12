# fakemeter

A Linux **BLE peripheral emulator** that impersonates Bluetooth multimeters, so a
meter's official phone app connects to *it* and decodes frames you craft. Set a
known reading (e.g. `4.200 V DC`); if the app displays it, the decode is correct —
if it differs, you've found a bug. A **hardware-free way to verify BLE multimeter
decode logic** (built to validate the `uni-t-mmu-ble` drivers).

```
   ┌─────────────────────────┐         BLE         ┌──────────────────────────┐
   │  fakemeter (this tool)  │  advertise 0xFFF0   │   phone                  │
   │  ───────────────────    │ ──────────────────▶ │   ┌────────────────────┐ │
   │  craft bytes for a      │                     │   │ vendor app /       │ │
   │  KNOWN reading, e.g.    │   notify (0xFFF4)   │   │ Web-Bluetooth app  │ │
   │  "4.200 V DC"           │ ──── frame bytes ──▶│   │ DECODES + DISPLAYS │ │
   │                         │                     │   └────────────────────┘ │
   │  (you know the input)   │ ◀── writes (0xFFF3) │   (human reads output)   │
   └─────────────────────────┘     logged          └──────────────────────────┘
```

Eleven profiles across the OWON, Voltcraft, UNI-T, AiCare and BDM families — five
live-validated against the real vendor apps (table below).

## Requirements

- Linux with **BlueZ** (tested on 5.72) and a working BLE adapter — `hciconfig`
  should show it `UP RUNNING`.
- `bluezero`, which sits on the distro's `python3-gi` + `python3-dbus` (PyGObject).
  Those don't pip-build cleanly, so install them from apt and let the install see
  them (below).

## Install

```bash
sudo apt install python3-gi python3-dbus            # system BLE stack

pipx install --system-site-packages fakemeter       # isolated CLI, recommended
# — or into a venv —
python3 -m venv --system-site-packages .venv && . .venv/bin/activate
pip install fakemeter
```

Both give you the `fakemeter` command. (From source: clone, then `pip install -e .`
in the same kind of `--system-site-packages` venv.)

## Run

```bash
fakemeter --profile voltcraft        # advertises + opens a REPL
```

`--profile` takes any id from the table. Connect the vendor app to the advertised
device — it shows the initial `4.200 V DC`. Drive it from the REPL:

```
p           play a preset             v 230.5 V    set value / function / prefix
f hold      toggle a flag             r            re-send the current frame
raw <hex>   inject an arbitrary frame s            voltcraft bit-sweep
series <id> / auth <mode> / walk on|off            ?  help        q  quit
```

Useful flags:

- `--name NAME` — advertised name. Several apps only list an **exact model name**
  (see Gotchas) — pass the real one, not the `*-FAKE` default.
- `--adapter hciN` — which adapter (default `hci0`; also accepts a BD address).
- `--self-check` — publish, verify advert + GATT, run encoder round-trips, exit.
  No phone needed; confirms the host is sane.
- `--no-walk` — fixed reading (for precise byte-mapping). `--no-unsolicited` —
  disable the raw-HCI no-CCCD delivery path. `-v` — log every notify / write.

## Profiles

"Live-validated" = a reading was read off the real vendor app's screen.
"Byte-verified" = the encoder round-trips bit-exact against a port of the app's
own decoder, but hasn't been put on-screen yet.

| Profile      | Family / format            | Vendor app (Android pkg)                 | Status | Notes / quirks |
| ------------ | -------------------------- | ---------------------------------------- | ------ | -------------- |
| `voltcraft`  | OWON R10W, 15-byte LE      | Voltcraft VC800/900 (`com.voltcraft.series800`, OWON iMeter rebadge) | ✅ **live-validated** | Flag order settled (LSB-first). Interactive buttons + value-walk + HOLD all on-screen. ⚠️ device-card shows a red **"disconnected"** badge even while live data flows — app quirk, does NOT block the reading. |
| `owon-plus`  | OWON 6-byte binary (R2W)   | OWON **iMeter** (`com.owon.imeter`)       | ✅ **live-validated** + real-HW corroborated | The OWON workhorse. Confirmed against a physical **B35T+**. The "+" meters (B35T+/B41T+) are this binary format. Use **iMeter** (writes the CCCD), not BLE4.0. |
| `owon-old`   | OWON 14-byte ASCII (B35)   | OWON BLE4.0 (`com.owon.MultimeterBLE`)    | ⚠️ **byte-verified, LEGACY** | 31/31 round-trip green, but no live oracle and no real hardware exists in the wild — every real meter is binary. **Flagged for likely removal** (see its module docstring). |
| `bdm`        | YSCoCo XOR-scrambled       | **Bluetooth DMM** (`com.yscoco.wyboem`), AN9002 | ✅ **live-validated** | Needed a device-type-byte fix (descrambled `byte[2]=0x03`, AB_300) to render the right unit. Advert name must be **exactly** `Bluetooth DMM` or `ZY`. |
| `ai-care`    | AiCare self-addressing (FFB0) | INTELLIGENT MULTIMETER (`aicare.net.cn.iMultimeter`) | ✅ **live-validated** | Scan gate is **manufacturer-data, not name** — the emulator advertises `AC FF <mac-reversed>` so the app lists it (see Gotchas). The readout only updates **after you tap the green "Start" button**. |
| `uni-t`      | UNI-T AB-CD, 19-byte (polled) | UNI-T **Smart Measure** (`com.uni_t.multimeter`), as UT60BT | ✅ **live-validated** | Handshake-then-stream. Needed a range-index unit fix (range 0 = mV, not V). Advert name must be **exactly** a supported model (e.g. `UT60BT`). |
| `ut202bt`    | UNI-T (shares `uni_t.encode`) | Smart Measure                          | 🟡 **inherits uni-t fix** | Same encoder as `uni-t`, so it inherits the range fix; not separately put on-screen. |
| `ut117c`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified** | Own encoder; per-model unit/range sweep against its app still owed. |
| `ut171`      | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified** | Own encoder; not live-swept. |
| `ut181a`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified, partial** | MAIN value block only; secondary block + datalog deferred (need a HW capture). |
| `ut219p`     | UNI-T 16-bit-len encoder   | Smart Measure                            | 🟡 **byte-verified, partial** | Standard live-data frame only; `daoPos`→param dispatch + battery-gate handshake deferred. |

## Verifying a decode

1. `fakemeter --profile <id> --name <exact-model>`.
2. Open the vendor app (or nRF Connect / a Web-Bluetooth client) and connect.
3. It shows the initial reading. Set values (`v 230.5 V`), toggle flags
   (`f hold`), play presets (`p`) — if the display matches what you sent, the
   decode is right. Changing the value live proves it's not a static template;
   `raw <hex>` maps any byte/bit to the screen.

## Gotchas

The practical ones — full detail in `docs/PROGRESS.md`.

- **Use the real model name.** App scan lists are filtered: *Bluetooth DMM* accepts
  only `Bluetooth DMM`/`ZY`; *UNI-T Smart Measure* only exact models (`UT60BT`,
  `UT219P`, …). *ai-care* instead gates on advertised manufacturer-data, which the
  profile emits automatically — so any `--name` works there.
- **OWON apps need an LE-only adapter.** They `connectGatt(AUTO)`; on a dual-mode
  adapter Android picks Classic and bonds, blocking LE GATT. Run
  `sudo btmgmt --index 0 bredr off` and clear the phone's cached device record.
- **The OWON BLE4.0 (Java) app never subscribes via CCCD.** fakemeter still reaches
  it by injecting ATT notifications over a raw HCI socket (on by default for stream
  profiles). That path needs `CAP_NET_RAW`:
  `sudo setcap cap_net_raw+ep "$(readlink -f "$(which python3)")"`. Apps that
  subscribe normally use the standard BlueZ path and need no extra privilege.
- **Restarting drops a phone's bond** (stale LTK → "incorrect PIN"). Clear it on
  both sides: `bluetoothctl remove <addr>` + toggle the phone's Bluetooth, then
  re-add from the app's scan screen.

## Tests

```bash
pip install pytest && pytest -q      # 262 passed
```

Pure-Python (no BLE hardware needed): each profile round-trips readings through its
encoder and a port of the matching decoder (`tests/decode_*.py`), checking
value / unit / decimal point / sign, plus the FFF1 auth math, the interactive
reactions, the meter-core engine, and the raw-HCI PDU layout. CI runs them on
Python 3.10–3.13.

## Docs

- `docs/PROGRESS.md` — per-profile validation log, protocol details, and gotchas in full.
- `docs/adding-a-profile.md` — how to add a profile + the layering map
  (`base` → `meter_core` → `owon_base`/`uni_t_base` → per-model).
- `docs/voltcraft-measurement-protocol.md`, `docs/owon-voltcraft-handshake.md` —
  the R10W measurement frame and the OWON FFF1/FFF2 connect handshake.

## License

MIT — see [LICENSE](LICENSE).
