"""Tests for the extracted meter-generic core + the OWON-shared base.

These exercise the reusable machinery directly (independent of the voltcraft
encoder) so every future profile that reuses them inherits the guarantees:
  * HOLD freeze/resume, REL capture/restore, Max/Min cycle, Select/AC-DC/Range,
    and the value-walk freezing under HOLD (meter_core.InteractiveMeter).
  * the OWON FFF1 MD5 auth + FFF2 info gate + FFF3 opcode dispatch (owon_base).
"""

from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakemeter.meter_core import InteractiveConfig, InteractiveMeter  # noqa: E402
from fakemeter.profiles import owon_base  # noqa: E402
from fakemeter.profiles.base import Reading  # noqa: E402


# A trivial encoder so the core is testable without any real frame format: pack
# the function label + value + flags into a readable bytes blob.
def _toy_encode(r: Reading) -> bytes:
    flags = "".join(n[0] for n in ("hold", "rel", "auto", "min", "max", "lpf")
                    if getattr(r, n))
    return f"{r.function}|{r.value}|dp{r.decimals}|{flags}".encode()


def _meter():
    cfg = InteractiveConfig(
        encode=_toy_encode,
        select_cycle=["V_DC", "V_AC", "OHM"],
        acdc_toggle={"V_DC": "V_AC", "V_AC": "V_DC"},
        range_dp_cycle=[3, 2, 1, 0],
    )
    return InteractiveMeter(cfg, Reading(value=4.2, function="V_DC",
                                         prefix="", decimals=3))


# -- meter_core: HOLD / REL / Max-Min / Select / AC-DC / Range / walk ----------
def test_hold_freezes_and_resumes():
    m = _meter()
    f = m.toggle_hold()
    assert m.state.hold is True
    assert m.runtime["held_frame"] == f
    assert m.current_frame() == f          # frozen
    # walk must NOT move the frozen frame
    m.tick()
    assert m.current_frame() == f
    m.toggle_hold()
    assert m.state.hold is False
    assert m.runtime["held_frame"] is None


def test_select_cycles_and_clears_modes():
    m = _meter()
    m.runtime["maxmin"] = 1
    m.state.rel = True
    m.select_next()
    assert m.state.function == "V_AC"
    assert m.runtime["maxmin"] == 0 and m.state.rel is False
    m.select_next()
    assert m.state.function == "OHM"
    m.select_next()
    assert m.state.function == "V_DC"      # wraps


def test_acdc_toggle():
    m = _meter()
    m.acdc_toggle()
    assert m.state.function == "V_AC"
    m.acdc_toggle()
    assert m.state.function == "V_DC"


def test_range_cycles_decimals_and_clears_auto():
    m = _meter()
    m.state.auto = True
    m.range_next()
    assert m.state.decimals == 2 and m.state.auto is False
    m.range_next(); m.range_next(); m.range_next()
    assert m.state.decimals == 3           # 2->1->0->3 (wrapped)


def test_rel_captures_and_restores():
    m = _meter()
    m.rel_toggle()
    assert m.state.rel is True and m.state.value == 0.0
    assert m.runtime["rel_base"] == 4.2
    m.rel_toggle()
    assert m.state.rel is False and m.state.value == 4.2


def test_maxmin_cycle():
    m = _meter()
    m.maxmin_next(); assert m.state.max and not m.state.min
    m.maxmin_next(); assert m.state.min and not m.state.max
    m.maxmin_next(); assert not m.state.max and not m.state.min   # AVG
    m.maxmin_next(); assert not m.state.max and not m.state.min   # off


def test_walk_drifts_then_reset_recenters():
    m = _meter()
    start = m.state.value
    moved = False
    for _ in range(20):
        m.tick()
        if m.state.value != start:
            moved = True
    assert moved
    m.reset_state(Reading(value=100.0, function="V_DC", prefix="", decimals=1))
    assert m.runtime["walk_center"] == 100.0 and m.runtime["live_value"] == 100.0


# -- owon_base: auth + info + dispatch -----------------------------------------
def test_owon_auth_raw_digest():
    auth = owon_base.OwonAuth(table="vc")
    written = bytes([0xCF, 0x77, 0x55, 0x2F, 0x2A, 0x14])
    assert auth.recover(written) == [7, 19, 35, 27, 32, 15]
    assert auth.pick(auth.recover(written)) == "ula2xl"
    assert auth.response(written).hex() == "ba8175919934b5873ea6462bd7888dc7"
    assert len(auth.response(written)) == 16


def test_owon_auth_java_ascii_hex():
    auth = owon_base.OwonAuth(table="java")
    written = bytes([0xCF, 0x77, 0x55, 0x2F, 0x2A, 0x14])
    picked = auth.pick(auth.recover(written))
    assert auth.response(written) == hashlib.md5(picked.encode()).hexdigest().encode()


def test_owon_auth_too_short():
    assert owon_base.OwonAuth().response(b"\x01\x02") == b""


def _owon_meter():
    cfg = owon_base.OwonProfile(
        id="toy", label="Toy", default_name="TOY", series=91, encode=_toy_encode,
        select_cycle=["V_DC", "V_AC"], acdc_toggle={"V_DC": "V_AC", "V_AC": "V_DC"},
    )
    return owon_base.OwonMeter(cfg)


def test_owon_info_response_six_bytes():
    om = _owon_meter()
    info = om.info_response()
    assert len(info) == 6 and info[0] == 91
    om.set_series(41)
    assert om.info_response()[0] == 41


def test_owon_command_dispatch():
    om = _owon_meter()
    # HOLD opcode freezes
    f = om.command(bytes([owon_base.CMD_HOLD, 1]))
    assert om.meter.state.hold is True and om.meter.current_frame() == f
    om.command(bytes([owon_base.CMD_HOLD, 1]))   # release
    # SELECT cycles
    om.command(bytes([owon_base.CMD_SELECT, 1]))
    assert om.meter.state.function == "V_AC"
    # special + unknown return None
    assert om.command(bytes([owon_base.CMD_COMPARE, 1])) is None
    assert om.command(bytes([0xEE, 1])) is None
    assert om.command(b"") is None
    # #TIMEsync is ignored (no reply)
    assert om.command(owon_base.TIMESYNC_PREFIX + b"\x00" * 7) is None


def test_make_profile_wires_everything():
    cfg = owon_base.OwonProfile(
        id="toy", label="Toy", default_name="TOY", series=91, encode=_toy_encode)
    om = owon_base.OwonMeter(cfg)
    p = owon_base.make_profile(cfg, om)
    assert p.interaction == "stream"
    assert p.service_uuid == owon_base.FFF0_SERVICE
    # bound methods compare by underlying func+self (a fresh object each access).
    assert p.auth_response.__func__ is om.auth_response.__func__
    assert p.info_response.__func__ is om.info_response.__func__
    assert p.command_handler.__func__ is om.command.__func__
    assert p.current_frame.__func__ is om.meter.current_frame.__func__
    assert p.tick.__func__ is om.meter.tick.__func__
    # the profile-agnostic REPL hooks are wired too
    assert p.reset_state is not None and p.set_walk is not None
    assert p.set_series is not None and p.set_auth is not None


def test_owon_use_auth_use_info_gates_drop_chars():
    # An OWON sibling whose app does NOT gate on FFF1/FFF2 (e.g. the Windows-derived
    # owon-plus path) drops those chars but keeps the shared GATT + dispatch + walk.
    cfg = owon_base.OwonProfile(
        id="toy", label="Toy", default_name="TOY", series=41, encode=_toy_encode,
        use_auth=False, use_info=False)
    om = owon_base.OwonMeter(cfg)
    p = owon_base.make_profile(cfg, om)
    assert p.secure_uuid is None and p.info_uuid is None
    assert p.auth_response is None and p.info_response is None
    assert p.set_series is None and p.set_auth is None
    # streaming + button dispatch still work
    assert p.command_handler is not None and p.current_frame is not None
    assert p.command_handler(bytes([owon_base.CMD_HOLD, 1])) is not None


def test_owon_custom_controls_opcode_map():
    # A sibling with a DIFFERENT opcode for HOLD reuses the dispatch via a custom map.
    cfg = owon_base.OwonProfile(
        id="toy", label="Toy", default_name="TOY", series=91, encode=_toy_encode,
        controls={0x42: "hold", 0x43: "select"},
        select_cycle=["V_DC", "V_AC"])
    om = owon_base.OwonMeter(cfg)
    f = om.command(bytes([0x42, 1]))           # custom HOLD opcode
    assert om.meter.state.hold is True and f is not None
    assert om.command(bytes([owon_base.CMD_HOLD, 1])) is None  # default opcode now unmapped
    om.command(bytes([0x42, 1]))               # release
    om.command(bytes([0x43, 1]))               # custom SELECT
    assert om.meter.state.function == "V_AC"


def test_reading_has_full_driver_flag_surface():
    # The Reading flag set must be a superset of the driver repo's Reading.flags so a
    # per-family flag->bit table is a 1:1 mapping for every profile.
    r = Reading()
    for name in ("max", "min", "hold", "rel", "auto", "low_battery",
                 "hv_warning", "peak_max", "peak_min"):
        assert hasattr(r, name), f"Reading missing flag {name}"


def test_profile_write_uuids_normalizes_str_and_list():
    # write_uuid accepts a single string (back-compat) or a list; write_uuids always
    # returns a list, so the GATT server can add one write characteristic per UUID.
    from fakemeter.profiles.base import Profile

    single = Profile(id="s", label="S", service_uuid="svc",
                     notify_uuid="ntf", write_uuid="w1")
    assert single.write_uuids == ["w1"]

    multi = Profile(id="m", label="M", service_uuid="svc",
                    notify_uuid="ntf", write_uuid=["w1", "w2"])
    assert multi.write_uuids == ["w1", "w2"]


def test_profile_on_start_field_default_none():
    # on_start is an optional Profile field (the polled handshake-then-stream seam);
    # default None means the server hands no push fn (stream families).
    from fakemeter.profiles.base import Profile

    p = Profile(id="p", label="P", service_uuid="svc",
                notify_uuid="ntf", write_uuid="w")
    assert p.on_start is None
