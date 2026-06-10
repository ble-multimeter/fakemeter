"""Encoder self-check: round-trip Readings through the owon-plus R2W encoder.

Asserts value / unit / decimal-point / sign / OL-UL / gear / prefix / flags survive
``encode`` -> the 6-byte-frame decode oracle (``decode_owon_plus``, a faithful port
of ``decodeOwonPlus``). Also verifies the mode-word flag bits are LSB-first.

Run:  python -m pytest -q tests/test_owon_plus.py
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import owon_plus as op  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_owon_plus import decode_owon_plus  # noqa: E402

_FLAG_LABEL = {
    "hold": "HOLD", "rel": "REL", "auto": "AUTO", "low_battery": "Bat",
    "min": "MIN", "max": "MAX",
}


def _decode(frame):
    return decode_owon_plus(bytes(frame))


def test_frame_is_6_bytes():
    f = op.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert len(f) == 6


def test_worked_example_4v200_dc():
    # symbols: fn 0 (V DC), scale 4 (''), point 3 -> (0<<6)|(4<<3)|3 = 0x23 -> LE 23 00.
    # measurement: 4.200 with 3 dp -> magnitude 4200 = 0x1068 -> LE 68 10. mode 0.
    f = op.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert f.hex() == "230000006810"
    d = _decode(f)
    assert d.function == "DCV" and d.acdc == "DC"
    assert math.isclose(d.display_value, 4.200, abs_tol=1e-9)
    assert d.display_text == "4.200"
    assert not d.overload


def test_overload_via_point_field():
    f = op.encode(Reading(value=None, function="V_AC", overload=True))
    d = _decode(f)
    assert d.overload and d.display_text == "O.L"
    assert d.function == "ACV"


def test_underload_via_point_field():
    f = op.encode(Reading(value=None, function="OHM", underload=True))
    d = _decode(f)
    assert d.overload and d.display_text == "U.L"


@pytest.mark.parametrize("reading,expected", [
    (Reading(value=4.200, function="V_DC", decimals=3), 4.200),
    (Reading(value=0.000, function="V_DC", decimals=3), 0.000),
    (Reading(value=-0.512, function="V_DC", decimals=3), -0.512),
    (Reading(value=12.30, function="A_DC", prefix="m", decimals=2), 12.30),
    (Reading(value=230.0, function="V_AC", decimals=1), 230.0),
    (Reading(value=-40.0, function="TEMP_C", decimals=1), -40.0),
    (Reading(value=99.99, function="OHM", prefix="k", decimals=2), 99.99),
    (Reading(value=-1.234, function="A_AC", decimals=3), -1.234),
])
def test_value_and_sign_roundtrip(reading, expected):
    d = _decode(op.encode(reading))
    assert d.display_value is not None
    assert math.isclose(d.display_value, expected, abs_tol=1e-4)
    is_neg = d.display_text.startswith("-")
    assert is_neg == (expected < 0)


def test_gear_table_decodes_to_right_function():
    cases = {
        "V_DC": "DCV", "V_AC": "ACV", "A_DC": "DCA", "A_AC": "ACA",
        "OHM": "OHM", "CAP": "CAP", "HZ": "Hz", "DUTY": "%",
        "TEMP_C": "°C", "TEMP_F": "°F", "DIODE": "DIODE", "CONT": "CONT",
    }
    for fn, func in cases.items():
        d = _decode(op.encode(Reading(value=1.0, function=fn, decimals=1)))
        assert d.function == func, f"{fn} -> {d.function}, want {func}"


@pytest.mark.parametrize("prefix,glyph", [
    ("p", "p"), ("n", "n"), ("µ", "µ"), ("m", "m"), ("", ""),
    ("k", "k"), ("M", "M"), ("G", "G"),
])
def test_all_prefixes_roundtrip(prefix, glyph):
    d = _decode(op.encode(Reading(value=1.0, function="V_DC", prefix=prefix,
                                  decimals=1)))
    assert d.display_unit == glyph + "V"


@pytest.mark.parametrize("flag,bit", list(op.STATE_BITS.items()))
def test_named_flag_sets_only_its_mode_bit(flag, bit):
    r = Reading(value=1.0, function="V_DC", decimals=1)
    setattr(r, flag, True)
    f = op.encode(r)
    mode = f[2] | (f[3] << 8)  # little-endian mode word
    assert mode == (1 << bit), f"{flag} should set only mode bit {bit}"
    assert _FLAG_LABEL[flag] in _decode(f).flags


def test_flag_bits_are_lsb_first():
    # HOLD must land at bit 0 (the corrected LSB-first order), not bit 15.
    f = op.encode(Reading(value=1.0, function="V_DC", decimals=1, hold=True))
    assert (f[2] & 0x01) == 1
    assert (f[3] & 0x80) == 0  # not MSB-first


def test_bitsweep_walks_single_mode_bits():
    sweep = op._preset_bitsweep()
    assert len(sweep) == 17  # baseline + bits 0..15
    assert sweep[0].raw_mode_word == 0
    for i in range(16):
        r = sweep[i + 1]
        assert r.raw_mode_word == (1 << i)
        f = op.encode(r)
        mode = f[2] | (f[3] << 8)
        assert mode == (1 << i)


def test_default_series_is_r2w():
    # series 18 (R2W 6-byte parser). 20/41 are alternates to pin at validation.
    assert op.info_response()[0] == op.SERIES_ID == 18


def test_unknown_function_rejected():
    with pytest.raises(ValueError):
        op.encode(Reading(value=1.0, function="NONSENSE"))


# --- FFF3 control-button command handler (shared interactive layer) ------------
def _dec(frame):
    return decode_owon_plus(bytes(frame))


def test_hold_command_freezes_and_lights_hold():
    op.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    frame = op.command(bytes([op.CMD_HOLD, 0x01]))
    assert frame is not None
    assert "HOLD" in _dec(frame).flags
    assert op.current_frame() == frame
    frame2 = op.command(bytes([op.CMD_HOLD, 0x01]))
    assert "HOLD" not in _dec(frame2).flags


def test_select_cycles_function():
    op.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert _dec(op.command(bytes([op.CMD_SELECT, 0x01]))).function == "ACV"


def test_acdc_toggles_variant():
    op.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert _dec(op.command(bytes([op.CMD_ACDC, 0x01]))).acdc == "AC"
    assert _dec(op.command(bytes([op.CMD_ACDC, 0x01]))).acdc == "DC"
