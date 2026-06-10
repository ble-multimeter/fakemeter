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

# id -> Profile
PROFILES: dict[str, Profile] = {
    voltcraft.profile.id: voltcraft.profile,
}

# Names of families we intend to support but haven't implemented yet — surfaced by
# the CLI so the user can see the roadmap.
PLANNED: list[str] = ["bdm", "owon-plus", "owon-old", "ai-care", "uni-t"]


def get(profile_id: str) -> Profile:
    if profile_id not in PROFILES:
        raise KeyError(
            f"unknown profile {profile_id!r}. "
            f"implemented: {sorted(PROFILES)}; planned: {PLANNED}"
        )
    return PROFILES[profile_id]


__all__ = ["Profile", "Reading", "PROFILES", "PLANNED", "get"]
