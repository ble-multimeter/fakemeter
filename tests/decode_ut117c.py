"""Python port of the UT117C measurement decoder (ORACLE) — from ut117c.ts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

SOF_H, SOF_L = 0xAB, 0xCD
TYPE_DATA = 2
DATA_LEN = 0x12
DATA_TOTAL = 4 + DATA_LEN + 2  # 24

FUNCTIONS = {
    1: "ACV", 2: "ACV", 3: "ACV", 4: "DCV", 5: "DCV", 6: "OHM", 7: "CONT",
    8: "DIODE", 9: "CAP", 10: "LozV", 11: "Hz", 12: "Hz", 13: "NCV",
    14: "DCA", 15: "ACA", 16: "Hz", 17: "ACA", 18: "ACA", 19: "DCA",
}

_PREFIX_EXP = {"n": -9, "µ": -6, "m": -3, "k": 3, "M": 6}
_NUMERIC = re.compile(r"^-?\d*\.?\d+$")


def _unit_info(display: str):
    head = display[:1]
    if len(display) > 1 and head in _PREFIX_EXP:
        return display[1:], _PREFIX_EXP[head]
    return display, 0


def _ascii_string(b: bytes, start: int, length: int, remap_ohm: bool) -> str:
    s = ""
    for i in range(length):
        if start + i >= len(b):
            break
        c = b[start + i]
        if c == 0:
            break
        if remap_ohm and c == 0x6F:
            s += "Ω"
        else:
            s += chr(c)
    return s.strip()


@dataclass
class Decoded:
    function: str = "?"
    display_text: str = ""
    display_value: Optional[float] = None
    display_unit: str = ""
    base_value: Optional[float] = None
    overload: bool = False
    acdc: str = ""
    flags: dict = field(default_factory=dict)


def checksum_ok(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    s = sum(frame[:-2]) & 0xFFFF
    return ((s >> 8) & 0xFF) == frame[-2] and (s & 0xFF) == frame[-1]


def decode_ut117c(b: bytes) -> Decoded:
    if len(b) < DATA_TOTAL or b[0] != SOF_H or b[1] != SOF_L or b[4] != TYPE_DATA:
        return Decoded()
    func_id = b[5]
    rang_index = b[6]
    ol_flag = b[18]
    flag1 = b[20]
    flag2 = b[21]
    fn = FUNCTIONS.get(func_id, f"#{func_id}")

    is_ac = (flag2 & 0x02) != 0
    acdc_relevant = fn in ("ACV", "DCV", "DCA", "ACA", "LozV")
    acdc = ("AC" if is_ac else "DC") if acdc_relevant else ""

    value_str = _ascii_string(b, 8, 7, False)
    display_unit = _ascii_string(b, 15, 3, True)

    flags = {
        "hold": (flag1 & 0x02) != 0, "rel": (flag1 & 0x01) != 0,
        "auto": (flag2 & 0x08) != 0, "low_battery": (flag1 & 0x08) != 0,
        "hv_warning": (flag2 & 0x01) != 0,
    }

    if func_id == 13:
        return Decoded(function="NCV", display_text=("HI" if rang_index == 0x31 else "LO"),
                       display_unit="", flags=flags)

    numeric = len(value_str) > 0 and bool(_NUMERIC.match(value_str))
    overload = numeric and (ol_flag in (0x31, 0x32))
    if overload:
        display_text = "-OL" if ol_flag == 0x32 else "OL"
    else:
        display_text = value_str
    display_value = float(value_str) if (numeric and not overload) else None
    base_unit, exp = _unit_info(display_unit)
    base_value = None if display_value is None else display_value * (10 ** exp)

    return Decoded(function=fn, display_text=display_text, display_value=display_value,
                   display_unit=display_unit, base_value=base_value, overload=overload,
                   acdc=acdc, flags=flags)
