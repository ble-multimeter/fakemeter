"""Python port of the UT171 live-measurement decoder (ORACLE, common-data path)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

CMD_LIVEDATA = 2

UNIT_TABLE = {
    0: ("V", "DC", "DCV"), 1: ("V", "AC", "ACV"), 3: ("mV", "DC", "DCV"),
    4: ("mV", "AC", "ACV"), 6: ("µA", "DC", "DCA"), 7: ("µA", "AC", "ACA"),
    9: ("mA", "DC", "DCA"), 10: ("mA", "AC", "ACA"), 12: ("A", "DC", "DCA"),
    13: ("A", "AC", "ACA"), 15: ("Ω", "", "OHM"), 16: ("kΩ", "", "OHM"),
    17: ("MΩ", "", "OHM"), 18: ("Hz", "", "Hz"), 19: ("kHz", "", "Hz"),
    20: ("MHz", "", "Hz"), 21: ("%", "", "%"), 22: ("nF", "", "CAP"),
    23: ("µF", "", "CAP"), 24: ("mF", "", "CAP"), 25: ("°C", "", "°C"),
    26: ("°F", "", "°F"), 27: ("V", "", "DIODE"), 28: ("Ω", "", "CONT"),
}

_PREFIX_EXP = {"n": -9, "µ": -6, "m": -3, "k": 3, "M": 6}


def _unit_info(display: str):
    head = display[:1]
    if len(display) > 1 and head in _PREFIX_EXP:
        return display[1:], _PREFIX_EXP[head]
    return display, 0


@dataclass
class Decoded:
    function: str = "?"
    display_value: Optional[float] = None
    display_unit: str = ""
    base_value: Optional[float] = None
    overload: bool = False
    acdc: str = ""
    flags: dict = field(default_factory=dict)


def checksum_ok(frame: bytes) -> bool:
    if len(frame) < 7 or frame[0] != 0xAB or frame[1] != 0xCD:
        return False
    s = sum(frame[2:-2]) & 0xFFFF
    return (s & 0xFF) == frame[-2] and ((s >> 8) & 0xFF) == frame[-1]


def decode_ut171(frame: bytes) -> Decoded:
    if len(frame) < 17 or frame[0] != 0xAB or frame[1] != 0xCD or frame[4] != CMD_LIVEDATA:
        return Decoded()
    flag_a = frame[5]
    flag_b = frame[6]
    flags = {
        "rel": bool(flag_a & 0x10), "hold": bool(flag_a & 0x80),
        "low_battery": bool(flag_a & 0x04), "auto": bool(flag_b & 0x01),
        "hv_warning": bool(flag_b & 0x02),
    }
    i = 9
    main_float = struct.unpack_from("<f", frame, i)[0]
    main_flag = frame[i + 4]
    unit_code = frame[i + 5]
    ol = main_flag & 0x0F
    dot = (main_flag & 0xF0) >> 4
    unit, acdc, fn = UNIT_TABLE.get(unit_code, ("", "", "?"))
    overload = ol in (1, 2)
    if overload:
        value = None
    else:
        p = 10 ** dot
        value = round(main_float * p) / p
    base_unit, exp = _unit_info(unit)
    base_value = None if value is None else value * (10 ** exp)
    return Decoded(function=fn, display_value=value, display_unit=unit,
                   base_value=base_value, overload=overload, acdc=acdc, flags=flags)
