"""Helpers for checking whether Gmail automation is allowed for a talent."""
from __future__ import annotations

from fastapi import HTTPException

from backend.core.config import get_settings


def get_talent_config(talent_key: str) -> dict | None:
    """Return the talent config row for a key, case-insensitive."""
    key = talent_key.lower()
    for talent in get_settings().talent_list:
        if talent.get("key", "").lower() == key:
            return talent
    return None


def is_talent_paused(talent_key: str) -> bool:
    """Return True when Gmail automation is disabled for the talent."""
    talent_cfg = get_talent_config(talent_key)
    return bool(talent_cfg and talent_cfg.get("paused"))


def ensure_talent_gmail_enabled(talent_key: str) -> None:
    """Raise if Gmail reads/writes are disabled for the talent."""
    if is_talent_paused(talent_key):
        raise HTTPException(
            status_code=403,
            detail=f"Gmail automation is disabled for {talent_key}.",
        )
