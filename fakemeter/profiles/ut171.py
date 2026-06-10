"""ut171 profile — UNI-T UT171 (UT171A/B/C) true-RMS multimeter (polled, len16 AB-CD).

The UT171 speaks its own AB-CD protocol (LE length, LE checksum, in-band float32
value + a unit code). This module is the inverse of the driver repo's
``decodeUt171`` (``ut171.ts``, common-data path). NOT bench-tested.

LIVE-MEASUREMENT FRAME (CMDID 2), the inverse of the decoder's common-data path:
    AB CD <len LE> 02 <flagA> <flagB> <measureCode> <rangeIndex>
        <main float32 LE @9..12> <mainFlag @13> <unitCode @14> <chk16 LE>
``createCmd`` length = cmd + payload (excl. checksum); checksum = LE additive over
bytes[2..end-of-body]. ``mainFlag`` low nibble = OL state (0 normal / 1 OL / 2 -OL),
high nibble = display decimal places (cosmetic; the value is the raw float). The
unit code carries the display unit + AC/DC + function (UNIT_TABLE).

flagA: bit4 REL, bit7 HOLD, bit2 lowBattery, bit5 MAX/MIN active, bit6 PEAK active.
flagB: bit0 AUTO, bit1 HV, bits5-6 = which (1=max,2=min).

The meter streams after a START handshake in the live driver; here we model it
polled — a GET_DATA op returns one measurement frame, matching the emulator seam.
"""

from __future__ import annotations

import struct

from . import uni_t_base
from .base import Profile, Reading

SOF0 = uni_t_base.SOF0
SOF1 = uni_t_base.SOF1
CMD_LIVEDATA = 2

# function label -> a canonical unit code (inverse of UNIT_TABLE; first match wins).
# We expose the dominant codes; a Reading.prefix can steer V/mV, Ω/kΩ/MΩ, etc.
_UNIT_TABLE = {
    0: ("V", "DCV"), 1: ("V", "ACV"), 3: ("mV", "DCV"), 4: ("mV", "ACV"),
    6: ("µA", "DCA"), 7: ("µA", "ACA"), 9: ("mA", "DCA"), 10: ("mA", "ACA"),
    12: ("A", "DCA"), 13: ("A", "ACA"), 15: ("Ω", "OHM"), 16: ("kΩ", "OHM"),
    17: ("MΩ", "OHM"), 18: ("Hz", "Hz"), 19: ("kHz", "Hz"), 20: ("MHz", "Hz"),
    21: ("%", "%"), 22: ("nF", "CAP"), 23: ("µF", "CAP"), 24: ("mF", "CAP"),
    25: ("°C", "°C"), 26: ("°F", "°F"), 27: ("V", "DIODE"), 28: ("Ω", "CONT"),
}

# (function, prefix) -> unit code. Prefix selects the metric variant of the unit.
_PREFIX_OF_UNIT = {"V": "", "mV": "m", "µA": "µ", "mA": "m", "A": "", "Ω": "",
                   "kΩ": "k", "MΩ": "M", "Hz": "", "kHz": "k", "MHz": "M",
                   "nF": "n", "µF": "µ", "mF": "m"}


def _unit_code_for(function: str, prefix: str) -> int:
    pref = "µ" if prefix == "u" else prefix
    # Prefer an entry whose function matches AND whose unit prefix matches.
    for code, (unit, fn) in _UNIT_TABLE.items():
        if fn == function and _PREFIX_OF_UNIT.get(unit, "") == pref:
            return code
    for code, (unit, fn) in _UNIT_TABLE.items():
        if fn == function:
            return code
    return 0


FUNCTION_CODES = sorted({fn for _u, fn in _UNIT_TABLE.values()})


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a UT171 CMDID-2 live-measurement frame.

    Inverse of ``decodeUt171`` (common-data path). Packs the flag bytes, the in-band
    float32 value, the OL/decimals ``mainFlag`` nibbles, and the unit code, then uses
    ``build_frame_len16`` with the LE checksum + ut171 length convention.
    """
    fn = reading.function
    unit_code = _unit_code_for(fn, reading.prefix)

    flag_a = 0
    if reading.rel:
        flag_a |= 0x10
    if reading.hold:
        flag_a |= 0x80
    if reading.low_battery:
        flag_a |= 0x04
    if reading.max or reading.min:
        flag_a |= 0x20
    if reading.peak_max or reading.peak_min:
        flag_a |= 0x40

    flag_b = 0
    if reading.auto:
        flag_b |= 0x01
    if reading.hv_warning:
        flag_b |= 0x02
    if reading.max or reading.peak_max:
        flag_b |= (1 << 5)
    elif reading.min or reading.peak_min:
        flag_b |= (2 << 5)

    # mainFlag: low nibble OL state, high nibble decimal places (cosmetic).
    if reading.overload:
        ol = 0x02 if (reading.value is not None and reading.value < 0) else 0x01
    else:
        ol = 0
    dot = reading.decimals if reading.decimals is not None else 3
    main_flag = (ol & 0x0F) | ((dot & 0x0F) << 4)

    value = float(reading.value) if reading.value is not None else 0.0
    main_float = struct.pack("<f", value)  # LE float32

    body = bytearray()
    body.append(flag_a & 0xFF)        # [5]
    body.append(flag_b & 0xFF)        # [6]
    body.append(0)                    # [7] measureCode (0 = common data, not OUTPUT)
    body.append(0)                    # [8] rangeIndex (cosmetic)
    body += main_float                # [9..12]
    body.append(main_flag & 0xFF)     # [13]
    body.append(unit_code & 0xFF)     # [14]

    return uni_t_base.build_frame_len16(
        CMD_LIVEDATA, bytes(body),
        little_endian_chk=True, little_endian_len=True, len_includes_chk=False)


def _name_frame() -> bytes:
    # Device-info reply (cmd 22 / 0x16 in the driver); the app doesn't parse it here.
    return uni_t_base.build_frame_len16(
        0x16, b"UT171", little_endian_chk=True, little_endian_len=True,
        len_includes_chk=False)


# Soft-button / poll opcodes. START (cmd 10) turns the live stream on in the driver;
# we treat it as GET_DATA (returns a measurement frame).
CMD_START = 0x0A
CMD_INFO = 0x16
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


def _preset_resistance_kohm() -> Reading:
    return Reading(value=4.700, function="OHM", prefix="k", decimals=3)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACV", overload=True)


_CFG = uni_t_base.UniTProfile(
    id="ut171",
    label="UNI-T UT171 True-RMS Multimeter",
    default_name="UT171-FAKE",
    encode=encode,
    name_frame=_name_frame(),
    get_name_op=CMD_INFO,
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
        "resistance_kohm": _preset_resistance_kohm,
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
