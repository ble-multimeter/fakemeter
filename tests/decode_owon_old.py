"""Python port of the owon-old 14-byte ASCII (B35) measurement-frame decoder.

ORACLE for the owon-old profile's encoder: a re-implementation of ``decodeOwonOld``
from ``uni-t-mmu-ble/packages/protocol/src/drivers/owon-old.ts``, cross-checked
against the OWON BLE4.0 Android app ``handleReceivedData_B35``.

ONE DELIBERATE DEVIATION FROM THE DRIVER: the nano ('n') prefix. The driver reads
it from ``byte10 bit2 && byte9 == 0`` (owon-old.ts:142) — a known bug (it works for
nF only by coincidence). The vendor app reads nano from ``byte8 bit1`` (BIT_NANO).
This oracle reads nano from byte8.1 (the app-correct location), matching what the
owon-old PROFILE encodes. Everything else is a faithful port of the driver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

SIGN_MINUS = 0x2D
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


def _bit(b: int, pos: int) -> bool:
    return (b & (1 << pos)) != 0


def decode_owon_old(frame: bytes) -> Decoded:
    if len(frame) < 14:
        raise ValueError("owon-old frame must be >= 14 bytes")

    text = "-" if frame[0] == SIGN_MINUS else ""

    # Decimal-point: (byte6 & 0x07) as a 4-bit field; index of first set bit.
    point_bits = format(frame[6] & 0x07, "04b")
    point = point_bits.find("1")
    if point < 0:
        point = 0

    digits = bytes(frame[1:5]).decode("latin-1")
    overload = digits.startswith("?") and digits.endswith("?")
    if overload:
        digits = " OL "

    if point != 0:
        text += digits[:len(digits) - point] + "." + digits[len(digits) - point:]
    else:
        text += digits
    display_text = text.strip()

    # Mode bitfield (byte 7).
    hold = _bit(frame[7], 1)
    rel = _bit(frame[7], 2)
    acdc = "AC" if _bit(frame[7], 3) else "DC" if _bit(frame[7], 4) else ""
    auto = _bit(frame[7], 5)

    # Min/Max + battery (byte 8).
    maximum = _bit(frame[8], 5)
    minimum = _bit(frame[8], 4)
    low_battery = _bit(frame[8], 3)

    diode = _bit(frame[9], 2)
    cont = _bit(frame[9], 3)

    # Unit assembly (driver order), EXCEPT nano from byte8.1 (app-correct).
    display_unit = ""
    if _bit(frame[10], 1):
        display_unit += "°C"
    if _bit(frame[10], 0):
        display_unit += "°F"
    if _bit(frame[8], 1):  # CORRECTED nano: app BIT_NANO = byte8 bit1.
        display_unit += "n"
    if _bit(frame[9], 6):
        display_unit += "m"
    if _bit(frame[9], 7):
        display_unit += "µ"
    if _bit(frame[9], 4):
        display_unit += "M"
    if _bit(frame[9], 5):
        display_unit += "k"
    if _bit(frame[10], 7):
        display_unit += "V"
    if _bit(frame[10], 2):
        display_unit += "F"
    if _bit(frame[9], 1):
        display_unit += "%"
    if _bit(frame[10], 5):
        display_unit += "Ω"
    if _bit(frame[10], 3):
        display_unit += "Hz"
    if _bit(frame[10], 6):
        display_unit += "A"

    numeric = (not overload) and bool(NUMERIC.match(display_text))
    display_value = float(display_text) if numeric else None

    flags = []
    if hold:
        flags.append("HOLD")
    if rel:
        flags.append("REL")
    if auto:
        flags.append("AUTO")
    if low_battery:
        flags.append("Bat")
    if minimum:
        flags.append("MIN")
    if maximum:
        flags.append("MAX")

    func = _function_for(display_unit, acdc, diode, cont)

    return Decoded(
        function=func, display_text=display_text, display_value=display_value,
        display_unit=display_unit, overload=overload, acdc=acdc, flags=flags,
    )


def _function_for(display_unit: str, acdc: str, diode: bool, cont: bool) -> str:
    base = display_unit
    for p in ("n", "µ", "m", "k", "M"):
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
