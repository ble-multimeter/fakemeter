"""owon-old profile — the legacy OWON B35T (old, pre-'+') plain-ASCII protocol.

STANCE / STATUS (2026-06-11) — **LEGACY/VESTIGIAL; candidate for removal.** This
14-byte ASCII protocol is byte-correct vs its reference decoders (the Windows app's
``b35tDecodeOld`` and the OWON BLE4.0 app's B35 ASCII decode — round-trip
tests green), but it has **no real-world corroboration and no live oracle**:
  * Every *real* OWON meter anyone in the community has (B35T+, B41T+, OW18x, …)
    streams the **6-byte BINARY** format = the ``owon-plus`` profile, NOT this ASCII
    one. Confirmed against physical B35T+ hardware via the community reader
    ``github.com/53845714nF/OWON_B35T`` (gatttool-verified) — its decode is bit-exact
    with ``owon_plus``. The "+" in B35T+/B41T+ *is* the switch to binary.
  * The only app that decodes this ASCII format is the **old** OWON BLE4.0 Java app,
    which never writes the FFF4 CCCD → a BlueZ peripheral can't stream to it (the
    no-CCCD wall), so it can't live-render. The newer iMeter Flutter app writes the
    CCCD but has no ASCII decoder (it mis-applies its R2W binary parser → garbage).
  * So this profile is byte-verified only; it cannot be live-validated on any app we
    have, and likely models a meter generation that's effectively extinct in the wild.
**Decision: keep for now (the vendor BLE4.0 app proves the ASCII format once existed,
and the driver's ``looksLikeOwonOldFrame`` content-sniff cheaply recognizes it), but
CONSIDER REMOVING it later if no real B35T-ASCII hardware ever surfaces.** ``owon_plus``
is the validated OWON workhorse (iMeter-live + real-hardware-corroborated).

REAL PROTOCOL — the **14-byte ASCII** measurement frame (CR/LF terminated). This
module is the encoder INVERSE of the driver-repo decoder ``decodeOwonOld`` in
``uni-t-mmu-ble/packages/protocol/src/drivers/owon-old.ts`` (and its annotated
``docs/protocols/owon-old.md``), cross-checked against the OWON BLE4.0 Android app
(``com.owon.MultimeterBLE``) B35 ASCII decode.

NANO-PREFIX DISCREPANCY (encoded CORRECTLY here): the driver ties the nano ('n')
prefix to ``byte10 bit2 && byte9 == 0`` (owon-old.ts:142) — which renders "nF"
correctly only by coincidence (the farad bit IS byte10.2). The vendor app's
B35 ASCII decode reads nano from ``byte8 bit1`` (BIT_NANO=2) — a separate
bit. THIS ENCODER writes nano at the CORRECT location (byte8.1). The bundled oracle
``tests/decode_owon_old.py`` therefore reads nano from byte8.1 too, so the
round-trip is against the app-correct behaviour, NOT the buggy driver line.
(Consequence: feeding this profile's nano frame to the CURRENT driver would FAIL
to show 'n' unless byte9==0 also happens to hold; flagged for the validation pass.)

LAYERING: the OWON-shared handshake (FFF1 MD5 auth, FFF2 series gate, FFF3 button
dispatch, free-streaming) lives in ``profiles/owon_base.py``; the meter-generic
interactive engine lives in ``fakemeter/meter_core.py``. THIS module carries only
the B35-ASCII-specific bits: the 14-byte encoder, the gear/prefix/state tables,
the Select gear cycle, and the FFF2 series id (35). Mirror of ``voltcraft.py``.

FRAME — 14 bytes, free-streamed on FFF4, one notification == one frame:
    byte 0      sign: '+'(0x2B) or '-'(0x2D)
    bytes 1..4  four ASCII value digits '0'..'9' (or '?'…'?' = OL sentinel)
    byte 5      a literal space (0x20)
    byte 6      decimal-point position: (byte6 & 0x07) as a 4-bit field, the index
                of its first set bit = #digits after the point (0 -> none).
                point N -> byte6 = 1 << (3 - N) for N in 1..3; 0 for N == 0.
    byte 7      mode bits: HOLD(1) REL(2) AC(3) DC(4) AUTO(5)
    byte 8      MAX(5) MIN(4) Bat/low-battery(3) NANO(1)   [nano = app's BIT_NANO]
    byte 9      prefix/special: %(1) diode(2) cont(3) M(4) k(5) m(6) µ(7)
    byte 10     base unit: °F(0) °C(1) F(2) Hz(3) Ω(5) A(6) V(7)
    byte 11     vendor status/checksum (not decoded; left 0)
    bytes 12..13  CR LF (0x0D 0x0A)
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

FRAME_LEN = 14  # B35 ASCII record length (owon-old.ts FRAME_LEN)

SIGN_PLUS = 0x2B
SIGN_MINUS = 0x2D
SPACE = 0x20
CR = 0x0D
LF = 0x0A

# Function label -> (byte9 prefix/special bit, byte10 unit bit, acdc). Each entry
# encodes which UNIT bit(s) the function lights. The SI prefix is layered on
# separately (PREFIX_BIT9 / nano at byte8.1). Diode/cont live in byte9.
#
# byte10 unit bits: V=7, A=6, Ω=5, Hz=3, F=2, °C=1, °F=0.
# byte9 special bits: %=1, diode=2, cont=3.
_UNIT_BIT10 = {
    "V_DC": (7, "DC"),
    "V_AC": (7, "AC"),
    "A_DC": (6, "DC"),
    "A_AC": (6, "AC"),
    "OHM": (5, ""),
    "CAP": (2, ""),    # farad
    "HZ": (3, ""),
    "TEMP_C": (1, ""),
    "TEMP_F": (0, ""),
}
# Functions whose symbol lives in byte9 (not a byte10 unit bit): %, diode, cont.
_BYTE9_FUNC = {
    "DUTY": 1,   # '%'
    "DIODE": 2,  # diode -> renders as a V display in the driver (acdc '')
    "CONT": 3,   # continuity -> renders as Ω
}

# Full function vocabulary (mirrors the OWON siblings so the Select/REPL tables
# read the same). DIODE additionally lights the V unit bit; CONT the Ω unit bit
# (the driver's functionFor keys off the base unit + diode/cont booleans).
FUNCTION_CODES = {
    "V_DC": 0, "V_AC": 1, "A_DC": 2, "A_AC": 3, "OHM": 4, "CAP": 5,
    "HZ": 6, "DUTY": 7, "TEMP_C": 8, "TEMP_F": 9, "DIODE": 10, "CONT": 11,
}

# SI prefix -> byte9 bit (k=5, M=4, m=6, µ=7) or the special byte8.1 nano bit.
# '' (base) sets no prefix bit. Prefixes the B35 frame can express: n µ m k M.
PREFIX_BIT9 = {"µ": 7, "u": 7, "m": 6, "M": 4, "k": 5}
NANO_BIT8 = 1  # app BIT_NANO: nano ('n') prefix lives at byte 8 bit 1
PREFIX = ["", "n", "µ", "m", "k", "M"]  # representable prefixes (for hints)

MAX_POINT = 3  # 4 ASCII digits -> at most 3 decimals (X.XXX)

# mode/min-max/battery bit positions used by the named flags.
STATE_BITS = {
    "hold": ("byte7", 1),
    "rel": ("byte7", 2),
    "auto": ("byte7", 5),
    "max": ("byte8", 5),
    "min": ("byte8", 4),
    "low_battery": ("byte8", 3),
}


def _point_byte(decimals: int) -> int:
    """byte6 value for a `decimals`-place display.

    APP-TRUE encoding (matches the OWON BLE4.0 app's B35 ASCII decode, confirmed
    live): the real OWON BLE4.0 Android app reads byte6 as an **ASCII digit**, NOT a
    bitmask:
        byte6 == '1' (0x31) -> 3 decimals (X.XXX)
        byte6 == '2' (0x32) -> 2 decimals (XX.XX)
        byte6 == '4' (0x34) -> 1 decimal  (XXX.X)
        anything else        -> 0 decimals (XXXX)   (we emit '0' = 0x30)
    The driver-repo decoder ``owon-old.ts`` instead reads byte6 as a first-set-bit
    BITMASK (1/2/4 as raw values) — that disagrees with the real meter and is a
    DRIVER bug surfaced by this validation (flagged in docs/PROGRESS.md). We encode
    the app-true ASCII byte so the OWON app renders the decimal point correctly; the
    bundled oracle decodes byte6 the same (app-true) way to keep the round-trip honest.
    """
    n = max(0, min(MAX_POINT, decimals))
    return {3: 0x31, 2: 0x32, 1: 0x34, 0: 0x30}[n]


def _digits_field(magnitude: int) -> bytes:
    """Four ASCII digits for a 0..9999 magnitude (zero-padded, like C# 0000)."""
    if magnitude > 9999:
        raise ValueError(
            f"magnitude {magnitude} exceeds the 4-digit owon-old display")
    return f"{magnitude:04d}".encode("ascii")


def _value_to_magnitude_and_decimals(value: float,
                                     decimals: int | None) -> tuple[int, int, bool]:
    """Pick (magnitude, decimals, negative) so magnitude / 10**decimals == |value|,
    with magnitude in 0..9999 (4 digits) and decimals in 0..3."""
    negative = value < 0
    mag = abs(value)
    if decimals is not None:
        dec = max(0, min(MAX_POINT, decimals))
    else:
        s = f"{mag:.3f}".rstrip("0")
        dec = len(s.split(".")[1]) if "." in s else 0
        dec = min(MAX_POINT, dec)
    while dec > 0 and round(mag * (10 ** dec)) > 9999:
        dec -= 1
    magnitude = round(mag * (10 ** dec))
    if magnitude > 9999:
        raise ValueError(
            f"value {value} does not fit in the 4-digit owon-old display")
    return magnitude, dec, negative


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a 14-byte owon-old ASCII frame (inverse of decodeOwonOld)."""
    if reading.function not in FUNCTION_CODES:
        raise ValueError(f"unknown owon-old function {reading.function!r}; "
                         f"known: {sorted(FUNCTION_CODES)}")
    prefix = reading.prefix
    if prefix not in PREFIX_BIT9 and prefix != "" and prefix != "n":
        raise ValueError(
            f"prefix {prefix!r} is not expressible in the owon-old frame; "
            f"one of {sorted(set(PREFIX) | set(PREFIX_BIT9))}")

    frame = bytearray(FRAME_LEN)

    # byte 0 sign; bytes 1..4 digits; byte 5 space; byte 6 decimal point.
    if reading.overload:
        frame[0] = SIGN_PLUS
        frame[1:5] = b"?00?"  # OL sentinel: outer chars '?' (inner don't-care)
        frame[6] = 0
    else:
        magnitude, dec, negative = _value_to_magnitude_and_decimals(
            reading.value or 0.0, reading.decimals)
        frame[0] = SIGN_MINUS if negative else SIGN_PLUS
        frame[1:5] = _digits_field(magnitude)
        frame[6] = _point_byte(dec)
    frame[5] = SPACE

    # bytes 7, 8 mode / min-max / battery.
    byte7 = 0
    byte8 = 0
    if reading.raw_mode_word is not None:
        # bit-sweep escape hatch: low byte -> byte7, high byte -> byte8.
        byte7 = reading.raw_mode_word & 0xFF
        byte8 = (reading.raw_mode_word >> 8) & 0xFF
    else:
        for name, (where, bit) in STATE_BITS.items():
            if getattr(reading, name):
                if where == "byte7":
                    byte7 |= 1 << bit
                else:
                    byte8 |= 1 << bit

    # byte 9 prefix/special; byte 10 base unit; byte 8 nano prefix.
    # AC/DC also live in byte7 (AC=bit3, DC=bit4) and the driver reads acdc from
    # there, so a voltage/current function must light the matching byte7 bit.
    byte9 = 0
    byte10 = 0
    fn = reading.function
    if fn in _BYTE9_FUNC:
        byte9 |= 1 << _BYTE9_FUNC[fn]
        # DIODE displays as V, CONT as Ω (so functionFor keys correctly).
        if fn == "DIODE":
            byte10 |= 1 << 7  # V
        elif fn == "CONT":
            byte10 |= 1 << 5  # Ω
    else:
        unit_bit, acdc = _UNIT_BIT10[fn]
        byte10 |= 1 << unit_bit
        # The AC/DC annunciator bit lives in byte7 (the driver derives acdc there).
        # Skip it when a raw mode word is forced (the bit-sweep owns byte7 wholly).
        if reading.raw_mode_word is None:
            if acdc == "AC":
                byte7 |= 1 << 3
            elif acdc == "DC":
                byte7 |= 1 << 4

    # SI prefix bits (only when not OL; prefix is display-only but the frame carries
    # it, so include it). 'n' is the CORRECTED byte8.1 nano bit.
    if prefix == "n":
        byte8 |= 1 << NANO_BIT8
    elif prefix in PREFIX_BIT9:
        byte9 |= 1 << PREFIX_BIT9[prefix]

    frame[7] = byte7
    frame[8] = byte8
    frame[9] = byte9
    frame[10] = byte10
    frame[11] = 0  # vendor status/checksum (not decoded)
    frame[12] = CR
    frame[13] = LF
    return bytes(frame)


# ---------------------------------------------------------------------------
# Preset patterns.
# ---------------------------------------------------------------------------
def _preset_dc_volts() -> Reading:
    return Reading(value=4.200, function="V_DC", prefix="", decimals=3)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=1.000, function="OHM", prefix="k", decimals=3)


def _preset_capacitance_nf() -> Reading:
    # nF exercises the CORRECTED byte8.1 nano bit (not the driver's byte10.2 path).
    return Reading(value=4.700, function="CAP", prefix="n", decimals=3)


def _preset_current_ua() -> Reading:
    return Reading(value=12.30, function="A_DC", prefix="µ", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="V_AC", overload=True)


def _preset_negative() -> Reading:
    return Reading(value=-0.512, function="V_DC", decimals=3)


def _preset_bitsweep() -> list[Reading]:
    """MODE-word bit sweep: a stable 4.200 V DC base reading with exactly ONE
    mode/min-max bit set, walking byte7 bits 0..7 then byte8 bits 0..7 (encoded via
    raw_mode_word low byte -> byte7, high byte -> byte8). Step on a timer/keypress
    to read which annunciator the phone lights for each bit."""
    base = dict(value=4.200, function="V_DC", prefix="", decimals=3)
    sweep: list[Reading] = [Reading(**base, raw_mode_word=0x0000)]  # baseline
    for bit in range(16):
        sweep.append(Reading(**base, raw_mode_word=1 << bit))
    return sweep


# ---------------------------------------------------------------------------
# Interactive layer — the OWON-shared handshake + meter-generic state machine.
#
# series id 35 -> the B35 14-byte ASCII path (the OWON Android app routes
# "series 35 without flash" to the B35 ASCII decode path; see owon-ble app-reference).
# use_auth/use_info both True (the Android app gates on FFF1 + FFF2, like voltcraft).
# ---------------------------------------------------------------------------

# Select cycles the common manual-measurement gears (skip TEMP_F to stay observable).
_SELECT_CYCLE = ["V_DC", "V_AC", "OHM", "CAP", "DIODE", "CONT", "HZ", "DUTY",
                 "TEMP_C", "A_DC", "A_AC"]
_RANGE_DP_CYCLE = [3, 2, 1, 0]
_ACDC_TOGGLE = {"V_DC": "V_AC", "V_AC": "V_DC", "A_DC": "A_AC", "A_AC": "A_DC"}

SERIES_ID = 35  # B35 -> 14-byte ASCII parser

_CFG = owon_base.OwonProfile(
    id="owon-old",
    label="Owon B35T (legacy ASCII)",
    default_name="OWON-OLD-FAKE",
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
        "capacitance_nf": _preset_capacitance_nf,
        "current_ua": _preset_current_ua,
        "overload": _preset_overload,
        "negative": _preset_negative,
        "bitsweep": _preset_bitsweep,
    },
)
_METER = owon_base.OwonMeter(_CFG)

# Back-compat: FFF3 opcode constants re-exported from owon_base.
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


profile: Profile = owon_base.make_profile(_CFG, _METER)
