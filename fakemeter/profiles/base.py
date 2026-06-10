"""Profile interface shared by every emulated meter family.

A *profile* is everything the GATT server needs to impersonate one meter family:

  * GATT UUIDs (service / notify / write, and optionally a "secure"/auth char),
  * a default advertised local name,
  * a frame *encoder* (``Reading`` -> ``bytes``) that is the exact inverse of the
    corresponding *decoder* in the driver repo,
  * a set of named preset patterns (representative readings + special modes),
  * optionally an auth-handshake responder for the FFF1 anti-counterfeit gate.

Adding a new meter family (bdm / ai-care / owon-plus / owon-old / uni-t) is just a
new module under ``profiles/`` that subclasses :class:`Profile` and registers
itself in ``profiles/__init__.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Union


# ---------------------------------------------------------------------------
# The thing the emulator pushes. This is the *input* to a profile's encoder —
# the inverse of the ``Reading`` a driver's decoder produces. It is deliberately
# small: just enough to drive an LCD.
# ---------------------------------------------------------------------------
@dataclass
class Reading:
    """A meter reading to be encoded into an on-wire frame.

    Attributes:
        value: The displayed numeric value (e.g. ``4.200``). ``None`` for special
            displays such as overload.
        function: Function/quantity code label the profile understands. For
            voltcraft this maps to the 5-bit function field; see
            ``voltcraft.FUNCTION_CODES``. Examples: ``"V_DC"``, ``"OHM"``,
            ``"A_DC"``.
        prefix: SI prefix as a single string: one of ``p n µ m '' k M G``.
        overload: If True, emit the OL ("O.L") sentinel instead of digits.
        underload: If True, emit the UL ("U.L") sentinel.
        # Mode-word annunciators. This is the SUPERSET the driver repo's
        # ``Reading.flags`` exposes (max/min/hold/rel/auto/lowBattery/hvWarning/
        # peakMax/peakMin) plus ``lpf`` (which the voltcraft R10W state word proved
        # real). A profile's encoder packs whichever flags its frame can express;
        # the rest stay False. Keep names in sync with the driver so a future shared
        # flag->bit table per family is a 1:1 mapping.
        hold: HOLD annunciator.
        rel: REL (relative/delta) annunciator.
        auto: AUTO (autorange) annunciator.
        low_battery: low-battery annunciator.
        min: MIN annunciator.
        max: MAX annunciator.
        lpf: LPF (low-pass-filter) annunciator (voltcraft R10W; not all families).
        hv_warning: high-voltage warning annunciator.
        peak_max: peak-max-capture annunciator.
        peak_min: peak-min-capture annunciator.
        # Escape hatch for the bit-sweep: set exactly one raw mode-word bit. When
        # not None this OVERRIDES the flag booleans above and writes a single bit.
        raw_mode_word: If not None, the literal 16-bit mode word to emit.
        decimals: Force this many decimal places on the LCD (a real meter's decimal
            point is fixed by range, not by the value's significant digits, so
            ``4.2`` on a 3-decimal range shows ``4.200``). ``None`` = derive from
            the value's own fractional digits.
    """

    value: Optional[float] = 0.0
    function: str = "V_DC"
    prefix: str = ""
    overload: bool = False
    underload: bool = False
    decimals: Optional[int] = None

    hold: bool = False
    rel: bool = False
    auto: bool = False
    low_battery: bool = False
    min: bool = False
    max: bool = False
    lpf: bool = False         # LPF (low-pass filter) annunciator (voltcraft)
    hv_warning: bool = False  # high-voltage warning
    peak_max: bool = False    # peak-max capture
    peak_min: bool = False    # peak-min capture

    raw_mode_word: Optional[int] = None


# A preset is a label + a factory that yields the Reading(s) to play. A factory
# returns a single Reading (static preset) or a list of Readings (the bit-sweep,
# stepped on a timer/keypress).
PresetFactory = Callable[[], "Reading | list[Reading]"]


@dataclass
class Profile:
    """A meter-family emulation profile."""

    id: str
    label: str

    # GATT
    service_uuid: str
    notify_uuid: str
    # The write characteristic UUID(s). A single string for most families, or a LIST
    # when the family exposes several interchangeable write chars (UNI-T's ISSC service
    # exposes both ``…8841…`` and ``…6daa…``; the Smart Measure app prefers ``…6daa…``).
    # The server adds one write characteristic per UUID, all dispatching to the same
    # command handler / write logger. ``write_uuids`` normalises this to a list.
    write_uuid: Union[str, List[str]]
    # Optional FFF1-style auth/"secure" characteristic. None if the family has none.
    secure_uuid: Optional[str] = None
    # Optional FFF2-style "info" characteristic. Many OWON-family apps READ this on
    # connect to identify the meter model/series and disconnect ("device not
    # supported") if it is absent or carries an unknown series id.
    info_uuid: Optional[str] = None

    default_name: str = "FAKE-METER"

    # INTERACTION MODE seam — how the app drives the meter:
    #   'stream' : the meter FREE-STREAMS frames on the notify char as soon as the
    #              app subscribes (the OWON/bdm/ai-care families). The server runs
    #              its 300ms re-push loop and FFF3 button writes mutate state +
    #              re-stream. This is the current/default behaviour.
    #   'polled' : the meter is REQUEST/RESPONSE — no free-stream re-push loop. The
    #              app WRITES a command to the write char and the meter replies on the
    #              notify char; ``command_handler(request) -> response_frame|None``
    #              services each write (the server pushes a non-None return). A polled
    #              profile need not set ``current_frame``.
    #              NOTE (from the live UNI-T AB-CD driver): polled is NOT purely
    #              reactive. (a) The meter answers DIFFERENT request opcodes with
    #              DIFFERENT frame KINDS (e.g. a 'control'/name frame for GET_NAME, a
    #              'measurement' frame for GET_DATA) — that routing lives inside the
    #              profile's command_handler. (b) The meter may also emit UNSOLICITED
    #              frames (the AB-CD type-request/data-request nudges the app's
    #              onRequest answers). The server's stream LOOP is off, but a polled
    #              profile can still push anytime via ``MeterServer.notify()``, and the
    #              ``tick`` hook is left callable if a uni_t_base wants timed nudges.
    # See docs/adding-a-profile.md for how a UNI-T base plugs into 'polled'.
    interaction: str = "stream"

    # reading -> on-wire frame bytes (inverse of the driver decoder)
    encode: Callable[[Reading], bytes] = field(default=lambda r: b"")

    # named presets the control CLI can play
    presets: dict[str, PresetFactory] = field(default_factory=dict)

    # auth responder: given the last bytes written to the secure characteristic,
    # return the bytes to hand back on a read of that characteristic. None => no
    # auth handling (server replies with whatever was last written / empty).
    auth_response: Optional[Callable[[bytes], bytes]] = None

    # info responder: bytes to hand back on a read of the FFF2 info characteristic
    # (e.g. [seriesId, battery, fwMajor, fwMinor, fwPatch]). None => no info char.
    info_response: Optional[Callable[[], bytes]] = None

    # command handler: given the raw bytes the app wrote to the WRITE
    # characteristic (FFF3 — control-button key-presses like HOLD/Select/Range),
    # mutate the profile's internal reading/state and return the NEW on-wire frame
    # to start streaming (so the next notifications reflect the reaction), or None
    # if the command does not change what is displayed (e.g. an RTC sync, or an
    # unrecognised opcode). None => the server just logs the write and keeps
    # streaming the current frame (legacy behaviour). The server wires the returned
    # frame into its stream loop via notify().
    command_handler: Optional[Callable[[bytes], Optional[bytes]]] = None

    # current-frame provider: the bytes the profile would stream RIGHT NOW given
    # its internal state. The controller uses this to seed the very first frame and
    # to let manual REPL edits and button-driven mutations share one source of
    # truth. None => the controller drives state itself (legacy behaviour).
    current_frame: Optional[Callable[[], bytes]] = None

    # time-step hook: called once per stream tick (on the GLib loop thread) to
    # advance any time-based state — e.g. the demo value-walk that makes a numeric
    # reading drift/jitter like a live measurement. After tick(), the server streams
    # current_frame(). None => no per-tick animation (a fixed reading is streamed).
    tick: Optional[Callable[[], None]] = None

    # start hook: called ONCE by the server after the notify characteristic is live
    # (post-publish / -resolve), handed the server's thread-safe push callback
    # (``MeterServer.notify``). A 'polled' family (UNI-T AB-CD handshake-then-stream)
    # uses it so its ``tick`` can SELF-PUSH the periodic measurement frames once the
    # app's GET_DATA write arms the stream — in polled mode the server's stream loop is
    # off and the profile otherwise has no reference to ``notify``. When a profile
    # self-pushes via this callback, the server's polled tick driver must NOT also push
    # ``current_frame`` (avoid a double-push — see gatt_server._polled_tick).
    # None => the server does not hand the profile a push fn (stream profiles).
    on_start: Optional[Callable[[Callable[[bytes], None]], None]] = None

    # ---- optional REPL/control hooks (so the controller stays profile-agnostic) ----
    # The controller (``__main__``) calls these by capability, NOT by profile id, so
    # every profile that maintains interactive state plugs in without the controller
    # importing its module. All optional; None => the controller skips that command.

    # reset_state(reading): sync the profile's interactive source-of-truth to a fresh
    # Reading set in the REPL, so a manual `v`/`f` edit and a button press don't fight.
    reset_state: Optional[Callable[["Reading"], None]] = None

    # set_walk(on): enable/disable the demo value-walk (drift). None => no walk.
    set_walk: Optional[Callable[[bool], None]] = None

    # set_series(id): set the FFF2 device series id (OWON family). None => not settable.
    set_series: Optional[Callable[[int], None]] = None

    # set_auth(table, upper): switch the FFF1 auth scheme for regression probing
    # (OWON family). None => not settable.
    set_auth: Optional[Callable[..., None]] = None

    # function_codes: the function labels this profile's encoder accepts, surfaced by
    # the REPL `v` prompt. None/empty => the REPL shows no function hint.
    function_codes: Optional[list] = None

    @property
    def write_uuids(self) -> List[str]:
        """The write characteristic UUID(s) as a list (single-string back-compat)."""
        if isinstance(self.write_uuid, str):
            return [self.write_uuid]
        return list(self.write_uuid)
