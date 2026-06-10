"""Python port of the owon-plus 6-byte R2W measurement-frame decoder.

ORACLE for the owon-plus profile's encoder: a faithful re-implementation of
``decodeOwonPlus`` from ``uni-t-mmu-ble/packages/protocol/src/drivers/owon-plus.ts``
(byte-verified against the OWON BLE4.0 Android app ``handleReceivedData_common``).
If ``encode(reading) -> bytes -> decode(bytes)`` reproduces the value / unit /
decimal / sign / OL-UL / flags, the encoder matches the driver's parser.

Flag bits are LSB-first (HOLD = bit 0), per the corrected driver (commit 4506bdc).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

PREFIXES = ["p", "n", "µ", "m", "", "k", "M", "G"]
NUMERIC = re.compile(r"^-?\d*\.?\d+$")


@dataclass
class Decoded:
    function: str
    display_text: str
    display_value: Optional[float]
    display_unit: str
    overload: bool
    acdc: str
    flags: list = field(default_factory=list)


def decode_owon_plus(frame: bytes) -> Decoded:
    if len(frame) < 6:
        raise ValueError("owon-plus frame must be >= 6 bytes")

    symbols = (frame[1] << 8) | frame[0]
    fn = (symbols >> 6) & 0x0F
    scale = (symbols >> 3) & 0x07
    point = symbols & 0x07

    raw = (frame[5] << 8) | frame[4]
    if point == 6:
        display_text = "U.L"
    elif point == 7:
        display_text = "O.L"
    else:
        value = raw if raw == (raw & 0x7FFF) else -1 * (raw & 0x7FFF)
        neg = value < 0
        temp = str(abs(value)).rjust(4, "0")
        if neg:
            temp = "-" + temp
        if point > 0:
            temp = temp[:len(temp) - point] + "." + temp[len(temp) - point:]
        display_text = ("-" if frame[0] == 45 else "") + temp

    # Unit symbol: SI prefix + base unit selected by the function code.
    display_unit = PREFIXES[scale]
    if fn == 8:
        display_unit += "°C"
    elif fn == 9:
        display_unit += "°F"
    elif fn in (0, 1, 10):
        display_unit += "V"
    elif fn == 5:
        display_unit += "F"
    elif fn == 7:
        display_unit += "%"
    elif fn in (4, 11):
        display_unit += "Ω"
    elif fn == 6:
        display_unit += "Hz"
    elif fn in (2, 3):
        display_unit += "A"

    acdc = "AC" if fn in (1, 3) else "DC" if fn in (0, 2) else ""

    mode = ((frame[3] << 8) | frame[2]) & 0xFFFF
    def bit(n: int) -> bool:
        return ((mode >> n) & 1) == 1
    flags = []
    if bit(0):
        flags.append("HOLD")
    if bit(1):
        flags.append("REL")
    if bit(2):
        flags.append("AUTO")
    if bit(3):
        flags.append("Bat")
    if bit(4):
        flags.append("MIN")
    if bit(5):
        flags.append("MAX")

    if fn == 13:  # NCV
        display_text = "-" * raw if raw > 0 else "EF"
        display_unit = ""
    elif fn == 12:  # hFE
        display_unit = ""

    overload = point in (6, 7)
    numeric = (not overload) and fn != 13 and bool(NUMERIC.match(display_text))
    display_value = float(display_text) if numeric else None

    if fn == 13:
        func = "NCV"
    elif fn == 12:
        func = "HFE"
    else:
        func = _function_for(display_unit, acdc, fn == 10, fn == 11)

    return Decoded(
        function=func, display_text=display_text, display_value=display_value,
        display_unit=display_unit, overload=overload, acdc=acdc, flags=flags,
    )


def _function_for(display_unit: str, acdc: str, diode: bool, cont: bool) -> str:
    # base unit = display_unit with any leading SI prefix stripped.
    base = display_unit
    for p in ("p", "n", "µ", "m", "k", "M", "G"):
        if base.startswith(p) and len(base) > len(p):
            base = base[len(p):]
            break
    if diode:
        return "DIODE"
    if cont:
        return "CONT"
    if base == "V":
        return f"{acdc}V" if acdc else "V"
    if base == "A":
        return f"{acdc}A" if acdc else "A"
    if base == "Ω":
        return "OHM"
    if base == "F":
        return "CAP"
    if base == "Hz":
        return "Hz"
    if base == "%":
        return "%"
    if base == "°C":
        return "°C"
    if base == "°F":
        return "°F"
    return base or "?"
