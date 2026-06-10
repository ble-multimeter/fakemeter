"""ut219p profile tests — best-effort standard live-data frame (ACV/ACA path).

Only the standard CMDID-5 cmdCode-0 measurement frame is emulated (the daoPos
parameter-set dispatch, battery-gate handshake, and waveform/harmonic frames are
deferred); these tests cover that subset round-tripping through the oracle.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ut219p  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_ut219p import checksum_ok, decode_ut219p  # noqa: E402


def test_frame_valid_checksum():
    f = ut219p.encode(Reading(value=230.0, function="ACV", decimals=1))
    assert f[0] == 0xAB and f[1] == 0xCD and f[4] == 0x05 and f[5] == 0x00
    assert checksum_ok(f)


def test_acv_value_roundtrip():
    d = decode_ut219p(ut219p.encode(Reading(value=230.0, function="ACV", decimals=1, auto=True)))
    assert d.function == "ACV"
    assert d.display_unit == "V"
    assert d.display_value == pytest.approx(230.0, abs=0.1)


def test_aca_value_roundtrip():
    d = decode_ut219p(ut219p.encode(Reading(value=12.0, function="ACA", decimals=2, auto=True)))
    assert d.function == "ACA"
    assert d.display_unit == "A"
    assert d.display_value == pytest.approx(12.0, abs=1.0)


def test_overload():
    d = decode_ut219p(ut219p.encode(Reading(value=None, function="ACV", overload=True)))
    assert d.overload is True and d.display_value is None


@pytest.mark.parametrize("flag", ["hold", "rel", "hv_warning"])
def test_flag_roundtrip(flag):
    r = Reading(value=230.0, function="ACV", decimals=1, auto=True)
    setattr(r, flag, True)
    assert decode_ut219p(ut219p.encode(r)).flags[flag] is True


def test_live_poll_arms_stream_and_returns_first_measurement():
    # LIVE_STD poll: AB CD 00 04 05 00 09 00 (cmd 0x05 = get_data_op).
    # Handshake-then-stream: the poll ARMS streaming.
    ut219p._METER.stop_stream()
    resp = ut219p.command(bytes([0xab, 0xcd, 0x00, 0x04, 0x05, 0x00, 0x09, 0x00]))
    assert resp is not None and checksum_ok(resp)
    assert ut219p._METER.streaming is True


def test_profile_polled():
    assert ut219p.profile.interaction == "polled"
    assert ut219p.profile.secure_uuid is None


def test_presets_encode():
    for factory in ut219p.profile.presets.values():
        out = factory()
        for r in (out if isinstance(out, list) else [out]):
            assert checksum_ok(ut219p.encode(r))
