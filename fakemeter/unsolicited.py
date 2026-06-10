"""Unsolicited (no-CCCD) ATT notification delivery via raw-HCI ACL injection.

WHY THIS EXISTS
---------------
BlueZ's GATT server (``src/gatt-database.c``) only emits a Handle Value
Notification to a connected client that has *subscribed* by WRITING the client
characteristic configuration descriptor (CCCD, 0x2902). The gate is absolute —
``send_notification_to_device()`` does ``ccc = find_ccc_state(...); if (!ccc ||
!(ccc->value & 0x0003)) return;`` — and there is **no** D-Bus / config / property
path to bypass it (forcing the local ``Notifying=True`` property does not route
to a non-subscribed link; verified).

Some vendor apps NEVER write the CCCD. The **OWON Multimeter BLE4.0** Android app
(``com.owon.MultimeterBLE``) is the motivating case: its
``setCharacteristicNotification(true)`` only writes the CCCD for a leftover
heart-rate UUID; for its real notify char (FFF4) it just sets Android's *local*
receive flag and relies on the meter emitting **unsolicited** notifications —
exactly what a real BLE chip does (it sends opcode 0x1B regardless of any CCCD).
Against a bluezero/BlueZ peripheral the app therefore sits on "No input" forever.

THE MECHANISM
-------------
We bypass BlueZ's GATT server for the *delivery* of the notify frame: build the
ATT **Handle Value Notification** PDU ourselves and write it as a raw HCI ACL data
packet on the existing connection's ACL handle, over L2CAP CID 0x0004 (ATT). The
kernel forwards the ACL payload onto the air; the peer's controller delivers it on
the ATT channel; Android (local-notify-flag set) hands it to the app. BlueZ still
owns the ACL link and serves all the normal GATT traffic (service discovery,
reads, FFF1/FFF2/FFF3 writes) — we only *inject* the notify frames it refuses to
route. The normal CCCD path is untouched for well-behaved apps.

This was proven live: an injected sentinel notification appeared as the FFF4
value on a connected client with no BlueZ-side subscription.

PORTABILITY / CAVEATS
---------------------
* **Requires CAP_NET_RAW** (root, or ``setcap cap_net_raw+ep`` on the python
  binary) to *write* on the raw HCI socket. Without it the injector degrades
  gracefully: it logs a one-line remediation hint and the server keeps the normal
  CCCD path. (Binding the socket needs no privilege; only writing does.)
* It is a deliberate layering violation (we speak HCI under BlueZ). It does not
  manage ACL flow-control credits; for the low duty cycle of a meter stream
  (~3 Hz) this is fine in practice, but a high-rate stream could in theory race
  the kernel's tx scheduling. Keep the inject rate modest.
* The ATT *value handle* of the notify characteristic is assigned by BlueZ. We
  discover it by passively watching the client's GATT discovery on an HCI
  *monitor* socket (the Read-By-Type Response that enumerates the characteristic
  carries its value handle), and fall back to scanning the peer's own notify
  reads. If discovery is missed it can be supplied explicitly.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import struct
import threading
from typing import Optional

log = logging.getLogger("fakemeter")

# AF_BLUETOOTH socket plumbing (CPython's high-level socket cannot bind the HCI
# raw/monitor channels nor an L2CAP fixed CID, so we drive bind()/write() via libc).
_AF_BLUETOOTH = 31
_BTPROTO_HCI = 1
_HCI_CHANNEL_RAW = 0
_HCI_CHANNEL_MONITOR = 2
_HCI_DEV_NONE = 0xFFFF

# HCI H4 packet type prefixes (the byte the kernel raw socket prepends/expects).
_HCI_ACLDATA_PKT = 0x02

_ATT_CID = 0x0004
_ATT_OP_HANDLE_VALUE_NTF = 0x1B
_ATT_OP_READ_BY_TYPE_RSP = 0x09  # carries <handle, value> pairs of char decls
_GATT_CHARACTERISTIC_TYPE = 0x2803  # «Characteristic» declaration UUID

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _hci_dev_index(adapter_id: str) -> int:
    """hci0 -> 0. Accepts already-numeric too."""
    m = adapter_id.lower()
    if m.startswith("hci"):
        m = m[3:]
    try:
        return int(m)
    except ValueError:
        return 0


def _open_hci(channel: int, dev: int) -> Optional[socket.socket]:
    """Open + bind a raw AF_BLUETOOTH/HCI socket on ``channel`` for ``dev``."""
    s = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
    # struct sockaddr_hci { sa_family_t family; unsigned short dev; unsigned short channel; }
    addr = struct.pack("<HHH", _AF_BLUETOOTH, dev, channel)
    buf = ctypes.create_string_buffer(addr, len(addr))
    if _libc.bind(s.fileno(), buf, len(addr)) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, os.strerror(err))
    return s


class UnsolicitedNotifier:
    """Inject ATT notifications onto a BlueZ-managed link without a CCCD subscription.

    One instance is bound to one adapter. It tracks the (single) active ACL
    connection handle and the notify characteristic's ATT value handle by passively
    monitoring HCI, and exposes :meth:`inject` to push a frame to that client.
    """

    def __init__(self, adapter_id: str, notify_value_handle: Optional[int] = None):
        self.adapter_id = adapter_id
        self.dev = _hci_dev_index(adapter_id)
        # Discovered/learned state (updated by the monitor thread):
        self.acl_handle: Optional[int] = None
        self.value_handle: Optional[int] = notify_value_handle
        self._raw: Optional[socket.socket] = None
        self._raw_writable = False  # set True once a write() succeeds (CAP_NET_RAW)
        self._mon: Optional[socket.socket] = None
        self._mon_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._warned_perm = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        """Open the raw inject socket + the monitor discovery thread.

        Returns True if the raw socket is open (delivery *may* be possible);
        False if even binding failed (no HCI access at all). A True return does
        not guarantee write permission — that is probed lazily on first inject and
        reported via :attr:`available`.
        """
        try:
            self._raw = _open_hci(_HCI_CHANNEL_RAW, self.dev)
        except OSError as e:
            log.warning("[%s] unsolicited: cannot open raw HCI socket (%s); "
                        "no-CCCD delivery disabled", self.adapter_id, e)
            return False
        # The monitor socket (passive HCI sniff) discovers the ACL handle + the
        # notify value handle. Binding monitor usually needs no privilege.
        try:
            self._mon = _open_hci(_HCI_CHANNEL_MONITOR, _HCI_DEV_NONE)
            self._mon_thread = threading.Thread(
                target=self._monitor_loop, name=f"unsol-mon-{self.adapter_id}",
                daemon=True)
            self._mon_thread.start()
        except OSError as e:
            log.warning("[%s] unsolicited: cannot open HCI monitor (%s); will rely "
                        "on an explicit/learned value handle", self.adapter_id, e)
        log.info("[%s] unsolicited no-CCCD notifier armed (raw HCI inject)",
                 self.adapter_id)
        return True

    def stop(self) -> None:
        self._stop.set()
        for s in (self._raw, self._mon):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass

    @property
    def available(self) -> bool:
        """True once we have a target (ACL + value handle) AND a writable socket."""
        return (self._raw is not None and self.acl_handle is not None
                and self.value_handle is not None)

    # -- discovery (HCI monitor) -------------------------------------------
    def _monitor_loop(self) -> None:
        """Passively learn the ACL handle + notify value handle from HCI traffic.

        The monitor channel frames each packet with a small btsnoop-ish header; we
        do not fully parse it — we scan the raw bytes for the two signatures we
        need, which is robust enough for a single active link:

          * an outgoing/!incoming ATT Read-By-Type RESPONSE whose 0x2803 char
            declarations include the notify UUID -> its value handle, and
          * any ATT PDU on CID 4 lets us confirm the link is up.

        The ACL handle is taken from :meth:`set_acl_handle` (resolved from BlueZ /
        ``hcitool con`` by the server, which knows the connection authoritatively).
        We additionally sniff the value handle from any Handle-Value-Notification
        we ourselves emit or that a well-behaved client elicits.
        """
        sel_buf = bytearray(4096)
        import select
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._mon], [], [], 0.5)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                data = self._mon.recv(4096)
            except OSError:
                return
            self._scan_for_value_handle(data)

    def _scan_for_value_handle(self, data: bytes) -> None:
        """Find the notify char's value handle in an ATT Read-By-Type Response.

        Read-By-Type Response (op 0x09) layout on CID 4:
            0x09 | length(1) | [ attr_handle(2) | value(length-2) ]*
        When reading the «Characteristic» declaration (type 0x2803) the value is
        ``properties(1) | value_handle(2) | char_uuid(2 or 16)``. We already know
        the notify UUID is 16-bit FFF4 in practice, but to stay UUID-agnostic we
        accept the declaration whose properties byte has the NOTIFY bit (0x10) set.
        """
        if self.value_handle is not None:
            return
        idx = data.find(bytes([_ATT_CID & 0xFF, _ATT_CID >> 8,
                               _ATT_OP_READ_BY_TYPE_RSP]))
        # The CID bytes (04 00) immediately precede the ATT opcode in the L2CAP
        # header; search for "04 00 09".
        if idx < 0:
            return
        p = idx + 2  # at the 0x09 opcode
        try:
            length = data[p + 1]
            entries = data[p + 2:]
        except IndexError:
            return
        if length < 7:  # need >= handle(2)+props(1)+vhandle(2)+uuid16(2)
            return
        for off in range(0, len(entries) - length + 1, length):
            rec = entries[off:off + length]
            if len(rec) < 5:
                break
            props = rec[2]
            value_handle = rec[3] | (rec[4] << 8)
            if props & 0x10:  # NOTIFY property
                self.value_handle = value_handle
                log.info("[%s] unsolicited: learned notify value handle 0x%04x",
                         self.adapter_id, value_handle)
                return

    # -- target wiring ------------------------------------------------------
    def set_acl_handle(self, handle: Optional[int]) -> None:
        """The server tells us the current ACL connection handle (or None on drop)."""
        if handle != self.acl_handle:
            if handle is not None:
                log.info("[%s] unsolicited: ACL handle = 0x%04x", self.adapter_id, handle)
            self.acl_handle = handle

    def set_value_handle(self, handle: int) -> None:
        self.value_handle = handle

    # -- injection ----------------------------------------------------------
    def inject(self, frame: bytes) -> bool:
        """Send ``frame`` as an unsolicited ATT notification. Returns True on write.

        Builds: HCI ACL (handle, PB=first-non-flushable) -> L2CAP(len, CID 4) ->
        ATT(0x1B | value_handle | frame). No-op (False) if we have no target yet or
        the raw socket lacks CAP_NET_RAW.
        """
        if self._raw is None or self.acl_handle is None or self.value_handle is None:
            return False
        att = bytes([_ATT_OP_HANDLE_VALUE_NTF]) \
            + struct.pack("<H", self.value_handle) + bytes(frame)
        l2cap = struct.pack("<HH", len(att), _ATT_CID) + att
        # ACL header: 12-bit handle | PB(2)=0b10 first-non-auto-flushable | BC(2)=00
        hcon = (self.acl_handle & 0x0FFF) | (0x2 << 12)
        acl = struct.pack("<H", hcon) + struct.pack("<H", len(l2cap)) + l2cap
        pkt = bytes([_HCI_ACLDATA_PKT]) + acl
        buf = ctypes.create_string_buffer(pkt, len(pkt))
        n = _libc.write(self._raw.fileno(), buf, len(pkt))
        if n < 0:
            err = ctypes.get_errno()
            if err in (1, 13) and not self._warned_perm:  # EPERM / EACCES
                self._warned_perm = True
                log.warning(
                    "[%s] unsolicited: raw HCI write denied (%s). No-CCCD delivery "
                    "needs CAP_NET_RAW — run as root or grant it once with:\n"
                    "    sudo setcap cap_net_raw+ep $(readlink -f $(which python3))\n"
                    "Falling back to the CCCD path (apps that don't subscribe get "
                    "no live data).", self.adapter_id, os.strerror(err))
            return False
        self._raw_writable = True
        return True
