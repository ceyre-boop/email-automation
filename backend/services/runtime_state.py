"""Runtime on/off switches, stored in the database.

Why this exists
---------------
``ai_enabled`` and per-talent pause used to be flipped by rewriting
``config/settings.json`` / ``sheets/sop.md`` on disk. Render's filesystem is rebuilt
on every deploy, so a guardian kill or a talent pause silently reverted the next
time the service restarted — and the unpause route edited a ``talents[]`` array that
no longer exists, so it did nothing at all.

Now the switches live in the existing ``app_state`` table:

  runtime_ai_enabled              "true" | "false"
  runtime_talent_paused:<key>     "true" | "false"   (key lower-cased)

A row, when present, WINS. When there is no row the file value is the default:
``ai_enabled`` from config/settings.json, ``Paused:`` from the talent's sop.md block.
An explicit "false" therefore un-pauses a talent whose sop.md still says
``Paused: yes``. Nothing here ever writes to a file.

Reads are cached for a few seconds (they sit on hot paths: every triage call, every
``Settings.talent_profiles`` access) and invalidated immediately on a local write.
If the database cannot be read the last known value is used; before any successful
read the file default applies.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime

logger = logging.getLogger(__name__)

KEY_AI_ENABLED = "runtime_ai_enabled"
KEY_TALENT_PAUSED_PREFIX = "runtime_talent_paused:"

CACHE_TTL_SECONDS = 5.0

_cache_lock = threading.Lock()
_cache: dict | None = None          # {"at": monotonic, "ai": bool|None, "paused": {key: bool}}
_last_good: dict = {"ai": None, "paused": {}}
_warned_at = 0.0


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _open_session():
    from backend.models.db import get_session_factory
    return get_session_factory()()


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


def _load() -> dict:
    """Return {"ai": bool|None, "paused": {key_lower: bool}} (cached, never raises)."""
    global _cache, _warned_at
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
            return _cache
    snap = {"at": now, "ai": _last_good["ai"], "paused": dict(_last_good["paused"])}
    session = None
    try:
        from backend.models.db import AppState
        session = _open_session()
        rows = (
            session.query(AppState.key, AppState.value_text)
            .filter(
                (AppState.key == KEY_AI_ENABLED)
                | (AppState.key.like(KEY_TALENT_PAUSED_PREFIX + "%"))
            )
            .all()
        )
        ai = None
        paused: dict[str, bool] = {}
        for key, value in rows:
            parsed = _parse_bool(value)
            if parsed is None:
                continue
            if key == KEY_AI_ENABLED:
                ai = parsed
            else:
                paused[key[len(KEY_TALENT_PAUSED_PREFIX):].lower()] = parsed
        snap.update(ai=ai, paused=paused)
        _last_good.update(ai=ai, paused=dict(paused))
    except Exception as exc:  # noqa: BLE001
        if now - _warned_at > 60:
            _warned_at = now
            logger.warning(
                "runtime_state: could not read switches from app_state (%s) — using last "
                "known values / file defaults", exc,
            )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
    with _cache_lock:
        _cache = snap
    return snap


# ── ai_enabled ───────────────────────────────────────────────────────────────

def _file_ai_default() -> bool:
    from backend.core.config import get_settings
    return bool(get_settings().app_config.get("ai_enabled", True))


def is_ai_enabled() -> bool:
    """DB value if one has been set, otherwise config/settings.json (default True)."""
    value = _load()["ai"]
    return _file_ai_default() if value is None else value


def set_ai_enabled(db, enabled: bool) -> None:
    """Persist the switch. Raises on DB failure — callers must not pretend it worked."""
    _set_state(db, KEY_AI_ENABLED, "true" if enabled else "false")
    _last_good["ai"] = enabled
    invalidate_cache()
    logger.warning("runtime_state: ai_enabled set to %s (stored in app_state)", enabled)


# ── talent pause ─────────────────────────────────────────────────────────────

def talent_pause_overrides() -> dict[str, bool]:
    """{talent_key_lower: paused} for every talent with an explicit DB value."""
    return dict(_load()["paused"])


def set_talent_paused(db, talent_key: str, paused: bool) -> None:
    """Persist a pause/unpause. Raises on DB failure."""
    key = talent_key.strip().lower()
    _set_state(db, KEY_TALENT_PAUSED_PREFIX + key, "true" if paused else "false")
    _last_good["paused"][key] = paused
    invalidate_cache()
    logger.warning("runtime_state: talent %s %s (stored in app_state)", key, "PAUSED" if paused else "UNPAUSED")


def apply_pause_overrides(profiles: dict) -> dict:
    """Return profiles with ``paused`` replaced by the DB value where one exists."""
    overrides = talent_pause_overrides()
    if not overrides:
        return profiles
    out = {}
    for key, profile in profiles.items():
        override = overrides.get(str(key).lower())
        out[key] = profile if override is None or override == profile.paused else replace(profile, paused=override)
    return out


def _set_state(db, key: str, value: str) -> None:
    from backend.models.db import AppState
    try:
        row = db.query(AppState).filter(AppState.key == key).first()
        if row is None:
            row = AppState(key=key)
        row.value_text = value
        row.updated_at = datetime.utcnow()
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
