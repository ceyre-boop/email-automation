"""
Google Sheets logging service.

Authenticates using either:
  - An OAuth refresh token (GOOGLE_SHEETS_REFRESH_TOKEN) from a Desktop App
    OAuth client — generated once with scripts/generate_google_refresh_token.py
  - A service account JSON (GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON) as a fallback

Sheet ID comes from config/settings.json → google_sheets.master_log_sheet_id
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timedelta

import gspread

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Auth circuit breaker ──────────────────────────────────────────────────────
# A dead refresh token does not heal by being retried. This one expired around
# 2026-08-13 and every email since paid for a failed OAuth round-trip to Google —
# several per talent per poll cycle, thousands a day, each logging an identical
# ERROR line that buried everything else. Retrying a credential failure is pure
# waste: the answer will not change until a human replaces the token.
#
# After _AUTH_FAILURE_LIMIT consecutive auth failures the breaker opens: calls
# return immediately without touching the network, and the reason is logged ONCE
# rather than once per email. It closes automatically when the credential
# actually changes (fingerprint) or after _BREAKER_COOLDOWN, so a fixed token is
# picked up without a redeploy.
_AUTH_FAILURE_LIMIT = 3
_BREAKER_COOLDOWN = timedelta(minutes=30)
_AUTH_ERROR_MARKERS = ("invalid_grant", "invalid_client", "unauthorized_client", "token has been expired")

_breaker_lock = threading.Lock()
_auth_failures = 0
_breaker_opened_at: datetime | None = None
_breaker_credential_fp: str | None = None
_suppressed_since_open = 0


def _credential_fingerprint() -> str:
    """Short hash of the active credential, so replacing it resets the breaker."""
    try:
        settings = get_settings()
        raw = str(getattr(settings, "google_sheets_refresh_token", "") or "")
        if not raw:
            raw = str(getattr(settings, "sheets_credentials", ""))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


def _is_auth_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return any(marker in err for marker in _AUTH_ERROR_MARKERS)


def breaker_state() -> dict:
    """Current breaker state, for /health and the dashboard."""
    with _breaker_lock:
        return {
            "open": _breaker_opened_at is not None,
            "consecutive_auth_failures": _auth_failures,
            "opened_at": _breaker_opened_at.isoformat() if _breaker_opened_at else None,
            "suppressed_calls": _suppressed_since_open,
            "credential_fingerprint": _breaker_credential_fp,
        }


def _breaker_is_open() -> bool:
    """True when Sheets calls should be skipped entirely."""
    global _auth_failures, _breaker_opened_at, _breaker_credential_fp, _suppressed_since_open
    with _breaker_lock:
        if _breaker_opened_at is None:
            return False
        # A new credential means a human fixed it — try again immediately.
        if _credential_fingerprint() != _breaker_credential_fp:
            logger.info("Sheets: credential changed — closing auth circuit breaker")
            _auth_failures = 0
            _breaker_opened_at = None
            _suppressed_since_open = 0
            return False
        if datetime.utcnow() - _breaker_opened_at > _BREAKER_COOLDOWN:
            logger.info("Sheets: breaker cooldown elapsed — retrying once")
            _breaker_opened_at = None
            _auth_failures = 0
            return False
        _suppressed_since_open += 1
        return True


def _record_auth_failure(exc: Exception) -> None:
    global _auth_failures, _breaker_opened_at, _breaker_credential_fp, _suppressed_since_open
    with _breaker_lock:
        _auth_failures += 1
        if _auth_failures >= _AUTH_FAILURE_LIMIT and _breaker_opened_at is None:
            _breaker_opened_at = datetime.utcnow()
            _breaker_credential_fp = _credential_fingerprint()
            _suppressed_since_open = 0
            logger.error(
                "SHEETS AUTH DEAD — %d consecutive auth failures (%s). Master Log writes "
                "are now SKIPPED until the credential is replaced; this will not be logged "
                "again per email. Fix: regenerate GOOGLE_SHEETS_REFRESH_TOKEN with "
                "scripts/generate_google_refresh_token.py and update it on Render. "
                "Triage, drafting and sending are unaffected.",
                _auth_failures, exc,
            )


def _record_success() -> None:
    global _auth_failures, _breaker_opened_at, _suppressed_since_open
    with _breaker_lock:
        if _auth_failures or _breaker_opened_at:
            logger.info("Sheets: auth recovered — breaker closed")
        _auth_failures = 0
        _breaker_opened_at = None
        _suppressed_since_open = 0

_LOG_COLUMNS = [
    "timestamp",
    "talent_key",
    "sender",
    "subject",
    "score",
    "brand_name",
    "proposed_rate",
    "offer_type",
    "status",
    "notes",
]


def _get_client() -> gspread.Client:
    settings = get_settings()
    return gspread.authorize(settings.sheets_credentials)


def _get_worksheet() -> gspread.Worksheet:
    settings = get_settings()
    cfg = settings.app_config.get("google_sheets", {})
    sheet_id = cfg.get("master_log_sheet_id", "")
    tab_name = cfg.get("master_log_tab_name", "Master Log")
    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(tab_name)


def log_email(
    talent_key: str,
    sender: str,
    subject: str,
    score: int,
    brand_name: str,
    proposed_rate: float,
    offer_type: str,
    status: str,
    notes: str = "",
) -> bool:
    """
    Append one row to the Master Activity Log sheet.
    Returns True on success, False on failure (logs the error; never raises).
    """
    if _breaker_is_open():
        return False

    try:
        ws = _get_worksheet()
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            talent_key,
            sender,
            subject[:256] if subject else "",
            score,
            brand_name,
            proposed_rate,
            offer_type,
            status,
            notes[:512] if notes else "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        _record_success()
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            _record_auth_failure(exc)
        else:
            # Transient/API errors still log per occurrence — those can genuinely
            # succeed on the next call, unlike a dead credential.
            logger.error("Sheets log failed for %s: %s", talent_key, exc)
        return False
