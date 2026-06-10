"""Encoder self-check: round-trip readings through the REAL VC915 R10W encoder.

Asserts value / unit-base / decimal-point / sign / over-range / flags survive a
round trip through ``encode`` and the R10W decode oracle (``decode_voltcraft``),
which ports the official app's parser. The SI-PREFIX label is the one lossy field
(R10W's 3-bit counting-unit field reaches only p/n/µ/m) so prefix tests use only
representable prefixes; the numeric value is asserted regardless.

Run:  pytest -q   (from the repo root, with the venv active)
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import voltcraft as vc  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_voltcraft import decode_voltcraft  # noqa: E402

# Profile's named flag -> the app's annunciator label that lights at that bit.
_FLAG_LABEL = {
    "hold": "HOLD", "rel": "REL", "auto": "AUTO", "low_battery": "Bat",
    "min": "MIN", "max": "MAX", "lpf": "LPF",
}


def test_frame_is_15_bytes_no_markers():
    f = vc.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert len(f) == 15
    # R10W has NO 0xF0 marker bytes (unlike the old voltcraft.ts format).
    # All words little-endian. gear word 0x000023 = gear 0(DC_V) | prefix 4('') |
    # dp 3(3 dec) -> LE bytes 23 00 00; value word 0x001068 = count 4200 -> LE
    # bytes 68 10 00; state word 0. (Confirmed live: this shows 4.200 V DC.)
    assert f.hex() == "230000681000000000000000000000"


def test_worked_example_4v200_dc():
    f = vc.encode(Reading(value=4.200, function="V_DC", decimals=3))
    d = decode_voltcraft(f)
    assert d.gear == "DC" and d.acdc == "DC"
    assert math.isclose(d.display_value, 4.200, abs_tol=1e-9)
    assert d.display_text == "4.200"
    assert not d.overload and not d.negative and not d.secondary_active


def test_worked_example_1k_ohm_value_correct():
    # kΩ label is not expressible (folded to µ), but the VALUE must be exact.
    f = vc.encode(Reading(value=1.000, function="OHM", prefix="M", decimals=3))
    d = decode_voltcraft(f)
    assert d.gear == "RES"
    assert math.isclose(d.display_value, 1.000, abs_tol=1e-9)
    assert d.display_text == "1.000"


def test_worked_example_overload():
    f = vc.encode(Reading(value=None, function="V_AC", overload=True))
    d = decode_voltcraft(f)
    assert d.overload and d.display_text == "OL"
    assert d.gear == "AC"


def test_worked_example_underload():
    f = vc.encode(Reading(value=None, function="OHM", underload=True))
    d = decode_voltcraft(f)
    assert d.underload and d.display_text == "UL"


@pytest.mark.parametrize("reading,expected", [
    (Reading(value=4.200, function="V_DC", decimals=3), 4.200),
    (Reading(value=0.000, function="V_DC", decimals=3), 0.000),
    (Reading(value=-0.512, function="V_DC", decimals=3), -0.512),
    (Reading(value=12.30, function="A_DC", prefix="µ", decimals=2), 12.30),
    (Reading(value=230.0, function="V_AC", decimals=1), 230.0),
    (Reading(value=-40.0, function="TEMP_C", decimals=1), -40.0),
    (Reading(value=99.99, function="OHM", prefix="m", decimals=2), 99.99),
])
def test_value_and_sign_roundtrip(reading, expected):
    d = decode_voltcraft(vc.encode(reading))
    assert d.display_value is not None
    assert math.isclose(d.display_value, expected, abs_tol=1e-4)
    assert d.negative == (expected < 0)


def test_representable_prefix_label_roundtrips():
    # µ and m ARE expressible in the 3-bit counting-unit field.
    d = decode_voltcraft(vc.encode(Reading(value=12.30, function="A_DC",
                                           prefix="µ", decimals=2)))
    assert d.display_unit == "uA"  # app glyph is 'u' for micro
    d = decode_voltcraft(vc.encode(Reading(value=4.2, function="V_DC",
                                           prefix="m", decimals=3)))
    assert d.display_unit == "mV"


def test_gear_codes_decode_to_right_function():
    cases = {
        "V_DC": "DC", "V_AC": "AC", "A_DC": "DC", "A_AC": "AC",
        "OHM": "RES", "CAP": "CAP", "HZ": "Hz", "DUTY": "DUTY",
    }
    for fn, gear in cases.items():
        d = decode_voltcraft(vc.encode(Reading(value=1.0, function=fn, decimals=1)))
        assert d.gear == gear, f"{fn} -> {d.gear}, want {gear}"


@pytest.mark.parametrize("flag,bit", list(vc.STATE_BITS.items()))
def test_named_flag_sets_only_its_state_bit(flag, bit):
    r = Reading(value=1.0, function="V_DC", decimals=1)
    setattr(r, flag, True)
    f = vc.encode(r)
    state = f[12] | (f[13] << 8) | (f[14] << 16)  # little-endian
    assert state == (1 << bit), f"{flag} should set only state bit {bit}"
    d = decode_voltcraft(f)
    assert _FLAG_LABEL[flag] in d.flags


def test_bitsweep_walks_single_state_bits():
    sweep = vc._preset_bitsweep()
    assert len(sweep) == 25  # baseline + bits 0..23
    assert sweep[0].raw_mode_word == 0
    for i in range(24):
        r = sweep[i + 1]
        assert r.raw_mode_word == (1 << i)
        f = vc.encode(r)
        state = f[12] | (f[13] << 8) | (f[14] << 16)  # little-endian
        assert state == (1 << i)


def test_default_series_is_vc915():
    assert vc.info_response()[0] == 91  # R10W parser selector


def test_auth_response_matches_app_computation():
    """The FFF1 responder must reproduce what the Voltcraft/OWON *Flutter* app
    expects so ``owonVerifies()`` returns true (else "code:3"). Unchanged by the
    measurement-frame rework; kept as a regression guard.
    """
    import hashlib
    orig = [5, 10, 0, 35, 12, 1]
    mix_elems = [200, 100, 50, 20, 10, 5]
    written = bytes((orig[i] + mix_elems[i]) & 0xFF for i in range(6))

    recovered = [(written[i] - mix_elems[i]) & 0xFF for i in range(6)]
    picked = "".join((vc._S1 if i < 3 else vc._S2)[recovered[i]] for i in range(6))
    expected = hashlib.md5(picked.encode()).digest()

    assert vc.auth_response(written) == expected
    assert len(vc.auth_response(written)) == 16


def test_auth_response_worked_example():
    written = bytes([0xCF, 0x77, 0x55, 0x2F, 0x2A, 0x14])
    assert vc._recover(written) == [7, 19, 35, 27, 32, 15]
    assert vc._pick(vc._recover(written)) == "ula2xl"
    assert vc.auth_response(written).hex() == "ba8175919934b5873ea6462bd7888dc7"


def test_unknown_function_rejected():
    with pytest.raises(ValueError):
        vc.encode(Reading(value=1.0, function="NONSENSE"))


# ---------------------------------------------------------------------------
# FFF3 control-button command handler (the interactive layer). Opcodes captured
# live from the Voltcraft series800 app; see docs/PROGRESS.md.
# ---------------------------------------------------------------------------
def _decode(frame):
    return decode_voltcraft(bytes(frame))


def test_hold_command_freezes_and_lights_hold():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    frame = vc.command(bytes([vc.CMD_HOLD, 0x01]))
    assert frame is not None
    assert "HOLD" in _decode(frame).flags
    # While held, the streamed frame stays frozen even if state would change.
    assert vc.current_frame() == frame
    # Second press releases HOLD.
    frame2 = vc.command(bytes([vc.CMD_HOLD, 0x01]))
    assert "HOLD" not in _decode(frame2).flags


def test_select_cycles_function():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    f1 = vc.command(bytes([vc.CMD_SELECT, 0x01]))
    assert _decode(f1).gear == "AC"  # V_DC -> V_AC
    f2 = vc.command(bytes([vc.CMD_SELECT, 0x01]))
    assert "Ω" in _decode(f2).display_unit  # -> OHM


def test_acdc_toggles_variant():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    f = vc.command(bytes([vc.CMD_ACDC, 0x01]))
    d = _decode(f)
    assert d.acdc == "AC"
    f2 = vc.command(bytes([vc.CMD_ACDC, 0x01]))
    assert _decode(f2).acdc == "DC"


def test_rel_zeros_value_and_lights_rel():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    f = vc.command(bytes([vc.CMD_REL, 0x01]))
    d = _decode(f)
    assert "REL" in d.flags
    assert abs(d.display_value) < 1e-9
    # Disengage restores the value.
    f2 = vc.command(bytes([vc.CMD_REL, 0x01]))
    assert "REL" not in _decode(f2).flags
    assert abs(_decode(f2).display_value - 4.200) < 1e-6


def test_maxmin_cycles_flags():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert "MAX" in _decode(vc.command(bytes([vc.CMD_MAXMIN, 0x01]))).flags
    assert "MIN" in _decode(vc.command(bytes([vc.CMD_MAXMIN, 0x01]))).flags
    # AVG step (no MAX/MIN), then back to off.
    d_avg = _decode(vc.command(bytes([vc.CMD_MAXMIN, 0x01])))
    assert "MAX" not in d_avg.flags and "MIN" not in d_avg.flags
    d_off = _decode(vc.command(bytes([vc.CMD_MAXMIN, 0x01])))
    assert "MAX" not in d_off.flags and "MIN" not in d_off.flags


def test_lpf_toggles_flag():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert "LPF" in _decode(vc.command(bytes([vc.CMD_LPF, 0x01]))).flags
    assert "LPF" not in _decode(vc.command(bytes([vc.CMD_LPF, 0x01]))).flags


def test_range_cycles_decimals():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    d1 = _decode(vc.command(bytes([vc.CMD_RANGE, 0x01])))  # 3 -> 2 dp
    assert d1.display_text.split(".")[1] == "20"  # 4.20 (2 decimals)


def test_compare_and_special_modes_no_frame():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert vc.command(bytes([vc.CMD_COMPARE, 0x01])) is None
    assert vc.command(bytes([vc.CMD_MOTOR, 0x01])) is None
    assert vc.command(bytes([vc.CMD_4_20MA, 0x01])) is None
    assert vc.command(bytes([vc.CMD_DISPLAY, 0x01])) is None


def test_unknown_opcode_ignored():
    vc.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert vc.command(bytes([0xEE, 0x01])) is None
    assert vc.command(b"") is None
