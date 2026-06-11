# OWON / Voltcraft BLE connect handshake (FFF2 series gate + FFF1 MD5 auth)

> Once connected, the meter **free-streams measurement frames on FFF4** with no
> command handshake. The frame format the VC915 sends (R10W, 15-byte big-endian) is
> documented separately in
> [`voltcraft-measurement-protocol.md`](./voltcraft-measurement-protocol.md), which
> also explains why the FFF2 series id must be **91 (VC915)**, not 41, to select the
> right parser.

This documents the connect-time handshake the OWON-family apps run, derived from the
`owon-plus.ts`/`voltcraft.ts` drivers and confirmed by live analysis with the
Voltcraft and OWON apps (`com.voltcraft.series800`, in-app pkg `com.owon.imeter`),
so future profiles (and the `voltcraft` profile) can pass it.

## GATT layout (service 0xFFF0)

| UUID   | role                | direction (app POV) | meaning                                   |
|--------|---------------------|---------------------|-------------------------------------------|
| FFF4   | notify              | meter → app         | measurement frames                        |
| FFF3   | write               | app → meter         | commands                                  |
| FFF2   | read                | meter → app         | 6-byte device info (series id / fw / …)   |
| FFF1   | write + read        | both                | MD5 anti-counterfeit challenge/response   |

## Connect sequence

1. The app reads **FFF2** (device info) and runs the series gate.
2. The app runs the **FFF1 MD5 challenge/response**.
3. Only if the FFF1 verify passes does the app proceed (activate, RTC sync, …) and
   show live readings. Any failure produces the on-screen error
   `"The device is not supported! code:N"` and disconnects.

### `code:N` map (observed on the app's connect path)

- `code:0`, `code:1` — FFF2 parse/shape failures.
- `code:-2` — FFF2 returned **fewer than 6 bytes** (byte 5 is read).
- `code:2 seriesId:<n>` — FFF2 series id `<n>` is **not a supported model**.
- `code:3` — **FFF1 MD5 mismatch** (the FFF1 verify failed). This is the auth gate.

## FFF2 — device info (6 bytes)

```
byte0 = SERIES ID        -> mapped to a model. Supported groups: B33 (33),
                            B35/B41 (35 & 41), C91. Unknown id ->
                            "code:2 seriesId:N".
byte1 = battery percent
byte2 = firmware major
byte3 = firmware minor
byte4 = firmware patch
byte5 = flash-record flag: 0xFF(-1)=not supportable, 0=supportable,
                           1=supportable & currently recording.
```

The emulator (`voltcraft.py`) defaults to series **41** (a B35/B41 member;
confirmed accepted, i.e. it reaches the FFF1 stage). Settable via `--series` /
the `series` REPL command.

## FFF1 — MD5 anti-counterfeit auth

App = BLE central, meter = peripheral (us).

1. App builds 6 random "coordinates" `orig[i]` in `0..35`, mixes them
   `mixed[i] = orig[i] + mixElems[i]` with
   `mixElems = [200, 100, 50, 20, 10, 5]`, zero-pads to 16 bytes and **writes**
   them to FFF1.
2. App **reads** FFF1; the meter must return `MD5(pick(recovered))` where:
   - `recovered[i] = (mixed[i] - mixElems[i]) & 0xFF`  → back to `0..35`
   - `pick`: concatenate `s1[recovered[i]]` for `i < 3`, `s2[recovered[i]]` for
     `i >= 3`. The tables:
     - `s1 = z y x 0 w v 1 u t s 2 r q 3 p o n 4 m l 5 k j i 6 h g 7 f e d 8 c b 9 a`
     - `s2 = a b 9 c d e 8 f g 7 h i j 6 k l 5 m n o 4 p q 3 r s t 2 u v 1 w x y 0 z`
   - `MD5`: plain `md5(picked.utf8Bytes)` — **no salt / prefix / suffix**.
3. **WIRE FORMAT — the critical bit.** The meter returns the **16 RAW DIGEST
   BYTES** (`md5.digest()`), *not* the 32 ASCII hex chars.
   The app reconstructs the expected string from the bytes it read with
   each byte → `toRadixString(16)` joined (lowercase, **no leading zero** per byte)
   and compares it to its own expected digest, which is the lowercase md5 hexdigest
   with every `0X` byte-pair compacted to `X`. These match **iff** the meter sent
   the raw digest bytes.
   - Returning ASCII hex (what an older OWON sibling expected, and what we tried
     first) makes the app hexify each *character code* (`'b'` = 0x62 → `"62"`),
     so it never matches → `code:3`. This was the entire blocker.

### Worked example

Challenge `cf 77 55 2f 2a 14`:
- recovered coords `[7, 19, 35, 27, 32, 15]`
- picked string `ula2xl`  (s1[7]=u, s1[19]=l, s1[35]=a, s2[27]=2, s2[32]=x, s2[15]=l)
- `md5("ula2xl")` = `ba8175919934b5873ea6462bd7888dc7`
- meter returns those **16 bytes** `ba 81 75 91 99 34 b5 87 3e a6 46 2b d7 88 8d c7`.
  App reconstructs `ba8175919934b5873ea6462bd7888dc7` == its expected digest → genuine.

Challenge `cf 85 3a 31 2c 18` → coords `[7,33,8,29,34,19]`, picked `ubtv0o`,
`md5` = `0f0837b9c4b8d4f90745fd59a73729a9` → meter returns those 16 bytes; the app
reconstructs the *compacted* `f837b9c4b8d4f9745fd59a73729a9` (note `0f→f`, `08→8`,
`07→7`) which equals its expected digest.

The emulator implements this as the default `"vc"` auth scheme in
`fakemeter/profiles/voltcraft.py` (`auth_response` → `md5(...).digest()`). Legacy
ASCII-hex schemes (`java`, `base36`, …) remain selectable via the `auth` REPL
command for regression testing.
