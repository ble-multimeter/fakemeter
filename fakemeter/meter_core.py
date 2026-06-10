"""Meter-generic interactive state machine, value-walk, and HOLD/REL/Max-Min.

This is the family-agnostic core that ANY numeric profile reuses. It owns the
single source-of-truth :class:`Reading` (the ``_STATE`` the REPL and the control
buttons both mutate, so they never fight) plus the extra runtime state a bare
``Reading`` can't hold (the frozen HOLD frame, the REL baseline, the Max/Min
cycle position, and the demo value-walk).

What lives here (generic, every meter gets it for free):
  * ``InteractiveMeter`` — holds ``state`` (a ``Reading``) + ``runtime`` dict.
  * ``reset_state`` / ``current_frame`` plumbing.
  * the value-walk (``tick``): a mean-reverting random walk around the nominal,
    in units of the display's own resolution, frozen under HOLD.
  * HOLD freeze/resume, REL capture/restore, Max/Min cycle, a generic flag toggle,
    a Select (function-cycle), an AC/DC toggle, and a Range (decimals-cycle).

What stays in the profile (genuinely per-family):
  * the frame *encoder* (``Reading`` -> bytes),
  * the gear/prefix/state tables and the Select gear cycle, AC/DC pairs, range
    cycle (passed in as ``InteractiveConfig``),
  * the actual FFF3 *opcodes* and which opcode triggers which generic action (the
    owon_base / a future uni-t base maps opcode -> action and calls these methods).

The OWON family wires its FFF3 button opcodes to these methods via
``owon_base``; a future polled (UNI-T AB-CD) base would reuse the SAME
``InteractiveMeter`` for HOLD/REL/walk and only differ in how the wire bytes are
framed (see the ``interaction`` seam in ``profiles/base.py`` and
``docs/adding-a-profile.md``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:  # avoid a circular import (profiles -> owon_base -> meter_core)
    from .profiles.base import Reading


@dataclass
class InteractiveConfig:
    """Per-family knobs for the generic interactive actions.

    Everything here is meter-family data, not behaviour: the encoder closes over
    the family's frame layout, and the cycle tables come from the family's gear
    set. ``meter_core`` supplies the (identical) HOLD/REL/walk/cycle *behaviour*.
    """

    # Reading -> on-wire frame bytes (the family encoder; inverse of its decoder).
    encode: Callable[[Reading], bytes]

    # Select cycles the primary function through these labels (family gear order).
    select_cycle: List[str] = field(default_factory=list)
    # AC/DC toggles map a function label to its AC/DC counterpart.
    acdc_toggle: dict = field(default_factory=dict)
    # Range cycles the decimal-place count through these values.
    range_dp_cycle: List[int] = field(default_factory=lambda: [3, 2, 1, 0])

    # Demo value-walk tuning (counts of display resolution; mean-reversion pull).
    walk_jitter_counts: float = 8.0
    walk_reversion: float = 0.05
    default_decimals: int = 3


class InteractiveMeter:
    """A meter-family-agnostic interactive reading engine.

    Holds the live :class:`Reading` and the runtime state, and exposes the generic
    reactions (HOLD/REL/Max-Min/LPF/Select/AC-DC/Range) plus the value-walk. A
    profile (or a base like ``owon_base``) maps its FFF3 opcodes onto these
    methods; the methods return the freshly-encoded frame to stream.
    """

    def __init__(self, cfg: InteractiveConfig, initial: Reading):
        self.cfg = cfg
        self.state: Reading = initial
        # Extra per-meter runtime state the bare Reading can't hold.
        self.runtime = {
            "held_frame": None,   # frozen frame while HOLD is engaged (or None)
            "live_value": initial.value if initial.value is not None else 0.0,
            "maxmin": 0,          # Max/Min cycle: 0=off 1=MAX 2=MIN 3=AVG
            "rel_base": None,     # REL captured baseline (None = REL off)
            "walk": True,         # demo value-walk on by default
            "walk_center": initial.value if initial.value is not None else 0.0,
        }

    # -- state plumbing -----------------------------------------------------
    def reset_state(self, reading: Optional[Reading] = None) -> None:
        """Reset the interactive state (the REPL calls this when the user sets a
        fresh reading, so manual edits and button commands share one truth)."""
        if reading is not None:
            self.state = reading
        self.runtime["held_frame"] = None
        self.runtime["maxmin"] = 0
        self.runtime["rel_base"] = None
        if self.state.value is not None:
            self.runtime["live_value"] = self.state.value
            # Re-centre the demo walk on the value the user just set.
            self.runtime["walk_center"] = self.state.value

    def set_walk(self, on: bool) -> None:
        """Enable/disable the demo value-walk (off => a fixed reading is streamed,
        for precise byte-mapping)."""
        self.runtime["walk"] = bool(on)

    def current_frame(self) -> bytes:
        """The frame the meter would stream right now given ``state``.

        While HOLD is engaged we keep streaming the frozen frame so the app's
        reading stays put (a real meter freezes the displayed value under HOLD)."""
        if self.runtime["held_frame"] is not None:
            return self.runtime["held_frame"]
        return self.cfg.encode(self.state)

    # -- the demo value-walk -------------------------------------------------
    def tick(self) -> None:
        """Advance the demo value-walk one step (called once per stream tick, on
        the GLib loop thread). A mean-reverting random walk around the nominal, in
        units of the display's own resolution. No-op while HOLD is engaged, for
        overload/underload/non-numeric, or when walk is off."""
        rt = self.runtime
        if not rt["walk"]:
            return
        if rt["held_frame"] is not None:          # HOLD: keep the value frozen
            return
        if self.state.overload or self.state.underload or self.state.value is None:
            return
        decimals = (self.state.decimals if self.state.decimals is not None
                    else self.cfg.default_decimals)
        res = 10.0 ** -decimals                    # one displayed count
        center = rt["walk_center"]
        live = rt["live_value"]
        live += random.uniform(-self.cfg.walk_jitter_counts,
                               self.cfg.walk_jitter_counts) * res
        live += (center - live) * self.cfg.walk_reversion
        rt["live_value"] = live
        shown = (live - rt["rel_base"]) if rt["rel_base"] is not None else live
        self.state.value = round(shown, decimals)

    # -- generic reactions (a base/profile maps opcodes onto these) ----------
    def _drop_hold(self) -> None:
        """A mode key other than HOLD exits HOLD (matches meter UX)."""
        if self.runtime["held_frame"] is not None:
            self.runtime["held_frame"] = None
            self.state.hold = False

    def toggle_hold(self) -> bytes:
        """Toggle HOLD: engaging freezes the current frame + lights HOLD; releasing
        resumes the live reading."""
        if self.runtime["held_frame"] is None:
            self.state.hold = True
            self.runtime["held_frame"] = self.cfg.encode(self.state)
        else:
            self.runtime["held_frame"] = None
            self.state.hold = False
        return self.current_frame()

    def select_next(self) -> bytes:
        """Cycle the primary function through the family's Select gear cycle.
        Clears REL/MAX/MIN (they don't carry across a function change)."""
        self._drop_hold()
        cycle = self.cfg.select_cycle
        try:
            idx = cycle.index(self.state.function)
        except ValueError:
            idx = -1
        self.state.function = cycle[(idx + 1) % len(cycle)]
        self.runtime["rel_base"] = None
        self.state.rel = False
        self.runtime["maxmin"] = 0
        self._apply_maxmin_flags()
        return self.current_frame()

    def range_next(self) -> bytes:
        """Cycle the decimal-place count (manual range stepping moves the decimal
        point); turns AUTO off."""
        self._drop_hold()
        cycle = self.cfg.range_dp_cycle
        cur = (self.state.decimals if self.state.decimals is not None
               else self.cfg.default_decimals)
        try:
            ridx = cycle.index(cur)
        except ValueError:
            ridx = -1
        self.state.decimals = cycle[(ridx + 1) % len(cycle)]
        self.state.auto = False
        return self.current_frame()

    def acdc_toggle(self) -> bytes:
        """Toggle the DC<->AC variant of the current V/A gear."""
        self._drop_hold()
        new_fn = self.cfg.acdc_toggle.get(self.state.function)
        if new_fn is not None:
            self.state.function = new_fn
        return self.current_frame()

    def rel_toggle(self) -> bytes:
        """Toggle REL: engaging captures the value as baseline + zeroes the display
        + lights REL; releasing restores."""
        self._drop_hold()
        if self.runtime["rel_base"] is None:
            self.runtime["rel_base"] = self.state.value or 0.0
            self.runtime["live_value"] = self.state.value or 0.0
            self.state.value = 0.0
            self.state.rel = True
        else:
            self.state.value = self.runtime["live_value"]
            self.runtime["rel_base"] = None
            self.state.rel = False
        return self.current_frame()

    def maxmin_next(self) -> bytes:
        """Cycle off->MAX->MIN->AVG->off, setting the MAX/MIN flags."""
        self._drop_hold()
        self.runtime["maxmin"] = (self.runtime["maxmin"] + 1) % 4
        self._apply_maxmin_flags()
        return self.current_frame()

    def _apply_maxmin_flags(self) -> None:
        """Reflect the Max/Min cycle position onto the named flags."""
        self.state.max = self.runtime["maxmin"] == 1
        self.state.min = self.runtime["maxmin"] == 2
        # AVG (cycle pos 3) has no Reading bool; express via raw_mode_word if needed.

    def toggle_flag(self, name: str) -> bytes:
        """Toggle a named Reading annunciator flag (e.g. ``lpf``) and re-stream."""
        self._drop_hold()
        setattr(self.state, name, not getattr(self.state, name))
        return self.current_frame()
