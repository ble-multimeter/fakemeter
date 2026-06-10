"""Encoder self-check: round-trip Readings through the owon-old (B35 ASCII) encoder.

Asserts value / unit / decimal-point / sign / OL / gear / prefix / flags survive
``encode`` -> the 14-byte-frame decode oracle (``decode_owon_old``). The oracle
reads the nano prefix from byte8.1 (the app-correct location) to match the
profile's CORRECTED encoding — see the module docstrings.

Run:  python -m pytest -q tests/test_owon_old.py
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import owon_old as oo  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_owon_old import decode_owon_old  # noqa: E402

_FLAG_LABEL = {
    "hold": "HOLD", "rel": "REL", "auto": "AUTO", "low_battery": "Bat",
    "min": "MIN", "max": "MAX",
}


def _decode(frame):
    return decode_owon_old(bytes(frame))


def test_frame_is_14_bytes_with_framing():
    f = oo.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert len(f) == 14
    assert f[0] in (0x2B, 0x2D)  # ASCII sign
    assert f[5] == 0x20           # space
    assert f[12] == 0x0D and f[13] == 0x0A  # CR LF


def test_worked_example_4v200_dc():
    f = oo.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    d = _decode(f)
    assert d.function == "DCV" and d.acdc == "DC"
    assert math.isclose(d.display_value, 4.200, abs_tol=1e-9)
    assert d.display_text == "4.200"
    assert d.display_unit == "V"
    assert not d.overload


def test_overload_sentinel():
    f = oo.encode(Reading(value=None, function="V_AC", overload=True))
    d = _decode(f)
    assert d.overload
    assert d.function == "ACV"


@pytest.mark.parametrize("reading,expected", [
    (Reading(value=4.200, function="V_DC", decimals=3), 4.200),
    (Reading(value=0.000, function="V_DC", decimals=3), 0.000),
    (Reading(value=-0.512, function="V_DC", decimals=3), -0.512),
    (Reading(value=12.30, function="A_DC", prefix="µ", decimals=2), 12.30),
    (Reading(value=230.0, function="V_AC", decimals=1), 230.0),
    (Reading(value=-40.0, function="TEMP_C", decimals=1), -40.0),
    (Reading(value=99.99, function="OHM", prefix="k", decimals=2), 99.99),
    (Reading(value=-1.234, function="A_AC", decimals=3), -1.234),
])
def test_value_and_sign_roundtrip(reading, expected):
    d = _decode(oo.encode(reading))
    assert d.display_value is not None
    assert math.isclose(d.display_value, expected, abs_tol=1e-4)
    assert d.display_text.startswith("-") == (expected < 0)


def test_gear_table_decodes_to_right_function():
    cases = {
        "V_DC": "DCV", "V_AC": "ACV", "A_DC": "DCA", "A_AC": "ACA",
        "OHM": "OHM", "CAP": "CAP", "HZ": "Hz", "DUTY": "%",
        "TEMP_C": "°C", "TEMP_F": "°F", "DIODE": "DIODE", "CONT": "CONT",
    }
    for fn, func in cases.items():
        d = _decode(oo.encode(Reading(value=1.0, function=fn, decimals=1)))
        assert d.function == func, f"{fn} -> {d.function}, want {func}"


@pytest.mark.parametrize("prefix,base,fn", [
    ("", "V", "V_DC"),
    ("m", "V", "V_DC"),
    ("k", "Ω", "OHM"),
    ("M", "Ω", "OHM"),
    ("µ", "A", "A_DC"),
])
def test_byte9_prefixes_roundtrip(prefix, base, fn):
    d = _decode(oo.encode(Reading(value=1.0, function=fn, prefix=prefix,
                                  decimals=1)))
    assert d.display_unit == prefix + base


def test_nano_prefix_uses_corrected_byte8_bit1():
    # nano ('n') must be encoded at byte8 bit1 (app-correct), NOT byte10.2/byte9-gate.
    f = oo.encode(Reading(value=4.700, function="CAP", prefix="n", decimals=3))
    assert (f[8] & 0x02) != 0, "nano must set byte8 bit1"
    d = _decode(f)
    assert d.display_unit == "nF"
    assert d.function == "CAP"
    assert math.isclose(d.display_value, 4.700, abs_tol=1e-9)


@pytest.mark.parametrize("flag,bit,byte_idx", [
    ("hold", 1, 7), ("rel", 2, 7), ("auto", 5, 7),
    ("max", 5, 8), ("min", 4, 8), ("low_battery", 3, 8),
])
def test_named_flag_sets_only_its_bit(flag, bit, byte_idx):
    # Use OHM (a non-AC/DC function) so byte7 carries no acdc bit to confuse the
    # isolation check.
    r = Reading(value=1.0, function="OHM", decimals=1)
    setattr(r, flag, True)
    f = oo.encode(r)
    # Only the target bit in its target byte should be set among byte7/byte8.
    assert f[byte_idx] == (1 << bit), f"{flag} should set only byte{byte_idx} bit {bit}"
    other = 8 if byte_idx == 7 else 7
    assert f[other] == 0
    assert _FLAG_LABEL[flag] in _decode(f).flags


def test_decimal_point_positions():
    # point N encodes to byte6 = 1 << (3-N); decode must recover N decimals.
    for dec in (0, 1, 2, 3):
        f = oo.encode(Reading(value=1.0, function="V_DC", decimals=dec))
        d = _decode(f)
        frac = d.display_text.split(".")
        got = len(frac[1]) if len(frac) == 2 else 0
        assert got == dec, f"decimals {dec} -> {d.display_text}"


def test_bitsweep_walks_single_bits():
    sweep = oo._preset_bitsweep()
    assert len(sweep) == 17  # baseline + bits 0..15
    assert sweep[0].raw_mode_word == 0
    for i in range(16):
        r = sweep[i + 1]
        assert r.raw_mode_word == (1 << i)
        f = oo.encode(r)
        word = f[7] | (f[8] << 8)  # byte7 = low byte, byte8 = high byte
        assert word == (1 << i)


def test_default_series_is_35():
    assert oo.info_response()[0] == oo.SERIES_ID == 35


def test_unknown_function_rejected():
    with pytest.raises(ValueError):
        oo.encode(Reading(value=1.0, function="NONSENSE"))


def test_unexpressible_prefix_rejected():
    # 'G' (giga) is not in the B35 prefix set.
    with pytest.raises(ValueError):
        oo.encode(Reading(value=1.0, function="V_DC", prefix="G"))


# --- FFF3 control-button command handler (shared interactive layer) ------------
def _dec(frame):
    return decode_owon_old(bytes(frame))


def test_hold_command_freezes_and_lights_hold():
    oo.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    frame = oo.command(bytes([oo.CMD_HOLD, 0x01]))
    assert frame is not None
    assert "HOLD" in _dec(frame).flags
    assert oo.current_frame() == frame
    assert "HOLD" not in _dec(oo.command(bytes([oo.CMD_HOLD, 0x01]))).flags


def test_select_cycles_function():
    oo.reset_state(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert _dec(oo.command(bytes([oo.CMD_SELECT, 0x01]))).function == "ACV"
