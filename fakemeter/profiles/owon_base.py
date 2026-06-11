"""OWON-family shared layer — the huge handshake every OWON rebadge shares.

``voltcraft`` (15-byte R10W), ``owon-plus`` (6-byte R2W) and ``owon-old`` (14-byte
ASCII) are all the SAME OWON "iMeter" GATT peripheral with the SAME gates; they
differ ONLY in the measurement-frame encoder and the FFF2 series id. This module
carries everything common so each OWON profile supplies just:

    {frame encoder, series id, gear/Select tables, GATT uuids}

What lives here (the OWON-shared layer):
  * **GATT** service 0xFFF0 + notify FFF4 / write FFF3 / secure FFF1 / info FFF2.
  * **FFF1 MD5 anti-counterfeit auth** (the s1/s2 pickout tables, the mix-elems
    recovery, the raw-16-byte digest wire format) — :class:`OwonAuth`.
  * **FFF2 series-info gate** — a 6-byte ``[series, battery, fwMajor, fwMinor,
    fwPatch, flash]`` response; the app maps ``series`` -> model -> protocol.
  * **FFF3 button-opcode dispatch** — the captured ``[opcode, 0x01]`` key-press
    map (Select/Range/Hold/Rel/Max-Min/LPF/AC-DC + the special modes) wired onto
    the generic :class:`fakemeter.meter_core.InteractiveMeter` actions, plus the
    ``#TIMEsync`` clock-set written to FFF1 (fire-and-forget, no reply).
  * **free-streaming on FFF4** (``interaction='stream'``).

A profile builds an :class:`OwonProfile` config and calls :func:`make_profile`,
which returns a fully-wired :class:`fakemeter.profiles.base.Profile`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..meter_core import InteractiveConfig, InteractiveMeter
from .base import Profile, Reading

# --- GATT UUIDs — common to every OWON "iMeter" rebadge -----------------------
FFF0_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
FFF4_NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
FFF3_WRITE = "0000fff3-0000-1000-8000-00805f9b34fb"
FFF1_SECURE = "0000fff1-0000-1000-8000-00805f9b34fb"  # MD5 anti-counterfeit gate
FFF2_INFO = "0000fff2-0000-1000-8000-00805f9b34fb"     # device-info: series/battery/fw


# ---------------------------------------------------------------------------
# FFF3 control-button opcodes — captured live from the OWON-family app. Pressing
# a meter-screen button writes a 2-byte frame ``[opcode, 0x01]`` (the trailing
# 0x01 is the key-press flag from pressKey(code, true)). Every meter-screen button
# is a real meter command.
# ---------------------------------------------------------------------------
CMD_SELECT = 0x01
CMD_RANGE = 0x02
CMD_HOLD = 0x03
CMD_REL = 0x04
CMD_MAXMIN = 0x06
CMD_LPF = 0x07
CMD_4_20MA = 0x0A
CMD_DISPLAY = 0x0C
CMD_COMPARE = 0x0F
CMD_ACDC = 0x10
CMD_MOTOR = 0x11

# Opcodes that open a special measurement mode (secondary block / dedicated
# parser): captured + acknowledged but produce no primary-display change.
SPECIAL_OPCODES = (CMD_COMPARE, CMD_MOTOR, CMD_4_20MA, CMD_DISPLAY)

# Named controls -> the generic InteractiveMeter action that services them. This is
# the family-agnostic vocabulary (it lines up with the driver repo's `MeterControl`
# enum: hold/rel/select/range/maxMin/…). A profile maps its OWON OPCODES onto these
# names via OwonProfile.controls; OwonMeter.command then dispatches by name. Voltcraft
# (and any OWON sibling that shares the opcodes) uses DEFAULT_CONTROLS below; a sibling
# with a different opcode map just overrides it. Actions take the InteractiveMeter and
# return the new frame (or None for an acknowledged-but-no-change control).
CONTROL_ACTIONS = {
    "hold": lambda m: m.toggle_hold(),
    "select": lambda m: m.select_next(),
    "range": lambda m: m.range_next(),
    "acdc": lambda m: m.acdc_toggle(),
    "rel": lambda m: m.rel_toggle(),
    "maxmin": lambda m: m.maxmin_next(),
    "lpf": lambda m: m.toggle_flag("lpf"),
    # Acknowledged-but-no-primary-change (special modes / secondary-block displays).
    "special": lambda m: None,
}

# Default OWON opcode -> control-name map (captured live from the Voltcraft app; the
# R10W/R2W OWON siblings share this set). A profile overrides via OwonProfile.controls.
DEFAULT_CONTROLS = {
    CMD_HOLD: "hold",
    CMD_SELECT: "select",
    CMD_RANGE: "range",
    CMD_ACDC: "acdc",
    CMD_REL: "rel",
    CMD_MAXMIN: "maxmin",
    CMD_LPF: "lpf",
    CMD_COMPARE: "special",
    CMD_MOTOR: "special",
    CMD_4_20MA: "special",
    CMD_DISPLAY: "special",
}

# #TIMEsync: after subscribe the app writes "#TIMEsync" + a 7-byte RTC datetime to
# FFF1 (createSyncRTCParams). Fire-and-forget clock-set, no reply needed.
TIMESYNC_PREFIX = b"#TIMEsync"


# ---------------------------------------------------------------------------
# FFF1 MD5 anti-counterfeit auth.  (Full derivation: docs/owon-voltcraft-handshake.md)
#
# App (BLE central) writes 6 "mixed coordinate" bytes to FFF1, zero-padded to 16.
# Each mixed byte = original_coordinate[i] + mixElems[i], original a small int.
# The meter recovers orig[i] = (mixed[i] - mixElems[i]) & 0xFF, picks s1[orig] for
# i<3 / s2[orig] for i>=3, and returns md5(utf8(picked)) as the RAW 16 DIGEST
# BYTES (the Flutter app reconstructs the expected hex from these bytes via
# toRadixString(16); returning ASCII hex fails -> "device not supported code:3").
# The legacy ASCII-hex variants remain selectable for regression testing.
# ---------------------------------------------------------------------------
_MIX_ELEMS = [200, 100, 50, 20, 10, 5]
# s1[] (coordinate indices 0..2):
_S1 = ['z', 'y', 'x', '0', 'w', 'v', '1', 'u', 't', 's', '2', 'r', 'q', '3', 'p',
       'o', 'n', '4', 'm', 'l', '5', 'k', 'j', 'i', '6', 'h', 'g', '7', 'f', 'e',
       'd', '8', 'c', 'b', '9', 'a']
# s2[] (coordinate indices 3..5):
_S2 = ['a', 'b', '9', 'c', 'd', 'e', '8', 'f', 'g', '7', 'h', 'i', 'j', '6', 'k',
       'l', '5', 'm', 'n', 'o', '4', 'p', 'q', '3', 'r', 's', 't', '2', 'u', 'v',
       '1', 'w', 'x', 'y', '0', 'z']
# base36 alphabet — a historic wrong guess at the pickout table.
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class OwonAuth:
    """The OWON FFF1 MD5 responder, with a selectable scheme for regression tests.

    ``table``: which pickout maps coord(0..35) -> char.
      'vc'      -> s1[c] for i<3, s2[c] for i>=3, response = RAW 16 MD5 DIGEST
                   BYTES. The CORRECT OWON-Flutter scheme (default).
      'base36'  -> _BASE36[c]      (historic, wrong)
      'base36r' -> _BASE36[35-c]   (historic, wrong)
      'java'    -> s1/s2 pick, returned as ASCII hex (the OWON *Java* app)
      's1'/'s2' -> single Java table for all 6, ASCII hex
    ``upper``: uppercase the ASCII-hex response (ignored for 'vc'; Dart md5 is lower).
    """

    def __init__(self, table: str = "vc", upper: bool = False):
        self.table = table
        self.upper = upper

    def set(self, table: Optional[str] = None, upper: Optional[bool] = None) -> None:
        if table is not None:
            self.table = table
        if upper is not None:
            self.upper = upper

    def recover(self, written: bytes) -> List[int]:
        """Recover the 6 original 0..35 coordinates from the mixed challenge bytes."""
        return [(written[i] - _MIX_ELEMS[i]) & 0xFF for i in range(6)]

    def pick(self, recovered: List[int]) -> str:
        t = self.table
        if t == "base36":
            return "".join(_BASE36[c] for c in recovered)
        if t == "base36r":
            return "".join(_BASE36[35 - c] for c in recovered)
        if t == "s1":
            return "".join(_S1[c] for c in recovered)
        if t == "s2":
            return "".join(_S2[c] for c in recovered)
        # 'java' and 'vc' both pick s1 for the first three coords, s2 for the rest.
        return "".join((_S1 if i < 3 else _S2)[c] for i, c in enumerate(recovered))

    def response(self, written: bytes) -> bytes:
        """Compute the FFF1 read-back value for the challenge the app wrote.

        For 'vc' this is the RAW 16-byte MD5 digest of the picked string. Returns
        b'' if the input is too short."""
        if written is None or len(written) < 6:
            return b""
        recovered = self.recover(written)
        picked = self.pick(recovered)
        digest = hashlib.md5(picked.encode("utf-8"))  # picked is pure ASCII
        if self.table == "vc":
            return digest.digest()
        md5_hex = digest.hexdigest()
        if self.upper:
            md5_hex = md5_hex.upper()
        return md5_hex.encode("ascii")


# ---------------------------------------------------------------------------
# Per-profile OWON config.
# ---------------------------------------------------------------------------
@dataclass
class OwonProfile:
    """Everything an OWON profile must supply on top of the shared layer.

    The encoder + Select/AC-DC/range tables come from the family's gear set; the
    series id selects the app's parser. GATT UUIDs default to the shared FFF0 set.
    """

    id: str
    label: str
    default_name: str
    series: int               # FFF2 series id -> app's model/parser selector

    # Reading -> on-wire frame bytes (inverse of the driver's decoder).
    encode: Callable[[Reading], bytes]

    # Interactive cycle tables (the family's gear ordering).
    select_cycle: List[str] = field(default_factory=list)
    acdc_toggle: Dict[str, str] = field(default_factory=dict)
    range_dp_cycle: List[int] = field(default_factory=lambda: [3, 2, 1, 0])

    # Initial streamed reading.
    initial: Reading = field(default_factory=lambda: Reading(
        value=4.200, function="V_DC", prefix="", decimals=3))

    # FFF2 device-info defaults (series is set above).
    battery: int = 100
    fw: tuple = (1, 0, 0)
    flash: int = 0xFF         # flash-record flag: 0xFF=not supportable

    # FFF3 opcode -> control-name map. None => the DEFAULT_CONTROLS OWON set (the
    # captured Voltcraft opcodes, shared by the R10W/R2W siblings). Override only if
    # a sibling uses different opcodes. Control names must be keys of CONTROL_ACTIONS.
    controls: Optional[Dict[int, str]] = None

    # Function labels this profile's encoder accepts (surfaced by the REPL `v` hint).
    function_codes: List[str] = field(default_factory=list)

    # Named presets the control CLI can play.
    presets: dict = field(default_factory=dict)

    # GATT UUIDs (default to the shared OWON FFF0 set).
    service_uuid: str = FFF0_SERVICE
    notify_uuid: str = FFF4_NOTIFY
    write_uuid: str = FFF3_WRITE
    secure_uuid: str = FFF1_SECURE
    info_uuid: str = FFF2_INFO

    # Gates. The Voltcraft "series800" Flutter app gates on BOTH the FFF1 MD5 auth
    # and the FFF2 series-info read. Other OWON apps (e.g. the Windows-derived
    # owon-plus path) stream immediately with no auth/info char. Set these False to
    # DROP the corresponding characteristic, so the profile reuses only the shared
    # GATT + FFF3 button dispatch + free-streaming without the handshake gate.
    use_auth: bool = True
    use_info: bool = True

    # Default auth scheme (ignored when use_auth is False).
    auth_table: str = "vc"


class OwonMeter:
    """A live OWON-family meter: ties the generic interactive engine to the OWON
    FFF1 auth + FFF2 info + FFF3 button dispatch for one profile config.

    The module-level wrapper functions a profile exposes (so the REPL keeps
    working unchanged) just delegate to a single shared instance of this.
    """

    def __init__(self, cfg: OwonProfile):
        self.cfg = cfg
        self.info = {"series": cfg.series & 0xFF, "battery": cfg.battery,
                     "fw": cfg.fw, "flash": cfg.flash}
        self.controls = cfg.controls if cfg.controls is not None else DEFAULT_CONTROLS
        self.auth = OwonAuth(table=cfg.auth_table)
        self.meter = InteractiveMeter(
            InteractiveConfig(
                encode=cfg.encode,
                select_cycle=cfg.select_cycle,
                acdc_toggle=cfg.acdc_toggle,
                range_dp_cycle=cfg.range_dp_cycle,
            ),
            initial=cfg.initial,
        )

    # -- FFF2 device-info ---------------------------------------------------
    def set_series(self, series: int) -> None:
        self.info["series"] = series & 0xFF

    def info_response(self) -> bytes:
        fw = self.info["fw"]
        return bytes([self.info["series"] & 0xFF, self.info["battery"] & 0xFF,
                      fw[0] & 0xFF, fw[1] & 0xFF, fw[2] & 0xFF,
                      self.info["flash"] & 0xFF])

    # -- FFF1 auth ----------------------------------------------------------
    def auth_response(self, written: bytes) -> bytes:
        return self.auth.response(written)

    def set_auth(self, table: Optional[str] = None,
                 upper: Optional[bool] = None) -> None:
        self.auth.set(table=table, upper=upper)

    # -- FFF3 button dispatch ----------------------------------------------
    def command(self, data: bytes) -> Optional[bytes]:
        """React to a 2-byte FFF3 control-button command; return the new frame.

        Table-driven: ``self.controls`` maps the opcode (data[0]) to a control name,
        and ``CONTROL_ACTIONS`` maps that name to the generic InteractiveMeter action.
        The ``#TIMEsync`` clock-set (written to FFF1 by the app) is fire-and-forget
        and is NOT routed here. Returns None for special-mode / unknown opcodes."""
        if data is None or len(data) < 1:
            return None
        # A #TIMEsync payload can land on the write char on some firmwares; ignore.
        if data[:len(TIMESYNC_PREFIX)] == TIMESYNC_PREFIX:
            return None
        name = self.controls.get(data[0])
        if name is None:
            return None  # unknown opcode for this model's button set
        action = CONTROL_ACTIONS.get(name)
        if action is None:
            return None  # mapped to an unknown control name (profile config bug)
        return action(self.meter)


def make_profile(cfg: OwonProfile, meter: OwonMeter) -> Profile:
    """Build the fully-wired :class:`Profile` for an OWON meter instance.

    ``cfg.use_auth`` / ``cfg.use_info`` gate the FFF1 / FFF2 characteristics: when
    False the char is dropped (secure_uuid / info_uuid = None) so the profile reuses
    only the shared GATT + FFF3 dispatch + streaming without that handshake gate."""
    return Profile(
        id=cfg.id,
        label=cfg.label,
        service_uuid=cfg.service_uuid,
        notify_uuid=cfg.notify_uuid,
        write_uuid=cfg.write_uuid,
        secure_uuid=cfg.secure_uuid if cfg.use_auth else None,
        info_uuid=cfg.info_uuid if cfg.use_info else None,
        default_name=cfg.default_name,
        encode=cfg.encode,
        auth_response=meter.auth_response if cfg.use_auth else None,
        info_response=meter.info_response if cfg.use_info else None,
        command_handler=meter.command,
        current_frame=meter.meter.current_frame,
        tick=meter.meter.tick,
        interaction="stream",
        presets=cfg.presets,
        # Profile-agnostic REPL hooks (the controller calls these by capability).
        reset_state=meter.meter.reset_state,
        set_walk=meter.meter.set_walk,
        set_series=(meter.set_series if cfg.use_info else None),
        set_auth=(meter.set_auth if cfg.use_auth else None),
        function_codes=(cfg.function_codes or None),
    )
