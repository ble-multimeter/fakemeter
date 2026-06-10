"""Control CLI/TUI for the fake-meter emulator.

Pick a profile + adapter, publish the GATT server + advertisement, then drive what
the phone sees from a simple keyboard menu:

  * play a named preset pattern (representative readings + the mode bit-sweep)
  * set a live reading (value + function + prefix) and toggle annunciators
  * run the bit-sweep stepping one mode-word bit at a time (timer or keypress)

Run it in a terminal while you watch the vendor app (or our web app) on the phone.

    python -m fakemeter --profile voltcraft --adapter hci0
    python -m fakemeter --profile voltcraft --name VC900-FAKE
    python -m fakemeter --profile voltcraft --self-check   # registration smoke test
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from . import profiles
from .gatt_server import MeterServer, adapter_address
from .profiles.base import Reading


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_banner(server: MeterServer) -> None:
    p = server.profile
    print()
    print("=" * 64)
    print(f"  fakemeter — impersonating: {p.label}  ({p.id})")
    print(f"  adapter : {server.adapter_id}  ({server.adapter_addr})")
    print(f"  name    : {server.local_name}   <- connect to THIS on the phone")
    print(f"  service : {p.service_uuid}")
    print(f"  notify  : {p.notify_uuid}")
    print(f"  write   : {', '.join(p.write_uuids)}")
    if p.secure_uuid:
        print(f"  secure  : {p.secure_uuid}  (FFF1 MD5 auth gate — auto-answered)")
    print("=" * 64)


def _menu() -> None:
    print(
        """
Commands:
  p           list + play a preset pattern
  s           start the MODE-WORD BIT SWEEP (the flag-order test)
  v           set a live reading (value / function / prefix)
  f           toggle an annunciator flag (hold/rel/auto/bat/min/max)
  r           re-send the current frame
  walk [on|off] toggle/set the demo value-walk (drifting reading; on by default)
  series <id> set the FFF2 device series id (voltcraft); reconnect app to apply
  auth <mode> set FFF1 auth scheme: vc|base36|base36r|java|s1|s2|upper|lower (reconnect)
              ('vc' = correct: raw 16 MD5 digest bytes, s1/s2 tables; is the default)
  ?           show this menu
  q           quit
"""
    )


class Controller:
    """Holds the current reading and drives the server from keyboard input."""

    def __init__(self, server: MeterServer):
        self.server = server
        self.profile = server.profile
        self.reading = Reading(value=4.200, function="V_DC")
        self._sweep_stop = threading.Event()
        self._walk_on = True  # mirrors the profile's value-walk default; `walk` toggles it

    # -- helpers ------------------------------------------------------------
    def send(self, reading: Reading | None = None) -> None:
        r = reading or self.reading
        # Keep the profile's interactive state (the source of truth the FFF3 button
        # commands mutate) in sync with manual REPL edits, so a manual `v`/`f` and a
        # button press don't fight. Capability-based: any profile exposing the
        # reset_state + current_frame hooks plugs in (no per-profile id check).
        if self.profile.reset_state is not None \
                and self.profile.current_frame is not None:
            self.profile.reset_state(r)
            frame = self.profile.current_frame()
        else:
            frame = self.profile.encode(r)
        self.server.notify(frame)
        self._describe(r, frame)

    def _send_initial(self) -> None:
        """Push the very first frame WITHOUT overriding the profile's own initial
        reading. The controller's default ``self.reading`` uses generic function
        labels (e.g. ``V_DC``) that not every family's encoder accepts (UNI-T uses
        ``DCV``); a profile that owns its interactive state already starts on a valid
        reading, so seed from ``current_frame()`` and adopt that reading. Falls back
        to the normal ``send()`` for profiles without interactive state."""
        if self.profile.current_frame is not None:
            frame = self.profile.current_frame()
            self.server.notify(frame)
            print(f"  -> sent {frame.hex()}   [initial reading from profile]")
        else:
            self.send()

    def _describe(self, r: Reading, frame: bytes) -> None:
        if r.overload:
            disp = "O.L"
        elif r.underload:
            disp = "U.L"
        else:
            disp = f"{r.value} {r.prefix}{r.function}"
        flags = [n for n in ("hold", "rel", "auto", "low_battery", "min", "max")
                 if getattr(r, n)]
        mode = (f" raw_mode=0x{r.raw_mode_word:04x}"
                if r.raw_mode_word is not None else
                (f" flags={','.join(flags)}" if flags else ""))
        print(f"  -> sent {frame.hex()}   [{disp}]{mode}")

    # -- commands -----------------------------------------------------------
    def play_preset(self) -> None:
        names = list(self.profile.presets)
        print("\nPresets:")
        for i, n in enumerate(names):
            print(f"  {i}) {n}")
        sel = input("preset # (or name): ").strip()
        name = names[int(sel)] if sel.isdigit() and int(sel) < len(names) else sel
        if name not in self.profile.presets:
            print(f"  ! no preset {name!r}")
            return
        result = self.profile.presets[name]()
        if name == "bitsweep" or isinstance(result, list):
            self._run_sweep(result)
        else:
            self.reading = result
            self.send()

    def _run_sweep(self, readings: list[Reading]) -> None:
        print("\nMODE-WORD BIT SWEEP — this settles the flag-order question.")
        print("For EACH step, read the phone and note which annunciator (if any)")
        print("lights. Step with ENTER (manual) or pick an auto interval.\n")
        mode = input("auto-step interval seconds (blank = manual ENTER stepping): ").strip()
        self._sweep_stop.clear()
        try:
            for i, r in enumerate(readings):
                label = ("baseline (no bits)" if r.raw_mode_word == 0
                         else f"bit {r.raw_mode_word.bit_length() - 1} "
                              f"(0x{r.raw_mode_word:04x})")
                print(f"\n[sweep {i}/{len(readings) - 1}] {label}")
                self.send(r)
                if mode:
                    time.sleep(float(mode))
                else:
                    input("   ENTER for next bit (Ctrl-C to stop)... ")
        except KeyboardInterrupt:
            print("\n  sweep stopped.")
        print("\nSweep done. Tell Claude which annunciator lit for each bit number.")

    def set_reading(self) -> None:
        funcs = self.profile.function_codes or []
        if funcs:
            print(f"  functions: {', '.join(funcs)}")
        val = input(f"value [{self.reading.value}] (or 'ol'/'ul'): ").strip()
        fn = input(f"function [{self.reading.function}]: ").strip() or self.reading.function
        pfx = input(f"prefix [{self.reading.prefix!r}] (p n µ m '' k M G): ").strip()
        r = Reading(function=fn, prefix=pfx if pfx else "")
        # carry flags forward
        for n in ("hold", "rel", "auto", "low_battery", "min", "max"):
            setattr(r, n, getattr(self.reading, n))
        if val.lower() == "ol":
            r.overload, r.value = True, None
        elif val.lower() == "ul":
            r.underload, r.value = True, None
        elif val:
            r.value = float(val)
        else:
            r.value = self.reading.value
        self.reading = r
        self.send()

    def toggle_flag(self) -> None:
        names = ["hold", "rel", "auto", "low_battery", "min", "max"]
        print("  flags:", ", ".join(f"{i}={n}" for i, n in enumerate(names)))
        sel = input("toggle which? ").strip()
        name = names[int(sel)] if sel.isdigit() and int(sel) < len(names) else sel
        if name not in names:
            print(f"  ! no flag {name!r}")
            return
        setattr(self.reading, name, not getattr(self.reading, name))
        self.reading.raw_mode_word = None  # named flags now drive the mode word
        self.send()

    def set_series_cmd(self, cmd: str) -> None:
        parts = cmd.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            print("  usage: series <id>   (e.g. series 91=VC915) — then RECONNECT the app")
            return
        if self.profile.set_series is None:
            print("  ! this profile has no settable FFF2 series id")
            return
        self.profile.set_series(int(parts[1]))
        print(f"  FFF2 series id -> {int(parts[1])}. Reconnect the app so it re-reads "
              f"FFF2; watch for 'device not supported! seriesId: N' vs a live reading.")

    def set_auth_cmd(self, cmd: str) -> None:
        parts = cmd.split()
        if len(parts) < 2:
            print("  usage: auth <vc|base36|base36r|java|s1|s2|upper|lower>  then RECONNECT")
            print("         'vc' = correct Voltcraft/OWON-Flutter scheme (raw 16 MD5 bytes); default")
            return
        if self.profile.set_auth is None:
            print("  ! this profile has no settable FFF1 auth scheme")
            return
        arg = parts[1].lower()
        if arg in ("upper", "lower"):
            self.profile.set_auth(upper=(arg == "upper"))
        else:
            self.profile.set_auth(table=arg)
        print(f"  auth scheme set to {arg!r}. Reconnect the app to test this scheme.")

    def send_raw_cmd(self, cmd: str) -> None:
        """`raw <hexbytes>` — push arbitrary bytes on the notify char verbatim.

        For empirically reverse-engineering the on-wire frame layout against the
        live vendor app (set a byte, read the phone). Hex may contain spaces.
        """
        parts = cmd.split(None, 1)
        if len(parts) < 2:
            print("  usage: raw 00 00 22 00 00 2a ...  (hex bytes, any spacing)")
            return
        hexstr = parts[1].replace(" ", "").replace(",", "")
        try:
            frame = bytes.fromhex(hexstr)
        except ValueError as e:
            print(f"  ! bad hex: {e}")
            return
        self.server.notify(frame)
        print(f"  -> sent RAW {frame.hex()} ({len(frame)} bytes)")

    def set_walk_cmd(self, cmd: str) -> None:
        """`walk [on|off]` — toggle/set the demo value-walk (drifting reading)."""
        if self.profile.set_walk is None:
            print("  ! this profile has no demo value-walk")
            return
        parts = cmd.split()
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            on = parts[1] == "on"
        else:
            on = not self._walk_on  # bare `walk` toggles the controller's tracked state
        self.profile.set_walk(on)
        self._walk_on = on
        print(f"  value-walk {'ON (reading drifts around the nominal)' if on else 'OFF (fixed reading)'}")

    def loop(self) -> None:
        _menu()
        self._send_initial()  # push an initial frame so the phone shows something
        while True:
            try:
                cmd = input("fakemeter> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd == "p":
                self.play_preset()
            elif cmd == "s":
                self._run_sweep(self.profile.presets["bitsweep"]())
            elif cmd == "v":
                self.set_reading()
            elif cmd == "f":
                self.toggle_flag()
            elif cmd == "r":
                self.send()
            elif cmd.startswith("walk"):
                self.set_walk_cmd(cmd)
            elif cmd.startswith("raw"):
                self.send_raw_cmd(cmd)
            elif cmd.startswith("series"):
                self.set_series_cmd(cmd)
            elif cmd.startswith("auth"):
                self.set_auth_cmd(cmd)
            elif cmd in ("?", "h", "help"):
                _menu()
            elif cmd == "":
                continue
            else:
                print(f"  ? unknown command {cmd!r} (press ? for help)")


def _self_check(args) -> int:
    """Smoke test: resolve adapter, build + publish, confirm registration, exit."""
    import dbus

    prof = profiles.get(args.profile)
    print(f"[self-check] profile={prof.id} adapter={args.adapter}")
    addr = adapter_address(args.adapter)
    print(f"[self-check] adapter address = {addr}")

    server = MeterServer(prof, adapter_id=args.adapter,
                         local_name=args.name or prof.default_name)
    server.start()
    time.sleep(2.5)  # let registration settle

    # Encoder round-trip sanity on a couple of presets.
    for name, factory in prof.presets.items():
        if name == "bitsweep":
            continue
        r = factory()
        frame = prof.encode(r)
        assert len(frame) == 15, f"{name}: bad frame length {len(frame)}"
        print(f"[self-check] preset {name:16s} -> {frame.hex()}")

    # Confirm the advertisement registered: BlueZ's LEAdvertisingManager1 counts
    # ACTIVE advertisement instances on this adapter (the authoritative signal —
    # the GATT app/advert objects are exported on OUR bus connection and registered
    # WITH BlueZ via RegisterApplication/RegisterAdvertisement, so they are not
    # listed under /org/bluez's ObjectManager).
    bus = dbus.SystemBus()
    props = dbus.Interface(
        bus.get_object("org.bluez", f"/org/bluez/{args.adapter}"),
        "org.freedesktop.DBus.Properties",
    )
    active = int(props.Get("org.bluez.LEAdvertisingManager1", "ActiveInstances"))
    print(f"[self-check] BlueZ LEAdvertisingManager1.ActiveInstances = {active}")

    # Confirm the advertised payload + our exported characteristics.
    adv = server._periph.advert
    print(f"[self-check] advertised LocalName  = {adv.local_name}")
    print(f"[self-check] advertised ServiceUUIDs = {list(adv.service_UUIDs)}")
    chars = []
    for ch in server._periph.characteristics:
        p = ch.props["org.bluez.GattCharacteristic1"]
        chars.append(str(p["UUID"]))
        print(f"[self-check] char {str(p['UUID'])}  flags={list(p['Flags'])}")

    # Confirm a notify push lands in the characteristic Value.
    frame = prof.encode(prof.presets["dc_volts"]())
    server.notify(frame)
    nv = bytes(server._notify_char.props["org.bluez.GattCharacteristic1"]["Value"])
    notify_ok = nv == frame
    print(f"[self-check] notify push round-trip: {'OK' if notify_ok else 'FAIL'} "
          f"({nv.hex()})")

    ok = active >= 1 and len(chars) >= 2 and notify_ok
    print(f"[self-check] {'PASS' if ok else 'FAIL'} — advertisement + GATT "
          f"{'live' if ok else 'NOT fully live'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fakemeter", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="voltcraft",
                    help=f"meter profile (implemented: {sorted(profiles.PROFILES)}; "
                         f"planned: {profiles.PLANNED})")
    ap.add_argument("--adapter", default="hci0",
                    help="BlueZ adapter id (hci0, hci1, ...) or BD address")
    ap.add_argument("--name", default=None,
                    help="advertised local name (default: profile's default)")
    ap.add_argument("--series", type=int, default=None,
                    help="FFF2 device series id the app reads on connect (voltcraft; "
                         "try 33/35/41 — the app echoes the id it read if unsupported)")
    ap.add_argument("--no-walk", action="store_true",
                    help="disable the demo value-walk; stream a fixed reading "
                         "(useful for precise byte-mapping)")
    ap.add_argument("--no-unsolicited", action="store_true",
                    help="disable no-CCCD (unsolicited) notification injection. By "
                         "default, stream profiles inject ATT notifications via a raw "
                         "HCI socket to clients that connect without writing the FFF4 "
                         "CCCD (e.g. the OWON Java app), so they still get live data. "
                         "Needs CAP_NET_RAW to deliver; the normal CCCD path is "
                         "unaffected either way.")
    ap.add_argument("--self-check", action="store_true",
                    help="publish, verify advertisement+GATT registration, exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug logging (logs every notify/write)")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        prof = profiles.get(args.profile)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    # Capability-based CLI knobs (no per-profile id check): any profile exposing the
    # hooks honours --series / --no-walk.
    if args.series is not None and prof.set_series is not None:
        prof.set_series(args.series)
    if args.no_walk and prof.set_walk is not None:
        prof.set_walk(False)

    if args.self_check:
        return _self_check(args)

    server = MeterServer(prof, adapter_id=args.adapter,
                         local_name=args.name or prof.default_name,
                         unsolicited=not args.no_unsolicited)
    server.start()
    _print_banner(server)
    Controller(server).loop()
    print("bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
