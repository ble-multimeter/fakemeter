"""Profile registry.

Implemented:
  * voltcraft — Voltcraft VC800/VC900 (OWON rebadge). First and reference profile.

Planned (add a module here, mirror voltcraft.py, register below):
  * bdm        — 11-byte frame, GATT 0xFFF0 family
  * owon-plus  — OWON gear-word, mode word at bytes[2..3] (LSB-first, already fixed)
  * owon-old   — older OWON 6-byte frame
  * ai-care    — AiCare / AC-series
  * uni-t      — UNI-T UT-series (different GATT + AB-CD handshake; see UT60BT memo)

To add one: create ``profiles/<name>.py`` exposing a module-level ``profile``
(a :class:`fakemeter.profiles.base.Profile`), then import + register it below.
"""

from __future__ import annotations

from .base import Profile, Reading
from . import voltcraft
# OWON family (owon_base): voltcraft (reference), owon-plus (6-byte R2W), owon-old (14-byte ASCII)
from . import owon_plus
from . import owon_old
# meter_core direct (no auth/info): bdm (FFF0, XOR), ai-care (FFB0, self-addressing)
from . import bdm
from . import ai_care
# UNI-T family (uni_t_base, AB-CD polled): generic + per-model
from . import uni_t
from . import ut202bt
from . import ut117c
from . import ut171
from . import ut181a
from . import ut219p

# id -> Profile
PROFILES: dict[str, Profile] = {
    voltcraft.profile.id: voltcraft.profile,
    owon_plus.profile.id: owon_plus.profile,
    owon_old.profile.id: owon_old.profile,
    bdm.profile.id: bdm.profile,
    ai_care.profile.id: ai_care.profile,
    uni_t.profile.id: uni_t.profile,
    ut202bt.profile.id: ut202bt.profile,
    ut117c.profile.id: ut117c.profile,
    ut171.profile.id: ut171.profile,
    ut181a.profile.id: ut181a.profile,
    ut219p.profile.id: ut219p.profile,
}

# Families with a profile but only a partial/best-effort implementation pending a
# hardware capture (documented in docs/PROGRESS.md): ut219p (standard live-data
# frame only), ut181a (MAIN value block only).
PLANNED: list[str] = []


def get(profile_id: str) -> Profile:
    if profile_id not in PROFILES:
        raise KeyError(
            f"unknown profile {profile_id!r}. "
            f"implemented: {sorted(PROFILES)}; planned: {PLANNED}"
        )
    return PROFILES[profile_id]


__all__ = ["Profile", "Reading", "PROFILES", "PLANNED", "get"]
