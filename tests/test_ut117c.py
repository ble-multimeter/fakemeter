"""ut117c profile tests — len16 AB-CD frame, big-endian checksum, polled seam."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ut117c  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_ut117c import DATA_TOTAL, checksum_ok, decode_ut117c  # noqa: E402


def test_frame_24_bytes_valid_checksum():
    f = ut117c.encode(Reading(value=4.200, function="DCV", decimals=3))
    assert len(f) == DATA_TOTAL
    assert f[0] == 0xAB and f[1] == 0xCD
    assert f[2] == 0x00 and f[3] == 0x12  # LEN = 0x0012
    assert f[4] == 0x02                    # type = measurement
    assert checksum_ok(f)


@pytest.mark.parametrize("value,function,prefix,decimals,exp_unit", [
    (4.200, "DCV", "", 3, "V"),
    (4.70, "OHM", "k", 2, "kΩ"),
    (1.500, "DIODE", "", 3, "V"),
    (12.34, "ACA", "", 2, "A"),
    (-0.512, "DCV", "", 3, "V"),
])
def test_roundtrip(value, function, prefix, decimals, exp_unit):
    d = decode_ut117c(ut117c.encode(
        Reading(value=value, function=function, prefix=prefix, decimals=decimals,
                auto=True)))
    assert d.function == function
    assert d.display_unit == exp_unit
    assert d.display_value == pytest.approx(value, abs=10 ** -decimals)


def test_overload():
    d = decode_ut117c(ut117c.encode(Reading(value=None, function="ACV", overload=True)))
    assert d.overload is True
    assert d.display_value is None


@pytest.mark.parametrize("flag", ["hold", "rel", "low_battery", "hv_warning"])
def test_flag_roundtrip(flag):
    r = Reading(value=4.200, function="DCV", decimals=3, auto=True)
    setattr(r, flag, True)
    assert decode_ut117c(ut117c.encode(r)).flags[flag] is True


def test_acdc():
    ac = decode_ut117c(ut117c.encode(Reading(value=230.0, function="ACV", decimals=1)))
    dc = decode_ut117c(ut117c.encode(Reading(value=4.2, function="DCV", decimals=1)))
    assert ac.acdc == "AC" and dc.acdc == "DC"


def test_poll_arms_stream_and_returns_first_measurement():
    # The app's poll: AB CD 00 04 05 00 01 81 (cmd 0x05 = POLL = get_data_op).
    # Handshake-then-stream: the poll ARMS streaming + returns the first frame.
    ut117c._METER.stop_stream()
    resp = ut117c.command(bytes([0xab, 0xcd, 0x00, 0x04, 0x05, 0x00, 0x01, 0x81]))
    assert resp is not None and len(resp) == DATA_TOTAL
    assert checksum_ok(resp)
    assert ut117c._METER.streaming is True


def test_hold_button():
    ut117c.reset_state(Reading(value=4.200, function="DCV", decimals=3, auto=True))
    ut117c.set_walk(False)
    # HOLD = cmd 0x12: AB CD 00 04 12 5A 01 E8.
    held = ut117c.command(bytes([0xab, 0xcd, 0x00, 0x04, 0x12, 0x5a, 0x01, 0xe8]))
    assert held is not None and decode_ut117c(held).flags["hold"] is True


def test_profile_polled():
    assert ut117c.profile.interaction == "polled"
    assert ut117c.profile.secure_uuid is None and ut117c.profile.info_uuid is None


def test_presets_encode():
    for factory in ut117c.profile.presets.values():
        out = factory()
        for r in (out if isinstance(out, list) else [out]):
            assert len(ut117c.encode(r)) == DATA_TOTAL
