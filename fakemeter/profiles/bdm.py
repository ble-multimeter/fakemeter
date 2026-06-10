"""bdm profile — the "Bluetooth DMM" clone family (Aneng / BSIDE / ZOYI / BABATools).

REAL PROTOCOL — the exact INVERSE of the driver decoder in
``uni-t-mmu-ble/packages/protocol/src/drivers/bdm.ts`` (``decodeBdm``), itself
ported from ``webspiderteam/Bluetooth-DMM-For-Windows`` ``DecoderBluetoothDMM.cs``
(``BDMDecode``, 11-byte path) and byte-verified against the official "Bluetooth DMM"
Android app (``com.yscoco.wyboem``). See ``uni-t-mmu-ble/docs/protocols/bdm.md``.

LAYERING (per ``docs/adding-a-profile.md`` Recipe B): bdm has NO OWON FFF1 auth /
FFF2 info gate — it free-streams the moment the app subscribes. So we do NOT use
``owon_base``; we build the :class:`Profile` directly (service FFF0, notify FFF4,
write FFF3, ``secure_uuid``/``info_uuid`` = None, ``interaction='stream'``) and wire
HOLD/REL/walk through ``meter_core.InteractiveMeter`` ourselves.

FRAME — 11 bytes, free-streamed on FFF4. No checksum, no handshake. Each raw byte
is XOR-scrambled with a fixed 11-element key (``DATASHIFT``); the descrambled 88
bits (MSB-first per byte) carry, at fixed bit offsets:

  * a constant raw header (raw bytes ``0x1B 0x84`` => descrambled bits for byte 0/1),
  * four 7-segment digits, each a 3-bit "first" + a 4-bit "second" field at
    NON-ADJACENT offsets, plus a per-digit sign/decimal-point prefix bit,
  * unit-annunciator bits (prefix + base unit), AC/DC bits, diode/continuity bits,
  * status-flag bits (max/min/hold/rel/auto/low-battery).

ENCODE = build the 88-bit field from the Reading (digits via the 7-seg table,
unit/flag bits at their offsets), then pack MSB-first into 11 bytes and XOR with
``DATASHIFT`` to scramble.

Digit bit layout (mirror of ``decodeBdm``'s loop, n = 0..3):
  * first  (3 bits) at ``(n+3)*8 .. +3``
  * prefix (1 bit)  at ``(n+3)*8 + 3``  (n==0 => '-', n>=1 => '.')
  * second (4 bits) at ``(n+4)*8 + 4 .. +4``
"""

from __future__ import annotations

from .. import meter_core
from .base import Profile, Reading

# --- GATT UUIDs (bdm's own FFF0 set; NO secure/info char) ----------------------
FFF0_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
FFF4_NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
FFF3_WRITE = "0000fff3-0000-1000-8000-00805f9b34fb"

FRAME_LEN = 11
BIT_LEN = FRAME_LEN * 8  # 88

# XOR (de)scramble key — bdm.ts DATASHIFT (first 11 of the source's 20-byte key).
DATASHIFT = (65, 33, 115, 85, 162, 193, 50, 113, 102, 170, 59)

# Constant raw header bytes (used by the driver's framer to sync the stream).
SYNC0 = 0x1B
SYNC1 = 0x84

# Descrambled byte 2 = device-type selector the official "Bluetooth DMM" Android app
# (com.yscoco.wyboem) dispatches on. The driver/profile fixed-offset frame is the
# app's AB_300 (type 3) decode path; emit it so the app's unit/AC-DC/flag remap
# (getRightOrderTable300) lines up. The driver ignores this byte (syncs on the raw
# 0x1B 0x84 header; digits start at bit 24), so it is invisible to the oracle.
DEVICE_TYPE_AB300 = 0x03

# 7-segment table (bdm.ts SEG): key = first(3 bits) + second(4 bits) -> glyph.
# Inverted below to glyph -> 7-bit key for encoding.
SEG = {
    "0000000": " ",
    "1111110": "A",
    "0010011": "U",
    "0110101": "T",
    "0010111": "O",
    "1110101": "E",
    "1110100": "F",
    "0110001": "L",
    "0000100": "-",
    "1111011": "0",
    "0001010": "1",
    "1011101": "2",
    "1001111": "3",
    "0101110": "4",
    "1100111": "5",
    "1110111": "6",
    "1001010": "7",
    "1111111": "8",
    "1101111": "9",
}
# Inverse: glyph -> 7-bit "first+second" key. (Each glyph is unique in SEG.)
SEG_INV = {glyph: bits for bits, glyph in SEG.items()}

# Unit-annunciator bit offsets (bdm.ts decode order). The encoder sets exactly the
# bits a displayed unit string needs; the decoder re-assembles them in this order.
# We drive these from a Reading.function + Reading.prefix (see _unit_bits).
UNIT_BIT = {
    "°C": 57,
    "°F": 58,
    "n": 64,   # prefix n (cap bank)
    "F": 67,
    "%": 69,
    "A": 72,
    "m_v": 74,  # prefix m, voltage bank
    "V": 75,
    "M": 76,
    "k": 77,
    "Ω": 78,
    "Hz": 79,
    "m_cur": 84,  # prefix m, current bank
    "µ_cur": 85,  # prefix µ, current bank
    "m_cap": 65,  # prefix m, cap bank
    "µ_cap": 66,  # prefix µ, cap bank
}

AC_BIT = 68
DC_BIT = 73
DIODE_BIT = 56
CONT_BIT = 28

# Status-flag bit positions (bdm.ts flags). Reading.flag-name -> bit.
STATE_BITS = {
    "max": 71,
    "min": 70,
    "hold": 59,
    "rel": 30,
    "auto": 87,
    "low_battery": 31,
}

# Reading.function -> the base-unit annunciator bit(s) to light + acdc + diode/cont.
# A function carries a "base unit" (V/A/Ω/F/Hz/%/°C/°F) and an AC/DC mode; the SI
# prefix is added separately from Reading.prefix via _PREFIX_BIT.
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
    "TEMP_F": dict(unit="°F"),
    "DIODE": dict(unit="V", diode=True),
    "CONT": dict(unit="Ω", cont=True),
}
FUNCTION_CODES = sorted(FUNCTION_SPEC)


def _prefix_bit(prefix: str, base_unit: str) -> int | None:
    """Pick the annunciator bit for an SI prefix, choosing the right "bank".

    The bdm frame drives prefixes from whichever range bank is active: voltage uses
    bit 74 for 'm'; the current bank uses 84('m')/85('µ'); the cap bank uses
    64('n')/65('m')/66('µ'). 'k'/'M' have single bits. Returns None for '' (none).
    """
    if prefix in ("", None):
        return None
    if prefix == "k":
        return UNIT_BIT["k"]
    if prefix == "M":
        return UNIT_BIT["M"]
    if prefix == "n":
        return UNIT_BIT["n"]
    if prefix in ("µ", "u"):
        # current bank for A, cap bank for F.
        return UNIT_BIT["µ_cur"] if base_unit == "A" else UNIT_BIT["µ_cap"]
    if prefix == "m":
        if base_unit == "V":
            return UNIT_BIT["m_v"]
        if base_unit == "A":
            return UNIT_BIT["m_cur"]
        return UNIT_BIT["m_cap"]
    raise ValueError(f"bdm: SI prefix {prefix!r} not expressible in this frame")


def _digit_glyphs(reading: Reading) -> tuple[list[str], list[bool]]:
    """Return 4 glyphs + 4 prefix bits (sign/decimal point) for the LCD.

    Mirrors the driver: digit 0 prefix = '-' (sign), digits 1..3 prefix = '.'. We
    render the value as a fixed-decimals string and lay it across the 4 digits.
    """
    glyphs = [" ", " ", " ", " "]
    prefix_on = [False, False, False, False]

    if reading.overload:
        # "0L." style — driver detects overload by an 'L' in the text. Show "0L".
        glyphs = [" ", " ", "0", "L"]
        return glyphs, prefix_on

    value = reading.value if reading.value is not None else 0.0
    negative = value < 0
    mag = abs(value)

    decimals = reading.decimals
    if decimals is None:
        s = f"{mag:.4f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
    decimals = max(0, min(3, decimals))

    # Integer mantissa over 4 digits at the chosen decimal position.
    scaled = round(mag * (10 ** decimals))
    digits = f"{scaled:04d}"[-4:]  # 4 displayed digits, leading zeros kept simple

    # The decimal point sits before digit (4 - decimals). prefix bit n (n>=1) is the
    # '.' that PRECEDES digit n. The driver concatenates prefix+glyph per digit, so a
    # point before digit k lights prefix bit k.
    point_index = 4 - decimals  # digit index that has '.' before it (if 1..3)

    for n in range(4):
        glyphs[n] = digits[n]
        if 1 <= n <= 3 and n == point_index:
            prefix_on[n] = True

    if negative:
        # Sign is digit 0's prefix bit. Driver renders '-' before digit 0's glyph.
        prefix_on[0] = True

    return glyphs, prefix_on


def _set_bit(bits: bytearray, i: int) -> None:
    bits[i] = 1


def encode(reading: Reading) -> bytes:
    """Encode a Reading into an 11-byte scrambled bdm frame (inverse of decodeBdm).

    Builds the 88-bit descrambled field MSB-first, then XORs with DATASHIFT.
    """
    bits = bytearray(BIT_LEN)  # one entry per bit position, value 0/1

    # Raw header 0x1B 0x84 => after XOR the descrambled bytes are
    # 0x1B^65=0x5A, 0x84^33=0xA5. The DESCRAMBLED bits for bytes 0/1 must therefore
    # be 0x5A 0xA5 so that scramble(0x5A)^... wait: we build descrambled bits then
    # XOR with DATASHIFT to get raw. Raw byte0 must be 0x1B, so descrambled byte0 =
    # 0x1B ^ DATASHIFT[0] = 0x5A; descrambled byte1 = 0x84 ^ DATASHIFT[1] = 0xA5.
    for bit, val in enumerate(f"{0x5A:08b}"):
        bits[bit] = int(val)
    for bit, val in enumerate(f"{0xA5:08b}"):
        bits[8 + bit] = int(val)

    # Descrambled byte 2 = the DEVICE-TYPE selector the official Android app
    # (``com.yscoco.wyboem`` ``DataParsing.dealData``) dispatches on: ``1``=QB_5G,
    # ``2``=S_5G, ``3``=AB_300, ``4``=P_66. The driver/this profile's fixed bit
    # offsets + the FFF4 11-byte frame are the app's **AB_300 (type 3)** path
    # (``getRightOrderTable300`` → ``getUnit``/``getTag``). With byte2 left at 0 the
    # app falls into its S_5G ``else`` branch, which remaps the unit/flag bits
    # differently → the app would show the right DIGITS but the WRONG unit/AC-DC.
    # The driver ``bdm.ts`` ignores byte 2 (it syncs on raw ``0x1B 0x84`` and the
    # digits start at bit 24), so this is invisible to the driver oracle. Setting
    # it to 3 makes the real app decode unit/AC-DC/flags correctly.
    for bit, val in enumerate(f"{DEVICE_TYPE_AB300:08b}"):
        bits[16 + bit] = int(val)

    if reading.raw_mode_word is not None:
        # Bit-sweep: lay the raw mode word across status-relevant region. We set one
        # bit at the absolute position encoded in raw_mode_word (a single bit index).
        # Convention: raw_mode_word is a 1<<bitindex into the 88-bit field.
        word = reading.raw_mode_word
        for i in range(BIT_LEN):
            if word & (1 << i):
                bits[i] = 1
        return _pack_and_scramble(bits)

    spec = FUNCTION_SPEC.get(reading.function)
    if spec is None:
        raise ValueError(f"unknown bdm function {reading.function!r}; "
                         f"known: {FUNCTION_CODES}")

    # Digits.
    glyphs, prefix_on = _digit_glyphs(reading)
    for n in range(4):
        key = SEG_INV.get(glyphs[n])
        if key is None:
            raise ValueError(f"bdm: glyph {glyphs[n]!r} not in 7-seg table")
        first = key[:3]
        second = key[3:]
        fi = (n + 3) * 8
        for k in range(3):
            bits[fi + k] = int(first[k])
        if prefix_on[n]:
            bits[(n + 3) * 8 + 3] = 1
        si = (n + 4) * 8 + 4
        for k in range(4):
            bits[si + k] = int(second[k])

    # Base-unit annunciator.
    base_unit = spec["unit"]
    if base_unit in UNIT_BIT:
        _set_bit(bits, UNIT_BIT[base_unit])

    # SI prefix bit (bank-aware).
    pbit = _prefix_bit(reading.prefix, base_unit)
    if pbit is not None:
        _set_bit(bits, pbit)

    # AC/DC.
    acdc = spec.get("acdc")
    if acdc == "AC":
        _set_bit(bits, AC_BIT)
    elif acdc == "DC":
        _set_bit(bits, DC_BIT)

    # diode / continuity.
    if spec.get("diode"):
        _set_bit(bits, DIODE_BIT)
    if spec.get("cont"):
        _set_bit(bits, CONT_BIT)

    # Status flags.
    for name, bit in STATE_BITS.items():
        if getattr(reading, name, False):
            _set_bit(bits, bit)

    return _pack_and_scramble(bits)


def _pack_and_scramble(bits: bytearray) -> bytes:
    """Pack the 88-bit field MSB-first into 11 bytes, then XOR with DATASHIFT."""
    raw = bytearray(FRAME_LEN)
    for byte_i in range(FRAME_LEN):
        b = 0
        for k in range(8):
            b = (b << 1) | bits[byte_i * 8 + k]
        raw[byte_i] = (b ^ DATASHIFT[byte_i]) & 0xFF
    return bytes(raw)


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="V_DC", prefix="", decimals=3)


def _preset_ac_volts() -> Reading:
    return Reading(value=230.0, function="V_AC", prefix="", decimals=1)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=1.000, function="OHM", prefix="k", decimals=3)


def _preset_current_ma() -> Reading:
    return Reading(value=12.50, function="A_DC", prefix="m", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="V_AC", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="V_DC", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """Bit sweep across the 88-bit descrambled field: a stable "0.000" base with
    exactly ONE field bit set, walking bits 0..87. Read which annunciator/segment
    the phone lights for each bit. (raw_mode_word here is 1<<bitindex into the
    88-bit field; the header bits 0..15 are still forced on by encode.)"""
    sweep: list[Reading] = []
    for bit in range(BIT_LEN):
        sweep.append(Reading(value=0.0, function="V_DC", raw_mode_word=1 << bit))
    return sweep


# ---------------------------------------------------------------------------
# Interactive layer — meter-generic state machine + value-walk (meter_core).
#
# bdm is a free-streaming, receive-only family (no soft-button commands in the
# driver), but the emulator still gets HOLD/REL/Select/walk locally via
# ``InteractiveMeter`` so the bench operator can drive the demo. There are no
# captured FFF3 opcodes for this family, so command() maps a tiny synthetic opcode
# set (single byte) onto the generic actions; an app that never writes simply
# free-streams ``current_frame()``.
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

# Synthetic single-byte opcodes (no captured bdm controls; emulator-local only).
CMD_HOLD = 0x03
CMD_SELECT = 0x01
CMD_RANGE = 0x02
CMD_REL = 0x04
CMD_MAXMIN = 0x06
CMD_ACDC = 0x10


def command(data: bytes) -> bytes | None:
    """React to a synthetic 1-byte control opcode; return the new frame, or None."""
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
    if op == CMD_MAXMIN:
        return _METER.maxmin_next()
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
    id="bdm",
    label="Bluetooth DMM (Aneng/BSIDE/ZOYI)",
    service_uuid=FFF0_SERVICE,
    notify_uuid=FFF4_NOTIFY,
    write_uuid=FFF3_WRITE,
    secure_uuid=None,
    info_uuid=None,
    default_name="BDM-FAKE",
    interaction="stream",
    encode=encode,
    presets={
        "dc_volts": _preset_dc_volts,
        "ac_volts": _preset_ac_volts,
        "resistance_kohm": _preset_resistance_kohm,
        "current_ma": _preset_current_ma,
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
