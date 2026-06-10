"""ut117c profile — UNI-T UT117C digital multimeter (polled, len16 AB-CD frame).

A different AB-CD generation from the UT60BT: a 16-bit length field, a packet-type
byte, in-band ASCII unit, and a 16-bit BIG-ENDIAN additive checksum. This module is
the inverse of the driver repo's ``decodeUt117c`` (``ut117c.ts``). NOT bench-tested.

MEASUREMENT FRAME (TYPE 2), 24 bytes total (LEN=0x12 = 18-byte payload):
    [0]=0xAB [1]=0xCD  [2..3]=LEN (big-endian, = payload bytes from [4]) = 0x0012
    [4]=type (2 = realtime measurement)
    [5]=funcID  [6]=rangIndex (ASCII digit)  [7]=hzRangIndex
    [8..14]  = 7-byte ASCII value string (space-padded, may have leading '-')
    [15..17] = 3-byte ASCII unit string ('o' -> 'Ω')
    [18]=olFlag (0x31='OL', 0x32='-OL', else 0)  [19]=maxminFlag
    [20]=flag1  [21]=flag2  [22..23]=checksum (16-bit BE additive, all preceding)

flag1: bit0 REL, bit1 HOLD, bit3 lowBattery.
flag2: bit0 hvWarning, bit1 AC(set)/DC(clear), bit3 auto.

The app polls (``AB CD 00 04 05 00 01 81``) and the meter answers ONE frame per
poll — modelled here via the polled command seam (GET_DATA op = the poll's cmd
byte 0x05).
"""

from __future__ import annotations

from . import uni_t_base
from .base import Profile, Reading

SOF0 = uni_t_base.SOF0
SOF1 = uni_t_base.SOF1
TYPE_DATA = 2
DATA_LEN = 0x12  # 18-byte payload

# funcID -> function key (reverse of the driver's FUNCTIONS map; we pick a canonical
# code per label so encode can address each function).
_FUNCTIONS = {
    "ACV": 1, "DCV": 4, "OHM": 6, "CONT": 7, "DIODE": 8, "CAP": 9,
    "LozV": 10, "Hz": 11, "NCV": 13, "DCA": 14, "ACA": 15,
}
FUNCTION_CODES = dict(_FUNCTIONS)

_ACDC_FUNCTIONS = {"ACV", "DCV", "DCA", "ACA", "LozV"}

# A representative unit per function (the frame carries the unit ASCII in-band).
_DEFAULT_UNIT = {
    "ACV": "V", "DCV": "V", "LozV": "V", "OHM": "Ω", "CONT": "Ω", "DIODE": "V",
    "CAP": "nF", "Hz": "Hz", "DCA": "A", "ACA": "A", "NCV": "",
}


def _value_string(reading: Reading) -> str:
    if reading.overload:
        # The decoder only honours olFlag when the value field is itself numeric
        # (overload = numeric && olFlag set), so emit a numeric placeholder; olFlag
        # then overrides the displayed text to "OL"/"-OL".
        return "0"
    value = reading.value if reading.value is not None else 0.0
    decimals = reading.decimals
    if decimals is None:
        s = f"{abs(value):.4f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
    return f"{value:.{max(0, decimals)}f}"[:7]


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 24-byte UT117C TYPE-2 measurement frame.

    Inverse of ``decodeUt117c``. Renders the ASCII value + in-band unit, packs the
    OL / flag bytes, and uses ``uni_t_base.build_frame_len16`` for the len16 +
    big-endian additive checksum envelope.
    """
    fn = reading.function
    func_id = _FUNCTIONS.get(fn)
    if func_id is None:
        raise ValueError(f"unknown ut117c function {fn!r}; known: {sorted(_FUNCTIONS)}")

    unit = (reading.prefix + _DEFAULT_UNIT.get(fn, "")) if not reading.overload else ""
    # 'Ω' is sent as 'o' on the wire (the driver remaps 'o' -> 'Ω' on decode).
    unit_wire = unit.replace("Ω", "o")

    value_str = _value_string(reading)
    value_field = value_str.encode("ascii", "replace")[:7].ljust(7, b" ")
    unit_field = unit_wire.encode("ascii", "replace")[:3].ljust(3, b"\x00")

    if reading.overload:
        ol_flag = 0x32 if (reading.value is not None and reading.value < 0) else 0x31
    else:
        ol_flag = 0

    # range index ASCII digit; NCV uses '1' (0x31) for HI strength.
    rang_index = 0x31 if (fn == "NCV" and reading.value) else 0x30

    flag1 = 0
    if reading.rel:
        flag1 |= 0x01
    if reading.hold:
        flag1 |= 0x02
    if reading.low_battery:
        flag1 |= 0x08

    flag2 = 0
    if reading.hv_warning:
        flag2 |= 0x01
    if fn in _ACDC_FUNCTIONS and fn.startswith("AC"):
        flag2 |= 0x02
    if reading.auto:
        flag2 |= 0x08

    body = bytearray()
    body.append(func_id & 0xFF)          # [5]
    body.append(rang_index & 0xFF)       # [6]
    body.append(0x30)                    # [7] hzRangIndex (unused -> '0')
    body += value_field                  # [8..14]
    body += unit_field                   # [15..17]
    body.append(ol_flag & 0xFF)          # [18]
    body.append(0)                       # [19] maxminFlag
    body.append(flag1 & 0xFF)            # [20]
    body.append(flag2 & 0xFF)            # [21]

    return uni_t_base.build_frame_len16(
        TYPE_DATA, bytes(body),
        little_endian_chk=False, len_includes_chk=False, chk_from_zero=True)


# --- The ACK reply (TYPE 1) to a control press. The driver classifies type!=2 as
# 'control'; the app resumes polling on it. We answer control presses with a fresh
# measurement frame instead (simpler + keeps a reading on screen), so no ACK builder
# is needed; the name frame doubles as the GET_NAME control reply.
def _name_frame() -> bytes:
    # A minimal TYPE-1 frame announcing the model (the app does not parse it).
    return uni_t_base.build_frame_len16(
        1, b"UT117C", little_endian_chk=False, len_includes_chk=False)


# Poll + soft-button opcodes (frame[4] = cmd). The poll cmd is 0x05.
POLL = 0x05
CMD_SELECT = 0x01
CMD_RANGE = 0x02
CMD_REL = 0x03
CMD_MAXMIN = 0x04
CMD_HOLD = 0x12
CMD_LPF = 0x14
CMD_BACKLIGHT = 0x15

_CONTROLS = {
    CMD_SELECT: "select",
    CMD_RANGE: "range",
    CMD_REL: "rel",
    CMD_MAXMIN: "maxmin",
    CMD_HOLD: "hold",
    CMD_LPF: "lpf",
    CMD_BACKLIGHT: "ack",
}

_SELECT_CYCLE = ["DCV", "ACV", "OHM", "CONT", "DIODE", "CAP", "Hz", "DCA", "ACA"]
_ACDC_TOGGLE = {"DCV": "ACV", "ACV": "DCV", "DCA": "ACA", "ACA": "DCA"}


def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="DCV", prefix="", decimals=3)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=4.70, function="OHM", prefix="k", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACV", overload=True)


_CFG = uni_t_base.UniTProfile(
    id="ut117c",
    label="UNI-T UT117C Multimeter",
    default_name="UT117C-FAKE",
    encode=encode,
    name_frame=_name_frame(),
    # UT117C has no GET_NAME handshake (the app just polls); set an opcode that
    # never appears so SELECT (cmd 0x01) isn't shadowed. The name frame is still
    # built for completeness but is not reachable via a real command.
    get_name_op=0xFF,
    get_data_op=POLL,     # the poll cmd answered with a measurement frame
    parse_opcode=uni_t_base.parse_opcode_len16,
    controls=_CONTROLS,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=[3, 2, 1, 0],
    function_codes=sorted(_FUNCTIONS),
    initial=Reading(value=4.200, function="DCV", prefix="", decimals=3),
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
