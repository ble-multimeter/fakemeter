"""Python port of the ai-care driver decoder — the ORACLE for the encoder.

Faithful re-implementation of ``decodeAiCare`` from
``uni-t-mmu-ble/packages/protocol/src/drivers/ai-care.ts``: self-addressing
descramble (scatter each low nibble to its high-nibble-addressed slot), then read
flags/digits/units from the 56-bit field. If ``encode -> decode`` reproduces the
value / unit / sign / over-range / flags, the encoder matches the driver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

FRAME_LEN = 14
BIT_LEN = FRAME_LEN * 4  # 56

SEG = {
    "1111101": "0", "0000101": "1", "1011011": "2", "0011111": "3",
    "0100111": "4", "0111110": "5", "1111110": "6", "0010101": "7",
    "1111111": "8", "0111111": "9", "1101000": "L", "0000000": "",
}

PREFIX_EXP = {"n": -9, "µ": -6, "m": -3, "k": 3, "M": 6}
_NUMERIC = re.compile(r"^-?\d*\.?\d+$")


@dataclass
class Decoded:
    display_text: str
    display_value: Optional[float]
    display_unit: str
    base_unit: str
    base_value: Optional[float]
    overload: bool
    acdc: str
    diode: bool
    cont: bool
    function: str
    flags: dict = field(default_factory=dict)


def descramble(b: bytes) -> str:
    slots = ["0000"] * FRAME_LEN
    for i in range(FRAME_LEN):
        slot = ((b[i] & 0xF0) >> 4) - 1
        if 0 <= slot < FRAME_LEN:
            slots[slot] = format(b[i] & 0x0F, "04b")
    return "".join(slots)


def _unit_info(display: str) -> tuple[str, int]:
    head = display[:1]
    if len(display) > 1 and head in PREFIX_EXP:
        return display[1:], PREFIX_EXP[head]
    return display, 0


def _function_for(base_unit: str, acdc: str, diode: bool, cont: bool) -> str:
    if diode:
        return "DIODE"
    if cont:
        return "CONT"
    return {
        "V": f"{acdc}V" if acdc else "V",
        "A": f"{acdc}A" if acdc else "A",
        "Ω": "OHM", "F": "CAP", "Hz": "Hz", "%": "%", "°C": "°C", "°F": "°F",
    }.get(base_unit, base_unit or "?")


def decode_ai_care(b: bytes, ts: int = 0) -> Decoded:
    v = descramble(b)
    on = lambda i: v[i] == "1"
    seg = lambda start: SEG.get(v[start:start + 7], "?")

    display_text = (
        ("-" if on(4) else "")
        + seg(5)
        + ("." if on(12) else "") + seg(13)
        + ("." if on(20) else "") + seg(21)
        + ("." if on(28) else "") + seg(29)
    ).strip()

    display_unit = ""
    if on(36):
        display_unit += "µ"
    if on(37):
        display_unit += "n"
    if on(38):
        display_unit += "k"
    if on(40):
        display_unit += "m"
    if on(42):
        display_unit += "M"
    if on(41):
        display_unit += "%"
    if on(44):
        display_unit += "F"
    if on(45):
        display_unit += "Ω"
    if on(48):
        display_unit += "A"
    if on(49):
        display_unit += "V"
    if on(50):
        display_unit += "Hz"
    if on(53):
        display_unit += "°C"

    acdc = "AC" if on(0) else ("DC" if on(1) else "")
    diode = on(39)
    cont = on(43)

    overload = "L" in display_text
    numeric = (not overload) and bool(_NUMERIC.match(display_text))
    display_value = float(display_text) if numeric else None

    base_unit, exp = _unit_info(display_unit)
    base_value = None if display_value is None else display_value * (10 ** exp)

    return Decoded(
        display_text=display_text,
        display_value=display_value,
        display_unit=display_unit,
        base_unit=base_unit,
        base_value=base_value,
        overload=overload,
        acdc=acdc,
        diode=diode,
        cont=cont,
        function=_function_for(base_unit, acdc, diode, cont),
        flags={
            "hold": on(47), "rel": on(46), "auto": on(2), "low_battery": on(51),
        },
    )
