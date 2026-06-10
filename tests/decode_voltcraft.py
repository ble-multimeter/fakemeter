"""Python port of the REAL VC915 R10W measurement-frame decoder.

This is the ORACLE for the voltcraft profile's encoder: a faithful re-implementation
of ``R10wProtocolParse.{parseRealTimeDataOnce,parseGearAndCountingUnit,parseState,
parseMeasureValue}`` from the decompiled official Voltcraft "series800" app
(com.owon.imeter v1.2.5; blutter dump at /tmp/vc125-out). If
``encode(reading) -> bytes -> decode(bytes)`` reproduces the value / unit / decimal
/ sign / over-range / flags, the encoder matches the app's parser BEFORE phone time.

See docs/voltcraft-measurement-protocol.md for the full derivation. The previous
version of this file ported the driver repo's ``voltcraft.ts``, which decodes a
DIFFERENT (third-party Windows-app) protocol and does not match the real meter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Tables CONFIRMED by live bit-sweep against the VC915 app (consecutive codes).
GEAR_MAP = {
    0: ("DC", "V"), 1: ("AC", "V"), 2: ("DC", "A"), 3: ("AC", "A"),
    4: ("RES", "Ω"), 5: ("CAP", "F"), 6: ("Hz", "Hz"), 7: ("DUTY", "%"),
    8: ("TEMP", "℃"), 9: ("TEMP", "℉"), 10: ("DIODE", "V"), 11: ("CONT", "Ω"),
    12: ("hFE", ""), 13: ("NCV", ""),
}
CU_MAP = {
    0: ("p", 1e-12), 1: ("n", 1e-9), 2: ("u", 1e-6), 3: ("m", 1e-3),
    4: ("", 1.0), 5: ("k", 1e3), 6: ("M", 1e6), 7: ("G", 1e9),
}
# dp code IS the number of decimal places (value = count / 10**dp).
DP_MAP = {n: (f"dp{n}", 10.0 ** (-n), n) for n in range(7)}
# Confirmed live: LSB-numbered bitmask starting at bit0 (HOLD).
STATE_MAP = {
    1 << 0: "HOLD", 1 << 1: "REL", 1 << 2: "AUTO", 1 << 3: "Bat", 1 << 4: "MIN",
    1 << 5: "MAX", 1 << 6: "AVG", 1 << 7: "RMR", 1 << 8: "Loz", 1 << 9: "LPF",
    1 << 10: "Peak", 1 << 12: "Cosφ", 1 << 13: "AC", 1 << 14: "DC", 1 << 15: "USB",
    1 << 16: "Err", 1 << 17: "INRUSH", 1 << 18: "OSC",
}


@dataclass
class Decoded:
    gear: str
    acdc: str
    display_unit: str
    display_value: Optional[float]
    display_text: str
    overload: bool
    underload: bool
    negative: bool
    secondary_active: bool
    flags: list = field(default_factory=list)


def _le24(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16)


def decode_voltcraft(frame: bytes) -> Decoded:
    """Decode one 15-byte R10W record (primary display).

    All words are LITTLE-endian over 3 consecutive bytes (confirmed live).
    """
    if len(frame) < 15:
        raise ValueError("R10W frame must be >= 15 bytes")

    # Gear word (bytes 0..2, 24-bit little-endian).
    g = _le24(frame, 0)
    dp_code = g & 0x07
    cu_code = (g >> 3) & 0x07
    gear_code = (g >> 6) & 0x1F
    secondary = bool((g >> 12) & 1)

    gear, base_unit = GEAR_MAP.get(gear_code, ("?", "?"))
    acdc = gear if gear in ("AC", "DC") else ""
    prefix, _cu_mult = CU_MAP.get(cu_code, ("?", 1.0))
    _fmt, dp_mult, decimals = DP_MAP.get(dp_code, ("?", 1.0, 0))

    # Value word (bytes 3..5, 24-bit little-endian).
    v = _le24(frame, 3)
    count = v & 0x7FFFF
    negative = bool((frame[5] >> 7) & 1)   # sign == bit7 of the 3rd value byte (b5)
    overrange = (v >> 20) & 0x07

    overload = overrange == 1
    underload = overrange == 2
    if overload:
        display_text, display_value = "OL", None
    elif underload:
        display_text, display_value = "UL", None
    elif overrange == 3:
        display_text, display_value = "HI", None
    else:
        value = count * dp_mult
        if negative:
            value = -value
        display_value = value
        display_text = f"{value:.{decimals}f}"

    display_unit = "" if base_unit == "" else prefix + base_unit

    # State word (bytes 12..14, 24-bit little-endian) — bitmask of annunciators.
    st = _le24(frame, 12)
    flags = [name for bitval, name in STATE_MAP.items() if st & bitval]

    return Decoded(
        gear=gear, acdc=acdc, display_unit=display_unit, display_value=display_value,
        display_text=display_text, overload=overload, underload=underload,
        negative=negative, secondary_active=secondary, flags=flags,
    )
