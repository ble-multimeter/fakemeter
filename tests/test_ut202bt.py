"""ut202bt profile tests — rides the UT60BT 19-byte decoder (decode_uni_t oracle)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ut202bt  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_uni_t import checksum_ok, decode_uni_t  # noqa: E402


def test_frame_19_bytes():
    f = ut202bt.encode(Reading(value=12.34, function="ACA", decimals=2, auto=True))
    assert len(f) == 19 and checksum_ok(f)


@pytest.mark.parametrize("value,function,prefix,decimals,exp_unit", [
    (12.34, "ACA", "", 2, "A"),
    (230.0, "ACV", "", 1, "V"),
    (4.70, "OHM", "k", 2, "kΩ"),
])
def test_roundtrip(value, function, prefix, decimals, exp_unit):
    d = decode_uni_t(ut202bt.encode(
        Reading(value=value, function=function, prefix=prefix, decimals=decimals,
                auto=True)))
    assert d.function == function
    assert d.display_unit == exp_unit
    assert d.display_value == pytest.approx(value, abs=10 ** -decimals)


def test_overload():
    d = decode_uni_t(ut202bt.encode(Reading(value=None, function="ACA", overload=True)))
    assert d.overload is True


def test_get_data_and_hold_seam():
    ut202bt.reset_state(Reading(value=12.34, function="ACA", decimals=2, auto=True))
    ut202bt.set_walk(False)
    data = ut202bt.command(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))
    assert data is not None and len(data) == 19
    # RANGE + HOLD are the only generic controls; HOLD = 0x4A.
    held = ut202bt.command(bytes([0xab, 0xcd, 0x03, 0x4a, 0x01, 0xc5]))
    assert decode_uni_t(held).flags["hold"] is True


def test_profile_polled():
    assert ut202bt.profile.interaction == "polled"
    assert ut202bt.profile.default_name == "UT202BT-FAKE"


def test_presets_encode():
    for factory in ut202bt.profile.presets.values():
        out = factory()
        for r in (out if isinstance(out, list) else [out]):
            assert len(ut202bt.encode(r)) == 19
