"""Python port of the UNI-T UT60BT/UT161 19-byte measurement decoder (the ORACLE).

A faithful re-implementation of the driver repo's ``decode.ts`` + ``types.ts``
(function/range tables) for the generic AB-CD 19-byte frame. If
``uni_t.encode(reading) -> bytes -> decode_uni_t(bytes)`` reproduces the
value/unit/function/sign/over-range/flags, the encoder matches the app's parser.

This same decoder validates ut202bt (which rides the identical frame).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

FUNCTIONS = [
    "ACV", "ACmV", "DCV", "DCmV", "Hz", "%", "OHM", "CONT", "DIODE", "CAP",
    "°C", "°F", "DCuA", "ACuA", "DCmA", "ACmA", "DCA", "ACA", "HFE", "Live",
    "NCV", "LozV", "ACA", "DCA", "LPF", "AC/DC", "LPF", "AC+DC", "LPFA",
    "AC+DC2", "INRUSH",
]

RANGE_UNITS = {
    "ACV": ["V", "V", "V", "V"], "DCV": ["V", "V", "V", "V"],
    "LozV": ["V", "V", "V", "V"], "ACmV": ["mV"], "DCmV": ["mV"],
    "Hz": ["Hz", "Hz", "kHz", "kHz", "kHz", "MHz", "MHz", "MHz"], "%": ["%"],
    "OHM": ["Ω", "kΩ", "kΩ", "kΩ", "MΩ", "MΩ", "MΩ"], "CONT": ["Ω"],
    "DIODE": ["V"], "CAP": ["nF", "nF", "µF", "µF", "µF", "mF", "mF", "mF"],
    "°C": ["°C"], "°F": ["°F"], "DCuA": ["µA", "µA"], "ACuA": ["µA", "µA"],
    "DCmA": ["mA", "mA"], "ACmA": ["mA", "mA"], "DCA": ["A", "A"], "ACA": ["A", "A"],
    "HFE": [""], "Live": [""], "NCV": [""],
    "LPF": ["V", "V", "V", "V"], "AC/DC": ["V", "V", "V", "V"],
    "LPFA": ["V", "V", "V", "V"], "AC+DC": ["A", "A"], "AC+DC2": ["A", "A"],
    "INRUSH": ["V", "V", "V", "V"],
}

ACDC_FUNCTIONS = {
    "ACV", "DCV", "LozV", "ACmV", "DCmV", "DCuA", "ACuA", "DCmA", "ACmA",
    "DCA", "ACA",
}

_PREFIX_EXP = {"n": -9, "µ": -6, "m": -3, "k": 3, "M": 6}
_OVERLOAD = re.compile(r"^-?OL$")


def _unit_info(display: str):
    head = display[:1]
    if len(display) > 1 and head in _PREFIX_EXP:
        return display[1:], _PREFIX_EXP[head]
    return display, 0


@dataclass
class Decoded:
    function: str = "?"
    display_text: str = ""
    display_value: Optional[float] = None
    display_unit: str = ""
    base_value: Optional[float] = None
    base_unit: str = ""
    overload: bool = False
    acdc: str = ""
    bargraph: int = 0
    flags: dict = field(default_factory=dict)


def decode_uni_t(b: bytes) -> Decoded:
    if len(b) != 19 or b[0] != 0xAB or b[1] != 0xCD:
        return Decoded()
    fn_index = b[3] & 0x7F
    fn_name = FUNCTIONS[fn_index] if fn_index < len(FUNCTIONS) else f"#{fn_index}"
    range_index = b[4] - 0x30

    display_text = b[5:12].decode("ascii", "replace").strip()

    ranges = RANGE_UNITS.get(fn_name)
    if ranges:
        display_unit = ranges[range_index] if range_index < len(ranges) else (ranges[0] if ranges else "?")
    else:
        display_unit = "?"
    base_unit, exp = _unit_info(display_unit)

    overload = bool(_OVERLOAD.match(display_text.replace(".", "")))
    display_value = None
    if not overload and display_text != "":
        try:
            display_value = float(display_text)
        except ValueError:
            display_value = None
    base_value = None if display_value is None else display_value * (10 ** exp)

    a, bb, c = b[14], b[15], b[16]
    return Decoded(
        function=fn_name,
        display_text=display_text,
        display_value=display_value,
        display_unit=display_unit,
        base_value=base_value,
        base_unit=base_unit,
        overload=overload,
        acdc=("AC" if (c & 0x08) else "DC") if fn_name in ACDC_FUNCTIONS else "",
        bargraph=b[12] * 10 + b[13],
        flags={
            "max": bool(a & 0x08), "min": bool(a & 0x04),
            "hold": bool(a & 0x02), "rel": bool(a & 0x01),
            "auto": not (bb & 0x04), "low_battery": bool(bb & 0x02),
            "hv_warning": bool(bb & 0x01),
            "peak_max": bool(c & 0x04), "peak_min": bool(c & 0x02),
        },
    )


def checksum_ok(b: bytes) -> bool:
    if len(b) != 19:
        return False
    s = sum(b[0:17])
    return ((s >> 8) & 0xFF) == b[17] and (s & 0xFF) == b[18]
