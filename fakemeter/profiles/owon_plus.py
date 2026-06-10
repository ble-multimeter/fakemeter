"""owon-plus profile — the modern OWON BLE family (B35T+/B41T+/OW18E/CM2100B).

REAL PROTOCOL — the **6-byte little-endian R2W** measurement frame. This module is
the encoder INVERSE of the driver-repo decoder ``decodeOwonPlus`` in
``uni-t-mmu-ble/packages/protocol/src/drivers/owon-plus.ts`` (and its annotated
``docs/protocols/owon-plus.md``), which was byte-verified against the OWON BLE4.0
Android app (``com.owon.MultimeterBLE`` ``handleReceivedData_common``). The flag
bit order was corrected LSB-first (uni-t-mmu-ble commit 4506bdc) — HOLD = bit 0.

LAYERING: the OWON-shared handshake (FFF1 MD5 auth, FFF2 series gate, FFF3 button
dispatch, free-streaming) lives in ``profiles/owon_base.py``; the meter-generic
interactive state machine + value-walk + HOLD/REL/Max-Min lives in
``fakemeter/meter_core.py``. THIS module carries only the R2W-specific bits: the
6-byte LITTLE-endian encoder, the gear/prefix/state tables, the Select gear cycle,
and the FFF2 series id. Mirror of ``voltcraft.py``.

FRAME — 6 bytes, NO marker bytes, NO checksum, NO XOR scramble, free-streamed on
FFF4. One notification == one frame. Three LITTLE-endian u16 words:

    bytes[0..1]  symbols : function / scale(prefix) / point packed bitfield
                   function = (symbols >> 6) & 0x0f   (MODE_* 0..13)
                   scale    = (symbols >> 3) & 0x07   (SI-prefix index)
                   point    = symbols & 0x07          (dp pos; 6="U.L", 7="O.L")
    bytes[2..3]  mode    : annunciator bitmask, LSB-first (HOLD=bit0 …)
    bytes[4..5]  measurement : low 15 bits magnitude, bit15 = negative sign

VALUE rendering (parseMeasureValue, owon-plus.ts:122-141): the magnitude is a
digit string padded to >=4 digits, and a decimal point is inserted ``point`` chars
from the END of that string (C# ``String.Insert(Length - point, ".")``). So a
``point``-place decimal display of value V uses magnitude = round(|V| * 10**point),
with the displayed value being magnitude / 10**point. point is a 3-bit field but
6/7 are the OL/UL sentinels, so only point 0..5 carry digits.

SI prefix: scale (3-bit) indexes pre[] = [p n µ m '' k M G]; all 8 expressible.

MODE word (bytes 2..3, LE) — LSB-first annunciator bitmask. The full vendor enum
(MultimeterClient.Status) is HOLD(0) REL(1) AUTO(2) Bat(3) MIN(4) MAX(5) OL(6)
RMR(7) PMIN(8) PMAX(9) UL(10) LPF0(11) LPF1(12) VBAR(13). The dispatched decoder
only surfaces bits 0..5 (HOLD/REL/AUTO/Bat/MIN/MAX); the rest are documented for
the bit-sweep.

FLAG-BIT SWEEP: the ``bitsweep`` preset walks one mode-word bit at a time so a
phone (or the oracle) reveals each annunciator's true bit.
"""

from __future__ import annotations

from . import owon_base
from .base import Profile, Reading

# --- GATT UUIDs (the shared OWON FFF0 set; re-exported for back-compat) --------
FFF0_SERVICE = owon_base.FFF0_SERVICE
FFF4_NOTIFY = owon_base.FFF4_NOTIFY
FFF3_WRITE = owon_base.FFF3_WRITE
FFF1_SECURE = owon_base.FFF1_SECURE
FFF2_INFO = owon_base.FFF2_INFO

FRAME_LEN = 6  # R2W 6-byte record (owon-plus.ts FRAME_LEN)

# --- R2W lookup tables (inverse of owon-plus.ts) -------------------------------
#
# function code (symbols bits 6..9): the MODE_* table the app's unit selector uses
# (owon-plus.ts:144-182). Inverse mapping below (Reading.function label -> code).
#   0=V DC 1=V AC 2=A DC 3=A AC 4=Ω 5=CAP(F) 6=Hz 7=DUTY(%) 8=°C 9=°F
#   10=DIODE(V) 11=CONT(Ω) 12=hFE 13=NCV
_GEAR_MAP = {
    0:  ("DC", "V"),
    1:  ("AC", "V"),
    2:  ("DC", "A"),
    3:  ("AC", "A"),
    4:  ("", "Ω"),
    5:  ("", "F"),
    6:  ("", "Hz"),
    7:  ("", "%"),
    8:  ("", "°C"),
    9:  ("", "°F"),
    10: ("", "V"),    # DIODE
    11: ("", "Ω"),    # CONT
    12: ("", ""),     # hFE
    13: ("", ""),     # NCV
}

# Reading.function label -> function code. Mirrors voltcraft's vocabulary so the
# REPL/Select tables read the same across the OWON siblings.
FUNCTION_CODES = {
    "V_DC": 0,
    "V_AC": 1,
    "A_DC": 2,
    "A_AC": 3,
    "OHM": 4,
    "CAP": 5,
    "HZ": 6,
    "DUTY": 7,
    "TEMP_C": 8,
    "TEMP_F": 9,
    "DIODE": 10,
    "CONT": 11,
    "HFE": 12,
    "NCV": 13,
}

# SI prefix per scale index (owon-plus.ts:34 PREFIXES). Index 4 ("") is the base.
PREFIX = ["p", "n", "µ", "m", "", "k", "M", "G"]
PREFIX_CODES = {
    "p": 0, "n": 1, "µ": 2, "u": 2, "m": 3, "": 4, "k": 5, "M": 6, "G": 7,
}

# point (symbols bits 0..2): 0..5 carry digits; 6 = "U.L", 7 = "O.L".
POINT_UL = 6
POINT_OL = 7
MAX_POINT = 5  # 6/7 are reserved for the UL/OL sentinels

# mode-word annunciator bit positions (LSB-first; owon-plus.ts:165-170 + the full
# Status enum). The dispatched decoder only surfaces hold/rel/auto/low_battery/
# min/max; STATE_BITS lists those so a named flag maps 1:1 onto the oracle.
STATE_BITS = {
    "hold": 0,
    "rel": 1,
    "auto": 2,
    "low_battery": 3,
    "min": 4,
    "max": 5,
}
# Full vendor Status enum (name -> bit). bits 6/10 (OL/UL) are NOT mode-word flags
# in this frame — the OL/UL display comes from the `point` field — but are part of
# the enum; bits 7..13 are not surfaced by the dispatched decoder.
STATE_BITS_ALL = {
    "HOLD": 0, "REL": 1, "AUTO": 2, "Bat": 3, "MIN": 4, "MAX": 5, "OL": 6,
    "RMR": 7, "PMIN": 8, "PMAX": 9, "UL": 10, "LPF0": 11, "LPF1": 12, "VBAR": 13,
}

# The measurement magnitude is the low 15 bits (bit15 is the sign).
MAX_MAGNITUDE = 0x7FFF


def _le16(word: int) -> tuple[int, int]:
    """Split a 16-bit value into 2 LITTLE-endian bytes (lo, hi)."""
    return (word & 0xFF, (word >> 8) & 0xFF)


def _encode_symbols(fn_code: int, scale: int, point: int) -> int:
    """Pack the 16-bit symbols word (inverse of the >>6/>>3/&7 unpacking)."""
    return ((fn_code & 0x0F) << 6) | ((scale & 0x07) << 3) | (point & 0x07)


def _encode_measurement(magnitude: int, negative: bool) -> int:
    """Pack the 16-bit measurement word: low 15 bits magnitude, bit15 = sign."""
    word = magnitude & MAX_MAGNITUDE
    if negative:
        word |= 0x8000
    return word


def _value_to_magnitude_and_point(value: float,
                                  decimals: int | None) -> tuple[int, int, bool]:
    """Pick (magnitude, point, negative) so magnitude / 10**point == |value|.

    ``decimals`` (range-fixed) wins; else derive from the value's own fractional
    digits. point is clamped to 0..5 (6/7 are the UL/OL sentinels), and the
    magnitude is kept inside the 15-bit field (<= 32767), reducing the decimal
    place count if needed.
    """
    negative = value < 0
    mag = abs(value)

    if decimals is not None:
        point = max(0, min(MAX_POINT, decimals))
    else:
        s = f"{mag:.5f}".rstrip("0")
        point = len(s.split(".")[1]) if "." in s else 0
        point = min(MAX_POINT, point)

    while point > 0 and round(mag * (10 ** point)) > MAX_MAGNITUDE:
        point -= 1
    magnitude = round(mag * (10 ** point))
    if magnitude > MAX_MAGNITUDE:
        raise ValueError(
            f"value {value} does not fit in the 15-bit owon-plus magnitude")
    return magnitude, point, negative


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 6-byte owon-plus R2W frame.

    Inverse of ``decodeOwonPlus``. Emits the three LE u16 words: symbols (function/
    scale/point), mode (annunciator bitmask), measurement (magnitude + sign).
    """
    fn_code = FUNCTION_CODES.get(reading.function)
    if fn_code is None:
        raise ValueError(f"unknown owon-plus function {reading.function!r}; "
                         f"known: {sorted(FUNCTION_CODES)}")
    scale = PREFIX_CODES.get(reading.prefix)
    if scale is None:
        raise ValueError(f"unknown SI prefix {reading.prefix!r}; "
                         f"one of {sorted(PREFIX_CODES)}")

    # Measurement + point: OL/UL sentinels (via the point field) or a normal value.
    if reading.overload:
        point = POINT_OL
        meas_word = 0
    elif reading.underload:
        point = POINT_UL
        meas_word = 0
    else:
        magnitude, point, negative = _value_to_magnitude_and_point(
            reading.value or 0.0, reading.decimals)
        meas_word = _encode_measurement(magnitude, negative)

    symbols = _encode_symbols(fn_code, scale, point)

    # Mode word: raw override (bit-sweep) wins; else pack the named flags.
    if reading.raw_mode_word is not None:
        mode_word = reading.raw_mode_word & 0xFFFF
    else:
        mode_word = 0
        for name, bit in STATE_BITS.items():
            if getattr(reading, name):
                mode_word |= 1 << bit

    frame = bytearray(FRAME_LEN)
    frame[0:2] = _le16(symbols)
    frame[2:4] = _le16(mode_word)
    frame[4:6] = _le16(meas_word)
    return bytes(frame)


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="V_DC", prefix="", decimals=3)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=1.000, function="OHM", prefix="k", decimals=3)


def _preset_current_ma() -> Reading:
    return Reading(value=12.30, function="A_DC", prefix="m", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="V_AC", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="V_DC", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """MODE-word bit sweep: a stable 4.200 V DC base reading with exactly ONE
    mode-word bit set, walking bits 0..15. Step on a timer/keypress and read which
    annunciator the phone lights for each bit -> definitive flag map. (The mode
    word is the LITTLE-endian bytes[2..3]; LSB-first per the vendor app: bit0=HOLD,
    bit1=REL, … — see STATE_BITS_ALL.)
    """
    base = dict(value=4.200, function="V_DC", prefix="", decimals=3)
    sweep: list[Reading] = [Reading(**base, raw_mode_word=0x0000)]  # baseline
    for bit in range(16):
        sweep.append(Reading(**base, raw_mode_word=1 << bit))
    return sweep


# ---------------------------------------------------------------------------
# Interactive layer — the OWON-shared handshake + meter-generic state machine.
#
# All the family-shared logic (FFF1 MD5 auth, FFF2 info gate, FFF3 button dispatch,
# free-streaming) is in ``owon_base``; the meter-generic interactive engine lives
# in ``meter_core``. Here we supply only the R2W-specific config: the encoder, the
# Select gear cycle, the AC/DC pairs, the range cycle, and the FFF2 series id.
#
# use_auth/use_info: the OWON Android app gates on FFF1 auth + FFF2 series (same as
# the Voltcraft Flutter app), so both True. The series id selects the parser: an
# R2W (6-byte) series. The R2W ids the app maps to protocolType 0 are 18/20/33/35/
# 41/21 (see voltcraft.py's note). We pick 18 ("B35"-era); 20/41 are alternates to
# pin at validation. (35 belongs to owon-old's 14-byte ASCII path, so avoid it.)
# ---------------------------------------------------------------------------

# Select cycles the primary function through the common manual-measurement gears.
_SELECT_CYCLE = ["V_DC", "V_AC", "OHM", "CAP", "DIODE", "CONT", "HZ", "DUTY",
                 "TEMP_C", "A_DC", "A_AC"]
# Range cycles the decimal-place count (the point field) 3->2->1->0.
_RANGE_DP_CYCLE = [3, 2, 1, 0]
# AC/DC toggles between the DC and AC variant of the current voltage/current gear.
_ACDC_TOGGLE = {"V_DC": "V_AC", "V_AC": "V_DC", "A_DC": "A_AC", "A_AC": "A_DC"}

# FFF2 series id -> R2W (6-byte) parser. 18 chosen; 20/41 to pin at validation.
SERIES_ID = 18

_CFG = owon_base.OwonProfile(
    id="owon-plus",
    label="Owon (B35T+/B41T+/OW18E/CM2100B)",
    default_name="OWON-PLUS-FAKE",
    series=SERIES_ID,
    encode=encode,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=_RANGE_DP_CYCLE,
    function_codes=sorted(FUNCTION_CODES),
    initial=Reading(value=4.200, function="V_DC", prefix="", decimals=3),
    use_auth=True,
    use_info=True,
    auth_table="vc",
    presets={
        "dc_volts": _preset_dc_volts,
        "resistance_kohm": _preset_resistance_kohm,
        "current_ma": _preset_current_ma,
        "overload": _preset_overload,
        "negative": _preset_negative,
        "bitsweep": _preset_bitsweep,
    },
)
_METER = owon_base.OwonMeter(_CFG)

# Back-compat: FFF3 opcode constants (owned by owon_base) re-exported here.
CMD_SELECT = owon_base.CMD_SELECT
CMD_RANGE = owon_base.CMD_RANGE
CMD_HOLD = owon_base.CMD_HOLD
CMD_REL = owon_base.CMD_REL
CMD_MAXMIN = owon_base.CMD_MAXMIN
CMD_LPF = owon_base.CMD_LPF
CMD_4_20MA = owon_base.CMD_4_20MA
CMD_DISPLAY = owon_base.CMD_DISPLAY
CMD_COMPARE = owon_base.CMD_COMPARE
CMD_ACDC = owon_base.CMD_ACDC
CMD_MOTOR = owon_base.CMD_MOTOR


# --- thin module-level wrappers (mirror voltcraft.py) --------------------------
def set_series(series: int) -> None:
    _METER.set_series(series)


def info_response() -> bytes:
    return _METER.info_response()


def set_auth(table: str | None = None, upper: bool | None = None) -> None:
    _METER.auth.set(table=table, upper=upper)


def auth_response(written: bytes) -> bytes:
    return _METER.auth_response(written)


def reset_state(reading: Reading | None = None) -> None:
    _METER.meter.reset_state(reading)


def set_walk(on: bool) -> None:
    _METER.meter.set_walk(on)


def tick() -> None:
    _METER.meter.tick()


def current_frame() -> bytes:
    return _METER.meter.current_frame()


def command(data: bytes) -> bytes | None:
    return _METER.command(data)


# The shared Profile (built by owon_base.make_profile, wired to the live meter).
profile: Profile = owon_base.make_profile(_CFG, _METER)
