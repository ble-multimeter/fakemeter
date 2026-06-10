"""Python port of the UT181A live-measurement decoder (ORACLE, MAIN value block)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

H0, H1 = 0xAB, 0xCD
OPCODE_LIVE = 0x02

_PREFIX_EXP = {"n": -9, "µ": -6, "m": -3, "k": 3, "M": 6}
_STATUS = {1: "OL", 2: "-OL", 4: "LEAD", 5: "DISC", 6: "Lo", 7: "Hi"}


def _unit_info(display: str):
    head = display[:1]
    if len(display) > 1 and head in _PREFIX_EXP:
        return display[1:], _PREFIX_EXP[head]
    return display, 0


def _decode_unit(b: bytes, at: int) -> str:
    s = ""
    for i in range(at, min(at + 8, len(b))):
        c = b[i]
        if c == 0:
            break
        if c == 0xB0:
            s += "°"
        elif c == 0x7E:
            s += "Ω"
        else:
            s += chr(c)
    return s.strip()


def _function_for(base_unit: str, acdc: str) -> str:
    return {
        "V": (f"{acdc}V" if acdc else "V"),
        "A": (f"{acdc}A" if acdc else "A"),
        "Ω": "OHM", "F": "CAP", "Hz": "Hz", "%": "%", "°C": "°C", "°F": "°F",
    }.get(base_unit, base_unit or "?")


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
    if len(frame) < 7 or frame[0] != H0 or frame[1] != H1:
        return False
    len16 = frame[2] | (frame[3] << 8)
    if len16 + 4 != len(frame):
        return False
    s = sum(frame[2:-2]) & 0xFFFF
    return (s & 0xFF) == frame[-2] and ((s >> 8) & 0xFF) == frame[-1]


def decode_ut181a(b: bytes) -> Decoded:
    if len(b) < 23 or b[0] != H0 or b[1] != H1 or b[4] != OPCODE_LIVE:
        return Decoded()
    if not checksum_ok(b):
        return Decoded()
    flags_a = b[5]
    flags_b = b[6]
    base = 10
    raw = struct.unpack_from("<f", b, base)[0]
    status_byte = b[base + 4]
    dot = (status_byte >> 4) & 0x0F
    status = _STATUS.get(status_byte & 0x0F)
    display_unit = _decode_unit(b, base + 5)
    base_unit, exp = _unit_info(display_unit)
    u = display_unit.upper()
    acdc = "AC" if "AC" in u else ("DC" if "DC" in u else "")
    overload = status in ("OL", "-OL")
    if status is not None:
        value = None
    else:
        f = 10 ** dot
        value = round(raw * f) / f
    base_value = None if value is None else value * (10 ** exp)
    flags = {
        "hold": bool(flags_a & 0x80), "rel": bool(flags_a & 0x10),
        "max": bool(flags_a & 0x20), "auto": bool(flags_b & 0x01),
        "hv_warning": bool(flags_b & 0x02), "peak_max": bool(flags_a & 0x40),
    }
    return Decoded(function=_function_for(base_unit, acdc), display_value=value,
                   display_unit=display_unit, base_value=base_value,
                   overload=overload, acdc=acdc, flags=flags)
