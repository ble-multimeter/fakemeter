"""ut181a profile tests — len16 LE AB-CD frame, in-band float32 + ASCII unit."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import ut181a  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_ut181a import checksum_ok, decode_ut181a  # noqa: E402


def test_frame_valid_checksum():
    f = ut181a.encode(Reading(value=4.2000, function="DCV", decimals=4))
    assert f[0] == 0xAB and f[1] == 0xCD and f[4] == 0x02
    assert checksum_ok(f)


# The UT181A carries AC/DC in the in-band unit text ("V AC"), and the driver's
# functionFor only maps a BARE base unit ("V"->"V") — so the round-tripped function
# is the decoder's own key (e.g. "V AC"), not "ACV". We assert the decoder output
# (the oracle is authoritative) plus the value + acdc, which are the load-bearing
# fields. (This unverified path is flagged for hardware confirmation.)
@pytest.mark.parametrize("value,function,prefix,exp_fn,exp_acdc", [
    (4.2000, "DCV", "", "V", ""),
    (230.00, "ACV", "", "V AC", "AC"),
    (4.700, "OHM", "k", "OHM", ""),
    (12.340, "ACA", "", "A AC", "AC"),
])
def test_roundtrip(value, function, prefix, exp_fn, exp_acdc):
    d = decode_ut181a(ut181a.encode(
        Reading(value=value, function=function, prefix=prefix, decimals=3, auto=True)))
    assert d.function == exp_fn
    assert d.acdc == exp_acdc
    assert d.display_value == pytest.approx(value, abs=1e-2)


def test_ohm_unit_glyph():
    d = decode_ut181a(ut181a.encode(Reading(value=470.0, function="OHM", decimals=1)))
    assert d.display_unit == "Ω"


def test_overload():
    d = decode_ut181a(ut181a.encode(Reading(value=None, function="ACV", overload=True)))
    assert d.overload is True and d.display_value is None


@pytest.mark.parametrize("flag", ["hold", "rel", "hv_warning"])
def test_flag_roundtrip(flag):
    r = Reading(value=4.2, function="DCV", decimals=3, auto=True)
    setattr(r, flag, True)
    assert decode_ut181a(ut181a.encode(r)).flags[flag] is True


def test_start_returns_measurement():
    # START (opcode 0x05) is our get_data_op.
    resp = ut181a.command(bytes([0xab, 0xcd, 0x04, 0x00, 0x05, 0x01, 0x0a, 0x00]))
    assert resp is not None and checksum_ok(resp)


def test_profile_polled():
    assert ut181a.profile.interaction == "polled"


def test_presets_encode():
    for factory in ut181a.profile.presets.values():
        out = factory()
        for r in (out if isinstance(out, list) else [out]):
            assert checksum_ok(ut181a.encode(r))
