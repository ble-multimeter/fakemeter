"""ut181a profile — UNI-T UT181A datalogging true-RMS multimeter (polled, len16 AB-CD).

The UT181A speaks its OWN AB-CD protocol (LE length, LE checksum, in-band float32
value + an 8-byte ASCII unit). This module is the inverse of the driver repo's
``decodeUt181a`` (``ut181a.ts``, MAIN value block). NOT bench-tested.

LIVE-MEASUREMENT FRAME (opcode 0x02), the inverse of the decoder:
    AB CD <len LE> 02 <flagsA> <flagsB> <measureCode LE @7..8> <rangeIndex @9>
        <main float32 LE @10..13> <statusByte @14> <8-byte ASCII unit @15..22> <chk LE>
``createCmd`` len16 = body + 3 (= total - 4); chk = LE additive over bytes[2..body-end].
``statusByte`` low nibble = OL/status (1=OL, 2=-OL, 4=LEAD, 5=DISC, 6=Lo, 7=Hi),
high nibble = decimal places. The unit string is sent IN-BAND (0x7E -> 'Ω',
0xB0 -> '°'), so the value+unit render without the absent measureCode JSON. AC/DC is
carried by the unit text itself (e.g. "V AC").

flagsA: bit4 REL, bit5 MAX/MIN, bit6 PEAK, bit7 HOLD.  flagsB: bit0 AUTO, bit1 HV.

NOTE: the real UT181A protocol is RICHER than this MAIN-only path — it has a
multi-value secondary block, datalogging record download, and a measureCode->name
JSON the driver lacks. This profile emulates only the primary live reading (enough
for connect + a live value + HOLD/REL). The secondary block, record replay, and the
exact measureCode/AC-DC encoding are DEFERRED (need a hardware capture).
"""

from __future__ import annotations

import struct

from . import uni_t_base
from .base import Profile, Reading

SOF0 = uni_t_base.SOF0
SOF1 = uni_t_base.SOF1
OPCODE_LIVE = 0x02

# function label -> an in-band display unit (the wire carries the unit ASCII). The
# AC/DC distinction is encoded into the unit text ("V AC") per the decoder.
_FUNCTION_UNIT = {
    "DCV": "V", "ACV": "V AC", "DCA": "A", "ACA": "A AC", "OHM": "Ω", "CAP": "F",
    "Hz": "Hz", "%": "%", "°C": "°C", "°F": "°F", "DIODE": "V", "CONT": "Ω",
    "dBm": "dBm",
}
FUNCTION_CODES = sorted(_FUNCTION_UNIT)

# Status nibble values (inverse of statusText).
_STATUS_OL = 0x01
_STATUS_NEG_OL = 0x02


def _unit_wire(unit: str) -> bytes:
    """Encode the display unit to its 8-byte in-band field ('Ω'->0x7E, '°'->0xB0)."""
    out = bytearray()
    for ch in unit:
        if ch == "Ω":
            out.append(0x7E)
        elif ch == "°":
            out.append(0xB0)
        else:
            out += ch.encode("ascii", "replace")
    return bytes(out[:8]).ljust(8, b"\x00")


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a UT181A opcode-0x02 live-measurement frame.

    Inverse of ``decodeUt181a`` (MAIN value block). Packs the flag bytes, the in-band
    float32 value, the OL/decimals status byte, and the 8-byte ASCII unit, then uses
    ``build_frame_len16`` with the LE checksum + ut181a length convention.
    """
    fn = reading.function
    unit = _FUNCTION_UNIT.get(fn)
    if unit is None:
        raise ValueError(f"unknown ut181a function {fn!r}; known: {FUNCTION_CODES}")
    unit = (reading.prefix + unit) if reading.prefix else unit

    flags_a = 0
    if reading.rel:
        flags_a |= 0x10
    if reading.max or reading.min:
        flags_a |= 0x20
    if reading.peak_max or reading.peak_min:
        flags_a |= 0x40
    if reading.hold:
        flags_a |= 0x80

    flags_b = 0
    if reading.auto:
        flags_b |= 0x01
    if reading.hv_warning:
        flags_b |= 0x02

    if reading.overload:
        ol = _STATUS_NEG_OL if (reading.value is not None and reading.value < 0) else _STATUS_OL
    else:
        ol = 0
    dot = reading.decimals if reading.decimals is not None else 3
    status_byte = (ol & 0x0F) | ((dot & 0x0F) << 4)

    value = float(reading.value) if reading.value is not None else 0.0
    main_float = struct.pack("<f", value)

    body = bytearray()
    body.append(flags_a & 0xFF)       # [5]
    body.append(flags_b & 0xFF)       # [6]
    body += b"\x00\x00"               # [7..8] measureCode LE (cosmetic)
    body.append(0)                    # [9] rangeIndex
    body += main_float                # [10..13]
    body.append(status_byte & 0xFF)   # [14]
    body += _unit_wire(unit)          # [15..22]

    return uni_t_base.build_frame_len16(
        OPCODE_LIVE, bytes(body),
        little_endian_chk=True, little_endian_len=True, len_includes_chk=True)


def _name_frame() -> bytes:
    # Device-info reply (opcode 0x05 START is the handshake in the driver); the app
    # does not parse this control frame's contents.
    return uni_t_base.build_frame_len16(
        0x05, b"UT181A", little_endian_chk=True, little_endian_len=True, len_includes_chk=True)


# Opcodes (UT181AManager). START 0x05 turns the live stream on (our GET_DATA);
# HOLD 0x12, REL 0x13.
CMD_START = 0x05
CMD_HOLD = 0x12
CMD_REL = 0x13
CMD_SELECT = 0x01
CMD_RANGE = 0x02
CMD_MAXMIN = 0x04

_CONTROLS = {
    CMD_HOLD: "hold",
    CMD_REL: "rel",
    CMD_SELECT: "select",
    CMD_RANGE: "range",
    CMD_MAXMIN: "maxmin",
}

_SELECT_CYCLE = ["DCV", "ACV", "OHM", "CAP", "Hz", "DIODE", "CONT", "DCA", "ACA"]
_ACDC_TOGGLE = {"DCV": "ACV", "ACV": "DCV", "DCA": "ACA", "ACA": "DCA"}


def _preset_dc_volts() -> Reading:
    return Reading(value=4.2000, function="DCV", prefix="", decimals=4)


def _preset_ac_volts() -> Reading:
    return Reading(value=230.00, function="ACV", prefix="", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACV", overload=True)


_CFG = uni_t_base.UniTProfile(
    id="ut181a",
    label="UNI-T UT181A Datalogging Multimeter",
    default_name="UT181A-FAKE",
    encode=encode,
    name_frame=_name_frame(),
    get_name_op=0xFF,        # no separate GET_NAME; START doubles as data kick
    get_data_op=CMD_START,
    parse_opcode=uni_t_base.parse_opcode_len16,
    controls=_CONTROLS,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=[4, 3, 2, 1],
    function_codes=FUNCTION_CODES,
    initial=Reading(value=4.2000, function="DCV", prefix="", decimals=4),
    presets={
        "dc_volts": _preset_dc_volts,
        "ac_volts": _preset_ac_volts,
        "overload": _preset_overload,
    },
)
_METER = uni_t_base.UniTMeter(_CFG)


def reset_state(reading: Reading | None = None) -> None:
    _METER.reset_state(reading)


def set_walk(on: bool) -> None:
    _METER.set_walk(on)


def tick() -> None:
    _METER.tick()


def current_frame() -> bytes:
    return _METER.current_frame()


def command(data: bytes) -> bytes | None:
    return _METER.command(data)


profile: Profile = uni_t_base.make_profile(_CFG, _METER)
