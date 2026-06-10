"""ut171 profile tests — len16 LE AB-CD frame, in-band float32, polled seam."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ut171  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_ut171 import checksum_ok, decode_ut171  # noqa: E402


def test_frame_valid_checksum():
    f = ut171.encode(Reading(value=4.2000, function="DCV", decimals=4))
    assert f[0] == 0xAB and f[1] == 0xCD and f[4] == 0x02
    assert checksum_ok(f)


@pytest.mark.parametrize("value,function,prefix,exp_unit,exp_acdc", [
    (4.2000, "DCV", "", "V", "DC"),
    (230.00, "ACV", "", "V", "AC"),
    (4.700, "OHM", "k", "kΩ", ""),
    (1.000, "OHM", "M", "MΩ", ""),
    (12.340, "ACA", "", "A", "AC"),
])
def test_roundtrip(value, function, prefix, exp_unit, exp_acdc):
    d = decode_ut171(ut171.encode(
        Reading(value=value, function=function, prefix=prefix, decimals=3, auto=True)))
    assert d.function == function
    assert d.display_unit == exp_unit
    assert d.acdc == exp_acdc
    assert d.display_value == pytest.approx(value, abs=1e-2)


def test_negative():
    d = decode_ut171(ut171.encode(Reading(value=-1.234, function="DCV", decimals=3)))
    assert d.display_value == pytest.approx(-1.234, abs=1e-2)


def test_overload():
    d = decode_ut171(ut171.encode(Reading(value=None, function="ACV", overload=True)))
    assert d.overload is True and d.display_value is None


@pytest.mark.parametrize("flag", ["hold", "rel", "low_battery", "hv_warning"])
def test_flag_roundtrip(flag):
    r = Reading(value=4.2, function="DCV", decimals=3, auto=True)
    setattr(r, flag, True)
    assert decode_ut171(ut171.encode(r)).flags[flag] is True


def test_start_returns_measurement():
    # START (cmd 0x0A) is our get_data_op.
    resp = ut171.command(bytes([0xab, 0xcd, 0x01, 0x00, 0x0a, 0x0a, 0x00]))
    assert resp is not None and checksum_ok(resp)


def test_profile_polled():
    assert ut171.profile.interaction == "polled"


def test_presets_encode():
    for factory in ut171.profile.presets.values():
        out = factory()
        for r in (out if isinstance(out, list) else [out]):
            assert checksum_ok(ut171.encode(r))
