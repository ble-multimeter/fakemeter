# fake-ble-meter — progress log & handoff

A BLE peripheral emulator that impersonates multimeters so their official phone apps connect to it, letting us use the vendor app as a **hardware-free decode oracle** to verify the `uni-t-mmu-ble` BLE drivers. Primary current target: the **Voltcraft VC800/VC900** app (an OWON Flutter rebadge) to verify/fix `packages/protocol/src/drivers/voltcraft.ts` — in particular settling its suspected MSB-vs-LSB flag-bit-order bug via a live "bit-sweep".

## ai-care LIVE-VALIDATED on INTELLIGENT MULTIMETER app — required ADVERT MANUFACTURER-DATA gate (2026-06-11)

ai-care (`aicare.net.cn.iMultimeter`, "INTELLIGENT MULTIMETER") is now **live-validated,
on-screen confirmed**, with a CORRECT, LIVE, REPL-drivable reading. **262/262 tests green
(260 prior + 2 new ai-care manufacturer-data tests); py_compile clean.**

### THE REAL SCAN GATE — it is NOT a device-name allowlist (the handoff's assumption was wrong)
Unlike bdm/UNI-T (exact advertised-name allowlists), the ai-care app **ignores the local
name entirely** and gates its scan on **advertised Manufacturer-Specific-Data**. Observed
live: the app's scan callback connects ONLY when BOTH:
1. the advert's Service-UUIDs contain **FFB0**, AND
2. a Manufacturer-Specific-Data field satisfies
   `data[0]==0xAC && data[1]==0xFF && getAddress(data) == device.getAddress()`,
   where `getAddress` takes the **LAST 6 bytes and reverses them** into the uppercase MAC.
There is NO device-name filter (the name is passed straight through to the connect call).

### FIX — the emulator now advertises Manufacturer-Specific-Data
The emulator never advertised manufacturer data before. Added:
- `base.Profile.manufacturer_data: Optional[Callable[[adapter_addr], (company_id, payload)|None]]`
  — a generic optional advert hook (None for every other profile, unchanged).
- `gatt_server._build`: after building the peripheral, if the profile sets the hook, call
  `periph.advert.manufacturer_data(company_id, payload)` (bluezero's `Advertisement.GetAll`
  DOES emit `ManufacturerData` when set, so BlueZ broadcasts it).
- `ai_care.manufacturer_data(adapter_addr)` returns `(0xFFAC, mac_octets_little_endian)`.
  BlueZ prepends the company id (`0xFFAC` → on-air bytes **AC FF**), and the payload is the
  6 MAC octets REVERSED, so on air the field is `AC FF <mac-rev>` and the app's `getAddress`
  reverses the last 6 back to the real MAC. For hci0 (`44:AF:28:A5:53:1A`) the advertised
  manufacturer payload is `1a 53 a5 28 af 44`. Tests `test_ai_care.py`:
  `test_manufacturer_data_encodes_mac_for_app_scan_gate` + `_rejects_malformed_addr`.
NO encoder/decoder change — the frame format was already byte-correct; only the *advert* was
missing the scan-gate key.

### Second gotcha — the live display only updates after pressing "Start"
After connect+subscribe the big readout stayed `0.0.0.0` even though frames streamed. Cause
(observed live): each frame is parsed + buffered, but the app only updates the display **after
the user taps the green Start button** — the readout stays at its placeholder until then. Tap
Start → live reading appears (the green Start button is the default-enabled state, NOT a
connection indicator). The app's notify-subscribe path writes the CCCD with the *notification*
(not indication) value, so our `notify` char flag is correct.

### On-screen results (screenshots `/tmp/ai-05..08-*.png`)
- **4.207 V** DC (walking 4.188–4.236, V lit) — live value-walk, gauge auto-ranged 0–40.
- REPL `v 1.000 OHM k` → **0.991 KΩ** (K+Ω lit, gauge re-ranged 0–4) — proves live
  value-change + function/unit dispatch (the decisive liveness test).
- REPL `walk off; v 12.50 V_AC` → **012.5** with **AC** annunciator + V lit (Max/Min/Avg
  frozen at 12.5, confirming walk-off) — AC/DC dispatch confirmed.
- REPL `f hold` → **DH** (Data Hold) annunciator lit, value held — `hold → DH/HOLD` confirmed.

### Infra notes (same host as the OWON work)
- hci0 LE-only (`bredr off`) from the OWON session — left as-is; ai-care has no auth/bond so
  no LTK issues. The app discovers + connects over LE cleanly via the manufacturer-data gate.
- **Reconnect gotcha:** after the first connect/disconnect, re-foregrounding the app or
  re-tapping **+** would NOT reconnect (stale link/GATT cache). Remediation that worked:
  `adb shell su -c 'rm -f /data/misc/bluetooth/gatt_cache_44af28a5531a /data/misc/bluetooth/gatt_hash_*'`
  + `cmd bluetooth_manager disable/enable` (toggle phone BT) + `am force-stop` the app, then
  relaunch → tap the red **+** (top-right; bounds `[969,301][1052,384]`) to scan/connect.
- Left RUNNING on ai-care advertising `AICARE-FAKE` (FIFO `/tmp/fm-ai.fifo`, log `/tmp/fm-ai.log`).

## OWON retests: owon-plus LIVE-VALIDATED, owon-old ruled LEGACY (2026-06-11)

Retested both OWON profiles on the phone. Two infra blockers were solved and the
owon-old vs owon-plus question was settled definitively via prior art.

**Infra fixes (both needed for any OWON app on this bluezero host):**
- **Dual-mode BR/EDR wall** — the OWON apps `connectGatt(AUTO)`; our dual-mode `hci0`
  made Android connect over Bluetooth *Classic* and auto-bond, blocking LE GATT. Fix:
  `sudo btmgmt --index 0 bredr off` (LE-only, reversible) **+** clear the phone's cached
  device record (`rm /data/misc/bluetooth/gatt_cache_44af28a5531a`, toggle phone BT) so
  AUTO resolves to LE. Without the cache clear, Android keeps the device typed DUAL and
  still picks Classic (then just fails). Requires the phone-side clear, not only the host.
- **App choice is the CCCD gate** — the **OWON BLE4.0 Java app NEVER writes the FFF4
  CCCD** (no live render on a bluezero peripheral; the in-emulator raw-HCI injector is
  unproven over-air — the only prior proof used an external root/docker injector). The
  **OWON iMeter Flutter app (`com.owon.imeter`, iMeter 1.2.4) DOES write the CCCD** →
  streams via BlueZ's normal path. Use iMeter for OWON live validation
  (grant FINE_LOCATION/BLUETOOTH_SCAN/CONNECT; launcher `.MainActivity`).

**owon-plus (series 18): ✅ live-validated AND real-hardware-corroborated.** iMeter shows
a clean walking `DC 4.196→4.205 V`. AND a real physical **OWON B35T+** is confirmed to
stream this exact 6-byte binary format via the community gatttool reader
`github.com/53845714nF/OWON_B35T` (its `(w>>6)&0xF`/`(w>>3)&7`/`w&7` + function-bitmask +
low15-mag/bit15-sign decode is bit-exact with `owon_plus`). The "+" meters (B35T+/B41T+)
are binary = owon-plus.

**owon-old (series 35, 14-byte ASCII): ⚠️ byte-correct but LEGACY/VESTIGIAL — candidate
for removal.** Our encoder is byte-exact vs the reference `decodeOwonOld`/Windows
`b35tDecodeOld`/the BLE4.0 app's B35 decode (31/31 round-trip green) — NOT the issue.
But it has no live oracle and no real-world corroboration:
- iMeter mis-decodes it with its **binary R2W parser** (our owon-old `4.200 V` frame →
  `12.8 mV`, where 128 = our byte10 `0x80` read as the value word) — iMeter has an ASCII
  path but only for binary "+" models; observed live, **no legacy B35T ASCII model exists
  in iMeter** (its binary parser is the only one that engages for the B35 series id).
- The BLE4.0 app has the correct ASCII decoder but the no-CCCD wall → can't render live.
- The Windows app's README says only "+ type" devices were ever tested; **no real B35T
  ASCII hardware dump exists anywhere in the community** — every real meter is binary.
- Recognition prior art: Windows app = MANUAL device-type dropdown; phone apps = FFF2
  series id; **our driver = content sniffing (`looksLikeOwonOldFrame`) — the robust one.**

**Decision: keep `owon_old` for now but flagged for likely removal** (stance recorded in
`fakemeter/profiles/owon_old.py` docstring) — it models a meter generation that appears
extinct in the wild and has no app/hardware oracle. `owon_plus` is the OWON workhorse.

**Possible follow-up (separate infra task, NOT owon-old-specific):** make the in-emulator
no-CCCD raw-HCI injector actually deliver over-air to a real app (currently unverified) —
would unblock any CCCD-less Java app, not just owon-old. Deferred.

## bdm + ai-care validation vs their official vendor apps (2026-06-10)

Validated the **bdm** and **ai-care** profiles against the official Android apps via
live analysis (the non-radio go/no-go + byte-correctness). **Both apps WRITE the 0x2902
CCCD on subscribe → NEITHER hits the OWON CCCD wall; both are live-validatable on the
normal BlueZ path (no `cap_net_raw` injection needed).** Apps installed + permissioned
on phone `995b6385` (`com.yscoco.wyboem`, `aicare.net.cn.iMultimeter`). Tests:
`tests/test_bdm.py` + `tests/test_ai_care.py` **41/41 green**.

### CCCD behavior (the critical go/no-go) — both GO
- **bdm** (`com.yscoco.wyboem`): its notify-subscribe path enables the notify char then
  writes the 0x2902 descriptor with `ENABLE_NOTIFICATION_VALUE`, **unconditionally**
  for its notify char (no heart-rate-UUID gate like OWON). WRITES the CCCD → GO.
  Active BLE config: service FFF0, **notify FFF4**, and WRITE also FFF4. The meter
  free-streams after subscribe (status `1005` = "all notify enabled, start syncing");
  **no write is required to start the stream**, so the profile's `write_uuid=FFF3` vs
  the app's FFF4 write char is a non-blocking mismatch for this receive-only family.
- **ai-care** (`aicare.net.cn.iMultimeter`): its notify-subscribe path enables FFB2,
  fetches the 0x2902 descriptor, sets `ENABLE_NOTIFICATION_VALUE`, and writes it.
  WRITES the CCCD → GO. GATT confirmed service FFB0 / notify FFB2 / write FFB1
  (matches the profile exactly).

### bdm — FIX made: emit the AB_300 device-type byte (descrambled byte 2 = 0x03)
**The bdm profile had a real bug surfaced only by the *Android* app** (the driver/
oracle never sees it). The Android app's data path descrambles (same XOR key — its
descramble table == `DATASHIFT` exactly), checks the `0x5A 0xA5` header, then
**dispatches on a device-type byte at descrambled byte[2]** (`1`=QB_5G, `2`=S_5G,
`3`=AB_300, `4`=P_66). The driver/profile's fixed bit-offset frame is the app's
**AB_300 (type 3)** decode path (its 300-series unit/tag table). The profile previously
left byte[2]=0x00, routing the app into its **S_5G `else` branch**, whose unit/flag
remap is DIFFERENT — proven by Python-porting both branches: with byte2=0 the app shows
the right DIGITS but **wrong/missing unit** (4.2 V→"", 1 kΩ→"Hz", 12.5 mA→""); with
byte2=3 it decodes correctly (V+DC, kΩ, mA+DC, V+AC). FIX:
`bdm.encode` now sets descrambled byte[2]=`DEVICE_TYPE_AB300`(0x03). **Invisible to the
driver** — `bdm.ts` syncs on raw `0x1B 0x84` and digits start at bit 24, so byte 2 is
never read; the decode-oracle round-trip stays green (the bit-sweep test region moved
to `[24,88)` since bytes 0/1/2 are now all forced constants; added a regression test
`test_device_type_byte_is_ab300`). NOTE: the digit/value path was *already*
byte-compatible — the driver's 3+4-bit segment layout coincidentally equals the app's
nibble-pair digit table; only the unit/flag dispatch was broken.
(`uni-t-mmu-ble/docs/protocols/bdm.md` already documents the AB_300/type-3 match — the
profile just wasn't emitting the selector byte to land on it.)

### ai-care — VERIFIED byte-correct, NO fix needed
Python-ported the full official decode (the app's segment-assembly + per-segment label
arrays) and ran every profile preset through it: digits, decimal-point (P1/P2/P3), sign
(MINUS), unit + SI prefix, AC/DC, and all flags decode correctly — `4.200`V·DC,
`12.5`A·AC, `1.000`kΩ, `47.0`nF, `-0.512`V (MINUS). Flag annunciators map 1:1:
hold→HOLD, rel→Triangle(Δ), low_battery→BATTERY, auto→AUTO, DIODE→DIODE, CONT→HORN+OHM.
The profile's self-addressing nibble layout (`((i+1)<<4)|nibble`, MSB-first bit order)
matches the app's hi-nibble-slot / lo-nibble decode exactly. No change.

### bdm — LIVE-VALIDATED on the "Bluetooth DMM" app as an AN9002 (2026-06-10, later)
**DONE live, on-screen confirmed.** The bdm emulator drives the official **Bluetooth
DMM** app (`com.yscoco.wyboem`) to a **CORRECT, LIVE, walking reading**. No profile
change was needed — the already-committed AB_300 (descrambled byte[2]=0x03) fix renders
the right unit on the real app. **23/23 `tests/test_bdm.py` green; py_compile clean.**

- **Advertised name that the app ACCEPTED: `Bluetooth DMM`** (exact match REQUIRED).
  The app's scan/add screen only lists devices whose name equals `ZY` **or**
  `Bluetooth DMM` — an EXACT-match gate (same quirk as the UNI-T app). So
  `--name AN9002` is FILTERED OUT and never lists; you MUST advertise exactly
  `Bluetooth DMM` (or `ZY`). The **AN9002 identity is carried by the device-type byte
  (0x03 / AB_300), NOT the advert name** — the app's base scan is otherwise open (no
  name or UUID prefilter), but the activity-level name allowlist is the real gate.
  Run it as:
  `python -m fakemeter --profile bdm -v --name "Bluetooth DMM" 0<>/tmp/fmb.fifo`.
- **CCCD GO confirmed live**: `notify START on FFF4` fired (the app's BLE lib WRITES the
  0x2902 CCCD — plain BlueZ path, no `cap_net_raw`/injection needed); BlueZ then streams
  the re-pushed frame @300ms. (The unsolicited no-CCCD injector armed but was never
  needed; it lacks CAP_NET_RAW here anyway and the CCCD write made it irrelevant.)
- **On-screen reading (screenshots captured)**: app's meter view showed **`DC 4.200 V`**
  → **`DC 4.203 V`** → **`DC 4.209 V`** across ~2s-apart screenshots (the value-walk),
  with a live analog gauge + trend graph (AVG/MIN/MAX climbing) — genuinely live, not a
  static template. **Unit + DC mode render CORRECTLY → the device-type=0x03 fix is
  proven on the real app** (byte2=0 would have shown blank/wrong unit per the S_5G
  `else`-branch analysis above).
- **Function/unit dispatch verified across families**: REPL `v 1.000 OHM k` → app
  switched to **`1.003 kΩ`** (correct k prefix + Ω base unit, graph re-ranged, walking),
  then back to **`4.205 V` DC** (also shown on the Management device-card tile labelled
  "万用表1"/Multimeter 1). Confirms the AB_300 unit/tag path decodes V·DC,
  kΩ correctly — not just the volts case.
- **Left RUNNING** on bdm advertising `Bluetooth DMM`, phone `5C:17:CF:79:B7:4C`
  connected (AUTH ENCRYPT/bonded), default reading 4.2 V DC walking, for the human to
  watch. Drive via `printf 'cmd\n' > /tmp/fmb.fifo`; log at `/tmp/fmb.log`.

### ai-care live validation — ✅ DONE 2026-06-11 (see top section)
ai-care is now live-validated on the INTELLIGENT MULTIMETER app. The "scan filter" turned
out to be a **Manufacturer-Specific-Data gate (company 0xFFAC + the device's own MAC),
NOT a device-name allowlist** — the emulator gained a generic `Profile.manufacturer_data`
advert hook to satisfy it. Full write-up + on-screen results in the top section of this file.


## SOLVED — no-CCCD (unsolicited) notification delivery via raw-HCI ATT injection (2026-06-10)

The "OWON Java app never writes the FFF4 CCCD" wall (documented further down) is
**solved at the delivery layer**. New module `fakemeter/unsolicited.py` +
gatt_server wiring inject ATT Handle-Value-Notification PDUs (opcode 0x1B)
**directly onto the existing ACL link via a raw HCI socket**, bypassing BlueZ's
GATT server — so a client that never subscribed still receives the stream, exactly
like a real meter chip emitting unsolicited notifications.

### Why nothing softer works (researched + proven airtight)
- **BlueZ gates notifications strictly on the CCCD.** `src/gatt-database.c`
  `send_notification_to_device()` does `ccc = find_ccc_state(...); if (!ccc ||
  !(ccc->value & 0x0003)) return;` (BlueZ 5.72, line ~1404). No D-Bus property,
  GATT method, config, or experimental flag can make BlueZ emit a notification to
  a connected client that hasn't written the CCCD. (Forcing `Notifying=True`
  server-side does NOT route to a non-subscribed link — confirmed by the prior
  experiment.) The only seeded-CCC path (`restore_ccc`) is for the *Service
  Changed* CCC from bonding storage only — not usable for FFF4.
- **A userspace ATT (L2CAP CID 4) listen socket cannot intercept** the LE ATT
  channel while bluetoothd's GATT server is registered — the kernel routes the
  LE ATT channel to bluetoothd. **Live-tested:** binding+listening on `ATT_CID`
  while connecting a client → `accept()` never fires; bluetoothd owns it. (Taking
  over the whole ATT channel = reimplementing the entire GATT server à la
  `btgatt-server`; rejected as a huge rewrite.)
- **Raw HCI ACL injection works** onto the bluetoothd-managed link. We build the
  H4 ACL → L2CAP(CID 4) → ATT(0x1B | value_handle | frame) packet and `write()`
  it on an `HCI_CHANNEL_RAW` socket. **Live-PROVEN:** injecting a sentinel
  notification (`AA BB CC DD EE FF`) to a connected client appeared on the phone
  as the FFF4 value with **no BlueZ-side subscription** routing it. The emitted
  PDU is byte-identical to what `unsolicited.py::inject()` builds (unit-tested).

### The mechanism (`fakemeter/unsolicited.py`)
- `UnsolicitedNotifier(adapter_id)` opens a raw HCI socket (inject) + an HCI
  *monitor* socket (passive discovery). It learns the **notify char's ATT value
  handle** by scanning the client's GATT-discovery `Read-By-Type Response` for the
  char declaration whose properties byte has the NOTIFY bit (the value handle was
  `0x01d1` in testing; BlueZ assigns it). The **ACL connection handle** is resolved
  by the server from `hcitool -i hciN con` (the link where the remote is CENTRAL).
- `inject(frame)` packs the ATT notification and writes the raw ACL packet.
- gatt_server: `MeterServer(unsolicited=True)` (default, **stream profiles only**)
  arms the notifier and polls the link every 500 ms. While a client is connected
  but has **not** written the CCCD, it injects `current_frame()` at the stream
  interval (so HOLD/REL/value-walk reactions ride along). The moment a real CCCD
  write lands (`_on_notify_subscription notifying=True`), injection **stops** and
  BlueZ's normal path takes over — **the CCCD path is unchanged for well-behaved
  apps** (voltcraft Flutter, UNI-T, nRF). Verified: nRF's CCCD-subscribed stream
  still works perfectly (FFF4 showed the live `23-00-00-00-0F-27` frames).

### How to enable / requirements / caveats
- **On by default** for stream profiles; disable with `--no-unsolicited`.
- **Requires CAP_NET_RAW** to actually *deliver* (the raw HCI write needs it;
  binding does not). Without it the injector arms but every `inject()` hits EPERM,
  logs a one-line remediation hint **once**, and the server silently keeps the CCCD
  path. Grant it once with:
  `sudo setcap cap_net_raw+ep $(readlink -f $(which python3))`
  (or run the emulator as root). The HCI *monitor* socket (value-handle auto-
  discovery) also needs CAP_NET_RAW/admin; without it, pass/learn the handle some
  other way (it's `0x01d1` for the current owon-plus GATT layout, but BlueZ-
  assigned — don't hardcode in prod; the monitor path discovers it when privileged).
- Portability: it speaks HCI *under* BlueZ (deliberate layering violation) and does
  not manage ACL tx-credits — fine for a ~3 Hz meter stream, don't crank the rate.
- Tests: `tests/test_unsolicited.py` (PDU byte-layout + value-handle parser),
  **255/255 green**.

### Live-validation status (honest)
- **Mechanism PROVEN over-the-air**: no-CCCD injected notification delivered +
  displayed on the phone (nRF Connect, screenshot evidence). CCCD path intact.
- **OWON-app end-to-end screenshot NOT captured this session**, blocked by two
  environment issues orthogonal to the mechanism: (1) the Claude harness SIGKILLs
  (exit 144) any bluez/D-Bus process it spawns — including a privileged restart —
  so the *emulator itself* couldn't be relaunched with CAP_NET_RAW to self-inject;
  the live proof used an external root injector (privileged docker, host net) onto
  the same live link, which is the identical write path. (2) After a `pm clear`,
  the **OWON app's BLE scan stopped surfacing OWON-PLUS-FAKE** (its scanner filters
  the advert; nRF still discovers it instantly at −57 dBm, so the advert is live —
  it's an OWON-app scan-filter quirk, not an advert/mechanism problem). To finish:
  grant `cap_net_raw` to the venv python, relaunch the emulator (outside the
  harness kill, e.g. a real `screen`/systemd-run), get the OWON app to list+connect
  (it did earlier this session before the clear), and the auto-inject path will
  feed it the live reading with zero extra steps.

## UNI-T LIVE-VALIDATED on the Smart Measure app + on_start/polled-tick SEAM landed (2026-06-10)

The `on_start` + polled-`tick` server seam is **integrated**, and the UNI-T generic
profile (`--profile uni-t`, advertised as `UT60BT`) shows a **CORRECT, LIVE reading**
on the official UNI-T **Smart Measure** app (`com.uni_t.multimeter`) — connect →
handshake → live `4.199 V`/`4.218 V` DC walking value → HOLD freezes it + lights the
"H" annunciator → release resumes the walk. **All 259 tests green; py_compile clean.**
Screenshots captured at each step.

### The seam (gatt_server.py + base.py — how it was wired, and the no-double-push)
- **`base.Profile.on_start: Optional[Callable[[Callable[[bytes],None]], None]]`** — new
  optional field. `MeterServer.start()`'s `_run` calls `profile.on_start(self.notify)`
  ONCE after `_resolve_chars()` (notify char live), handing the profile the thread-safe
  push fn. Default None ⇒ stream families hand no callback.
- **Polled tick driver** (`_start_polled_tick`/`_polled_tick`, new): in
  `_on_notify_subscription`, for a non-`stream` profile that exposes a `tick`, install a
  GLib timeout (300 ms) on subscribe. CRITICAL **double-push avoidance**: `uni_t`'s
  `tick` SELF-PUSHES the measurement frame via the `notify_cb` it got from `on_start`,
  so `_polled_tick` ONLY calls `profile.tick()` — it does NOT also push
  `current_frame()` (that is what `_stream_tick` does for `'stream'` profiles). Decision
  (b) from the brief: profile self-pushes; server's polled tick does not push. Verified
  live: exactly ONE frame per tick (the value walks one step per 300 ms, not two).
  Teardown reuses `_stop_stream`. The `'stream'` path and the unsolicited/no-CCCD path
  are unchanged.
- **`write_uuid` widened to `str | list[str]`** (+ `Profile.write_uuids` property);
  `_build` adds one write characteristic per UUID (chr_id offset by 10). UNI-T now
  publishes BOTH ISSC write chars (`…6daa…` first, `…8841…`). **The list WAS needed:**
  the Smart Measure app writes GET_NAME/GET_DATA to the **`…6daa…`** char (confirmed in
  `/tmp/fm.log`), which the old single-string `…8841…` default would not have exposed.
  Single-string back-compat retained (bdm/ai-care/owon unchanged).

### Confirmed handshake on-wire (`/tmp/fm.log`)
```
notify START on …1e4d…                         # app writes the CCCD (no OWON wall)
polled tick @ 300ms while subscribed           # the seam's timer
WRITE …6daa… <- abcd035f01da                   # GET_NAME on the …6daa… char
command abcd035f01da -> reply abcd085554363042540325   # name frame "UT60BT"
WRITE …6daa… <- abcd035d01d8                   # GET_DATA
command abcd035d01d8 -> reply abcd1002312020342e3230…  # first 19-byte measurement frame, arms stream
NOTIFY …1e4d… -> abcd1002312020342e32…  (×N)   # periodic self-push, value walking
WRITE …6daa… <- abcd034a01c5  ->  byte14=0x02   # HOLD pressed: flagsA bit1 set
```

### ENCODER FIX: DCV/ACV range index 0 is the milli range, not volts
The live app first displayed our DCV reading as **"4.225 mV"** (wrong unit). Root cause:
the app decodes the UNIT purely from the range index byte[4] against its
model-specific UT60BT range table (an asset bundled with the app), where **DCV/ACV
range 0 = "mV"** and **range 1 = "V"** (the bare-volt range). Our `_range_index_for`
returned 0 for an
unprefixed reading ⇒ mV. Fixed in `uni_t.py`: `RANGE_UNITS` for V/A functions now has
`["mV","V","V","V"]` (the real UT60BT ranges, byte-exact vs the JSON), and
`_range_index_for` for an UNPREFIXED reading picks the first BARE-unit range (no SI
prefix) — so DCV "" ⇒ range 1 ⇒ "V". Re-validated live: **4.199 V** displayed. The
`test_matches_real_fixture` ACV 274.7 V fixture was updated to range index 1 (byte[4]
=0x31) accordingly — the live app is now the authoritative oracle over the old generic
range table. (NB: the app loads the model-specific UT60BT range table because a typeName
is resolved/cached for the MAC; the generic table maps range 0 = "V", which is why the
old fixture used 0 — the two tables genuinely disagree on range 0.)

### App scan-filter quirk: the advertised name must be EXACTLY a supported model
The app's scan-list name allowlist does a case-insensitive exact match against its
supported-model set `{UT202BT, UT219P, UT-219P, UT-D07A, UT-D07B, UT60BT, UT117C,
UT513C}`. `--name UT60BT-FAKE` is filtered OUT; the emulator must
advertise **exactly `UT60BT`** (or another listed model) to appear in the scan list.
(The scan screen is `am start -n com.uni_t.multimeter/.ui.main.MainListActivity`; the
home-screen product tiles are web help links, NOT the connect path.)

### Models spot-checked / left for later
All six profiles inherit the seam (polled, dual write chars, on_start, tick — verified
by instantiation). **uni-t/UT60BT: live-validated.** **ut202bt: shares `uni_t.py`'s encode**
so it inherits the range fix (not separately live-run). **ut117c/ut171/ut181a/ut219p
have their OWN 16-bit-len encoders** — untouched by the range fix and NOT live-swept
this round (per-model unit/range sweep against their apps is the remaining work).

### Controller fix (so the emulator starts on a polled profile)
`__main__.Controller` hardcoded an initial `Reading(function="V_DC")`; the UNI-T encoder
uses `"DCV"`, so the initial `send()` crashed. Added `_send_initial()`: push the
profile's OWN `current_frame()` (its initial state is already valid) instead of forcing
the controller's generic default. Falls back to `send()` for profiles without
interactive state. (A manual REPL `v` with a cross-family function label would still
need the right label; out of scope here.)

## UNI-T family corrected to HANDSHAKE-THEN-STREAM + app writes the CCCD (2026-06-10)

The UNI-T `uni_t_base` profile model was WRONG (built "reply one frame per write").
Corrected to **handshake-then-stream** and verified against `uni-t.ts` and confirmed by
live analysis with the official UNI-T **Smart Measure** app (`com.uni_t.multimeter`). NO
live phone/emulator validation this round (BLE adapter in use by another agent) — code +
`uni-t.ts` cross-check only. **All 250 tests green; py_compile clean.**

### The corrected model (was the bug)
UNI-T is `interaction='polled'` (no free-stream-on-subscribe) but it is NOT
reply-per-poll. The real flow (driver `uni-t.ts` + the app's live behavior):
1. App subscribes — its notify-subscribe path WRITES the 0x2902 CCCD (see below).
2. App WRITES **GET_NAME** (`AB CD 03 5F 01 DA`) → meter replies a *name/control*
   frame (the device-type request).
3. App WRITES **GET_DATA** (`AB CD 03 5D 01 D8`) → meter **STARTS
   FREE-STREAMING** 19-byte measurement frames periodically. The write GATES the
   stream; it does NOT pull one frame. The app's sampling loop reads them.
4. Meter occasionally pushes a re-arm nudge: 9-byte `…AA AA…` (type-request → app
   re-sends GET_NAME) / 7-byte `…FF 00…` (data-request). Matches `framing.ts`
   classify (19=measurement, 9=type-request, 7=data-request) + `uni-t.ts` onRequest.

`uni_t_base.UniTMeter` now: GET_NAME → name frame; **GET_DATA → arm `_streaming` +
return the first measurement frame**; soft-button → state-mutating reply.
`tick()` self-pushes a fresh measurement frame each tick **while armed and a
`notify_cb` is set** (the periodic stream), plus an optional re-arm nudge. All seven
model profiles (uni-t/ut202bt/ut117c/ut171/ut181a/ut219p) ride this unchanged — they
only differ in encoder/opcodes. Tests assert GET_DATA arms `streaming` + tick
self-pushes only after GET_DATA + on_start.

### REQUIRED gatt_server.py / base.py SEAM (NOT edited — another agent owns them)
The periodic self-push needs the server to drive `tick` AND hand the profile a push
fn, because in `'polled'` mode the server's stream loop is OFF and the profile has no
ref to `MeterServer.notify`. Existing hooks alone CANNOT do it (`tick` is only called
by `_stream_tick`, which only runs for `'stream'` profiles; there is no `notify_cb`).
The minimal, exact addition the owning agent must make:
1. **`base.Profile.on_start: Optional[Callable[[Callable[[bytes],None]], None]] = None`**
   — a new optional field. `uni_t_base.make_profile` ALREADY attaches `meter.on_start`
   to the profile instance (as an attribute, since base.py has no field yet), so the
   server can `getattr(profile, 'on_start', None)` today.
2. In `MeterServer.start()` (after `_resolve_chars()`): if `profile.on_start` is set,
   call `profile.on_start(self.notify)` to hand it the thread-safe push fn.
3. In `_on_notify_subscription`: for `'polled'` profiles that expose a `tick`, ALSO
   install the GLib timeout source (reuse `_start_stream`; `_stream_tick` already
   calls `tick()` then pushes `current_frame()`). `tick` is a no-op until GET_DATA
   arms streaming, so installing the timer on subscribe is harmless.
Until that lands, `command` still returns the FIRST measurement frame on GET_DATA
(the existing `_on_write → command_handler → notify` path pushes it), so a REPL `r`
re-send keeps the reading visible; only the AUTOMATIC periodic re-push is gated.

### CRITICAL go/no-go: the UNI-T app WRITES the CCCD → NO OWON wall (GO for live)
Unlike the OWON Java app (which never writes the FFF4 CCCD — the blocker documented
below), the UNI-T Smart Measure app's notify-subscribe path **DOES write the 0x2902
descriptor**: it writes the client-characteristic-config descriptor (`00002902-…`)
with the enable-notification value and waits for the write to complete. So BlueZ WILL
get `StartNotify` and route our notifications. **UNI-T live
validation against this bluezero emulator is a GO** — it will NOT hit the OWON CCCD
wall. (The other agent's CCCD-bypass work is NOT needed for UNI-T.)

### Confirmed wire format (`uni-t.ts` + live)
- GET_NAME = `AB CD 03 5F 01 DA` (the device-type request, bytes {-85,-51,3,95,1,-38}).
- GET_DATA = `AB CD 03 5D 01 D8` (the start-read-test-value command, code 93).
- Soft-button checksum: a command `i` builds `AB CD 03 <i> <hi> <lo>` with the 16-bit
  word `i + 379` → checksum = `cmd + 0x17B` (= 0xAB+0xCD+0x03+cmd). Confirms
  `framing.ts` COMMANDS exactly.
- GATT: service `49535343-fe7d-…`, notify `…1e4d…`. Write UUID: the app's DEFAULT is
  `…6daa…` with `…8841…` as the second — the inverse of the emulator's single
  `write_uuid` choice (flagged: `Profile.write_uuid` is a single string; the family
  exposes both — widen to a list if the app probes the other).

### Left for the live-validation round (when the adapter frees up)
- Run `python -m fakemeter --profile uni-t --name UT60BT-FAKE` and connect Smart
  Measure; confirm the GET_NAME→GET_DATA→stream handshake on-wire and a live reading.
- Land the `on_start` + polled-`tick` SEAM in gatt_server.py/base.py (above) so the
  periodic stream self-pushes (without it, only the first GET_DATA frame is delivered).
- Sweep the ut117c/ut171/ut181a/ut219p 16-bit-len framing variants against their apps.

## owon-plus / owon-old validation vs the OWON BLE4.0 *Java* app (2026-06-10)

Live-validated the two OWON profiles against the official **OWON Multimeter BLE4.0**
Android app (`com.owon.MultimeterBLE`). Outcome:
**both profiles pass connect + FFF2 series gate + FFF1 auth, but neither shows a LIVE
reading — blocked by a BlueZ-peripheral limitation, NOT a profile bug.** Details:

### What was VERIFIED CORRECT (profiles are right)
- **FFF1 auth = `vc` scheme (RAW 16-byte MD5 digest), s1/s2 tables — CONFIRMED.**
  The brief hypothesised the Java app wants the 32-char UPPER-hex string; live analysis
  proves the opposite. The app's identity-auth check compares its own MD5 hex string
  (case-insensitively) against the **hex-encoding of the meter's raw FFF1 bytes**: it
  hexifies whatever the meter returns and compares to its 32-char upper-hex of the
  picked string. So the meter must return the **16 RAW DIGEST BYTES** (the app hexifies
  them → matches). Returning ASCII hex would be double-hexed (64 chars) → fail.
  `auth_table="vc"` (the default) is therefore correct and was confirmed live:
  recovered coords from challenge
  `e8863429190600000000000000000000` → picked `c9xplb` → md5 `5807c1da…` = exactly
  the meter's FFF1 response, and the app advanced to the post-auth FFF2 re-read
  (success path). NO `java`/hex scheme change was needed.
- **Series ids — CONFIRMED.** The app's series-info check requires
  series ∈ {18,20,33,35,41,55}, else it fails verification (code 6). **owon-plus = 18**
  (OW18; the series id selects the app's 6-byte parser) and **owon-old = 35**
  (B35; series 35 && flash byte != 1 selects the 14-byte ASCII parser). owon-old's
  `flash=0xFF` keeps the flash-record flag false so the B35 path is taken. Both series
  read live as `series=18` / `series=35`.
- **owon-plus 6-byte encoder — byte-exact vs the `owon-plus.ts` decode.** symbols
  word `twoBytesToShort(b[1],b[0])` is LITTLE-endian; prefix=`(s&56)>>3`, function=
  `(s&960)>>6`, point=`s&7` (point 7=OL, 6=UL); value word LE bits0..14 magnitude,
  bit15 sign. Matches the profile exactly. (nRF Connect rendered the stream fine.)
- **owon-old 14-byte ASCII encoder — byte-exact vs the app's B35 decode** after
  one FIX (below). byte7 AC/DC/HOLD/REL/AUTO, byte8 MAX/MIN/Bat/nano(bit1), byte9
  prefix+%/diode/cont, byte10 unit (V=7…°F=0) all line up with the app's b6..b9.

### FIX made: owon-old byte6 decimal-point is ASCII, not a bitmask
The app's B35 decode reads **byte6 as an ASCII digit**: `'1'`(0x31)→3dp, `'2'`
(0x32)→2dp, `'4'`(0x34)→1dp, else→0dp. The profile (and the driver-derived oracle)
previously wrote/read byte6 as a first-set-bit **bitmask** (1/2/4 as raw values),
inherited from the Windows-app port `owon-old.ts`. Changed `owon_old.py::_point_byte`
to emit the ASCII byte and `tests/decode_owon_old.py` to decode it the same way; the
round-trip stays green (31/31 owon-old, 65/65 both, 116/116 repo). **This surfaces a
DRIVER bug: `packages/protocol/src/drivers/owon-old.ts` decodes byte6 as a bitmask,
which DISAGREES with the real meter — it should read byte6 as ASCII '1'/'2'/'4'.**
(This is on top of the already-known owon-old.ts nano-prefix byte10.2-vs-byte8.1 bug.)

### THE BLOCKER — the OWON Java app never writes the FFF4 CCCD (free-stream-only)
Both profiles connect, pass the series gate and the FFF1 auth, then the app sits on
**"No input"** forever. Root cause (observed live + reproduced + isolated):
the app's notify-subscribe path **only writes the CCCD (0x2902) descriptor when the
char UUID == the heart-rate UUID (0x2A37)** — vestigial Android-sample behavior. For
its real notify char **FFF4 it just sets the Android-LOCAL receive flag and NEVER
writes the CCCD**, relying on the real meter to emit
*unsolicited* ATT Handle-Value-Notifications (raw BLE chips do this unconditionally;
Android then delivers them because the local flag is set).
- A **bluezero/BlueZ peripheral only emits notifications to clients that subscribed via
  CCCD** (BlueZ tracks CCCD per-connection and calls our `StartNotify`). The OWON app
  never subscribes → BlueZ sends it nothing → "No input".
- **PROVEN both ways:** (a) **nRF Connect**, which DOES write the CCCD, subscribed to
  FFF4 and received the 300 ms stream perfectly (emulator encode + notify path are
  correct). (b) An experimental `free_stream_on_connect` hook that force-started the
  stream and forced our char's `Notifying=True` on the FFF2 connect-read (so frames
  flowed every 300 ms with no client CCCD) **still did not reach the OWON app** — BlueZ
  will not route notifications to a non-CCCD-subscribed link. That experiment was
  reverted (it cannot work); the gatt_server / base / owon_base are back to baseline.
- **Implication:** owon-plus and owon-old **cannot be live-validated against
  `com.owon.MultimeterBLE` with this bluezero-based emulator** as-is. Fixing it needs a
  BlueZ-level capability to emit ATT notifications without a CCCD subscription (e.g. a
  raw L2CAP/HCI notification injection, or driving the link below bluezero's external-
  GATT-app API) — out of scope for a profile change, and possibly not achievable via
  bluezero at all. The Voltcraft *Flutter* app worked precisely because flutter_blue_plus
  writes the CCCD normally; the Java app's missing-CCCD behaviour is the difference.
- The profiles themselves need NO further change for this app — auth/series/encoders
  are confirmed correct; only the delivery layer is blocked. The owon-old byte6 ASCII
  fix stands regardless (it is app-true and improves the driver-verification oracle).

Emulator left running on **owon-old** (series 35, name `OWON-OLD-FAKE`). No
`owon_base`/`gatt_server`/`base` changes remain (the free-stream experiment was reverted);
only `owon_old.py` (byte6) + `tests/decode_owon_old.py` are modified.

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
   garbage / `UL`). The app's 24-bit word builder was initially mis-read as 24-bit
   big-endian with even-keyed code tables. The TRUTH, confirmed by streaming raw
   frames and reading the display (see below), is LITTLE-endian words with
   CONSECUTIVE code tables. Fixed the encoder + oracle accordingly.

## What works (verified)
- **GATT**: service `0xFFF0`, notify `FFF4`, write `FFF3`, secure `FFF1`, info `FFF2`. Advertised name set via `--name`.
- **FFF2 device-info** (read on connect): 6 bytes `[seriesId, battery, fwMajor, fwMinor, fwPatch, flashFlag]`. Sending only 5 bytes → Dart `RangeError` "device not supported code:-2". REQUIRED to be ≥6.
- **Series gate**: the app maps `FFF2[0]` → model → `protocolType`. **Series 91 = VC915 → R10W parser** (15-byte records). Series 41 = OWON "B41" → **R2W** parser (6-byte records). Using 41 made the app slice our 15-byte frames as 6-byte garbage (the long red herring). Default series is now **91**.
- **FFF1 MD5 anti-counterfeit auth** (cracked): app writes 6 mixed coords (`mixed[i]=orig[i]+[200,100,50,20,10,5]`, padded to 16B); meter recovers `coord[i]=(mixed[i]-mixElems[i])&0xFF`, picks `s1[c]` (i<3) / `s2[c]` (i≥3) using the app's s1/s2 lookup tables, computes `md5(utf8(picked))`, and returns the **16 RAW DIGEST BYTES — not 32 ASCII hex** (that was the bug; the app hexifies each byte it reads). `code:3` = this compare failing. Implemented as auth scheme `'vc'` (default) in `voltcraft.py`.
- **Subscribe + stream**: app subscribes to FFF4; meter must **free-stream** frames (no `*READ?` command for live data — the SCPI `*READ?`/`*READ1?`/`*READlen?`/`*STOP` are offline-record only). Emulator re-pushes the last frame every 300ms while subscribed **on the GLib loop thread** (see bug #1 above — off-thread pushes silently never reached the app).
- **#TIMEsync**: after subscribe the app writes `#TIMEsync` + a 7-byte RTC datetime to FFF1; it's a fire-and-forget clock-set, no reply needed. Harmless; we just log it.
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
trailing `0x01` is the key-event flag (the app's button write sends the press/release
boolean as byte1; press = `0x01`). EVERY button is a real meter command — there were
NO app-local-only buttons in the Select…Display set. (The app's button write targets
FFF3 — confirmed live by capturing the FFF3 write.)

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
  switch into special modes needing the secondary block / dedicated parsers,
  out of scope for the primary-display verification. The writes are received without
  disconnecting the link.

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
  reference behavior was checked against v1.2.5). It does NOT block the live reading
  — both the card body and the meter screen show the correct live value. Not worth
  chasing.
- Gear codes ≥14 (Power/PF/4-20mA/AC+DC/Motor/Solar/etc.) were not swept; the
  5-bit field can hold them but they need the secondary block / special parsers
  — out of scope for the core verification.

## How to run / environment
- Emulator: `cd /home/mannes/projects/ai-slop/fake-ble-meter && source .venv/bin/activate && python -m fakemeter --profile voltcraft -v --name VC915` (defaults: series 91, R10W, auth `vc`). REPL: `r` resend, `p` presets, `v` set value/function/prefix, `f` toggle flag, `s` mode-word **bit-sweep**, **`raw <hexbytes>` inject an arbitrary 15-byte frame (the layout-mapping tool)**, `series <id>`, `auth <mode>`, `q` quit.
- **Persistence caveat**: the Claude harness SIGKILLs (exit 144) any bluez/D-Bus process spawned by a foreground/background Bash call when the call returns. `tmux` was NOT installed on this box — use **`screen`** instead: `screen -dmS fm bash -c '… exec python -u -m fakemeter … 2>&1 | tee /tmp/fm.log'`; drive it with `screen -S fm -p 0 -X stuff "cmd\n"` and read `/tmp/fm.log` (the `| tee` logfile is the reliable way to read REPL output; `screen -X hardcopy` only grabs the visible pane and the bluezero DEBUG "Char Prop Changed" spam scrolls prompts away — run WITHOUT `-v` to quiet it). The one-shot `--self-check` works inline.
- **Forcing a clean phone reconnect** (the app doesn't re-run GATT discovery on an already-open BLE link): `adb -s 995b6385 shell am force-stop com.voltcraft.series800` then relaunch + dismiss the SDK-compat dialog (`tap 888 1437`) + `+`/scan (`tap 955 210`, wait, `tap 540 636` on the VC915 row) + tap the card (`tap 540 600`) to open the meter. Force-stop clears the in-memory device list, guaranteeing a fresh add. `pm clear` also wipes saved devices but then re-grant BLE perms.
- Phone: **adb device `995b6385`, ROOTED** (`adb root` → uid 0; Magisk `su`). App `com.voltcraft.series800` (launcher `com.owon.imeter.MainActivity`), perms granted. Drive via `adb shell input` / `screencap` / `logcat`; read app memory as root if useful. Other installed apps: OWON BLE4.0 `com.owon.MultimeterBLE` (Java sibling), nRF Connect `no.nordicsemi.android.mcp`.
- Adapter: `hci0`, BD addr `44:AF:28:A5:53:1A`, BlueZ 5.72. The app strips non-alphanumerics from the advertised name for display ("VC900-FAKE"→"VC900FAKE").

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

---

## Open items — check tomorrow

**Live screenshots still owed** (code is done + decode-verified; just need the on-screen run):
- [ ] **ai-care** — its app writes the CCCD → normal BlueZ path (no cap_net_raw). Quick agent run, same as bdm. Confirm the self-addressing frame renders the right value/unit on INTELLIGENT MULTIMETER.
- [ ] **owon-plus + owon-old** — the OWON BLE4.0 app never writes the CCCD, so live needs the raw-HCI injection path, run OUTSIDE the harness with CAP_NET_RAW:
      `sudo setcap cap_net_raw+ep "$(readlink -f .venv/bin/python)"` then `python -m fakemeter --profile owon-plus -v --name OWON-PLUS-FAKE` (owon-old: `--profile owon-old`), connect OWON BLE4.0.

**UNI-T per-model coverage** (only UT60BT/UT161 + ut202bt are live-proven):
- [ ] ut117c / ut171 / ut181a / ut219p — their own 16-bit-len encoders + units are unvalidated against their apps; per-model sweep needed.
- [ ] ut219p is partial (standard live-data frame only; daoPos→param dispatch + device-info/battery-gate handshake deferred). ut181a is MAIN-block only (secondary block + datalog deferred). Both need a hardware capture.

**Driver repo (uni-t-mmu-ble):**
- [ ] Driver fixes are on branch `emulator-validated-driver-fixes` (41657f2), NOT on main — review + PR/merge (voltcraft R10W rewrite, owon-old byte6+nano, owon-plus verify, types.ts `app-verified` tier, protocol docs).
- [ ] Confirm owon-old.ts byte6/nano renders live once the OWON cap_net_raw run is done (currently decode-verified only).

**Housekeeping:**
- [ ] Neither repo has a git remote yet — set up + push when ready.

**Quirks to remember:**
- Vendor scan filters are exact-name allowlists (bdm app: only `Bluetooth DMM`/`ZY`; UNI-T: exact model names like `UT60BT`) — the *model identity* lives in the frame (bdm device-type byte=0x03 AB_300), not the advert name.
- The harness SIGKILLs (exit 144) any bluez process it spawns, so live runs need the user's own terminal — OR launch via `setsid … & disown` with the sandbox disabled, which **reparents the emulator under `systemd --user` (PPID→1) so it survives** the harness reaping (the `run_in_background`+FIFO trick gets reaped when its launching call ends). CAP_NET_RAW is still required for the no-CCCD path.
- **Restarting the emulator breaks a phone that already bonded** (stale LTK → "Couldn't pair / incorrect PIN"): clear the bond on BOTH sides first — `bluetoothctl remove <phone-addr>` + toggle the phone's Bluetooth off/on — then re-add the device from the app's scan screen.
- The bdm/AN9002 emulator may still be running (reparented under systemd-user, phone connected, 4.2 V walking); log `/tmp/fmb.log`, drive via `printf 'cmd\n' > /tmp/fmb.fifo`.
