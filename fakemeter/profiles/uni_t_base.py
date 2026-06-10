"""UNI-T family shared layer — the polled AB-CD handshake-then-stream codec.

This is to the UNI-T family what ``owon_base`` is to the OWON family: everything
the UNI-T meters share, so a per-model profile supplies only its measurement
encoder + a tiny config. The UNI-T family is ``interaction='polled'`` — there is NO
free-stream-on-subscribe — but it is NOT reply-one-frame-per-write. The corrected
model (verified against the driver ``uni-t.ts`` + the decompiled UNI-T Smart Measure
app, ``com.uni_t.multimeter`` ``BleManager``/``com.inuker`` BLE lib) is
**handshake-then-stream**:

  1. App subscribes (the inuker lib WRITES the 0x2902 CCCD — see the report; this is
     the OPPOSITE of the OWON Java app, so UNI-T does NOT hit the CCCD wall).
  2. App WRITES ``GET_NAME`` (``AB CD 03 5F 01 DA``) → meter replies a *name/control*
     frame announcing the model (``requestDeviceType`` in BleManager).
  3. App WRITES ``GET_DATA`` (``AB CD 03 5D 01 D8``; = ``sendCmd(93)``/``pushCmdToList(93)``)
     → meter **STARTS FREE-STREAMING** 19-byte measurement frames periodically; the
     app's ``SamplingManager`` reads them continuously. The write GATES the stream;
     it does NOT pull a single frame.
  4. The meter periodically pushes a re-arm nudge: a 9-byte ``…AA AA…`` (=
     ``type-request`` → app re-sends GET_NAME) or a 7-byte ``…FF 00…`` (=
     ``data-request``). Optional; the app keeps streaming regardless.

What lives here (the UNI-T-shared layer):
  * **GATT** — the Microchip/ISSC "Transparent UART" service (NOT FFF0):
    service ``49535343-fe7d-…``, notify ``…1e4d…``, write ``…8841…`` (with a
    ``…6daa…`` fallback the real meters also expose). No secure/info char.
  * **AB-CD framing** — both length conventions the family uses:
      - the UT60BT/UT161 generic frame: single ``<len>`` byte, 16-bit BIG-ENDIAN
        additive checksum over bytes[0..n-3] (see ``build_frame_len8``).
      - the newer ut117c/ut171/ut181a/ut219p frames: 16-bit length, additive
        checksum stored either BE or LE (per model) — see ``build_frame_len16``.
  * **the command seam** — ``command(request) -> response|None`` that PARSES the
    inbound AB-CD frame, branches on its opcode, and returns the right KIND of
    reply: a *name/control* frame for GET_NAME, the FIRST *measurement* frame for
    GET_DATA (which ALSO arms periodic streaming — see below). Different request
    opcodes map to different reply frame kinds.
  * **handshake-then-stream self-push** — on GET_DATA the meter ARMS streaming
    (``self._streaming = True``). The first measurement frame is returned from
    ``command`` (so the app gets one immediately even with no further help). The
    SUBSEQUENT periodic frames are self-pushed by ``tick()``: while armed and a
    ``notify_cb`` is set, every ``tick`` advances the value-walk and pushes a fresh
    measurement frame (plus the occasional re-arm nudge).
  * **HOLD/REL/value-walk** via the shared ``meter_core.InteractiveMeter``.

A profile builds a :class:`UniTProfile` config + a :class:`UniTMeter`, then calls
:func:`make_profile`, which returns a fully-wired :class:`Profile` with
``interaction='polled'``.

REQUIRED gatt_server.py / base.py SEAM (flagged, NOT edited here — see the agent
report). The handshake-then-stream self-push needs the server to drive ``tick`` AND
to hand the profile a push callback, because in ``'polled'`` mode the server's
stream loop is OFF and the profile has no reference to ``MeterServer.notify``. The
exact, minimal addition the OTHER agent must make to ``gatt_server.py`` + ``base.py``:

  1. **``base.Profile.on_start: Optional[Callable[[Callable[[bytes], None]], None]] = None``**
     — a new optional field. ``make_profile`` already sets it to ``meter.on_start``.
  2. In ``gatt_server.MeterServer``: after ``_resolve_chars()`` (in ``start``'s
     ``_run`` or on first subscribe), if ``profile.on_start`` is not None call
     ``profile.on_start(self.notify)``, handing the profile a thread-safe push fn.
  3. In ``gatt_server._on_notify_subscription``: for ``'polled'`` profiles that
     expose a ``tick``, ALSO install the GLib timeout source (the same
     ``_start_stream`` machinery, which already calls ``tick`` then pushes
     ``current_frame``) — OR simply run ``_start_stream`` for polled-with-tick too.
     Driving ``tick`` on the loop timer is what produces the periodic measurement
     frames after GET_DATA arms streaming. (``tick`` is a no-op until GET_DATA arms
     it, so installing the timer on subscribe is harmless.)

Until that seam lands, ``command`` still returns the FIRST measurement frame on
GET_DATA (the server's existing ``_on_write`` → ``command_handler`` → ``notify``
path pushes it), so a manual REPL ``r`` re-send keeps the reading visible; only the
AUTOMATIC periodic re-push is gated on the seam above.

OTHER SHARED-INTERFACE GAP (flagged):
  * ``Profile.write_uuid`` is a single string; the UNI-T ``gatt.write`` is a LIST
    (primary ``…8841…`` + fallback ``…6daa…`` — the app actually defaults to the
    ``…6daa…`` one, see BleManager.WriteUUID). We expose only the primary here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..meter_core import InteractiveConfig, InteractiveMeter
from .base import Profile, Reading

# --- GATT UUIDs — the Microchip/ISSC "Transparent UART" service ---------------
# Confirmed by enumerating the physical UT60BTk (2026-06-06) and matching the
# driver repo's uni-t.ts/ut*.ts `gatt`. NOT the OWON FFF0 set; there is no
# secure/info characteristic in this family.
ISSC_SERVICE = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
ISSC_NOTIFY = "49535343-1e4d-4bd9-ba61-23c647249616"
ISSC_WRITE = "49535343-8841-43f4-a8d4-ecbe34729bb3"
# The real meters expose a second write char as a fallback. `Profile.write_uuid`
# is a single string so we expose only the primary; the gap is flagged above.
ISSC_WRITE_FALLBACK = "49535343-6daa-4d02-abf6-19569aca69fe"

SOF0 = 0xAB
SOF1 = 0xCD


# ---------------------------------------------------------------------------
# AB-CD frame builders / parsers.
#
# The family has TWO length conventions. The UT60BT/UT161 generic frame uses a
# single <len> byte (len = bytes-after-it) and a 16-bit BIG-ENDIAN additive
# checksum over bytes[0..n-3]. The newer models (ut117c/ut171/ut181a/ut219p) use
# a 16-bit length and an additive checksum stored BE or LE depending on the model.
# ---------------------------------------------------------------------------
def be16_checksum(body: bytes) -> tuple[int, int]:
    """16-bit additive checksum of ``body`` as (hi, lo) — UT60BT measurement frame."""
    s = sum(body) & 0xFFFF
    return (s >> 8) & 0xFF, s & 0xFF


def le16_checksum(body: bytes) -> tuple[int, int]:
    """16-bit additive checksum of ``body`` as (lo, hi) — ut171/ut181a frames."""
    s = sum(body) & 0xFFFF
    return s & 0xFF, (s >> 8) & 0xFF


def build_frame_len8(payload: bytes) -> bytes:
    """Build an ``AB CD <len> <payload...> <chkHi> <chkLo>`` frame (UT60BT family).

    ``<len>`` counts the bytes AFTER it, i.e. payload + the 2 checksum bytes. The
    checksum is the 16-bit BIG-ENDIAN additive sum of all preceding bytes
    (``AB CD <len> <payload>``). This is the inverse of ``FrameParser`` /
    ``checksumOk`` in the driver repo's framing.ts.
    """
    length = len(payload) + 2  # payload + 2 checksum bytes
    head = bytes([SOF0, SOF1, length & 0xFF]) + payload
    hi, lo = be16_checksum(head)
    return head + bytes([hi, lo])


def build_frame_len16(opcode: int, body: bytes, *, little_endian_chk: bool = False,
                      little_endian_len: bool = False, len_includes_chk: bool = True,
                      chk_from_zero: bool = False) -> bytes:
    """Build an ``AB CD <len16> <opcode> <body...> <chk16>`` frame (newer models).

    ``little_endian_len`` picks the length byte order:
      * ut117c/ut219p: BIG-endian length (``[2]=hi [3]=lo``).
      * ut171/ut181a: LITTLE-endian length (``[2]=lo [3]=hi``).
    ``len_includes_chk`` picks the family's len convention:
      * ut117c/ut219p: len = payload byte count starting at the opcode (excl. chk).
      * ut171/ut181a: len = opcode + body + 2 checksum bytes.
    ``chk_from_zero`` picks the checksum span:
      * ut117c sums from byte 0 (INCLUDING the ``AB CD`` header).
      * ut171/ut181a/ut219p sum from byte 2 (excluding the header).
    The checksum is stored BE or LE per ``little_endian_chk``.
    """
    payload = bytes([opcode & 0xFF]) + bytes(body)
    length = len(payload) + (2 if len_includes_chk else 0)
    if little_endian_len:
        len_bytes = bytes([length & 0xFF, (length >> 8) & 0xFF])
    else:
        len_bytes = bytes([(length >> 8) & 0xFF, length & 0xFF])
    head = bytes([SOF0, SOF1]) + len_bytes + payload
    chk_body = head if chk_from_zero else head[2:]
    if little_endian_chk:
        a, b = le16_checksum(chk_body)
    else:
        a, b = be16_checksum(chk_body)
    return head + bytes([a, b])


def parse_opcode_len8(frame: bytes) -> Optional[int]:
    """Return the command opcode of an ``AB CD <len> <cmd> ...`` request, or None.

    The app's poll/soft-button commands are ``AB CD 03 <cmd> 01 <chk>`` (len8). The
    opcode is byte[3]. Returns None for anything not a plausible AB-CD frame.
    """
    if len(frame) < 4 or frame[0] != SOF0 or frame[1] != SOF1:
        return None
    return frame[3]


def parse_opcode_len16(frame: bytes) -> Optional[int]:
    """Return the command opcode of an ``AB CD <len16> <cmd> ...`` request, or None."""
    if len(frame) < 5 or frame[0] != SOF0 or frame[1] != SOF1:
        return None
    return frame[4]


# ---------------------------------------------------------------------------
# Per-profile UNI-T config.
# ---------------------------------------------------------------------------
@dataclass
class UniTProfile:
    """Everything a UNI-T profile supplies on top of the shared polled layer.

    The encoder builds the model's measurement frame from a Reading; ``name_frame``
    is the bytes the meter replies to GET_NAME (the 11-byte control/name frame on
    the UT60BT). ``get_name_op`` / ``get_data_op`` are the request opcodes the
    command_handler branches on (the meter answers GET_NAME with a name frame and
    GET_DATA with a measurement frame). ``parse_opcode`` extracts the opcode from an
    inbound frame (len8 for UT60BT, len16 for the newer models).
    """

    id: str
    label: str
    default_name: str

    # Reading -> measurement frame bytes (inverse of the model's driver decoder).
    encode: Callable[[Reading], bytes]

    # The reply to a GET_NAME request: the control/name frame announcing the model.
    # Bytes, or a callable building it from the advertised name.
    name_frame: bytes = b""

    # Request opcodes the command_handler routes on. Defaults match the UT60BT
    # generic frame (GET_NAME 0x5F, GET_DATA 0x5D).
    get_name_op: int = 0x5F
    get_data_op: int = 0x5D

    # Extract the opcode from an inbound AB-CD frame. Defaults to the len8 (UT60BT)
    # convention; newer models pass ``parse_opcode_len16``.
    parse_opcode: Callable[[bytes], Optional[int]] = parse_opcode_len8

    # Opcode -> control-name map for the soft-button keys (HOLD/REL/SELECT/RANGE/…).
    # Control names must be keys of CONTROL_ACTIONS. A control press mutates the
    # interactive state; the meter then replies with a fresh measurement frame.
    controls: Dict[int, str] = field(default_factory=dict)

    # Interactive cycle tables (the model's gear ordering).
    select_cycle: List[str] = field(default_factory=list)
    acdc_toggle: Dict[str, str] = field(default_factory=dict)
    range_dp_cycle: List[int] = field(default_factory=lambda: [3, 2, 1, 0])

    # Initial reading the meter reports.
    initial: Reading = field(default_factory=lambda: Reading(
        value=4.200, function="DCV", prefix="", decimals=3))

    function_codes: List[str] = field(default_factory=list)
    presets: dict = field(default_factory=dict)

    # GATT (the shared ISSC set). The real UNI-T meters expose BOTH write chars
    # (``…8841…`` + ``…6daa…``); the Smart Measure app actually prefers ``…6daa…``.
    # ``write_uuid`` now accepts a list, so we publish both and the app can write to
    # whichever it probes. ``…6daa…`` is listed first to match the app's default.
    service_uuid: str = ISSC_SERVICE
    notify_uuid: str = ISSC_NOTIFY
    write_uuid: object = field(  # str | list[str]; default = both write chars
        default_factory=lambda: [ISSC_WRITE_FALLBACK, ISSC_WRITE])


# Named controls -> the generic InteractiveMeter action that services them. Same
# vocabulary as owon_base (lines up with the driver repo's MeterControl enum), so
# a profile's opcode map points at these names.
CONTROL_ACTIONS = {
    "hold": lambda m: m.toggle_hold(),
    "select": lambda m: m.select_next(),
    "range": lambda m: m.range_next(),
    "acdc": lambda m: m.acdc_toggle(),
    "rel": lambda m: m.rel_toggle(),
    "maxmin": lambda m: m.maxmin_next(),
    "lpf": lambda m: m.toggle_flag("lpf"),
    # Acknowledged but no primary-display change (backlight, Hz/duty toggle, etc.).
    "ack": lambda m: m.current_frame(),
}


class UniTMeter:
    """A live polled UNI-T meter: handshake-then-stream over the AB-CD command seam.

    Holds the single source-of-truth reading and answers each app WRITE. The seam:
      * a GET_NAME request -> the control/name frame (model identity),
      * a GET_DATA request -> ARM periodic streaming + return the FIRST measurement
        frame (the write gates the stream; subsequent frames come from ``tick``),
      * a soft-button opcode -> mutate state via CONTROL_ACTIONS, reply with the new
        measurement frame,
      * anything else -> None (acknowledged-but-silent / unknown opcode).

    Once GET_DATA has armed streaming and the server has handed us a ``notify_cb``
    (via :meth:`on_start`), :meth:`tick` self-pushes a fresh measurement frame each
    tick — this is the periodic free-stream the app's SamplingManager consumes —
    and occasionally a re-arm nudge (the AB-CD type-request/data-request the driver's
    ``onRequest`` answers).
    """

    # Push a re-arm nudge every Nth streaming tick (mirrors the real meter's
    # occasional 9-byte AA-AA / 7-byte FF-00 frames). 0 disables nudges.
    NUDGE_EVERY = 0

    def __init__(self, cfg: UniTProfile):
        self.cfg = cfg
        self.controls = cfg.controls
        self.meter = InteractiveMeter(
            InteractiveConfig(
                encode=cfg.encode,
                select_cycle=cfg.select_cycle,
                acdc_toggle=cfg.acdc_toggle,
                range_dp_cycle=cfg.range_dp_cycle,
            ),
            initial=cfg.initial,
        )
        # Handshake-then-stream state. ``_streaming`` is armed by GET_DATA and
        # gates the periodic self-push in ``tick``. ``_notify_cb`` is the
        # thread-safe push fn the server hands us via on_start (None until then,
        # in which case ``tick`` simply advances the walk without self-pushing).
        self._streaming = False
        self._notify_cb: Optional[Callable[[bytes], None]] = None
        self._tick_count = 0

    # -- server handshake seam ---------------------------------------------
    def on_start(self, notify_cb: Callable[[bytes], None]) -> None:
        """Receive the server's thread-safe push callback (``MeterServer.notify``).

        Called once by the server after publish/resolve (see the REQUIRED SEAM in
        the module docstring). Lets a polled meter self-push the periodic stream +
        re-arm nudges without reaching into the server. Safe to call more than once.
        """
        self._notify_cb = notify_cb

    @property
    def streaming(self) -> bool:
        return self._streaming

    def stop_stream(self) -> None:
        """Disarm the stream (e.g. on disconnect / a stop command)."""
        self._streaming = False
        self._tick_count = 0

    # -- the polled command seam -------------------------------------------
    def command(self, data: bytes) -> Optional[bytes]:
        """Service one app WRITE; return the reply frame (or None).

        Branches on the inbound opcode: GET_NAME -> name frame, GET_DATA -> ARM
        streaming + first measurement frame, a mapped soft-button -> state-mutating
        reply. Returns None for an unrecognised opcode (the server stays silent)."""
        if not data:
            return None
        op = self.cfg.parse_opcode(bytes(data))
        if op is None:
            return None
        if op == self.cfg.get_name_op:
            return self.cfg.name_frame or None
        if op == self.cfg.get_data_op:
            # GET_DATA gates the free-stream: arm periodic self-push and return the
            # first measurement frame immediately (the write does NOT pull a single
            # frame — it starts the stream).
            self._streaming = True
            self._tick_count = 0
            return self.meter.current_frame()
        name = self.controls.get(op)
        if name is None:
            return None
        action = CONTROL_ACTIONS.get(name)
        if action is None:
            return None
        # A control press mutates state and yields a fresh measurement frame.
        return action(self.meter)

    # -- periodic self-push (the post-GET_DATA stream) ----------------------
    def tick(self) -> None:
        """Advance one stream tick. While streaming is armed and a notify_cb is
        present, push a fresh measurement frame (the periodic stream the app reads),
        and occasionally a re-arm nudge. A no-op until GET_DATA arms streaming, so
        the server may install the tick timer on subscribe harmlessly."""
        self.meter.tick()  # advance the demo value-walk regardless
        if not self._streaming or self._notify_cb is None:
            return
        self._tick_count += 1
        if self.NUDGE_EVERY and self._tick_count % self.NUDGE_EVERY == 0:
            nudge = self.rearm_nudge()
            if nudge is not None:
                self._notify_cb(nudge)
        self._notify_cb(self.meter.current_frame())

    def rearm_nudge(self) -> Optional[bytes]:
        """The occasional unsolicited re-arm frame the meter emits (the driver's
        ``onRequest`` answers it by re-writing GET_NAME/GET_DATA). The UT60BT generic
        frame uses a 9-byte ``…AA AA…`` (type-request → re-send GET_NAME). Override
        per-model (or set ``NUDGE_EVERY = 0`` to disable). Default: the 9-byte
        type-request nudge."""
        # AB CD 06 AA AA <pad...> — a 9-byte frame whose bytes[3..4] = AA AA, which
        # the app classifies as a type-request (re-send GET_NAME). build_frame_len8
        # makes len = payload+2; payload of 4 bytes => total 9.
        return build_frame_len8(bytes([0xAA, 0xAA, 0x00, 0x00]))

    # -- REPL hooks ---------------------------------------------------------
    def current_frame(self) -> bytes:
        return self.meter.current_frame()

    def reset_state(self, reading: Optional[Reading] = None) -> None:
        self.meter.reset_state(reading)

    def set_walk(self, on: bool) -> None:
        self.meter.set_walk(on)


def make_profile(cfg: UniTProfile, meter: UniTMeter) -> Profile:
    """Build the fully-wired polled :class:`Profile` for a UNI-T meter instance.

    No secure/info char (the family has none). ``interaction='polled'`` so the
    server stays silent on subscribe; the app's GET_DATA write ARMS the stream and
    ``tick`` self-pushes the periodic measurement frames (handshake-then-stream).

    The profile carries:
      * ``command_handler`` = ``meter.command`` — GET_NAME→name, GET_DATA→arm+first
        frame, soft-button→state-mutating reply (the server pushes the return).
      * ``tick`` = ``meter.tick`` — the periodic self-push once GET_DATA armed it.
      * ``current_frame`` — the current measurement frame (REPL ``r`` / first push).
      * ``on_start`` (attached below) — receives the server's ``notify`` callback so
        ``tick`` can self-push. ``Profile`` has no ``on_start`` FIELD yet (base.py is
        owned by another agent); we attach it as an instance attribute so the
        server can ``getattr(profile, 'on_start', None)`` once it wires the seam.
        See the REQUIRED SEAM in the module docstring.
    """
    prof = Profile(
        id=cfg.id,
        label=cfg.label,
        service_uuid=cfg.service_uuid,
        notify_uuid=cfg.notify_uuid,
        write_uuid=cfg.write_uuid,
        secure_uuid=None,
        info_uuid=None,
        default_name=cfg.default_name,
        encode=cfg.encode,
        command_handler=meter.command,
        current_frame=meter.current_frame,
        tick=meter.tick,
        interaction="polled",
        presets=cfg.presets,
        reset_state=meter.reset_state,
        set_walk=meter.set_walk,
        function_codes=(cfg.function_codes or None),
    )
    # Seam: the server hands the profile its thread-safe push fn here so the
    # handshake-then-stream tick can self-push. ``base.Profile.on_start`` is now a
    # real field and ``gatt_server.MeterServer.start`` calls it after the notify char
    # is live; ``tick`` then self-pushes the periodic stream once GET_DATA arms it.
    prof.on_start = meter.on_start
    return prof
