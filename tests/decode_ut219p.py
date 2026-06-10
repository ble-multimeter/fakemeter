"""Python port of the UT219P standard live-data decoder (ORACLE, ACV/ACA path)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

SOF0, SOF1 = 0xAB, 0xCD
CMD_LIVE = 0x05

ACV_SET = {1: ("ACV", "V"), 2: ("V(PEAK)", "V"), 3: ("Hz(V)", "Hz")}
ACA_SET = {1: ("ACA", "A"), 2: ("A(PEAK)", "A"), 3: ("Hz(A)", "Hz")}
_OVERLOAD_TEXT = {1: "OL", 2: "Err", 3: "--"}


def _payload_len(frame: bytes) -> int:
    return ((frame[2] & 0xFF) << 8) | (frame[3] & 0xFF)


def checksum_ok(frame: bytes) -> bool:
    if len(frame) < 6 or frame[0] != SOF0 or frame[1] != SOF1:
        return False
    i = _payload_len(frame)
    if i > len(frame) - 6:
        return False
    s = sum(frame[2:i + 4]) & 0xFFFF
    return (s & 0xFF) == frame[i + 4] and ((s >> 8) & 0xFF) == frame[i + 5]


def _overload_codes(frame: bytes):
    j = ((frame[13] << 24) | (frame[14] << 16) | (frame[15] << 8) | frame[16]) & 0xFFFFFFFF
    out = []
    for _ in range(16):
        out.append((j & 0xC0000000) >> 30)
        j = (j << 2) & 0xFFFFFFFF
    return out


def _choose_set(dao_pos: int):
    return ACA_SET if dao_pos == 2 else ACV_SET


def _function_for(unit: str, title: str) -> str:
    return {"V": "ACV", "A": "ACA", "Hz": "Hz"}.get(unit, title or "?")


def _decimals_for(unit: str, title: str, range_value: int) -> int:
    if title == "ACV" or unit == "V":
        return 1
    if unit == "A":
        return [2, 1, 0, 0][range_value] if range_value < 4 else 1
    if unit == "Hz":
        return 1
    return 1


@dataclass
class Decoded:
    function: str = "?"
    display_value: Optional[float] = None
    display_unit: str = ""
    overload: bool = False
    acdc: str = ""
    flags: dict = field(default_factory=dict)


def decode_ut219p(b: bytes) -> Decoded:
    if len(b) < 6 or b[0] != SOF0 or b[1] != SOF1:
        return Decoded()
    if not checksum_ok(b):
        return Decoded()
    if b[4] != CMD_LIVE or b[5] != 0:
        return Decoded()
    if len(b) < 23:
        return Decoded()
    dao_pos = b[6]
    b1, b2 = b[7], b[8]
    max_min = (b1 & 0xC0) >> 6
    range_value = (b1 & 0x0C) >> 2
    flags = {
        "max": max_min == 1, "min": max_min == 2, "hold": bool(b1 & 0x10),
        "rel": bool(b2 & 0x08), "auto": (b1 & 0x02) == 0, "hv_warning": bool(b2 & 0x04),
    }
    codes = _overload_codes(b)
    idx = (b[11] & 0xF0) >> 4
    sset = _choose_set(dao_pos)
    title, unit = sset.get(idx, sset.get(1, (f"#{dao_pos}.{idx}", "")))
    code0 = codes[0]
    value = struct.unpack_from("<f", b, 19)[0]
    if code0 != 0:
        return Decoded(function=_function_for(unit, title),
                       display_value=None, display_unit="",
                       overload=(code0 in (1, 3)),
                       acdc=("AC" if unit in ("V", "A") else ""), flags=flags)
    dp = _decimals_for(unit, title, range_value)
    value = round(value, dp)
    return Decoded(function=_function_for(unit, title), display_value=value,
                   display_unit=unit, overload=False,
                   acdc=("AC" if unit in ("V", "A") else ""), flags=flags)
