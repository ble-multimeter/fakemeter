"""BlueZ GATT-server + LE advertisement that impersonates one meter profile.

Built on ``bluezero`` (a thin wrapper over BlueZ's D-Bus GATT API). Each
:class:`MeterServer` is fully instance-scoped — the adapter is threaded through as
a constructor argument and there are no module globals — so two servers can run on
two adapters at once (``hci0`` + ``hci1``).

The BLE peripheral exposes, for the chosen profile:
  * service        (e.g. 0xFFF0)
  * notify char    (e.g. 0xFFF4)  — we push crafted frames here
  * write char     (e.g. 0xFFF3)  — every write the app sends is LOGGED
  * secure char    (e.g. 0xFFF1)  — OWON MD5 anti-counterfeit gate, if the profile
                                     declares one: writes are logged + remembered,
                                     reads return the profile's computed response.

bluezero's ``publish()`` blocks on the GLib main loop, so we run it on a background
thread and marshal value updates back onto that loop with ``GLib.idle_add`` (BlueZ
D-Bus objects must be touched from the loop thread).
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Optional

from bluezero import adapter as bz_adapter
from bluezero import peripheral
from gi.repository import GLib

from .profiles.base import Profile
from .unsolicited import UnsolicitedNotifier

log = logging.getLogger("fakemeter")

# Characteristic instance ids (unique per service; arbitrary small ints).
_CHR_NOTIFY = 1
_CHR_WRITE = 2
_CHR_SECURE = 3
_CHR_INFO = 4
_SRV_ID = 1


def adapter_address(adapter_id: str) -> str:
    """Map an adapter id (``hci0``) to its BD address, which bluezero wants.

    Accepts either an ``hciN`` name or an already-resolved ``AA:BB:..`` address.
    """
    if re.fullmatch(r"(?i)[0-9a-f]{2}(:[0-9a-f]{2}){5}", adapter_id):
        return adapter_id.upper()
    try:
        out = subprocess.run(
            ["hciconfig", adapter_id], capture_output=True, text=True, check=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            f"could not resolve adapter {adapter_id!r} via hciconfig: {e}"
        ) from e
    m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", out)
    if not m:
        raise RuntimeError(f"no BD address found for adapter {adapter_id!r}")
    return m.group(1).upper()


class MeterServer:
    """A running fake-meter BLE peripheral for one profile on one adapter."""

    def __init__(self, profile: Profile, adapter_id: str = "hci0",
                 local_name: Optional[str] = None, unsolicited: bool = True):
        self.profile = profile
        self.adapter_id = adapter_id
        self.adapter_addr = adapter_address(adapter_id)
        self.local_name = local_name or profile.default_name

        # No-CCCD (unsolicited) notification delivery. Some vendor apps (notably
        # the OWON Multimeter BLE4.0 Java app) never write the FFF4 CCCD; BlueZ
        # then refuses to route notifications to them (it gates strictly on the
        # CCCD — see unsolicited.py). When enabled (default, stream profiles only)
        # we inject ATT notifications via a raw HCI socket so those apps still get
        # a live reading, while the normal CCCD path keeps serving well-behaved
        # apps. Needs CAP_NET_RAW to actually deliver; degrades gracefully if not.
        self._unsol_enabled = unsolicited and profile.interaction == "stream"
        self._unsol: Optional[UnsolicitedNotifier] = None
        self._unsol_streaming = False
        self._unsol_source_id: Optional[int] = None
        self._client_subscribed = False  # True once the client writes the CCCD

        self._periph: Optional[peripheral.Peripheral] = None
        self._notify_char = None  # localGATT.Characteristic
        self._secure_char = None
        self._last_secure_write: bytes = b""
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        # Continuous streaming: real meters free-stream readings, and the app
        # disconnects (~1.5s data timeout) if a subscribed notify char goes silent.
        # While a client is subscribed we re-push the last frame on this interval.
        #
        # CRITICAL: the re-push (and every notify) MUST run on the GLib main-loop
        # thread. bluezero emits the BLE notification by firing a D-Bus
        # PropertiesChanged signal from set_value(); dbus-python + GLib is not
        # thread-safe, so emitting that signal from a plain Python thread does NOT
        # reliably reach BlueZ — the phone subscribes but never receives any frame
        # (app stays "disconnected"/UL). We therefore drive the stream with a
        # GLib timeout source on the loop thread (scheduled via GLib.idle_add so
        # the source itself is created on that thread).
        self._last_frame: Optional[bytes] = None
        self._stream_interval_ms = 300
        self._streaming = False
        self._stream_source_id: Optional[int] = None

        self._build()

    # -- GATT construction --------------------------------------------------
    def _build(self) -> None:
        p = self.profile
        periph = peripheral.Peripheral(
            self.adapter_addr, local_name=self.local_name
        )
        periph.add_service(srv_id=_SRV_ID, uuid=p.service_uuid, primary=True)

        # Notify characteristic — the meter free-streams frames here.
        periph.add_characteristic(
            srv_id=_SRV_ID, chr_id=_CHR_NOTIFY, uuid=p.notify_uuid,
            value=[], notifying=False, flags=["notify", "read"],
            read_callback=self._on_notify_read,
            notify_callback=self._on_notify_subscription,
        )

        # Write characteristic(s) — log whatever the app writes (auth probes, control
        # commands, …). A family may expose SEVERAL interchangeable write chars (UNI-T
        # publishes both …8841… and …6daa…; the Smart Measure app prefers …6daa…), so
        # add one per UUID — all dispatch to the same _on_write handler. chr_ids are
        # offset from _CHR_WRITE so each is unique within the service.
        for offset, write_uuid in enumerate(p.write_uuids):
            periph.add_characteristic(
                srv_id=_SRV_ID, chr_id=_CHR_WRITE + offset * 10, uuid=write_uuid,
                value=[], notifying=False,
                flags=["write", "write-without-response"],
                write_callback=self._on_write,
            )

        # Optional secure / FFF1 MD5 characteristic.
        if p.secure_uuid:
            periph.add_characteristic(
                srv_id=_SRV_ID, chr_id=_CHR_SECURE, uuid=p.secure_uuid,
                value=[], notifying=False, flags=["read", "write"],
                read_callback=self._on_secure_read,
                write_callback=self._on_secure_write,
            )

        # Optional FFF2 "info" characteristic — the app reads this on connect to
        # identify the meter series/model.
        if p.info_uuid:
            periph.add_characteristic(
                srv_id=_SRV_ID, chr_id=_CHR_INFO, uuid=p.info_uuid,
                value=list(p.info_response()) if p.info_response else [],
                notifying=False, flags=["read"],
                read_callback=self._on_info_read,
            )

        self._periph = periph

    # -- callbacks ----------------------------------------------------------
    @staticmethod
    def _char_uuid(ch) -> str:
        """Read a localGATT.Characteristic's UUID out of its D-Bus props."""
        return str(ch.props["org.bluez.GattCharacteristic1"]["UUID"]).lower()

    def _resolve_chars(self) -> None:
        """Grab the live localGATT.Characteristic objects after publish()."""
        for ch in self._periph.characteristics:
            uuid = self._char_uuid(ch)
            if uuid == self.profile.notify_uuid.lower():
                self._notify_char = ch
            elif (self.profile.secure_uuid
                  and uuid == self.profile.secure_uuid.lower()):
                self._secure_char = ch

    def _on_notify_subscription(self, notifying, characteristic):
        state = "START" if notifying else "STOP"
        log.info("[%s] notify %s on %s", self.adapter_id, state, self.profile.notify_uuid)
        # A real CCCD write arrived: the client IS a well-behaved subscriber. Hand
        # delivery back to BlueZ's normal path and stop any unsolicited injection.
        self._client_subscribed = notifying
        if notifying:
            self._stop_unsol_stream()
        # INTERACTION seam: only the free-STREAMING families run the re-push loop.
        # A 'polled' (request/response, e.g. UNI-T AB-CD) meter stays silent on
        # subscribe and only answers writes — see _on_write / Profile.interaction.
        if self.profile.interaction != "stream":
            # Polled handshake-then-stream (UNI-T): the meter does NOT free-stream on
            # subscribe, but once the app writes GET_DATA the profile's tick must run
            # on a timer to emit the periodic measurement frames. Install that timer
            # on subscribe (tick is a no-op until GET_DATA arms it). The profile
            # SELF-PUSHES each frame via the notify_cb it got from on_start, so the
            # polled tick driver only DRIVES tick — it must NOT also push
            # current_frame() (that would double the stream rate).
            if getattr(self.profile, "tick", None) is not None:
                if notifying:
                    self._start_polled_tick()
                else:
                    self._stop_stream()
            return
        if notifying:
            self._start_stream()
        else:
            self._stop_stream()

    def _start_stream(self) -> None:
        if self._streaming:
            return
        self._streaming = True
        log.info("[%s] streaming frames @ %dms while subscribed (GLib loop)",
                 self.adapter_id, self._stream_interval_ms)
        # Create the GLib timeout source ON the main-loop thread.
        GLib.idle_add(self._install_stream_source)

    def _install_stream_source(self) -> bool:
        if not self._streaming:
            return False  # subscription already gone
        self._stream_source_id = GLib.timeout_add(
            self._stream_interval_ms, self._stream_tick)
        return False  # one-shot idle

    def _stream_tick(self) -> bool:
        # Runs on the GLib main-loop thread, so set_value()'s PropertiesChanged
        # signal is emitted from the correct thread and reaches BlueZ.
        if not self._streaming:
            return False  # stop the timeout source
        prof = self.profile
        # Advance any per-tick animation (the demo value-walk) on this loop thread,
        # then stream the profile's CURRENT frame so the drift is reflected. Falls
        # back to the last explicitly-pushed frame for profiles without these hooks.
        if getattr(prof, "tick", None) is not None:
            try:
                prof.tick()
            except Exception:
                log.exception("[%s] tick failed", self.adapter_id)
        frame = self._last_frame
        if getattr(prof, "current_frame", None) is not None:
            try:
                frame = prof.current_frame()
                self._last_frame = frame
            except Exception:
                log.exception("[%s] current_frame failed", self.adapter_id)
        if frame is not None and self._notify_char is not None:
            try:
                self._notify_char.set_value(list(frame))
            except Exception:
                log.exception("[%s] stream push failed", self.adapter_id)
        return True  # keep the timeout source alive

    # -- polled (handshake-then-stream) tick driver ------------------------
    def _start_polled_tick(self) -> None:
        """Drive a 'polled' profile's tick on the loop timer (UNI-T handshake-stream).

        Unlike _start_stream this does NOT push current_frame() — the polled profile
        self-pushes each measurement frame via the notify_cb it got from on_start, so
        the driver only advances tick (which is a no-op until GET_DATA arms the
        stream). Pushing current_frame() here too would emit two frames per tick.
        """
        if self._streaming:
            return
        self._streaming = True
        log.info("[%s] polled tick @ %dms while subscribed (self-push on GET_DATA)",
                 self.adapter_id, self._stream_interval_ms)
        GLib.idle_add(self._install_polled_source)

    def _install_polled_source(self) -> bool:
        if not self._streaming:
            return False  # subscription already gone
        self._stream_source_id = GLib.timeout_add(
            self._stream_interval_ms, self._polled_tick)
        return False  # one-shot idle

    def _polled_tick(self) -> bool:
        # Runs on the GLib main-loop thread. Drive the profile's tick ONLY; the
        # profile self-pushes the frame via its notify_cb (no current_frame() push).
        if not self._streaming:
            return False  # stop the timeout source
        try:
            self.profile.tick()
        except Exception:
            log.exception("[%s] polled tick failed", self.adapter_id)
        return True  # keep the timeout source alive

    def _stop_stream(self) -> None:
        self._streaming = False
        src = self._stream_source_id
        self._stream_source_id = None
        if src is not None:
            GLib.idle_add(self._remove_source, src)

    @staticmethod
    def _remove_source(src: int) -> bool:
        try:
            GLib.source_remove(src)
        except Exception:
            pass
        return False

    # -- unsolicited (no-CCCD) delivery ------------------------------------
    def _connected_acl_handle(self) -> Optional[int]:
        """The ACL handle of a client connected to OUR adapter, via ``hcitool con``.

        We want the link where the remote is CENTRAL (the phone connected to us as
        a peripheral). ``hcitool con`` lists ``... handle N state 1 lm CENTRAL``.
        Returns the handle int, or None if no such link.
        """
        try:
            out = subprocess.run(["hcitool", "-i", self.adapter_id, "con"],
                                 capture_output=True, text=True, timeout=4).stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        best = None
        for line in out.splitlines():
            m = re.search(r"handle\s+(\d+).*lm\s+(\w+)", line)
            if not m:
                continue
            handle, role = int(m.group(1)), m.group(2).upper()
            # The phone is CENTRAL on a link to our peripheral; prefer those.
            if role == "CENTRAL":
                best = handle
            elif best is None:
                best = handle
        return best

    def _poll_connection(self) -> bool:
        """GLib timeout: track the client link + drive unsolicited injection.

        While a client is connected but has NOT subscribed via the CCCD, run an
        injection stream (real meters free-stream regardless of CCCD; apps that
        don't subscribe — e.g. the OWON Java app — only get data this way). As soon
        as a CCCD write arrives (_on_notify_subscription) we defer to BlueZ.
        """
        if self._unsol is None:
            return True  # keep polling in case the notifier arms later
        handle = self._connected_acl_handle()
        self._unsol.set_acl_handle(handle)
        connected = handle is not None
        want_inject = connected and not self._client_subscribed
        if want_inject and not self._unsol_streaming:
            self._start_unsol_stream()
        elif not want_inject and self._unsol_streaming:
            self._stop_unsol_stream()
        if not connected:
            self._client_subscribed = False  # reset for the next connection
        return True  # keep the poll alive

    def _start_unsol_stream(self) -> None:
        if self._unsol_streaming:
            return
        self._unsol_streaming = True
        log.info("[%s] client connected without CCCD subscription — injecting "
                 "unsolicited frames @ %dms (no-CCCD path)",
                 self.adapter_id, self._stream_interval_ms)
        self._unsol_source_id = GLib.timeout_add(
            self._stream_interval_ms, self._unsol_tick)

    def _unsol_tick(self) -> bool:
        if not self._unsol_streaming or self._unsol is None:
            return False
        if self._client_subscribed:
            return False  # BlueZ took over
        prof = self.profile
        if getattr(prof, "tick", None) is not None:
            try:
                prof.tick()
            except Exception:
                log.exception("[%s] tick failed", self.adapter_id)
        frame = self._last_frame
        if getattr(prof, "current_frame", None) is not None:
            try:
                frame = prof.current_frame()
                self._last_frame = frame
            except Exception:
                log.exception("[%s] current_frame failed", self.adapter_id)
        if frame is not None:
            try:
                self._unsol.inject(bytes(frame))
            except Exception:
                log.exception("[%s] unsolicited inject failed", self.adapter_id)
        return True

    def _stop_unsol_stream(self) -> None:
        self._unsol_streaming = False
        src = self._unsol_source_id
        self._unsol_source_id = None
        if src is not None:
            try:
                GLib.source_remove(src)
            except Exception:
                pass

    def _on_notify_read(self):
        # A read of the notify char just returns the current value (last frame).
        ch = self._notify_char
        return list(ch.value) if ch and ch.value else []

    def _on_write(self, value, options):
        data = bytes(value)
        log.info("[%s] WRITE %s <- %s (%d bytes)",
                 self.adapter_id, self.profile.write_uuids[0], data.hex(), len(data))
        # If the profile knows how to react to control-button commands (e.g. the
        # Voltcraft HOLD/Select/Range/AC-DC keys written to FFF3), let it mutate
        # its internal reading and hand back the new frame to stream. We push it via
        # notify(), which also updates _last_frame so the re-push loop keeps the
        # reaction on screen.
        handler = self.profile.command_handler
        if handler is None:
            return
        try:
            frame = handler(data)
        except Exception:
            log.exception("[%s] command handler failed for %s",
                          self.adapter_id, data.hex())
            return
        if frame is not None:
            log.info("[%s] command %s -> reply frame %s",
                     self.adapter_id, data.hex(), bytes(frame).hex())
            self.notify(bytes(frame))

    def _on_secure_write(self, value, options):
        data = bytes(value)
        self._last_secure_write = data
        log.info("[%s] SECURE-WRITE %s <- %s (%d bytes)  [FFF1 challenge]",
                 self.adapter_id, self.profile.secure_uuid, data.hex(), len(data))

    def _on_secure_read(self):
        # Reply with the profile's computed auth response to the last challenge.
        if self.profile.auth_response is None:
            return list(self._last_secure_write)
        resp = self.profile.auth_response(self._last_secure_write)
        log.info("[%s] SECURE-READ %s -> %s (%d bytes)  [FFF1 response]",
                 self.adapter_id, self.profile.secure_uuid, resp.hex(), len(resp))
        return list(resp)

    def _on_info_read(self):
        # The app reads FFF2 on connect to identify the meter (byte0 = series id).
        data = self.profile.info_response() if self.profile.info_response else b""
        log.info("[%s] INFO-READ %s -> %s (%d bytes)  [FFF2 device-info, series=%d]",
                 self.adapter_id, self.profile.info_uuid, bytes(data).hex(), len(data),
                 data[0] if data else -1)
        return list(data)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Publish the GATT app + advertisement on a background thread."""
        def _run():
            self._resolve_chars()
            # Hand a 'polled' (handshake-then-stream) profile the thread-safe push fn
            # so its tick can self-push the periodic measurement stream once the app's
            # GET_DATA write arms it. The notify char is live now (post-resolve).
            on_start = self.profile.on_start
            if on_start is not None:
                try:
                    on_start(self.notify)
                except Exception:
                    log.exception("[%s] profile.on_start failed", self.adapter_id)
            log.info("[%s] publishing '%s'  service=%s notify=%s write=%s%s",
                     self.adapter_id, self.local_name, self.profile.service_uuid,
                     self.profile.notify_uuid, ",".join(self.profile.write_uuids),
                     f" secure={self.profile.secure_uuid}" if self.profile.secure_uuid else "")
            self._ready.set()
            try:
                self._periph.publish()  # blocks on the GLib main loop
            except Exception:
                log.exception("[%s] publish() failed", self.adapter_id)
                raise

        self._thread = threading.Thread(target=_run, name=f"gatt-{self.adapter_id}",
                                        daemon=True)
        self._thread.start()
        # give BlueZ a moment to register the application + advertisement
        self._ready.wait(timeout=5.0)

        # Arm the no-CCCD (unsolicited) notifier + the connection poll that drives
        # it. Scheduled onto the GLib loop thread so the timeout source is created
        # there. Stream profiles only (polled meters answer writes, not free-stream).
        if self._unsol_enabled:
            self._unsol = UnsolicitedNotifier(self.adapter_id)
            if self._unsol.start():
                # seed the learned value handle from any explicit override later;
                # poll the link every 500ms (connection up/down + inject lifecycle).
                GLib.idle_add(self._install_unsol_poll)
            else:
                self._unsol = None  # binding failed entirely; no-op

    def _install_unsol_poll(self) -> bool:
        GLib.timeout_add(500, self._poll_connection)
        return False  # one-shot idle

    def notify(self, frame: bytes) -> None:
        """Push one crafted frame on the notify characteristic.

        Safe to call from any thread. The actual set_value() (which emits the
        D-Bus PropertiesChanged signal that BlueZ turns into a BLE notification)
        is marshalled onto the GLib main-loop thread via GLib.idle_add, because
        dbus-python signal emission is not thread-safe — emitting off-thread does
        not reliably reach BlueZ, so the phone would never receive the frame.
        """
        if self._notify_char is None:
            self._resolve_chars()
        if self._notify_char is None:
            raise RuntimeError("notify characteristic not ready")
        # Remember the latest frame so the streaming loop keeps re-pushing it while
        # a client is subscribed (real meters free-stream; silence => app times out).
        self._last_frame = bytes(frame)
        GLib.idle_add(self._do_notify, list(frame))
        log.debug("[%s] NOTIFY %s -> %s", self.adapter_id,
                  self.profile.notify_uuid, frame.hex())

    def _do_notify(self, value_list) -> bool:
        # Runs on the GLib main-loop thread.
        try:
            if self._notify_char is not None:
                self._notify_char.set_value(value_list)
        except Exception:
            log.exception("[%s] notify push failed", self.adapter_id)
        return False  # one-shot
