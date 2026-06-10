"""uni-t profile — UNI-T UT60BT / UT161 series (the generic AB-CD 19-byte frame).

REAL PROTOCOL — the modern UNI-T "iDMM 2.0" ``AB CD`` protocol, live-tested on a
physical UT60BTk (2026-06-06; see the ``ut60bt-ble-protocol`` memo). This module is
the exact INVERSE of the driver repo's ``packages/protocol/src/decode.ts``: it
encodes a :class:`Reading` into the 19-byte measurement frame the app decodes.

LAYERING: the UNI-T-shared polled AB-CD codec (ISSC GATT, frame build/checksum, the
GET_NAME/GET_DATA command seam, soft-button dispatch) lives in ``uni_t_base``; the
meter-generic interactive engine + value-walk + HOLD/REL lives in ``meter_core``.
THIS module carries only the UT60BT/UT161-specific bits: the 19-byte encoder, the
function/range tables (ported from ``types.ts``), and the soft-button opcode map.

19-BYTE MEASUREMENT FRAME (confirmed live), the inverse of ``decode``:
    [0]=0xAB [1]=0xCD [2]=len(0x10)
    [3] = function index & 0x7F  (FUNCTIONS table; bit7 unused/0)
    [4] = range index + 0x30     (ASCII digit '0'..'7' -> per-function unit/prefix)
    [5..11] = 7 ASCII display chars (sign + digits + decimal point), space-padded
    [12],[13] = bargraph (×10 + low)
    [14] flagsA: bit3 MAX, bit2 MIN, bit1 HOLD, bit0 REL
    [15] flagsB: bit2 autorange-OFF (auto = bit CLEAR), bit1 batt-low, bit0 HV
    [16] flagsC: bit3 AC (1=AC/0=DC), bit2 peak-max, bit1 peak-min, bit0 bar-polarity
    [17][18] checksum = 16-bit BIG-ENDIAN sum of bytes[0..16]

The display string is the load-bearing field: the app reads the NUMBER from the
ASCII text (``Number(displayText)``) and the UNIT from the function+range table, so
the encoder must render the value with the right sign/decimal-point and pick a range
index whose unit matches the prefix.
"""

from __future__ import annotations

from . import uni_t_base
from .base import Profile, Reading

# --- GATT (the shared ISSC set; re-exported for back-compat) ------------------
ISSC_SERVICE = uni_t_base.ISSC_SERVICE
ISSC_NOTIFY = uni_t_base.ISSC_NOTIFY
ISSC_WRITE = uni_t_base.ISSC_WRITE

FRAME_LEN = 19
LEN_BYTE = 0x10  # bytes[2]: 16 bytes follow the len byte (frame[3..18])

# --- Function table (frame[3] & 0x7F), ported verbatim from types.ts FUNCTIONS.
# Index -> function label. Codes 0..21 verified on the UT60BT; 22..30 are other
# UT-series models (unverified) but kept so the encoder can address them.
FUNCTIONS = [
    "ACV", "ACmV", "DCV", "DCmV", "Hz", "%", "OHM", "CONT", "DIODE", "CAP",
    "°C", "°F", "DCuA", "ACuA", "DCmA", "ACmA", "DCA", "ACA", "HFE", "Live",
    "NCV", "LozV", "ACA", "DCA", "LPF", "AC/DC", "LPF", "AC+DC", "LPFA",
    "AC+DC2", "INRUSH",
]
# label -> the FIRST code that yields it (so encode picks a canonical index).
FUNCTION_CODES = {}
for _i, _name in enumerate(FUNCTIONS):
    FUNCTION_CODES.setdefault(_name, _i)

# --- Range -> unit table (frame[4]-0x30 selects the unit), ported from types.ts.
RANGE_UNITS = {
    "ACV": ["V", "V", "V", "V"],
    "DCV": ["V", "V", "V", "V"],
    "LozV": ["V", "V", "V", "V"],
    "ACmV": ["mV"],
    "DCmV": ["mV"],
    "Hz": ["Hz", "Hz", "kHz", "kHz", "kHz", "MHz", "MHz", "MHz"],
    "%": ["%"],
    "OHM": ["Ω", "kΩ", "kΩ", "kΩ", "MΩ", "MΩ", "MΩ"],
    "CONT": ["Ω"],
    "DIODE": ["V"],
    "CAP": ["nF", "nF", "µF", "µF", "µF", "mF", "mF", "mF"],
    "°C": ["°C"],
    "°F": ["°F"],
    "DCuA": ["µA", "µA"],
    "ACuA": ["µA", "µA"],
    "DCmA": ["mA", "mA"],
    "ACmA": ["mA", "mA"],
    "DCA": ["A", "A"],
    "ACA": ["A", "A"],
    "HFE": [""],
    "Live": [""],
    "NCV": [""],
    "LPF": ["V", "V", "V", "V"],
    "AC/DC": ["V", "V", "V", "V"],
    "LPFA": ["V", "V", "V", "V"],
    "AC+DC": ["A", "A"],
    "AC+DC2": ["A", "A"],
    "INRUSH": ["V", "V", "V", "V"],
}

# Functions where AC/DC is meaningful — decode reads flagsC bit3 only for these.
ACDC_FUNCTIONS = {
    "ACV", "DCV", "LozV", "ACmV", "DCmV", "DCuA", "ACuA", "DCmA", "ACmA",
    "DCA", "ACA",
}

# A prefix char -> the range index whose unit carries that prefix, per function.
# Built from RANGE_UNITS so a Reading.prefix can pick a range index whose unit
# string starts with that prefix (e.g. prefix 'k' on OHM -> range index 1 = "kΩ").
_PREFIXES = {"n", "µ", "u", "m", "k", "M", "G"}


def _range_index_for(function: str, prefix: str) -> int:
    """Pick a range index whose unit carries ``prefix`` (or 0 if none/unprefixed).

    Mirrors how the meter's range knob fixes the displayed unit's prefix. ``µ`` and
    ``u`` are treated alike. The numeric value in the display string is independent
    of this (the app reads the raw number); the range index only selects the unit.
    """
    units = RANGE_UNITS.get(function, [""])
    pref = "µ" if prefix == "u" else prefix
    if pref in ("", None):
        return 0
    for idx, unit in enumerate(units):
        if unit[:1] == pref:
            return idx
    return 0


def _display_string(reading: Reading) -> str:
    """Render the 7-char ASCII LCD string (sign + digits + decimal point).

    Overload is structural ("OL" / "-OL"); a normal value is formatted with the
    Reading's forced decimal count (a real meter's dp is range-fixed). The result is
    what ``decode`` runs through ``Number()`` for the displayValue, so it must be a
    clean numeric string for non-overload readings.
    """
    if reading.overload:
        return "-OL" if (reading.value is not None and reading.value < 0) else "OL"
    if reading.underload:
        return "UL"
    value = reading.value if reading.value is not None else 0.0
    decimals = reading.decimals
    if decimals is None:
        # Derive from the value's own fractional digits (cap at 4 for the 7-char LCD).
        s = f"{abs(value):.4f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
    text = f"{value:.{max(0, decimals)}f}"
    if len(text) > 7:
        text = text[:7]
    return text


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 19-byte UT60BT/UT161 measurement frame.

    Inverse of ``decode`` (decode.ts). Picks the function code + a range index whose
    unit matches the prefix, renders the ASCII display string, packs the three flag
    bytes, and appends the 16-bit big-endian checksum.
    """
    fn = reading.function
    fn_code = FUNCTION_CODES.get(fn)
    if fn_code is None:
        raise ValueError(f"unknown uni-t function {fn!r}; known: {sorted(FUNCTION_CODES)}")

    range_index = _range_index_for(fn, reading.prefix)

    frame = bytearray(FRAME_LEN)
    frame[0] = uni_t_base.SOF0
    frame[1] = uni_t_base.SOF1
    frame[2] = LEN_BYTE
    frame[3] = fn_code & 0x7F
    frame[4] = (range_index + 0x30) & 0xFF

    # ASCII display string, space-padded into the 7-char field (bytes[5..11]).
    text = _display_string(reading)
    field = text.encode("ascii", "replace")[:7].rjust(7, b" ")
    frame[5:12] = field

    # Bargraph bytes[12..13]: leave at 0 (the analog dial is cosmetic). A future
    # tweak could derive a bar from the value.
    frame[12] = 0
    frame[13] = 0

    # flagsA (bytes[14]).
    a = 0
    if reading.max:
        a |= 0x08
    if reading.min:
        a |= 0x04
    if reading.hold:
        a |= 0x02
    if reading.rel:
        a |= 0x01
    frame[14] = a

    # flagsB (bytes[15]): bit2 = autorange-OFF, so set it when NOT auto.
    b = 0
    if not reading.auto:
        b |= 0x04
    if reading.low_battery:
        b |= 0x02
    if reading.hv_warning:
        b |= 0x01
    frame[15] = b

    # flagsC (bytes[16]): bit3 = AC (only meaningful for the V/A functions, but the
    # bit is harmless elsewhere — decode masks it off for non-ACDC functions).
    c = 0
    if fn in ACDC_FUNCTIONS and fn.startswith("AC"):
        c |= 0x08
    if reading.peak_max:
        c |= 0x04
    if reading.peak_min:
        c |= 0x02
    frame[16] = c

    # raw_mode_word escape hatch: write a single raw byte across the 3 flag bytes for
    # the bit-sweep. The 24-bit word maps onto bytes[14..16] (A=low, B=mid, C=high).
    if reading.raw_mode_word is not None:
        w = reading.raw_mode_word & 0xFFFFFF
        frame[14] = w & 0xFF
        frame[15] = (w >> 8) & 0xFF
        frame[16] = (w >> 16) & 0xFF

    hi, lo = uni_t_base.be16_checksum(bytes(frame[0:17]))
    frame[17] = hi
    frame[18] = lo
    return bytes(frame)


# --- The GET_NAME reply: the 11-byte control/name frame announcing the model. ---
# Built as AB CD <len> "UT60BT" <chk> per the memo (the app waits for this control
# frame before GET_DATA). build_frame_len8 appends the BE checksum; the app does not
# validate the control checksum, so any plausible 11-byte AB-CD frame works.
def _name_frame(name: str = "UT60BT") -> bytes:
    return uni_t_base.build_frame_len8(name.encode("ascii", "replace"))


# --- Soft-button opcodes (framing.ts COMMANDS / the driver's controls map). -----
GET_NAME = 0x5F
GET_DATA = 0x5D
CMD_MAXMIN = 0x41
CMD_RANGE = 0x46
CMD_RANGE_AUTO = 0x47
CMD_REL = 0x48
CMD_HZ_DUTY = 0x49
CMD_HOLD = 0x4A
CMD_BACKLIGHT = 0x4B
CMD_SELECT = 0x4C

_CONTROLS = {
    CMD_HOLD: "hold",
    CMD_REL: "rel",
    CMD_SELECT: "select",
    CMD_RANGE: "range",
    CMD_RANGE_AUTO: "range",
    CMD_MAXMIN: "maxmin",
    CMD_HZ_DUTY: "ack",     # toggles Hz/duty sub-mode; no generic action
    CMD_BACKLIGHT: "ack",   # backlight: acknowledged, no display change
}

# Select cycles the common manual gears (the dial is mechanical, but Select walks
# sub-functions). Range cycles the decimal-place count.
_SELECT_CYCLE = ["DCV", "ACV", "DCmV", "ACmV", "OHM", "CONT", "DIODE", "CAP",
                 "Hz", "%", "°C", "DCuA", "DCmA", "DCA", "ACA"]
_RANGE_DP_CYCLE = [3, 2, 1, 0]
_ACDC_TOGGLE = {
    "DCV": "ACV", "ACV": "DCV", "DCmV": "ACmV", "ACmV": "DCmV",
    "DCuA": "ACuA", "ACuA": "DCuA", "DCmA": "ACmA", "ACmA": "DCmA",
    "DCA": "ACA", "ACA": "DCA",
}


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="DCV", prefix="", decimals=3)


def _preset_ac_volts() -> Reading:
    return Reading(value=230.0, function="ACV", prefix="", decimals=1)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=98.5, function="OHM", prefix="k", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACV", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="DCV", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """Flag-byte sweep: a stable 4.200 V DC base with exactly ONE of the 24 flag
    bits (bytes[14..16]) set, so the phone reveals each annunciator's true bit."""
    base = dict(value=4.200, function="DCV", prefix="", decimals=3)
    sweep = [Reading(**base, raw_mode_word=0)]
    for bit in range(24):
        sweep.append(Reading(**base, raw_mode_word=1 << bit))
    return sweep


_CFG = uni_t_base.UniTProfile(
    id="uni-t",
    label="UNI-T UT60BT / UT161",
    default_name="UT60BT-FAKE",
    encode=encode,
    name_frame=_name_frame("UT60BT"),
    get_name_op=GET_NAME,
    get_data_op=GET_DATA,
    parse_opcode=uni_t_base.parse_opcode_len8,
    controls=_CONTROLS,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=_RANGE_DP_CYCLE,
    function_codes=sorted(FUNCTION_CODES),
    initial=Reading(value=4.200, function="DCV", prefix="", decimals=3),
    presets={
        "dc_volts": _preset_dc_volts,
        "ac_volts": _preset_ac_volts,
        "resistance_kohm": _preset_resistance_kohm,
        "overload": _preset_overload,
        "negative": _preset_negative,
        "bitsweep": _preset_bitsweep,
    },
)
_METER = uni_t_base.UniTMeter(_CFG)


# --- thin module-level wrappers (mirror voltcraft.py so the REPL stays uniform) ---
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
