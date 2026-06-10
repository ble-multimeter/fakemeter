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
    # ACV 274.7 V, auto-ranging. byte[4] = 0x31 (range index 1 = the "V" range): the
    # live UT60BT Smart Measure app decodes the UNIT from the range index against its
    # funOl1_UT60BT.json, where ACV range 0 = "mV" and range 1 = "V". A volt reading
    # must therefore use range index 1, NOT 0 (range 0 makes the app display "mV" —
    # confirmed live, then fixed in _range_index_for). The rest of the frame (function
    # code 0x00=ACV, ASCII "  274.7", flags) is unchanged from the original capture.
    fixture = bytes([
        0xab, 0xcd, 0x10, 0x00, 0x31, 0x20, 0x20, 0x32, 0x37, 0x34, 0x2e, 0x37,
        0x00, 0x00, 0x00, 0x00, 0x08, 0x03, 0x03,
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


def test_get_data_arms_stream_and_returns_first_frame():
    # AB CD 03 5D 01 D8  (GET_DATA). The corrected model is handshake-then-STREAM:
    # GET_DATA does NOT pull one frame — it ARMS periodic streaming and returns the
    # FIRST measurement frame. (The driver loops GET_DATA until measurements start.)
    uni_t._METER.stop_stream()
    assert uni_t._METER.streaming is False
    resp = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))
    assert resp is not None
    assert len(resp) == 19
    assert checksum_ok(resp)
    assert uni_t._METER.streaming is True  # the write started the stream


def test_tick_self_pushes_only_after_get_data_and_on_start():
    # Before GET_DATA arms it, tick must NOT self-push (the meter is silent until the
    # app writes GET_DATA). After GET_DATA + on_start(notify_cb), every tick pushes a
    # fresh measurement frame — the periodic free-stream the app's SamplingManager reads.
    pushed: list[bytes] = []
    uni_t._METER.stop_stream()
    uni_t._METER.on_start(pushed.append)
    uni_t.set_walk(False)
    uni_t.tick()
    assert pushed == []  # silent until GET_DATA arms the stream
    uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))  # GET_DATA arms it
    uni_t.tick()
    uni_t.tick()
    assert len(pushed) == 2
    assert all(len(f) == 19 and checksum_ok(f) for f in pushed)
    # stop_stream disarms: no more self-push.
    uni_t._METER.stop_stream()
    uni_t.tick()
    assert len(pushed) == 2


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


def test_profile_exposes_on_start_seam():
    # The handshake-then-stream self-push needs the server to hand the profile its
    # push callback via the ``on_start`` field (now a real base.Profile field that
    # gatt_server.start() calls after the notify char is live).
    on_start = uni_t.profile.on_start
    assert callable(on_start)
    # tick is still wired so the server's polled-tick timer drives the periodic push.
    assert uni_t.profile.tick is not None
    assert uni_t.profile.current_frame is not None


def test_seam_one_frame_per_tick_no_double_push():
    # Reproduce exactly what the server does end-to-end and assert NO double-push:
    # the server calls profile.on_start(notify) once, then its polled-tick driver
    # calls profile.tick() each timer fire. The profile self-pushes via the notify_cb
    # — the server's polled tick must NOT also push current_frame(). So after
    # GET_DATA arms the stream, every tick must yield EXACTLY ONE pushed frame.
    pushed: list[bytes] = []
    uni_t._METER.stop_stream()
    uni_t.profile.on_start(pushed.append)  # server hands the profile its push fn
    uni_t.set_walk(False)
    # Before GET_DATA: tick is a no-op (silent until the app arms the stream).
    uni_t.profile.tick()
    assert pushed == []
    # App writes GET_DATA -> arms streaming + returns the first frame (the server
    # pushes that return separately via _on_write; not counted here).
    uni_t.profile.command_handler(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))
    # Now each polled tick self-pushes EXACTLY ONE measurement frame.
    for n in range(5):
        before = len(pushed)
        uni_t.profile.tick()
        assert len(pushed) - before == 1, "double-push: tick emitted >1 frame"
    assert all(len(f) == 19 and checksum_ok(f) for f in pushed)
    uni_t._METER.stop_stream()


def test_profile_exposes_both_write_uuids():
    # The UNI-T ISSC service exposes both write chars; the app prefers …6daa…. The
    # widened write_uuid (list) must surface both, with the 6daa fallback first.
    uuids = uni_t.profile.write_uuids
    assert uni_t_base.ISSC_WRITE in uuids
    assert uni_t_base.ISSC_WRITE_FALLBACK in uuids
    assert len(uuids) == 2
    # the app's default (…6daa…) is listed first
    assert uuids[0] == uni_t_base.ISSC_WRITE_FALLBACK


def test_name_vs_measurement_frame_kinds():
    # GET_NAME -> a name/CONTROL frame (not 19 bytes); GET_DATA -> a 19-byte
    # MEASUREMENT frame. Mirrors framing.ts classify(): 19=measurement, else control.
    name = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5f, 0x01, 0xda]))
    meas = uni_t.command(bytes([0xab, 0xcd, 0x03, 0x5d, 0x01, 0xd8]))
    assert name is not None and len(name) != 19  # control/name frame
    assert meas is not None and len(meas) == 19   # measurement frame
    # the re-arm nudge classifies as a 9-byte type-request (AB CD .. AA AA ..).
    nudge = uni_t._METER.rearm_nudge()
    assert nudge is not None and len(nudge) == 9
    assert nudge[3] == 0xAA and nudge[4] == 0xAA


def test_presets_encode():
    for name, factory in uni_t.profile.presets.items():
        out = factory()
        readings = out if isinstance(out, list) else [out]
        for r in readings:
            f = uni_t.encode(r)
            assert len(f) == 19 and checksum_ok(f)
