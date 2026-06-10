"""ai-care profile — AICARE Bluetooth clamp-meter family (AP-570C-APP + rebadges).

REAL PROTOCOL — the exact INVERSE of the driver decoder in
``uni-t-mmu-ble/packages/protocol/src/drivers/ai-care.ts`` (``decodeAiCare``),
ported from ``webspiderteam/Bluetooth-DMM-For-Windows`` ``Utilities.cs``
``aiCareDecode`` and byte-verified against the official "INTELLIGENT MULTIMETER"
Android app (``aicare.net.cn.iMultimeter``). See
``uni-t-mmu-ble/docs/protocols/ai-care.md``.

LAYERING (per ``docs/adding-a-profile.md`` Recipe B): ai-care has NO OWON FFF1
auth / FFF2 info gate, and owns its OWN GATT service FFB0 (notify FFB2, write
FFB1). We build the :class:`Profile` directly with ``secure_uuid``/``info_uuid`` =
None, ``interaction='stream'``, and wire HOLD/REL/walk through
``meter_core.InteractiveMeter``.

FRAME — 14 bytes, free-streamed on FFB2, no checksum/handshake. Each byte is
SELF-ADDRESSING: high nibble = 1-based slot index, low nibble = 4 payload bits.
The 14 payload nibbles concatenated in addressed order form a 56-bit field, from
which AC/DC/AUTO/BT flags (bits 0..3), four 7-segment digits (point/sign bit + 7
segs at offsets 4,12,20,28) and unit/status bits (offsets >= 36) are read.

ENCODE = build the 56-bit field from the Reading, slice it into 14 four-bit
nibbles, and scatter each nibble into its addressed byte:
``byte_i = ((i + 1) << 4) | nibble_i``.

ai-care surfaces FEWER flags than the generic Reading: only hold/rel/auto/
low_battery (NO max/min/peak), so STATE_BITS covers exactly those.
"""

from __future__ import annotations

from .. import meter_core
from .base import Profile, Reading

# --- GATT UUIDs (ai-care owns FFB0; NO secure/info char) -----------------------
FFB0_SERVICE = "0000ffb0-0000-1000-8000-00805f9b34fb"
FFB2_NOTIFY = "0000ffb2-0000-1000-8000-00805f9b34fb"
FFB1_WRITE = "0000ffb1-0000-1000-8000-00805f9b34fb"

FRAME_LEN = 14
BIT_LEN = FRAME_LEN * 4  # 56

# 7-segment table (ai-care.ts SEG): key = 7 segment bits -> glyph. Inverted below.
SEG = {
    "1111101": "0",
    "0000101": "1",
    "1011011": "2",
    "0011111": "3",
    "0100111": "4",
    "0111110": "5",
    "1111110": "6",
    "0010101": "7",
    "1111111": "8",
    "0111111": "9",
    "1101000": "L",
    "0000000": "",  # blank digit (leading-zero suppression / no segments lit)
}
SEG_INV = {glyph: bits for bits, glyph in SEG.items()}

# Leading flag bits (decoded BEFORE any digit).
AC_BIT = 0
DC_BIT = 1
AUTO_BIT = 2
BT_BIT = 3  # decoded by driver but not surfaced in Reading

# Sign / decimal-point bits.
SIGN_BIT = 4
POINT_BITS = {1: 12, 2: 20, 3: 28}  # '.' preceding digit B/C/D

# Digit segment-field start offsets (7 bits each).
DIGIT_SEG_START = [5, 13, 21, 29]  # A, B, C, D

# Unit-annunciator bit offsets (ai-care.ts decode order).
UNIT_BIT = {
    "µ": 36,
    "n": 37,
    "k": 38,
    "m": 40,
    "M": 42,
    "%": 41,
    "F": 44,
    "Ω": 45,
    "A": 48,
    "V": 49,
    "Hz": 50,
    "°C": 53,
}
DIODE_BIT = 39
CONT_BIT = 43

# Status-flag bit positions (only the flags the driver surfaces from this frame).
STATE_BITS = {
    "hold": 47,
    "rel": 46,
    "low_battery": 51,
    # auto is bit 2 (a leading flag), handled separately.
}

# Reading.function -> base-unit + acdc + diode/cont. ai-care has no °F annunciator
# (only °C), so TEMP_F is omitted.
FUNCTION_SPEC = {
    "V_DC": dict(unit="V", acdc="DC"),
    "V_AC": dict(unit="V", acdc="AC"),
    "A_DC": dict(unit="A", acdc="DC"),
    "A_AC": dict(unit="A", acdc="AC"),
    "OHM": dict(unit="Ω"),
    "CAP": dict(unit="F"),
    "HZ": dict(unit="Hz"),
    "DUTY": dict(unit="%"),
    "TEMP_C": dict(unit="°C"),
    "DIODE": dict(unit="V", diode=True),
    "CONT": dict(unit="Ω", cont=True),
}
FUNCTION_CODES = sorted(FUNCTION_SPEC)


def _digit_glyphs(reading: Reading) -> tuple[list[str], list[bool], bool]:
    """Return 4 glyphs (A..D), 3 point bits (before B/C/D), and a sign bool."""
    glyphs = ["", "", "", ""]
    points = [False, False, False]  # before digit B, C, D
    negative = False

    if reading.overload:
        # "0L" — driver detects overload by an 'L'. Render digit C='0', D='L'.
        glyphs = ["", "", "0", "L"]
        return glyphs, points, False

    value = reading.value if reading.value is not None else 0.0
    negative = value < 0
    mag = abs(value)

    decimals = reading.decimals
    if decimals is None:
        s = f"{mag:.4f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
    decimals = max(0, min(3, decimals))

    scaled = round(mag * (10 ** decimals))
    digits = f"{scaled:04d}"[-4:]
    point_index = 4 - decimals  # digit that has '.' before it (1..3)

    for n in range(4):
        glyphs[n] = digits[n]
    if 1 <= point_index <= 3:
        points[point_index - 1] = True

    return glyphs, points, negative


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 14-byte self-addressing ai-care frame.

    Inverse of decodeAiCare: build the 56-bit field, slice to 14 nibbles, scatter
    each into ``((i+1)<<4) | nibble``.
    """
    bits = bytearray(BIT_LEN)

    if reading.raw_mode_word is not None:
        word = reading.raw_mode_word
        for i in range(BIT_LEN):
            if word & (1 << i):
                bits[i] = 1
        return _scatter(bits)

    spec = FUNCTION_SPEC.get(reading.function)
    if spec is None:
        raise ValueError(f"unknown ai-care function {reading.function!r}; "
                         f"known: {FUNCTION_CODES}")

    glyphs, points, negative = _digit_glyphs(reading)

    # Sign.
    if negative:
        bits[SIGN_BIT] = 1
    # Decimal points (before B/C/D => bits 12/20/28).
    for k, on in enumerate(points):
        if on:
            bits[POINT_BITS[k + 1]] = 1
    # Digit segments.
    for n in range(4):
        key = SEG_INV.get(glyphs[n])
        if key is None:
            raise ValueError(f"ai-care: glyph {glyphs[n]!r} not in 7-seg table")
        start = DIGIT_SEG_START[n]
        for k in range(7):
            bits[start + k] = int(key[k])

    # Base-unit annunciator.
    base_unit = spec["unit"]
    if base_unit in UNIT_BIT:
        bits[UNIT_BIT[base_unit]] = 1

    # SI prefix.
    prefix = reading.prefix
    if prefix in ("u",):
        prefix = "µ"
    if prefix:
        if prefix not in UNIT_BIT:
            raise ValueError(f"ai-care: SI prefix {prefix!r} not expressible")
        bits[UNIT_BIT[prefix]] = 1

    # AC/DC.
    acdc = spec.get("acdc")
    if acdc == "AC":
        bits[AC_BIT] = 1
    elif acdc == "DC":
        bits[DC_BIT] = 1

    # diode / continuity.
    if spec.get("diode"):
        bits[DIODE_BIT] = 1
    if spec.get("cont"):
        bits[CONT_BIT] = 1

    # auto (leading flag bit 2).
    if reading.auto:
        bits[AUTO_BIT] = 1

    # Status flags.
    for name, bit in STATE_BITS.items():
        if getattr(reading, name, False):
            bits[bit] = 1

    return _scatter(bits)


def _scatter(bits: bytearray) -> bytes:
    """Slice the 56-bit field into 14 nibbles and self-address each byte."""
    raw = bytearray(FRAME_LEN)
    for i in range(FRAME_LEN):
        nibble = 0
        for k in range(4):
            nibble = (nibble << 1) | bits[i * 4 + k]
        raw[i] = (((i + 1) & 0x0F) << 4) | (nibble & 0x0F)
    return bytes(raw)


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="V_DC", prefix="", decimals=3)


def _preset_ac_amps() -> Reading:
    return Reading(value=12.5, function="A_AC", prefix="", decimals=1)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=1.000, function="OHM", prefix="k", decimals=3)


def _preset_capacitance_nf() -> Reading:
    return Reading(value=47.0, function="CAP", prefix="n", decimals=1)


def _preset_overload() -> Reading:
    return Reading(value=None, function="V_AC", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="V_DC", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """Bit sweep across the 56-bit field: exactly ONE bit set per step, bits 0..55
    (raw_mode_word = 1<<bitindex). Read which annunciator/segment the phone lights."""
    sweep: list[Reading] = []
    for bit in range(BIT_LEN):
        sweep.append(Reading(value=0.0, function="V_DC", raw_mode_word=1 << bit))
    return sweep


# ---------------------------------------------------------------------------
# Interactive layer — meter-generic state machine + value-walk (meter_core).
#
# ai-care is free-streaming + receive-only (the driver exposes no controls), but
# the emulator gets HOLD/REL/Select/walk locally via ``InteractiveMeter`` so the
# bench operator can drive the demo. No captured FFB1 opcodes, so command() maps a
# synthetic single-byte opcode set onto the generic actions.
# ---------------------------------------------------------------------------
_SELECT_CYCLE = ["V_DC", "V_AC", "OHM", "CAP", "DIODE", "CONT", "HZ", "DUTY",
                 "TEMP_C", "A_DC", "A_AC"]
_ACDC_TOGGLE = {"V_DC": "V_AC", "V_AC": "V_DC", "A_DC": "A_AC", "A_AC": "A_DC"}
_RANGE_DP_CYCLE = [3, 2, 1, 0]

_CFG = meter_core.InteractiveConfig(
    encode=encode,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle=_ACDC_TOGGLE,
    range_dp_cycle=_RANGE_DP_CYCLE,
    default_decimals=3,
)
_METER = meter_core.InteractiveMeter(
    _CFG, Reading(value=4.200, function="V_DC", prefix="", decimals=3))

CMD_HOLD = 0x03
CMD_SELECT = 0x01
CMD_RANGE = 0x02
CMD_REL = 0x04
CMD_ACDC = 0x10


def command(data: bytes) -> bytes | None:
    """React to a synthetic 1-byte control opcode; return the new frame, or None.

    NOTE: ai-care's frame has no MAX/MIN, so there is no Max/Min control here (the
    flags would not survive a round-trip through the decoder).
    """
    if not data:
        return None
    op = data[0]
    if op == CMD_HOLD:
        return _METER.toggle_hold()
    if op == CMD_SELECT:
        return _METER.select_next()
    if op == CMD_RANGE:
        return _METER.range_next()
    if op == CMD_REL:
        return _METER.rel_toggle()
    if op == CMD_ACDC:
        return _METER.acdc_toggle()
    return None


def reset_state(reading: Reading | None = None) -> None:
    _METER.reset_state(reading)


def set_walk(on: bool) -> None:
    _METER.set_walk(on)


def tick() -> None:
    _METER.tick()


def current_frame() -> bytes:
    return _METER.current_frame()


profile = Profile(
    id="ai-care",
    label="AICARE clamp meter",
    service_uuid=FFB0_SERVICE,
    notify_uuid=FFB2_NOTIFY,
    write_uuid=FFB1_WRITE,
    secure_uuid=None,
    info_uuid=None,
    default_name="AICARE-FAKE",
    interaction="stream",
    encode=encode,
    presets={
        "dc_volts": _preset_dc_volts,
        "ac_amps": _preset_ac_amps,
        "resistance_kohm": _preset_resistance_kohm,
        "capacitance_nf": _preset_capacitance_nf,
        "overload": _preset_overload,
        "negative": _preset_negative,
        "bitsweep": _preset_bitsweep,
    },
    command_handler=command,
    current_frame=current_frame,
    tick=tick,
    reset_state=reset_state,
    set_walk=set_walk,
    function_codes=FUNCTION_CODES,
)
