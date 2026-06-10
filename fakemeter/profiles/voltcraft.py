"""voltcraft profile — Voltcraft VC915/VC925 (OWON "iMeter" rebadge) BLE multimeter.

REAL PROTOCOL — reverse-engineered from the official Voltcraft "series800" app
(``com.voltcraft.series800`` / in-app ``com.owon.imeter`` v1.2.5), blutter dump at
``/tmp/vc125-out``. See ``docs/voltcraft-measurement-protocol.md`` for the full
annotated derivation. THIS SUPERSEDES the layout in the driver repo's
``packages/protocol/src/drivers/voltcraft.ts``, which decodes a *different*,
third-party (Windows-app) protocol generation and is wrong for the real meter.

LAYERING (post-refactor): the OWON-shared handshake (FFF1 MD5 auth, FFF2 series
gate, FFF3 button dispatch, free-streaming) lives in ``profiles/owon_base.py``;
the meter-generic interactive state machine + value-walk + HOLD/REL/Max-Min lives
in ``fakemeter/meter_core.py``. THIS module carries only the R10W-specific bits:
the 15-byte LITTLE-endian encoder, the gear/prefix/state tables, the Select gear
cycle, and series id 91. owon-plus / owon-old will be the same shape (their own
encoder + series id on top of ``owon_base``).

The app selects a parser from the FFF2 series id via ``owonMultimeterModels``:
  * series 18/20/33/35/41/21 -> protocolType 0 -> **R2W** parser (6-byte records,
    16-bit big-endian fields). These are OWON OW18x / "B33/B35/B41" / CM2100.
  * series 91(VC915) 92(VC925) 83/85/87/89(VC831/851/871/891) 61/65/67/69/101
    -> protocolType 1 -> **R10W** parser (15-byte records, 24-bit LITTLE-endian
    fields). THE VOLTCRAFT VC915/VC925 ARE HERE.

So the emulator must report **series 91 (VC915)** and stream **R10W 15-byte
frames**. (Reporting 41 made the app treat us as an OWON "B41" expecting the R2W
6-byte format, so our old 15-byte frame was parsed as garbage -> "disconnected".)

R10W FRAME — 15 bytes, NO marker bytes, NO checksum, free-streamed on FFF4 (no
command handshake; the SCPI ``*READ?``/``*STOP`` commands are OFFLINE record
download only). One notification == one frame. Each field is a 24-bit
**LITTLE-ENDIAN** word (``b[i] | b[i+1]<<8 | b[i+2]<<16``):

    bytes[0..2]   PRIMARY gear/symbols word
    bytes[3..5]   PRIMARY value word
    bytes[6..8]   SECONDARY gear/symbols word (parsed only if primary bit12 set)
    bytes[9..11]  SECONDARY value word
    bytes[12..14] STATE / annunciator bitmask word

ALL OF THE BELOW WAS CONFIRMED LIVE (2026-06-10) by streaming raw frames to the
real Voltcraft series800 app and reading its display — see the table block lower
down. Two corrections vs. the earlier decompile-guessed layout: the words are
LITTLE-endian (not big-endian), and the dp/prefix/gear fields use CONSECUTIVE
codes (0,1,2,3,…), not even-only codes.

GEAR/SYMBOLS word (LE), parseGearAndCountingUnit:
    bits 0..2   decimal-place count (the code IS the #decimals: 0->0, 1->1dp, …)
    bits 3..5   SI-prefix code: 0=p 1=n 2=µ 3=m 4=(none) 5=k 6=M 7=G  (all 8 work)
    bits 6..10  gear/function code: 0=V DC 1=V AC 2=A DC 3=A AC 4=Ω 5=CAP 6=Hz
                7=DUTY 8=°C 9=°F 10=DIODE 11=CONT 12=hFE 13=NCV
    bit  12     SECONDARY-display-active (controls whether bytes[6..11] are parsed)

VALUE word (LE), parseMeasureValue:
    bits 0..18  count magnitude (count = word & 0x7FFFF, up to 524287)
    bits 20..22 over-range selector: 0=normal, 1="OL", 2="UL", 3="HI"
    bit  23 (== bit7 of the THIRD value byte b5)  SIGN: 1 => negative
    displayed value = count / 10**decimals  (then sign applied)

STATE word (LE) — a BITMASK; for each set bit the matching annunciator lights in
the app's "Mode" box (see STATE_BITS_ALL). HOLD=bit0, REL=bit1, AUTO=bit2,
Bat=bit3, MIN=bit4, MAX=bit5, AVG=bit6, RMR=bit7, Loz=bit8, LPF=bit9, Peak=bit10,
(bit11 blank), Cosφ=bit12, AC=bit13, DC=bit14, USB=bit15, Err=bit16, INRUSH=bit17,
OSC=bit18.  -> a straight LSB-numbered bitmask, so voltcraft.ts's MSB-first flag
read is WRONG.

FLAG-BIT SWEEP: the ``bitsweep`` preset walks one STATE-word bit at a time so the
phone reveals each annunciator's true bit.
"""

from __future__ import annotations

from . import owon_base
from .base import Profile, Reading

# --- GATT UUIDs (the shared OWON FFF0 set; re-exported for back-compat) --------
FFF0_SERVICE = owon_base.FFF0_SERVICE
FFF4_NOTIFY = owon_base.FFF4_NOTIFY
FFF3_WRITE = owon_base.FFF3_WRITE
FFF1_SECURE = owon_base.FFF1_SECURE  # OWON MD5 anti-counterfeit gate
FFF2_INFO = owon_base.FFF2_INFO       # device-info: series id / battery / fw

FRAME_LEN = 15  # R10W record length (ble_multimeter._r10wParseRealTimeData: cmp #0xf)

# --- R10W lookup tables (CONFIRMED by live bit-sweep against the VC915 app) -----
#
# IMPORTANT byte/bit layout (empirically confirmed 2026-06-10 by streaming raw
# frames to the real Voltcraft series800 app and reading its display):
#
#   * Each 24-bit word is built from 3 CONSECUTIVE bytes in LITTLE-ENDIAN order,
#     i.e. word = b[i] | b[i+1]<<8 | b[i+2]<<16. (The earlier "24-bit big-endian"
#     reading of the decompiled create24bits was WRONG.)
#   * The dp / prefix / gear fields are keyed with CONSECUTIVE integer codes
#     (0,1,2,3,…), NOT the even-only (0,2,4,…) keys previously assumed.
#
# Confirmed mappings (each verified on-screen):
#   gear code (gear-word bits 6..10): 0=V DC, 1=V AC, 2=A DC, 3=A AC, 4=Ω(RES),
#     5=CAP, 6=Hz, 7=DUTY, 8=TEMP °C, 9=TEMP °F, 10=DIODE, 11=CONT, 12=hFE, 13=NCV
#   prefix code (gear-word bits 3..5): 0=p, 1=n, 2=µ, 3=m, 4=(none), 5=k, 6=M, 7=G
#     -> ALL EIGHT prefixes ARE expressible (the old "only p/n/µ/m" claim was wrong)
#   decimals (gear-word bits 0..2): the raw code IS the number of decimal places
#     (0->0, 1->1dp, 2->2dp, 3->3dp, …); displayed value = count / 10**decimals
#   value (value-word bits 0..18): count magnitude
#   over-range (value-word bits 20..22): 1=OL, 2=UL, 3=HI (0=normal)
#   sign (value-word bit 23 == bit7 of the 3rd value byte b5): 1 => negative

# _GEAR_MAP: gear code -> (gearType label, base unit). Inverse of _gearUnitMap.
_GEAR_MAP = {
    0:  ("DC", "V"),    # DC volts
    1:  ("AC", "V"),    # AC volts
    2:  ("DC", "A"),    # DC amps
    3:  ("AC", "A"),    # AC amps
    4:  ("RES", "Ω"),   # resistance
    5:  ("CAP", "F"),   # capacitance
    6:  ("Hz", "Hz"),   # frequency
    7:  ("DUTY", "%"),  # duty cycle
    8:  ("TEMP", "℃"),
    9:  ("TEMP", "℉"),
    10: ("DIODE", "V"),
    11: ("CONT", "Ω"),
    12: ("hFE", ""),
    13: ("NCV", ""),
}

# Reading.function label -> gear code (consecutive).
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

# _CU_MAP: counting-unit (SI prefix) code (gear-word bits 3..5) -> prefix char.
# Display-only label; the numeric value comes from the dp decimals, not this.
_CU_MAP = {
    0: "p", 1: "n", 2: "µ", 3: "m", 4: "", 5: "k", 6: "M", 7: "G",
}
# Reading.prefix string -> counting-unit code. ALL EIGHT are expressible (3-bit
# field, codes 0..7), confirmed live (e.g. prefix 5 displays "kV", 4 displays "V").
PREFIX_CODES = {
    "p": 0, "n": 1, "µ": 2, "u": 2, "m": 3, "": 4, "k": 5, "M": 6, "G": 7,
}
PREFIX = ["p", "n", "µ", "m", "", "k", "M", "G"]  # code-index ordering

# Decimal-point: the code IS the number of decimal places (0..7 in a 3-bit field;
# 0..6 are physically meaningful for the 6-digit display).
_DECIMALS_TO_DP = {n: n for n in range(7)}  # decimals -> dp code (identity)

# VALUE-word over-range selector (bits 20..22). 0 = normal numeric.
OVERRANGE_NORMAL = 0
OVERRANGE_OL = 1  # "OL" over-load
OVERRANGE_UL = 2  # "UL" under-load
OVERRANGE_HI = 3  # "HI"

# STATE-word annunciator bit positions (LSB index into the 24-bit LITTLE-endian
# state word, bytes 12..14). CONFIRMED by live bit-sweep against the VC915 app:
# the word is a straight LSB-numbered bitmask; each set bit lights its annunciator
# in the app's top-right "Mode" box. NOTE the map starts at bit0 (HOLD), one lower
# than the earlier (decompile-guessed) numbering.
STATE_BITS = {
    "hold": 0,
    "rel": 1,
    "auto": 2,
    "low_battery": 3,   # "Bat" low-battery annunciator
    "min": 4,
    "max": 5,
    "lpf": 9,           # LPF (low-pass filter) annunciator
}
# Full confirmed state map (name -> bit index). bit11 and bits>=19 showed no
# annunciator (bit18 puts the app into its OSC/oscilloscope view).
STATE_BITS_ALL = {
    "HOLD": 0, "REL": 1, "AUTO": 2, "Bat": 3, "MIN": 4, "MAX": 5, "AVG": 6,
    "RMR": 7, "Loz": 8, "LPF": 9, "Peak": 10, "Cosφ": 12, "AC": 13, "DC": 14,
    "USB": 15, "Err": 16, "INRUSH": 17, "OSC": 18,
}


def _le24(word: int) -> tuple[int, int, int]:
    """Split a 24-bit value into 3 LITTLE-endian bytes (the R10W on-wire order).

    Confirmed live: the app reads each word as b[i] | b[i+1]<<8 | b[i+2]<<16.
    """
    return (word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF)


def _encode_gear_word(gear_code: int, prefix_code: int, dp_code: int,
                      secondary_active: bool = False) -> int:
    """Pack the 24-bit gear/symbols word (inverse of R10W parseGearAndCountingUnit)."""
    word = (dp_code & 0x07) | ((prefix_code & 0x07) << 3) | ((gear_code & 0x1F) << 6)
    if secondary_active:
        word |= 1 << 12
    return word & 0xFFFFFF


def _encode_value_word(count: int, negative: bool, overrange: int = 0) -> int:
    """Pack the 24-bit value word (inverse of R10W parseMeasureValue).

    count occupies bits 0..18 (& 0x7FFFF); bits 20..22 hold the over-range selector;
    bit 23 (== bit7 of the first/most-significant byte) is the sign.
    """
    word = count & 0x7FFFF
    word |= (overrange & 0x07) << 20
    if negative:
        word |= 1 << 23
    return word & 0xFFFFFF


def _value_to_count_and_dp(value: float,
                           decimals: int | None = None) -> tuple[int, int, bool]:
    """Pick a (count, dp_code, negative) so count / 10**dp_code == value.

    The dp field IS the decimal-place count (confirmed live), reaching 0..6 places
    on the 6-digit display. ``decimals`` (range-fixed) wins; otherwise we pick the
    smallest dp that preserves the value's fractional digits while keeping the
    count inside the 19-bit field (<= 524287).
    """
    negative = value < 0
    mag = abs(value)

    if decimals is not None:
        dec = max(0, min(6, decimals))
    else:
        s = f"{mag:.6f}".rstrip("0")
        dec = len(s.split(".")[1]) if "." in s else 0

    while dec > 0 and round(mag * (10 ** dec)) > 0x7FFFF:
        dec -= 1
    count = round(mag * (10 ** dec))
    if count > 0x7FFFF:
        raise ValueError(f"value {value} does not fit in the 19-bit R10W count")
    return count, _DECIMALS_TO_DP[dec], negative


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 15-byte R10W frame (the real VC915 measurement frame).

    Inverse of R10wProtocolParse.parseRealTimeDataOnce. Emits a single (primary)
    display; the secondary block is left zero and the secondary-active bit (gear
    bit 12) is cleared, so the app parses only the primary measurement.
    """
    gear_code = FUNCTION_CODES.get(reading.function)
    if gear_code is None:
        raise ValueError(f"unknown voltcraft function {reading.function!r}; "
                         f"known: {sorted(FUNCTION_CODES)}")
    prefix_code = PREFIX_CODES.get(reading.prefix)
    if prefix_code is None:
        raise ValueError(f"unknown SI prefix {reading.prefix!r}; "
                         f"one of {sorted(PREFIX_CODES)}")

    # Value word: over-range (OL/UL) or a normal count.
    if reading.overload:
        dp_code = 0
        value_word = _encode_value_word(0, False, OVERRANGE_OL)
    elif reading.underload:
        dp_code = 0
        value_word = _encode_value_word(0, False, OVERRANGE_UL)
    else:
        count, dp_code, negative = _value_to_count_and_dp(
            reading.value or 0.0, reading.decimals)
        value_word = _encode_value_word(count, negative, OVERRANGE_NORMAL)

    gear_word = _encode_gear_word(gear_code, prefix_code, dp_code,
                                  secondary_active=False)

    # State word: raw override (bit-sweep) wins; else pack the named flags.
    if reading.raw_mode_word is not None:
        state_word = reading.raw_mode_word & 0xFFFFFF
    else:
        state_word = 0
        for name, bit in STATE_BITS.items():
            if getattr(reading, name):
                state_word |= 1 << bit

    frame = bytearray(FRAME_LEN)
    frame[0:3] = _le24(gear_word)
    frame[3:6] = _le24(value_word)
    frame[6:9] = (0, 0, 0)       # secondary gear word (unused; bit12 cleared)
    frame[9:12] = (0, 0, 0)      # secondary value word (unused)
    frame[12:15] = _le24(state_word)
    return bytes(frame)


# Back-compat alias: old code/tests referenced MODE_BITS_LSB. The real state word
# is the same bitmask under a new name.
MODE_BITS_LSB = STATE_BITS


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    # 4.200 V DC (unprefixed). All 8 prefixes are expressible; default '' -> "V".
    return Reading(value=4.200, function="V_DC", prefix="", decimals=3)


def _preset_resistance_mohm() -> Reading:
    # 1.000 MΩ — prefix 'M' is now expressible, shows a clean "MΩ".
    return Reading(value=1.000, function="OHM", prefix="M", decimals=3)


def _preset_current_ua() -> Reading:
    # 12.30 µA DC — clean "µA".
    return Reading(value=12.30, function="A_DC", prefix="µ", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="V_AC", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="V_DC", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """STATE-word bit sweep: a stable 4.200 V DC base reading with exactly ONE
    state-word bit set, walking bits 0..23. Step on a timer/keypress and read which
    annunciator the phone lights for each bit -> definitive flag map. (The state
    word is the LITTLE-endian bytes[12..14]; CONFIRMED live: bit0=HOLD … bit18=OSC,
    with bit11 and bits>=19 blank — see STATE_BITS_ALL.)
    """
    base = dict(value=4.200, function="V_DC", prefix="", decimals=3)
    sweep: list[Reading] = []
    sweep.append(Reading(**base, raw_mode_word=0x000000))  # baseline, no bit set
    for bit in range(24):
        sweep.append(Reading(**base, raw_mode_word=1 << bit))
    return sweep


# ---------------------------------------------------------------------------
# Interactive layer — the OWON-shared handshake + meter-generic state machine.
#
# All the family-shared logic (FFF1 MD5 auth, FFF2 info gate, FFF3 button dispatch,
# free-streaming) is in ``owon_base``; the meter-generic interactive engine (HOLD/
# REL/Max-Min/Select/AC-DC/Range + the value-walk) is in ``meter_core``. Here we
# only supply the R10W-specific config: the encoder, the Select gear cycle, the
# AC/DC pairs, the range cycle, and series 91.
#
# FFF3 button -> opcode map (captured live; see docs/PROGRESS.md): 0x01 Select,
# 0x02 Range, 0x03 Hold, 0x04 Rel, 0x06 Max/Min, 0x07 LPF, 0x0a 4~20mA, 0x0c
# Display, 0x0f Compare, 0x10 AC/DC, 0x11 Motor. (These constants live in
# ``owon_base`` now; re-exported below for back-compat with old code/tests.)
# ---------------------------------------------------------------------------

# Select cycles the primary function through the meter's logical gears. We walk the
# common manual-measurement gears (skip TEMP_F/NCV/HFE specials to stay observable).
_SELECT_CYCLE = ["V_DC", "V_AC", "OHM", "CAP", "DIODE", "CONT", "HZ", "DUTY",
                 "TEMP_C", "A_DC", "A_AC"]
# Range cycles the decimal-place count (the dp field) 3->2->1->0.
_RANGE_DP_CYCLE = [3, 2, 1, 0]
# AC/DC toggles between the DC and AC variant of the current voltage/current gear.
_ACDC_TOGGLE = {"V_DC": "V_AC", "V_AC": "V_DC", "A_DC": "A_AC", "A_AC": "A_DC"}

# Build the OWON profile config + the live meter instance (the single source of
# truth the REPL and the FFF3 buttons share).
_CFG = owon_base.OwonProfile(
    id="voltcraft",
    label="Voltcraft VC915/VC925",
    default_name="VC915-FAKE",
    series=91,                 # FFF2 series 91 -> VC915 -> R10W parser
    encode=encode,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=_RANGE_DP_CYCLE,
    function_codes=sorted(FUNCTION_CODES),
    initial=Reading(value=4.200, function="V_DC", prefix="", decimals=3),
    auth_table="vc",
    presets={
        "dc_volts": _preset_dc_volts,
        "resistance_mohm": _preset_resistance_mohm,
        "current_ua": _preset_current_ua,
        "overload": _preset_overload,
        "negative": _preset_negative,
        "bitsweep": _preset_bitsweep,
    },
)
_METER = owon_base.OwonMeter(_CFG)

# Back-compat: FFF3 opcode constants (now owned by owon_base) re-exported here so
# existing code/tests that reference vc.CMD_* keep working.
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

# Back-compat: the auth tables/helpers used to live here. The REPL + tests still
# reference vc._S1/_S2/_pick/_recover/_AUTH, so re-expose them off the shared
# OwonAuth instance.
_S1 = owon_base._S1
_S2 = owon_base._S2
_MIX_ELEMS = owon_base._MIX_ELEMS
_BASE36 = owon_base._BASE36


# --- thin module-level wrappers (keep the REPL + tests calling vc.x unchanged) ---
def set_series(series: int) -> None:
    """Set the FFF2 series id the app will read on its next (re)connect."""
    _METER.set_series(series)


def info_response() -> bytes:
    return _METER.info_response()


def set_auth(table: str | None = None, upper: bool | None = None) -> None:
    _METER.auth.set(table=table, upper=upper)


def _pick(recovered: list[int]) -> str:
    return _METER.auth.pick(recovered)


def _recover(written: bytes) -> list[int]:
    """Recover the 6 original 0..35 coordinates from the mixed challenge bytes."""
    return _METER.auth.recover(written)


def auth_response(written: bytes) -> bytes:
    """Compute the FFF1 read-back value for the challenge the app wrote (raw 16-byte
    MD5 digest for the default 'vc' scheme); b'' if too short."""
    return _METER.auth_response(written)


def reset_state(reading: Reading | None = None) -> None:
    """Reset the meter's interactive state (the REPL calls this when the user sets a
    fresh reading, so manual edits and button commands share one truth)."""
    _METER.meter.reset_state(reading)


def set_walk(on: bool) -> None:
    """Enable/disable the demo value-walk."""
    _METER.meter.set_walk(on)


def tick() -> None:
    """Advance the demo value-walk one step (per stream tick)."""
    _METER.meter.tick()


def current_frame() -> bytes:
    """The frame the meter would stream right now (frozen while HOLD is engaged)."""
    return _METER.meter.current_frame()


def command(data: bytes) -> bytes | None:
    """React to a 2-byte FFF3 control-button command; return the new frame."""
    return _METER.command(data)


# The shared Profile (built by owon_base.make_profile, wired to the live meter).
profile: Profile = owon_base.make_profile(_CFG, _METER)
