"""Round-trip tests for the bdm encoder against a faithful decode oracle.

encode(Reading) -> 11 scrambled bytes -> decode_bdm() must reproduce the value,
sign, over-range, unit, AC/DC, diode/continuity and status flags. Also checks the
descramble header (raw 0x1B 0x84) and frame length.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import bdm  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_bdm import decode_bdm, descramble  # noqa: E402


def test_frame_len_and_header():
    f = bdm.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    assert len(f) == 11
    # Raw header is the constant 0x1B 0x84 used by the driver's framer.
    assert f[0] == 0x1B and f[1] == 0x84
    # Descrambled header is 0x5A 0xA5.
    bits = descramble(f)
    assert bits[0:8] == "01011010"   # 0x5A
    assert bits[8:16] == "10100101"  # 0xA5


def test_sniff_predicate():
    f = bdm.encode(Reading(value=1.0, function="V_DC", decimals=3))
    assert len(f) == 11 and f[0] == 0x1B and f[1] == 0x84


def test_dc_volts():
    f = bdm.encode(Reading(value=4.200, function="V_DC", prefix="", decimals=3))
    d = decode_bdm(f)
    assert d.display_text == "4.200"
    assert math.isclose(d.display_value, 4.200, abs_tol=1e-9)
    assert d.display_unit == "V"
    assert d.acdc == "DC"
    assert d.function == "DCV"
    assert not d.overload


def test_ac_volts():
    f = bdm.encode(Reading(value=230.0, function="V_AC", prefix="", decimals=1))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 230.0, abs_tol=1e-9)
    assert d.acdc == "AC"
    assert d.function == "ACV"
    assert d.display_unit == "V"


def test_negative():
    f = bdm.encode(Reading(value=-0.512, function="V_DC", decimals=3))
    d = decode_bdm(f)
    assert d.display_text == "-0.512"
    assert math.isclose(d.display_value, -0.512, abs_tol=1e-9)


def test_overload():
    f = bdm.encode(Reading(value=None, function="V_AC", overload=True))
    d = decode_bdm(f)
    assert d.overload
    assert d.display_value is None
    assert "L" in d.display_text


def test_resistance_kohm_prefix_and_base_value():
    f = bdm.encode(Reading(value=1.000, function="OHM", prefix="k", decimals=3))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 1.000, abs_tol=1e-9)
    assert d.display_unit == "kΩ"
    assert d.base_unit == "Ω"
    assert math.isclose(d.base_value, 1000.0, abs_tol=1e-6)
    assert d.function == "OHM"


def test_current_milliamp_prefix_bank():
    f = bdm.encode(Reading(value=12.50, function="A_DC", prefix="m", decimals=2))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 12.50, abs_tol=1e-9)
    assert d.display_unit == "mA"
    assert d.base_unit == "A"
    assert math.isclose(d.base_value, 0.0125, abs_tol=1e-9)
    assert d.acdc == "DC"
    assert d.function == "DCA"


def test_capacitance_nanofarad():
    f = bdm.encode(Reading(value=47.0, function="CAP", prefix="n", decimals=1))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 47.0, abs_tol=1e-9)
    assert d.display_unit == "nF"
    assert d.base_unit == "F"
    assert d.function == "CAP"


def test_capacitance_microfarad():
    f = bdm.encode(Reading(value=2.2, function="CAP", prefix="µ", decimals=2))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 2.2, abs_tol=1e-9)
    assert d.display_unit == "µF"
    assert d.base_unit == "F"


def test_frequency():
    f = bdm.encode(Reading(value=50.0, function="HZ", prefix="", decimals=2))
    d = decode_bdm(f)
    assert math.isclose(d.display_value, 50.0, abs_tol=1e-9)
    assert d.display_unit == "Hz"
    assert d.function == "Hz"


def test_temperature_c():
    f = bdm.encode(Reading(value=25.0, function="TEMP_C", prefix="", decimals=1))
    d = decode_bdm(f)
    assert d.display_unit == "°C"
    assert d.function == "°C"


def test_diode():
    f = bdm.encode(Reading(value=0.6, function="DIODE", decimals=3))
    d = decode_bdm(f)
    assert d.diode
    assert d.function == "DIODE"


def test_continuity():
    f = bdm.encode(Reading(value=0.5, function="CONT", decimals=3))
    d = decode_bdm(f)
    assert d.cont
    assert d.function == "CONT"


@pytest.mark.parametrize("flag", ["max", "min", "hold", "rel", "auto", "low_battery"])
def test_status_flags(flag):
    r = Reading(value=1.000, function="V_DC", decimals=3)
    setattr(r, flag, True)
    d = decode_bdm(bdm.encode(r))
    assert d.flags[flag] is True
    # And the others stay off.
    for other in ("max", "min", "hold", "rel", "auto", "low_battery"):
        if other != flag:
            assert d.flags[other] is False


def test_bitsweep_single_bit():
    sweep = bdm.profile.presets["bitsweep"]()
    assert len(sweep) == bdm.BIT_LEN
    # Each sweep entry sets exactly one field bit (plus the forced header bits).
    for i, r in enumerate(sweep):
        f = bdm.encode(r)
        bits = descramble(f)
        # header bits forced on; count set bits beyond the header region [16:88).
        body_set = sum(1 for k in range(16, bdm.BIT_LEN) if bits[k] == "1")
        if i >= 16:
            assert body_set == 1


def test_command_hold_freezes():
    bdm.reset_state(Reading(value=4.200, function="V_DC", decimals=3))
    bdm.set_walk(False)
    f0 = bdm.current_frame()
    held = bdm.command(bytes([bdm.CMD_HOLD]))
    d = decode_bdm(held)
    assert d.flags["hold"] is True
    # Releasing HOLD clears the flag.
    bdm.command(bytes([bdm.CMD_HOLD]))
    d2 = decode_bdm(bdm.current_frame())
    assert d2.flags["hold"] is False
