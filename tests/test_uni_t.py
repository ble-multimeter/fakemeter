"""uni-t profile + uni_t_base codec tests.

Round-trips Readings through the UT60BT/UT161 encoder and the decode oracle
(``decode_uni_t``, a port of the driver's decode.ts), plus AB-CD frame build/parse
round-trips and the polled command seam (GET_NAME -> name frame, GET_DATA ->
measurement frame, soft-button -> state-mutating reply).

Run:  pytest -q tests/test_uni_t.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.profiles import uni_t, uni_t_base  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402
from tests.decode_uni_t import checksum_ok, decode_uni_t  # noqa: E402


# --- frame shape + checksum --------------------------------------------------
def test_frame_is_19_bytes_with_header():
    f = uni_t.encode(Reading(value=4.200, function="DCV", decimals=3))
    assert len(f) == 19
    assert f[0] == 0xAB and f[1] == 0xCD and f[2] == 0x10
    assert checksum_ok(f)


def test_matches_real_fixture():
    # The driver test's real UT60BT capture: ACV 274.7 V, auto-ranging.
    fixture = bytes([
        0xab, 0xcd, 0x10, 0x00, 0x30, 0x20, 0x20, 0x32, 0x37, 0x34, 0x2e, 0x37,
        0x00, 0x00, 0x00, 0x00, 0x08, 0x03, 0x02,
    ])
    f = uni_t.encode(Reading(value=274.7, function="ACV", decimals=1, auto=True))
    assert f == fixture


# --- value / function / sign round-trips -------------------------------------
@pytest.mark.parametrize("value,function,prefix,decimals,exp_unit", [
    (4.200, "DCV", "", 3, "V"),
    (274.7, "ACV", "", 1, "V"),
    (98.5, "OHM", "k", 1, "kΩ"),
    (1.000, "OHM", "M", 3, "MΩ"),
    (12.34, "ACA", "", 2, "A"),
    (-0.512, "DCV", "", 3, "V"),
    (3.30, "DIODE", "", 2, "V"),
])
def test_roundtrip_value_unit(value, function, prefix, decimals, exp_unit):
    r = Reading(value=value, function=function, prefix=prefix, decimals=decimals,
                auto=True)
    d = decode_uni_t(uni_t.encode(r))
    assert d.function == function
    assert d.display_unit == exp_unit
    assert d.display_value == pytest.approx(value, abs=10 ** -decimals)


def test_negative_sign_survives():
    d = decode_uni_t(uni_t.encode(
        Reading(value=-1.234, function="DCV", decimals=3)))
    assert d.display_value == pytest.approx(-1.234)


def test_overload():
    d = decode_uni_t(uni_t.encode(
        Reading(value=None, function="ACV", overload=True)))
    assert d.overload is True
    assert d.display_value is None


# --- flags round-trip --------------------------------------------------------
@pytest.mark.parametrize("flag", ["hold", "rel", "max", "min", "low_battery",
                                  "hv_warning", "peak_max", "peak_min"])
def test_flag_roundtrip(flag):
    r = Reading(value=4.200, function="DCV", decimals=3, auto=True)
    setattr(r, flag, True)
    d = decode_uni_t(uni_t.encode(r))
    assert d.flags[flag] is True


def test_auto_flag():
    auto_on = decode_uni_t(uni_t.encode(
        Reading(value=4.2, function="DCV", decimals=3, auto=True)))
    auto_off = decode_uni_t(uni_t.encode(
        Reading(value=4.2, function="DCV", decimals=3, auto=False)))
    assert auto_on.flags["auto"] is True
    assert auto_off.flags["auto"] is False


def test_acdc():
    ac = decode_uni_t(uni_t.encode(Reading(value=230.0, function="ACV", decimals=1)))
    dc = decode_uni_t(uni_t.encode(Reading(value=12.0, function="DCV", decimals=1)))
    assert ac.acdc == "AC"
    assert dc.acdc == "DC"


# --- AB-CD frame build/parse round-trip --------------------------------------
def test_build_frame_len8_roundtrip():
    f = uni_t_base.build_frame_len8(b"\x5f\x01")  # like a GET_NAME body
    assert f[0] == 0xAB and f[1] == 0xCD
    assert f[2] == len(f) - 3  # <len> counts the bytes after it
    # checksum is the BE additive sum of all but the trailing 2 bytes.
    s = sum(f[:-2]) & 0xFFFF
    assert f[-2] == (s >> 8) & 0xFF and f[-1] == s & 0xFF


def test_parse_opcode_len8():
    # The app's GET_NAME command frame: AB CD 03 5F 01 DA.
    assert uni_t_base.parse_opcode_len8(bytes([0xab, 0xcd, 0x03, 0x5f, 0x01, 0xda])) == 0x5F
    assert uni_t_base.parse_opcode_len8(b"\x00\x01") is None


# --- the polled command seam -------------------------------------------------
def test_get_name_returns_name_frame():
    # AB CD 03 5F 01 DA  (GET_NAME)
    resp = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5f, 0x01, 0xda]))
    assert resp is not None
    assert resp[0] == 0xAB and resp[1] == 0xCD
    assert b"UT60BT" in resp


def test_get_data_returns_measurement_frame():
    # AB CD 03 5D 01 D8  (GET_DATA)
    resp = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))
    assert resp is not None
    assert len(resp) == 19
    assert checksum_ok(resp)


def test_hold_button_freezes_and_lights_hold():
    uni_t.reset_state(Reading(value=4.200, function="DCV", decimals=3, auto=True))
    uni_t.set_walk(False)
    # AB CD 03 4A 01 C5  (HOLD)
    resp = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x4a, 0x01, 0xc5]))
    assert resp is not None and len(resp) == 19
    assert decode_uni_t(resp).flags["hold"] is True
    # second press releases HOLD
    resp2 = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x4a, 0x01, 0xc5]))
    assert decode_uni_t(resp2).flags["hold"] is False


def test_unknown_opcode_is_silent():
    assert uni_t.command(bytes([0xab, 0xcd, 0x03, 0x99, 0x01, 0x00])) is None


def test_profile_is_polled_no_secure_no_info():
    assert uni_t.profile.interaction == "polled"
    assert uni_t.profile.secure_uuid is None
    assert uni_t.profile.info_uuid is None
    assert uni_t.profile.service_uuid == uni_t_base.ISSC_SERVICE
    assert uni_t.profile.command_handler is not None


def test_presets_encode():
    for name, factory in uni_t.profile.presets.items():
        out = factory()
        readings = out if isinstance(out, list) else [out]
        for r in readings:
            f = uni_t.encode(r)
            assert len(f) == 19 and checksum_ok(f)
