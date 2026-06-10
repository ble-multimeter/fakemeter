"""Round-trip tests for the ai-care encoder against a faithful decode oracle.

encode(Reading) -> 14 self-addressing bytes -> decode_ai_care() must reproduce the
value, sign, over-range, unit, AC/DC, diode/continuity and the flags the frame
carries (hold/rel/auto/low_battery — NO max/min/peak). Also checks the
self-addressing (high nibble = 1-based slot) and frame length.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ai_care  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_ai_care import decode_ai_care, descramble  # noqa: E402


def test_frame_len_and_self_addressing():
    f = ai_care.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert len(f) == 14
    # Each byte's high nibble is its 1-based slot index.
    for i in range(14):
        assert ((f[i] & 0xF0) >> 4) == i + 1


def test_dc_volts():
    f = ai_care.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    d = decode_ai_care(f)
    assert d.display_text == "4.200"
    assert math.isclose(d.display_value, 4.200, abs_tol=1e-9)
    assert d.display_unit == "V"
    assert d.acdc == "DC"
    assert d.function == "DCV"
    assert not d.overload


def test_ac_amps():
    f = ai_care.encode(Reading(value=12.5, function="A_AC", prefix="", decimals=1))
    d = decode_ai_care(f)
    assert math.isclose(d.display_value, 12.5, abs_tol=1e-9)
    assert d.acdc == "AC"
    assert d.display_unit == "A"
    assert d.function == "ACA"


def test_negative():
    f = ai_care.encode(Reading(value=-0.512, function="V_DC", decimals=3))
    d = decode_ai_care(f)
    assert d.display_text == "-0.512"
    assert math.isclose(d.display_value, -0.512, abs_tol=1e-9)


def test_overload():
    f = ai_care.encode(Reading(value=None, function="V_AC", overload=True))
    d = decode_ai_care(f)
    assert d.overload
    assert d.display_value is None
    assert "L" in d.display_text


def test_resistance_kohm():
    f = ai_care.encode(Reading(value=1.000, function="OHM", prefix="k", decimals=3))
    d = decode_ai_care(f)
    assert math.isclose(d.display_value, 1.000, abs_tol=1e-9)
    assert d.display_unit == "kΩ"
    assert d.base_unit == "Ω"
    assert math.isclose(d.base_value, 1000.0, abs_tol=1e-6)
    assert d.function == "OHM"


def test_capacitance_nanofarad():
    f = ai_care.encode(Reading(value=47.0, function="CAP", prefix="n", decimals=1))
    d = decode_ai_care(f)
    assert math.isclose(d.display_value, 47.0, abs_tol=1e-9)
    assert d.display_unit == "nF"
    assert d.base_unit == "F"
    assert d.function == "CAP"


def test_capacitance_microfarad():
    f = ai_care.encode(Reading(value=2.2, function="CAP", prefix="µ", decimals=2))
    d = decode_ai_care(f)
    assert math.isclose(d.display_value, 2.2, abs_tol=1e-9)
    assert d.display_unit == "µF"


def test_frequency():
    f = ai_care.encode(Reading(value=50.0, function="HZ", prefix="", decimals=2))
    d = decode_ai_care(f)
    assert math.isclose(d.display_value, 50.0, abs_tol=1e-9)
    assert d.display_unit == "Hz"
    assert d.function == "Hz"


def test_temperature_c():
    f = ai_care.encode(Reading(value=25.0, function="TEMP_C", prefix="", decimals=1))
    d = decode_ai_care(f)
    assert d.display_unit == "°C"
    assert d.function == "°C"


def test_diode():
    f = ai_care.encode(Reading(value=0.6, function="DIODE", decimals=3))
    d = decode_ai_care(f)
    assert d.diode
    assert d.function == "DIODE"


def test_continuity():
    f = ai_care.encode(Reading(value=0.5, function="CONT", decimals=3))
    d = decode_ai_care(f)
    assert d.cont
    assert d.function == "CONT"


def test_auto_flag():
    r = Reading(value=1.000, function="V_DC", decimals=3, auto=True)
    d = decode_ai_care(ai_care.encode(r))
    assert d.flags["auto"] is True


@pytest.mark.parametrize("flag", ["hold", "rel", "low_battery"])
def test_status_flags(flag):
    r = Reading(value=1.000, function="V_DC", decimals=3)
    setattr(r, flag, True)
    d = decode_ai_care(ai_care.encode(r))
    assert d.flags[flag] is True
    for other in ("hold", "rel", "low_battery"):
        if other != flag:
            assert d.flags[other] is False


def test_bitsweep_single_bit():
    sweep = ai_care.profile.presets["bitsweep"]()
    assert len(sweep) == ai_care.BIT_LEN
    for r in sweep:
        f = ai_care.encode(r)
        bits = descramble(f)
        assert sum(1 for c in bits if c == "1") == 1


def test_command_hold_freezes():
    ai_care.reset_state(Reading(value=4.200, function="V_DC", decimals=3))
    ai_care.set_walk(False)
    held = ai_care.command(bytes([ai_care.CMD_HOLD]))
    d = decode_ai_care(held)
    assert d.flags["hold"] is True
    ai_care.command(bytes([ai_care.CMD_HOLD]))
    d2 = decode_ai_care(ai_care.current_frame())
    assert d2.flags["hold"] is False
