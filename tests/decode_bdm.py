"""Python port of the bdm driver decoder — the ORACLE for the bdm encoder.

Faithful re-implementation of ``decodeBdm`` from
``uni-t-mmu-ble/packages/protocol/src/drivers/bdm.ts``: XOR-descramble with
DATASHIFT, concatenate 11 bytes MSB-first into 88 bits, read four 7-segment
digits + unit/flag bits at fixed offsets. If ``encode -> decode`` reproduces the
value / unit / sign / over-range / flags, the encoder matches the driver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

DATASHIFT = (65, 33, 115, 85, 162, 193, 50, 113, 102, 170, 59)
FRAME_LEN = 11

SEG = {
    "0000000": " ", "1111110": "A", "0010011": "U", "0110101": "T",
    "0010111": "O", "1110101": "E", "1110100": "F", "0110001": "L",
    "0000100": "-", "1111011": "0", "0001010": "1", "1011101": "2",
    "1001111": "3", "0101110": "4", "1100111": "5", "1110111": "6",
    "1001010": "7", "1111111": "8", "1101111": "9",
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
    bits = ""
    for i in range(FRAME_LEN):
        bits += format((b[i] ^ DATASHIFT[i]) & 0xFF, "08b")
    return bits


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


def decode_bdm(b: bytes, ts: int = 0) -> Decoded:
    bits = descramble(b)
    on = lambda i: bits[i] == "1"

    pre_points = ["-", ".", ".", "."]
    text = ""
    for n in range(4):
        fi = (n + 3) * 8
        first = bits[fi:fi + 3]
        si = (n + 4) * 8 + 4
        second = bits[si:si + 4]
        prefix = pre_points[n] if on((n + 3) * 8 + 3) else ""
        text += prefix + SEG.get(first + second, "?")
    display_text = text.strip()

    display_unit = ""
    if on(57):
        display_unit += "°C"
    if on(58):
        display_unit += "°F"
    if on(74):
        display_unit += "m"
    if on(75):
        display_unit += "V"
    if on(64):
        display_unit += "n"
    if on(65):
        display_unit += "m"
    if on(66):
        display_unit += "µ"
    if on(67):
        display_unit += "F"
    if on(69):
        display_unit += "%"
    if on(76):
        display_unit += "M"
    if on(77):
        display_unit += "k"
    if on(78):
        display_unit += "Ω"
    if on(79):
        display_unit += "Hz"
    if on(85):
        display_unit += "µ"
    if on(84):
        display_unit += "m"
    if on(72):
        display_unit += "A"

    acdc = "AC" if on(68) else ("DC" if on(73) else "")
    diode = on(56)
    cont = on(28)

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
            "max": on(71), "min": on(70), "hold": on(59), "rel": on(30),
            "auto": on(87), "low_battery": on(31),
        },
    )
