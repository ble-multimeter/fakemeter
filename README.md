# fakemeter — a fake BLE multimeter

A **BLE peripheral emulator** that impersonates Bluetooth multimeters so their
official phone apps (and our own Web-Bluetooth web app) connect to it and render
frames *we* craft. It is a **hardware-free black-box oracle** for verifying BLE
decode logic: we push known bytes on the notify characteristic and a human reads
how the app on a phone decodes / displays them.

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

The first profile is **voltcraft** (Voltcraft VC800/VC900, an OWON rebadge). Its
frame *encoder* is the exact inverse of the `decodeVoltcraft` decoder in the driver
repo. Its #1 job is to settle a suspected **flag-bit-order bug** (see below).

> This repo is a standalone sibling of the driver repo (`uni-t-mmu-ble`). The
> driver repo is **read-only reference** for the byte layouts — nothing here
> modifies it.

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

This publishes a GATT service `0xFFF0` (notify `0xFFF4`, write `0xFFF3`, secure
`0xFFF1`) and starts advertising as **`VC900-FAKE`**. You then get a keyboard menu:

```
  p   list + play a preset pattern
  s   start the MODE-WORD BIT SWEEP   (the flag-order test — see below)
  v   set a live reading (value / function / prefix)
  f   toggle an annunciator flag (hold/rel/auto/bat/min/max)
  r   re-send the current frame
  ?   help     q   quit
```

Flags:

- `--adapter hciN` — which BlueZ adapter (default `hci0`). Also accepts a BD address.
- `--name NAME` — override the advertised local name (default per profile).
- `--self-check` — publish, verify advertisement + GATT registered, run encoder
  round-trips, then exit. No phone needed. Use this to confirm the host is sane.
- `-v` — debug logging (logs every notify / write, including the FFF1 challenge).

### Preset patterns (`p`)

| preset            | what the phone should show                         |
| ----------------- | -------------------------------------------------- |
| `dc_volts`        | `4.200 V` DC                                       |
| `resistance_kohm` | `1.000 kΩ`                                          |
| `current_ua`      | `12.30 µA` DC                                       |
| `overload`        | `O.L` (overload, AC volts)                          |
| `negative`        | `-0.512 V` DC                                       |
| `bitsweep`        | the mode-word bit sweep (below)                    |

## The mode-bit sweep — the flag-order question (READ THIS)

The driver's `voltcraft.ts` currently reads the mode-flags word (`bytes[12..13]`)
**MSB-first** (`hold=bit15, rel=bit14, auto=bit13, lowBattery=bit12, min=bit11,
max=bit10`). The sibling `owon-plus` driver had the **identical** bug and was
corrected to **LSB-first** (`hold=bit0 … max=bit5`). We strongly suspect voltcraft
has the same reversed-flag bug, but it has never been proven on hardware.

`fakemeter` settles it. Run the sweep (menu `s`, or play preset `bitsweep`): it
emits a stable **4.200 V DC** base reading and toggles **exactly one** mode-word
bit at a time, walking bits 0..15. For each step, read the phone and note which
annunciator (HOLD / REL / AUTO / battery / MIN / MAX, or none) lights up. That map
is definitive:

- If **bit 0 → HOLD, bit 1 → REL, … bit 5 → MAX**, the order is **LSB-first** and
  `voltcraft.ts` is **wrong** (needs the same fix as commit `4506bdc` did for
  owon-plus).
- If **bit 15 → HOLD, bit 14 → REL, …**, the driver's current MSB-first order is
  right after all.

You can step the sweep manually (press ENTER per bit) or give an auto interval in
seconds. Tell Claude, for each bit number, which annunciator lit — that resolves
the driver bug.

## Drive the phone-side check

1. Start the tool: `python -m fakemeter --profile voltcraft --adapter hci0`.
2. On the phone, open **either** the vendor app ("Voltcraft VC800 VC900 Series" /
   the OWON multimeter app) **or** our Web-Bluetooth web app.
3. Connect to the advertised device **`VC900-FAKE`** (service `0xFFF0`).
4. It should immediately show **`4.200 V DC`** (the initial frame).
5. Press `p` and play the presets — confirm each renders as the table above says.
6. Press `s` and run the **mode-bit sweep**; record which annunciator lights per
   bit. This is the key result.

If the vendor app shows **nothing** until something happens on `0xFFF1`, that's the
anti-counterfeit gate — see below; it is auto-answered, and `-v` logs the exchange.

## Two radios / two instances

The design is fully instance-scoped — the adapter is threaded through, no globals —
so when a second BLE dongle is present you can run a **second** independent
emulator on it:

```bash
# terminal 1
python -m fakemeter --profile voltcraft --adapter hci0 --name VC900-FAKE-A
# terminal 2 (requires a second adapter present as hci1)
python -m fakemeter --profile voltcraft --adapter hci1 --name VC900-FAKE-B
```

This is **documented config, not yet exercised** — only one adapter (`hci0`) is
present on this machine, so two-radio operation has not been run here.

## The FFF1 auth gate (anti-counterfeit)

OWON-family meters expose an `FFF1` "secure" characteristic with an MD5 *identity*
challenge. Our drivers ignore it (the meter free-streams on `FFF4` regardless), but
the **app may gate its UI** on it. `fakemeter` implements the responder, recovered
from the decompiled OWON "OWON Multimeter BLE4.0" Java app
(`com.lilliput.Multimeter.ble.encrypt.{BleClientIdentityVerify, UseMd5, MD5For32}`):

1. The app **writes** 6 "mixed coordinate" bytes to `FFF1` (each byte =
   `original_coord[i] + mixElems[i]`, `mixElems = [200,100,50,20,10,5]`; sometimes
   padded to 16 bytes).
2. The app **reads** `FFF1`. The meter (us) must reply with the **uppercase MD5
   hex** (32 ASCII chars) of a string built by *recovering* the original
   coordinate (`orig = mixed - mixElems`) and mapping each coordinate through the
   app's char tables `s1[]` (indices 0..2) / `s2[]` (indices 3..5).
3. The app computes the same MD5 itself and compares; on match it shows its UI.

`fakemeter` reproduces step 2 exactly, so a genuine-looking response is returned
automatically. **Every write to `FFF1`/`FFF3` is logged** (run with `-v`), so if a
particular app build deviates, you'll see the actual challenge bytes and can adapt
the responder. The auth math is unit-tested
(`tests/test_voltcraft_encoder.py::test_auth_response_matches_app_computation`).

> Caveat: the challenge/response scheme is confirmed against the **Java** OWON app.
> The Flutter rebadge (`com.voltcraft.series800`) uses an `encryptByMD52`/
> `generateMd5` path with the same intent; if that app rejects the response, capture
> the `FFF1` write with `-v` and send it over — the responder can be tuned from the
> observed bytes.

## Tests

```bash
. .venv/bin/activate
pytest -q
```

`tests/` round-trips readings through the encoder and a minimal Python port of the
voltcraft *decoder* (`tests/decode_voltcraft.py`), asserting value / unit / decimal
point / sign survive. **Flags are deliberately NOT asserted** in the round trip —
the MSB-vs-LSB flag order is the very thing the phone sweep settles, so the tests
only confirm the encoder is self-consistent with its chosen LSB-first orientation.

## Privilege / D-Bus notes

- The GATT server + advertisement **register fine as a normal user** on this host
  (BlueZ 5.72) — no root, no custom D-Bus policy file needed. `bluetoothctl`
  already works for the user, i.e. user-level D-Bus access to BlueZ exists.
- `--self-check` confirms registration via BlueZ's
  `org.bluez.LEAdvertisingManager1.ActiveInstances` property (it reads `1` while
  running, `0` after exit — the tool cleans up its advertisement on quit).
- **`btmon`** (raw HCI monitor, to sniff the actual advertising PDUs on air)
  **does need root** here (`Failed to bind channel: Operation not permitted` as a
  user). It was **not** run during development because passwordless sudo is not
  available and the task forbids silent system changes. If you want an on-air
  capture, run `sudo btmon` in another terminal while the tool advertises. The
  emulator itself needs no such privilege.

## Status / what's verified

Verified here, hardware-free:

- ✅ Adapter resolves (`hci0` → `44:AF:28:A5:53:1A`); GATT app + LE advertisement
  **register as a normal user**; `LEAdvertisingManager1.ActiveInstances = 1` while
  running; advertised `LocalName = VC900-FAKE`, `ServiceUUIDs = [0xFFF0]`.
- ✅ All three characteristics export with correct UUIDs/flags (FFF4 notify, FFF3
  write, FFF1 secure).
- ✅ Notify push round-trips into the characteristic Value.
- ✅ Encoder round-trip tests pass (value / unit / decimal point / sign), 20 tests.
- ✅ FFF1 MD5 auth responder reproduces the OWON Java app's own computation
  (unit-tested) — but only **stub-level confidence** against the Flutter rebadge.

Not yet verified (needs the phone / extra hardware):

- ⬜ **The mode-bit-sweep flag order** — the whole point. Needs a phone.
- ⬜ Whether any specific app **gates its UI on FFF1** and accepts our response.
- ⬜ Two-radio operation (only one adapter present).
- ⬜ On-air advertising PDU capture via `btmon` (needs root).
- ⬜ The 15-byte dual-display framing / secondary block / `0xF0` markers as the app
  actually parses them (the driver's framing is inferred from a C# port; the OWON
  app symbols didn't corroborate a dual-display parser).
```
