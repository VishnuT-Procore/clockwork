"""Backwards compatibility with the pre-railway names (waves, regions, windows, ...)."""
from __future__ import annotations

_MISSING = object()


def pick(raw: dict, key: str, *legacy: str, default=None):
    """Read `key` from a config mapping, falling back to its legacy names."""
    present = [k for k in (key, *legacy) if k in raw]
    if len(present) > 1:
        raise ValueError(f"use only one of {present} (they are aliases)")
    return raw[present[0]] if present else default


# Status values written by versions before the rename.
LEGACY_STATUS = {
    "pending": "scheduled",
    "waiting": "held",
    "baking": "dwelling",
    "running": "en_route",
    "awaiting_approval": "at_signal",
}
