# Voltcraft VC915/VC925 — real BLE measurement-frame protocol (R10W)

> Reverse-engineered statically from the official Voltcraft "series800" app
> (`com.voltcraft.series800`, in-app package `com.owon.imeter`, v1.2.5, Dart 3.9.2)
> via a blutter ARM64 dump at `/tmp/vc125-out`. Companion to
> [`owon-voltcraft-handshake.md`](./owon-voltcraft-handshake.md) (the FFF2 device-info
> gate and FFF1 MD5 auth). This documents the **measurement frame the meter sends on
> FFF4**, which the prior `voltcraft.ts` driver decodes **wrongly** (it ports a
> third-party Windows-app format). The emulator now emits the format below.

## TL;DR

- **The VC915 is series 91, NOT 41.** The app maps the FFF2 series id through
  `owonMultimeterModels` to a model + a `protocolType`:
  - `protocolType 0` → **R2W** parser (6-byte records, 16-bit big-endian fields).
    Series 18/20 (OW18B/E), 33/35/41 (OWON "B33/B35/B41"), 21 (CM2100).
  - `protocolType 1` → **R10W** parser (15-byte records, 24-bit big-endian fields).
    Series **91 (VC915)**, 92 (VC925), 83/85/87/89 (VC831/851/871/891),
    61/65/67/69, 101.

  So a real **VC915 reports series 91 and streams R10W 15-byte frames.** Reporting
  41 (what the emulator did) made the app treat us as an OWON **B41** expecting the
  **R2W 6-byte** format, so our 15-byte frame was parsed as garbage → the app showed
  "disconnected / no readings". **Fix: report series 91 and send R10W frames.**
  ("R10W"/"R2W" = the record width the dispatch loop slices: R10W loops in 15-byte
  (`0x0f`) chunks, R2W in 6-byte chunks.)

- **No command handshake for live data.** Realtime measurements **free-stream** on
  FFF4 the moment the app subscribes; `ble_multimeter.parseRealtimeData` is the
  notification handler. The SCPI strings (`*READ?`, `*READ1?`, `*READlen?`, `*STOP`)
  exist **only** for OFFLINE flash-record download (`createReadOfflineRecordParams`
  / `createStopRecordParams` / `createReadRecordLengthParams`), not for streaming.
  The emulator must NOT wait for or require any FFF3 write.

- **No marker bytes, no checksum.** One notification == one 15-byte frame.

## R10W frame — 15 bytes, big-endian, five 24-bit words

`R10wProtocolParse.parseRealTimeDataOnce` slices the 15-byte record into five
3-byte groups and builds each value with `create24bits(g) = g[0]<<16 | g[1]<<8 | g[2]`
(big-endian; the helper returns 0 unless the slice is exactly 6 long — it is fed
`sublist`s, only the first 3 bytes of each matter):

| Bytes  | Word | Meaning |
| ------ | ---- | ------- |
| 0..2   | gear word (primary)  | function / SI-prefix / decimal-point / display flags |
| 3..5   | value word (primary) | count magnitude + over-range selector + sign |
| 6..8   | gear word (secondary)  | parsed only if primary gear **bit 12** is set |
| 9..11  | value word (secondary) | parsed only if primary gear **bit 12** is set |
| 12..14 | state word           | annunciator **bitmask** (`getAllStateTypeByCode`) |

### Gear / symbols word (bytes 0..2) — `parseGearAndCountingUnit`

24-bit BE word `g`:

| Field | Bits | Extract | Maps via |
| ----- | ---- | ------- | -------- |
| decimal-point code | 0..2 | `g & 7` | `_decimalPointPositionMap` |
| counting-unit (SI prefix) code | 3..5 | `(g>>3) & 7` | `_countingUnitMap` |
| gear / function code | 6..10 | `(g>>6) & 0x1f` | `_gearUnitMap` |
| "extra digit" flag | 11 | `(g>>11) & 1` | carried to `GearsConfig` (UI hint) |
| **secondary-display active** | 12 | `(g>>12) & 1` | gates parsing of bytes 6..11 |

**All three lookup maps are keyed by the EVEN code value** (the Dart maps are
literally keyed `0,2,4,6,…`; the parser uses the field value as a direct map key
with no transform, and throws `NullCastError` on an absent key). So the meter must
emit even codes.

`_gearUnitMap` (code → gear, AC/DC, base unit):

| code | gear | code | gear | code | gear | code | gear |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 0 | DC V | 16 | TEMP ℃ | 32 | Power Factor | 48 | Motor |
| 2 | AC V | 18 | TEMP ℉ | 34 | 4~20mA % | 50 | Solar W/m² |
| 4 | DC A | 20 | DIODE V | 36 | Power Ah | 52 | Angle ° |
| 6 | AC A | 22 | CONT Ω | 38 | Time | 54 | Compass ° |
| 8 | RES Ω | 24 | hFE | 40 | Power Wh | 56 | DC_HV V |
| 10 | CAP F | 26 | NCV | 42 | Power V | 58 | AC_HV V |
| 12 | Hz | 28 | Power W | 44 | Power A |  |  |
| 14 | DUTY % | 30 | Power VA | 46 | AC+DC V |  |  |

> **Reachability caveat.** The gear field is 5 bits (`&0x1f`, 0..31) → only codes
> 0..30 (DC V … Power VA) are expressible in R10W. Codes 32..58 need a 6th bit and
> are unreachable through this field (listed for completeness; some may be carried
> via the secondary display or a model-specific path we did not need).

`_countingUnitMap` (SI-prefix code → prefix, multiplier — **3-bit field reaches only
keys 0/2/4/6**):

| code | 0 | 2 | 4 | 6 | 8 | 10 | 12 | 14 |
| ---- | - | - | - | - | - | -- | -- | -- |
| prefix | p | n | µ(`u`) | m | (none) | k | M | G |

> **Prefix caveat.** Because the counting-unit field is only 3 bits, R10W can send
> **only p / n / µ / m**. The unprefixed unit and k/M/G are not expressible. This is
> cosmetic: the displayed **number** comes from the decimal multiplier (below), NOT
> from this counting-unit multiplier, so the value is correct whatever prefix code is
> sent — only the unit *label* changes.

`_decimalPointPositionMap` (decimal code → format, multiplier — **3-bit field reaches
only 0/2/4/6**):

| code | format | mult | decimals |  | code | format | mult | decimals |
| ---- | ------ | ---- | -------- |--| ---- | ------ | ---- | -------- |
| 0 | `000000` | 1 | 0 |  | 8 | `00.0000` | 1e-4 | 4 *(unreachable)* |
| 2 | `00000.0` | 0.1 | 1 |  | 10 | `0.00000` | 1e-5 | 5 *(unreachable)* |
| 4 | `0000.00` | 0.01 | 2 |  | 12 | `UL` | — | *(R2W/legacy)* |
| 6 | `000.000` | 0.001 | 3 |  | 14 | `OL` | — | *(R2W/legacy)* |

### Value word (bytes 3..5) — `parseMeasureValue`

24-bit BE word `v`:

| Field | Bits | Extract |
| ----- | ---- | ------- |
| count magnitude | 0..18 | `v & 0x7FFFF` (0..524287) |
| over-range selector | 20..22 | `(v>>20) & 7` |
| **sign** | 23 (== bit 7 of byte 3) | `byte3 >> 7` → 1 = negative |

`displayed value = count × decimalPosition.multiplier`, then negate if sign set.
(For NCV/Motor gears the value goes through `transformNCVValue`/`transformMotorValue`
instead — not needed for the common gears.)

**Over-range selector** (this is how R10W signals OL/UL — NOT via the decimal field):

| selector | display |
| -------- | ------- |
| 0 | normal numeric |
| 1 | `OL` (over-load) |
| 2 | `UL` (under-load) |
| 3 | `HI` |
| 4/5 | OL/UL combos |

### State word (bytes 12..14) — `getAllStateTypeByCode`

24-bit BE word, a **bitmask**: every annunciator whose bit is set lights up.

| bit | annunciator | bit | annunciator | bit | annunciator |
| --- | ----------- | --- | ----------- | --- | ----------- |
| 1 | HOLD | 7 | AVG | 14 | AC |
| 2 | REL | 8 | RMR | 15 | DC |
| 3 | AUTO | 9 | Loz | 16 | USB |
| 4 | Bat (low-batt) | 10 | LPF | 17 | Err |
| 5 | MIN | 11 | Peak | 18 | INRUSH |
| 6 | MAX | 13 | Cosφ | 19 | OSC |

(bits 0, 12, 20+ are unused → useful "blank" steps in the bit-sweep.)

## Worked examples (exact bytes for live testing)

These come straight out of the emulator's `encode()` (verified by round-tripping
through a Python port of the R10W parser, `tests/decode_voltcraft.py`):

| Reading | Frame (hex) | Notes |
| ------- | ----------- | ----- |
| **4.200 V DC** | `00 00 26 00 10 68 00 00 00 00 00 00 00 00 00` | gear 0(DC V), cu 4(µ¹), dp 6(3 dec); count 0x1068=4200 → 4.200 |
| **4.200 mV DC** | `00 00 36 00 10 68 00 00 00 00 00 00 00 00 00` | cu 6(m) → clean "mV" label |
| **1.000 (M)Ω** | `00 02 26 00 03 e8 00 00 00 00 00 00 00 00 00` | gear 8(RES), count 0x3e8=1000, 3 dec → 1.000 |
| **OL (V AC)** | `00 00 a0 10 00 00 00 00 00 00 00 00 00 00 00` | gear 2(AC V); value selector=1 (bits20-22) → "OL" |
| **UL (Ω)** | `00 02 00 20 00 00 00 00 00 00 00 00 00 00 00` | gear 8(RES); selector=2 → "UL" |
| **−0.512 V DC** | `00 00 06 80 02 00 00 00 00 00 00 00 00 00 00` | dp 6; count 0x200=512; byte3 bit7=1 → −0.512 |
| **12.30 µA DC** | `00 01 24 00 04 ce 00 00 00 00 00 00 00 00 00` | gear 4(DC A), cu 4(µ), dp 4(2 dec); count 0x4ce=1230 |
| **4.200 V + HOLD** | `00 00 26 00 10 68 00 00 00 00 00 00 00 00 02` | state word bit 1 set → HOLD |

¹ The unprefixed unit isn't expressible (3-bit prefix field), so "4.200 V DC" shows a
`µ` prefix on the *label*; the number 4.200 and the DC-V gear are correct. Use a `m`
or `µ` prefix for a clean prefixed label.

## Why the old 15-byte frame was rejected

Three independent reasons, any one fatal:

1. **Wrong parser selected.** Reporting series 41 → OWON "B41" → `protocolType 0` →
   **R2W** parser, which slices the stream in **6-byte** records. Our 15-byte frame
   was chopped into 2½ bogus 6-byte records.
2. **Wrong field width & endianness.** Even under R10W, the old frame used **16-bit
   little-endian** fields with **0xF0 marker bytes** at offsets 2 and 8. The real
   protocol is **24-bit big-endian**, no markers — so byte 2 (`0xF0`) landed in the
   middle of the gear word and corrupted gear/prefix/decimal.
3. **Wrong OL/sign/flag encoding.** OL was encoded in a (nonexistent) decimal-point
   sentinel; the real OL lives in value-word bits 20..22. Sign was byte 5 bit 7; the
   real sign is byte 3 bit 7. Flags were a 16-bit LE word at bytes 12..13; the real
   state is a 24-bit BE bitmask at bytes 12..14.

## `voltcraft.ts` (driver) vs. reality

`packages/protocol/src/drivers/voltcraft.ts` decodes a **different protocol
generation** (FireBird3314's annotations of a third-party Windows app), which does
not match the real VC915. Side-by-side:

| Aspect | `voltcraft.ts` (driver) | Real VC915 (R10W) |
| ------ | ----------------------- | ----------------- |
| Record length | 15 bytes (`>14`) | 15 bytes ✓ (coincidentally same) |
| Marker bytes | `0xF0` at bytes 2 **and** 8 | **none** |
| Field endianness | 16-bit **little-endian** | 24-bit **big-endian** |
| Primary value | bytes 3..4 LE, 0..65535 | bytes 3..5 BE, count `&0x7FFFF` (19-bit) |
| Sign | byte 5 bit 7 | byte 3 bit 7 (== value-word bit 23) |
| Decimal point | gear bits 0..2 (0..4 places) | gear bits 0..2 (0/1/2/3 places only) |
| SI prefix | gear bits 3..5, table `p n µ m _ k M G` | gear bits 3..5, but **only p/n/µ/m reachable** |
| Function code | gear bits **6..10 (5-bit)**, own 0..22 table | gear bits 6..10 (5-bit), `_gearUnitMap` even codes 0..30 |
| Function table | DC/AC folded into separate codes 0..22 | gear + separate AC/DC tag; codes 0..58 (even) |
| OL / UL | decimal-point sentinel 7 / 6 | **value-word bits 20..22** selector (1=OL, 2=UL, 3=HI) |
| Secondary display | bytes 6..11, second `0xF0` marker | bytes 6..11, gated by gear **bit 12** |
| Flags word | bytes 12..13 **LE**, 6 flags, **MSB-first** (bug) | bytes 12..14 **BE**, 18-flag **bitmask**, LSB-indexed |
| Flag bit order | `hold=bit15…max=bit10` (MSB-first) | `HOLD=bit1, REL=2, AUTO=3, Bat=4, MIN=5, MAX=6, …` |
| Checksum | none | none ✓ |
| Handshake | none (free-stream) ✓ | none (free-stream) ✓ |

The driver's **flag-bit order is wrong twice over**: wrong byte width (16 vs 24-bit),
wrong endianness (LE vs BE), and wrong direction (it reads MSB-first; the real map is
an LSB-indexed bitmask starting at **bit 1**, not bit 0 — `HOLD=0x2`). The bit-sweep
preset (`bitsweep`) walks state-word bits 0..23 so the phone confirms each mapping.

> **Scope note:** this doc does NOT change `voltcraft.ts` — fixing the driver is a
> separate decision (it may need a full R10W rewrite, since the formats are
> structurally different, not just bit-shuffled).

## Confidence & gaps

- **High confidence:** parser dispatch (series→protocolType→R10W/R2W), 15-byte record
  length, the five 24-bit BE words and their byte offsets, gear/cu/dp bit positions
  and their lookup tables, the value count mask + sign bit + over-range selector, the
  state-word bitmask and its bit→annunciator map, and "no command handshake for
  streaming". All traced in the ARM disassembly and cross-checked by round-tripping
  the encoder through a parser port.
- **Medium confidence:** that a real **VC915 reports series 91** specifically (the
  app maps 91→"VC915"→R10W, and the device is a Voltcraft VC-series, so 91 is the
  best candidate). If the physical meter reports a different R10W id (92/83/85/87/89),
  set it via the `series` REPL command — any R10W id selects the same parser.
- **Open question (cosmetic):** because R10W's SI-prefix field reaches only p/n/µ/m,
  how a real VC915 displays a *plain* "V"/"Ω" or "kΩ"/"MΩ" range is unresolved
  (the number is always correct; only the prefix label is constrained). A live
  capture would settle it; it does not block the app showing live readings or the
  flag bit-sweep, which were the goals.
- **Not implemented:** secondary-display content, NCV/Motor `transform*` value
  remaps, and the gear codes 32..58 that need a wider gear field. None are needed for
  the worked examples or the bit-sweep.
