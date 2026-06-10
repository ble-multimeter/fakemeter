"""ut219p profile — UNI-T UT219P AC power / clamp meter (polled, len16-BE AB-CD).

BEST-EFFORT + PARTIALLY STUBBED. The UT219P is the most divergent UNI-T model: the
driver (``ut219p.ts``) decodes a standard live-data frame (CMDID 0x05, cmdCode 0)
whose primary value is a float32 LE at byte 19, but the title/unit come from an
``daoPos -> parameter-set`` dispatch (``UTDeviceBean.setValue``) that was NOT
decompilable and is INFERRED in the driver. The handshake is also multi-step
(device-info 0x17, then a battery probe the app gates on, then the live poll).

This profile emulates ONLY the standard ACV/ACA live-data frame: it packs the
float32 value at byte 19, a benign flags pair, and zeroed per-value overload codes,
using the BIG-endian length + LE checksum envelope the driver validates. It does
NOT reproduce the device-info/battery handshake replies, the parameter-set title
selection, the 16-value secondary table, or the waveform/harmonic frames — those are
DEFERRED until a hardware capture pins down the daoPos dispatch and the battery gate.

LIVE-DATA FRAME (CMDID 0x05, cmdCode 0), the subset we emit:
    AB CD <len16 BE> 05 00 <daoPos @6> <statusB1 @7> <statusB2 @8> ...
        <idx nibble @11 hi> ... <overload codes @13..16> ... <float32 LE @19..22> <chk16 LE>
``len`` (BE at [2..3]) counts bytes from [4]; total = len+6; chk = LE additive over
bytes[2..len+4). statusB1: bit4 HOLD, bits6-7 max/min; statusB2: bit2 HV, bit3 REL.
"""

from __future__ import annotations

import struct

from . import uni_t_base
from .base import Profile, Reading

SOF0 = uni_t_base.SOF0
SOF1 = uni_t_base.SOF1
CMD_LIVE = 0x05

# This is an AC meter: the parameter sets are ACV (V) / ACA (A). idx 1 = the primary
# line. We expose ACV and ACA only (the driver's confidently-tabled sets).
FUNCTION_CODES = ["ACV", "ACA", "Hz"]


def encode(reading: Reading) -> bytes:
    """Encode a Reading into a UT219P standard live-data frame (CMDID 5, cmdCode 0).

    Emits the inferred ACV/ACA primary line: float32 value at byte 19, idx nibble 1
    at byte 11, zeroed overload codes (byte 13..16) unless ``reading.overload`` (then
    the first 2-bit field is set to 1=OL). Inverse of the driver's ``decodeUt219p``
    standard-measurement path. daoPos picks ACA when the function is current.
    """
    fn = reading.function
    dao_pos = 2 if fn == "ACA" else 1  # driver: daoPos 2 -> ACA set, else ACV

    status_b1 = 0
    if reading.hold:
        status_b1 |= 0x10
    if reading.max:
        status_b1 |= (1 << 6)
    elif reading.min:
        status_b1 |= (2 << 6)
    if not reading.auto:
        status_b1 |= 0x02  # bit1 set = manual range

    status_b2 = 0
    if reading.rel:
        status_b2 |= 0x08
    if reading.hv_warning:
        status_b2 |= 0x04

    # idx nibble (byte 11 hi-nibble) selects the primary line; 1 = the main reading.
    idx_byte = (1 << 4)

    # 16 two-bit per-value overload codes packed MSB-first into bytes[13..16].
    # 0=normal; set the FIRST field to 1=OL when overloaded.
    codes = 0
    if reading.overload:
        codes |= (1 << 30)  # first field = top 2 bits
    code_bytes = struct.pack(">I", codes & 0xFFFFFFFF)

    value = float(reading.value) if reading.value is not None else 0.0
    main_float = struct.pack("<f", value)  # LE float32 @ byte 19

    # Build the body (everything after the opcode, i.e. starting at byte 5). The
    # driver indexes absolute byte offsets, so we lay them out exactly.
    body = bytearray()
    body.append(0x00)              # [5] cmdCode 0 = standard measurement
    body.append(dao_pos & 0xFF)    # [6] daoPos
    body.append(status_b1 & 0xFF)  # [7]
    body.append(status_b2 & 0xFF)  # [8]
    body += b"\x00\x00"            # [9..10] (range/aux, cosmetic)
    body.append(idx_byte & 0xFF)   # [11] idx nibble (hi)
    body += b"\x00"               # [12]
    body += code_bytes             # [13..16] overload codes
    body += b"\x00\x00"           # [17..18] padding to reach the float at 19
    body += main_float             # [19..22]

    return uni_t_base.build_frame_len16(
        CMD_LIVE, bytes(body),
        little_endian_chk=True, len_includes_chk=False)


def _name_frame() -> bytes:
    # Device-info reply (CMDID 0x17). The app actually parses battery/model from the
    # real reply; this stub just answers a plausible control frame so a GET-info kick
    # gets *something* back. The battery-gate handshake is DEFERRED.
    return uni_t_base.build_frame_len16(
        0x17, b"\x00", little_endian_chk=True, len_includes_chk=False)


# Opcodes. The live poll is CMDID 0x05; device-info 0x17. HOLD is app-local on this
# model (no BLE frame); LOCK/UNLOCK (0x19) are the real device freeze commands.
CMD_DEVINFO = 0x17
CMD_LOCK = 0x19

_CONTROLS = {
    # The UT219P has no AB-CD-03 soft-button scheme; HOLD is app-local. We map HOLD
    # onto the generic action anyway so the emulator's HOLD button does something
    # observable (freeze the value) for testing.
    CMD_LOCK: "hold",
}

_SELECT_CYCLE = ["ACV", "ACA"]


def _preset_ac_volts() -> Reading:
    return Reading(value=230.0, function="ACV", prefix="", decimals=1)


def _preset_ac_amps() -> Reading:
    return Reading(value=12.34, function="ACA", prefix="", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACV", overload=True)


_CFG = uni_t_base.UniTProfile(
    id="ut219p",
    label="UNI-T UT219P AC Power Clamp Meter",
    default_name="UT219P-FAKE",
    encode=encode,
    name_frame=_name_frame(),
    get_name_op=CMD_DEVINFO,
    get_data_op=CMD_LIVE,
    parse_opcode=uni_t_base.parse_opcode_len16,
    controls=_CONTROLS,
    select_cycle=_SELECT_CYCLE,
    acdc_toggle={},
    range_dp_cycle=[2, 1, 0],
    function_codes=FUNCTION_CODES,
    initial=Reading(value=230.0, function="ACV", prefix="", decimals=1),
    presets={
        "ac_volts": _preset_ac_volts,
        "ac_amps": _preset_ac_amps,
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
