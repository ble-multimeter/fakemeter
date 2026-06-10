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
                 local_name: Optional[str] = None):
        self.profile = profile
        self.adapter_id = adapter_id
        self.adapter_addr = adapter_address(adapter_id)
        self.local_name = local_name or profile.default_name

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

        # Write characteristic — log whatever the app writes (auth probes, etc).
        periph.add_characteristic(
            srv_id=_SRV_ID, chr_id=_CHR_WRITE, uuid=p.write_uuid,
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
        # INTERACTION seam: only the free-STREAMING families run the re-push loop.
        # A 'polled' (request/response, e.g. UNI-T AB-CD) meter stays silent on
        # subscribe and only answers writes — see _on_write / Profile.interaction.
        if self.profile.interaction != "stream":
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

    def _on_notify_read(self):
        # A read of the notify char just returns the current value (last frame).
        ch = self._notify_char
        return list(ch.value) if ch and ch.value else []

    def _on_write(self, value, options):
        data = bytes(value)
        log.info("[%s] WRITE %s <- %s (%d bytes)",
                 self.adapter_id, self.profile.write_uuid, data.hex(), len(data))
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
            log.info("[%s] command %s -> new frame %s",
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
            log.info("[%s] publishing '%s'  service=%s notify=%s write=%s%s",
                     self.adapter_id, self.local_name, self.profile.service_uuid,
                     self.profile.notify_uuid, self.profile.write_uuid,
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
