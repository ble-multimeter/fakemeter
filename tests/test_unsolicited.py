"""Unit tests for the no-CCCD (unsolicited) ATT-notification injector.

These cover the load-bearing byte layout of the HCI ACL / L2CAP / ATT packet and
the passive value-handle discovery parser — the parts that must be exactly right
for the on-air notification to be accepted by the peer. They do NOT touch a real
HCI socket (no privilege / hardware needed): the libc ``write`` is stubbed to
capture the bytes the injector would put on the wire.
"""

from __future__ import annotations

import struct

import fakemeter.unsolicited as U
from fakemeter.unsolicited import UnsolicitedNotifier, _hci_dev_index


def test_hci_dev_index():
    assert _hci_dev_index("hci0") == 0
    assert _hci_dev_index("hci3") == 3
    assert _hci_dev_index("0") == 0
    assert _hci_dev_index("nonsense") == 0  # safe fallback


class _FakeRaw:
    def fileno(self):
        return -1


def _capture_inject(notifier, frame):
    """Run inject() with libc.write stubbed; return the bytes it would send."""
    captured = {}

    def fake_write(fd, buf, ln):
        captured["pkt"] = bytes(buf.raw[:ln])
        return ln

    orig = U._libc.write
    U._libc.write = fake_write
    try:
        ok = notifier.inject(frame)
    finally:
        U._libc.write = orig
    return ok, captured.get("pkt")


def test_inject_pdu_layout():
    n = UnsolicitedNotifier("hci0")
    n._raw = _FakeRaw()
    n.acl_handle = 0x0100
    n.value_handle = 0x01D1
    frame = bytes.fromhex("230000000f27")  # an owon-plus 9.999 V_DC frame

    ok, pkt = _capture_inject(n, frame)
    assert ok is True
    # H4 ACL data packet type
    assert pkt[0] == 0x02
    # ACL header: handle 0x0100 with PB=0b10 (first non-auto-flushable), BC=0
    hcon = struct.unpack_from("<H", pkt, 1)[0]
    assert (hcon & 0x0FFF) == 0x0100
    assert ((hcon >> 12) & 0x3) == 0x2
    acl_len = struct.unpack_from("<H", pkt, 3)[0]
    assert acl_len == len(pkt) - 5
    # L2CAP header: length + CID 0x0004 (ATT)
    l2_len, cid = struct.unpack_from("<HH", pkt, 5)
    assert cid == 0x0004
    # ATT Handle Value Notification: opcode 0x1B | value handle | frame
    assert pkt[9] == 0x1B
    vhandle = struct.unpack_from("<H", pkt, 10)[0]
    assert vhandle == 0x01D1
    assert pkt[12:] == frame
    assert l2_len == 1 + 2 + len(frame)  # opcode + handle + value


def test_inject_noop_without_target():
    n = UnsolicitedNotifier("hci0")
    n._raw = _FakeRaw()
    # No acl_handle / value_handle yet -> must be a no-op, not a crash.
    ok, pkt = _capture_inject(n, b"\x00\x01")
    assert ok is False and pkt is None

    n.acl_handle = 0x0040
    ok, pkt = _capture_inject(n, b"\x00\x01")
    assert ok is False  # still no value handle


def test_value_handle_discovery_from_read_by_type_rsp():
    """The monitor parser extracts the notify char's value handle from a
    Read-By-Type Response that enumerates the «Characteristic» declaration."""
    n = UnsolicitedNotifier("hci0")
    assert n.value_handle is None

    # Build a plausible monitor frame containing, somewhere in it, an ATT
    # Read-By-Type Response on CID 4 with one characteristic declaration whose
    # properties byte has the NOTIFY bit (0x10) set and value handle 0x01D1.
    #   L2CAP: 04 00 (CID) preceding ATT 09 (op) | length | [decl_handle, value...]
    decl_handle = 0x01D0
    value_handle = 0x01D1
    props = 0x12  # NOTIFY (0x10) | READ (0x02)
    char_uuid16 = 0xFFF4
    record = struct.pack("<H", decl_handle) + bytes([props]) \
        + struct.pack("<H", value_handle) + struct.pack("<H", char_uuid16)
    length = len(record)  # 7
    att = bytes([0x09, length]) + record
    # CID bytes precede the ATT opcode (the parser searches for "04 00 09").
    frame = b"\xAA\xBB" + struct.pack("<H", 0x0004) + att + b"\xCC"

    n._scan_for_value_handle(frame)
    assert n.value_handle == value_handle


def test_value_handle_discovery_ignores_non_notify():
    n = UnsolicitedNotifier("hci0")
    # Same shape but properties lack the NOTIFY bit -> must NOT be picked.
    record = struct.pack("<H", 0x0010) + bytes([0x02]) \
        + struct.pack("<H", 0x0011) + struct.pack("<H", 0x180A)
    att = bytes([0x09, len(record)]) + record
    frame = struct.pack("<H", 0x0004) + att
    n._scan_for_value_handle(frame)
    assert n.value_handle is None
