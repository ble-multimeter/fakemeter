"""ut202bt profile — UNI-T UT202BT AC clamp meter.

The UT202BT streams the SAME generic AB-CD 19-byte frame as the UT60BT and is
decoded by the same routine in the UNI-T Smart Measure app (driver repo's
``ut202bt.ts`` reuses the shared ``decode()`` + ``FrameParser`` + handshake). So
this profile REUSES ``uni_t``'s encoder + name/measurement seam wholesale and only
differs in the advertised name, the clamp-relevant presets, and the soft-button set
(the UT202BT panel exposes RANGE + HOLD generically; its ACA/ACV/Ω/NCV direct
function-select keys are clamp-specific and left out, matching the driver).

verification: ported-unverified — the decode path is the live-tested UT60BT one, but
no physical UT202BT clamp was bench-tested.
"""

from __future__ import annotations

from . import uni_t, uni_t_base
from .base import Profile, Reading

# Reuse the UT60BT 19-byte encoder unchanged (same frame, same decoder).
encode = uni_t.encode


def _preset_ac_amps() -> Reading:
    return Reading(value=12.34, function="ACA", prefix="", decimals=2)


def _preset_ac_volts() -> Reading:
    return Reading(value=230.0, function="ACV", prefix="", decimals=1)


def _preset_resistance_kohm() -> Reading:
    return Reading(value=4.70, function="OHM", prefix="k", decimals=2)


def _preset_overload() -> Reading:
    return Reading(value=None, function="ACA", overload=True)


def _preset_bitsweep() -> list[Reading]:
    base = dict(value=12.34, function="ACA", prefix="", decimals=2)
    sweep = [Reading(**base, raw_mode_word=0)]
    for bit in range(24):
        sweep.append(Reading(**base, raw_mode_word=1 << bit))
    return sweep


# Only the controls shared with the UT60BT panel are exposed (per the driver).
_CONTROLS = {
    uni_t.CMD_RANGE: "range",
    uni_t.CMD_HOLD: "hold",
}

_CFG = uni_t_base.UniTProfile(
    id="ut202bt",
    label="UNI-T UT202BT Clamp",
    default_name="UT202BT-FAKE",
    encode=encode,
    name_frame=uni_t._name_frame("UT202BT"),
    get_name_op=uni_t.GET_NAME,
    get_data_op=uni_t.GET_DATA,
    parse_opcode=uni_t_base.parse_opcode_len8,
    controls=_CONTROLS,
    select_cycle=["ACA", "ACV", "OHM"],
    acdc_toggle={},
    range_dp_cycle=[2, 1, 0],
    function_codes=sorted(uni_t.FUNCTION_CODES),
    initial=Reading(value=12.34, function="ACA", prefix="", decimals=2),
    presets={
        "ac_amps": _preset_ac_amps,
        "ac_volts": _preset_ac_volts,
        "resistance_kohm": _preset_resistance_kohm,
        "overload": _preset_overload,
        "bitsweep": _preset_bitsweep,
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
