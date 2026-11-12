"""
Google OAuth helpers.

Each talent connects their Gmail ONCE via:
  GET /auth/connect?talent_key=<key>

Their tokens are stored in the `talents` table and refreshed automatically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow


class TokenRefreshError(Exception):
    """Raised when Google rejects a token refresh. Talent must reconnect."""


from backend.core.config import get_settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def build_flow() -> Flow:
    """Return a configured OAuth Flow object."""
    settings = get_settings()
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=settings.google_redirect_uri,
    )
    return flow


def build_authorization_url(state: str) -> str:
    """Return the Google consent URL with a CSRF state token."""
    flow = build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code(code: str) -> dict:
    """
    Exchange an authorization code for tokens.
    Returns a dict with: access_token, refresh_token, expiry, id_token
    """
    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "expiry": creds.expiry,  # datetime or None
    }


def credentials_from_token_row(row) -> Credentials:
    """Build a google.oauth2.credentials.Credentials from a TalentToken DB row."""
    settings = get_settings()
    creds = Credentials(
        token=row.access_token,
        refresh_token=row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    if row.token_expiry:
        creds.expiry = row.token_expiry  # stored as naive UTC; google-auth compares with utcnow()
    return creds


# A token is only auto-deactivated after this many CONSECUTIVE failed refreshes. One
# rejected refresh is often transient (Google hiccup, clock skew); deactivating on the
# first silently drops a talent's whole inbox from polling.
TOKEN_DEACTIVATE_AFTER_FAILURES = 3


def record_token_failure(db, row, exc: Exception, *, auth_rejected: bool = True) -> bool:
    """Record one failed token use/refresh on ``row`` and deactivate after 3 in a row.

    Always increments ``consecutive_failures`` and stores the reason in ``last_error``.
    Only an ``auth_rejected`` failure (Google refused the refresh token) counts toward
    deactivation — a network blip must not take a healthy inbox offline. On the 3rd
    consecutive rejection it sets ``active=False`` and ``deactivated_at`` (the reason
    stays in ``last_error``), logs at ERROR and posts to ALERT_WEBHOOK_URL.

    Commits. Returns True if this call deactivated the token.
    """
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
    deactivated = False
    if auth_rejected and row.active and row.consecutive_failures >= TOKEN_DEACTIVATE_AFTER_FAILURES:
        row.active = False
        row.deactivated_at = datetime.utcnow()
        deactivated = True
        logger.error(
            "Gmail token for %s DEACTIVATED after %d consecutive failures — reconnect required. "
            "Last error: %s",
            row.talent_key, row.consecutive_failures, row.last_error,
        )
    else:
        logger.warning(
            "Gmail token failure for %s (%d/%d%s): %s",
            row.talent_key, row.consecutive_failures, TOKEN_DEACTIVATE_AFTER_FAILURES,
            "" if auth_rejected else ", not an auth rejection — will not deactivate", exc,
        )
    db.add(row)
    db.commit()
    if deactivated:
        try:
            from backend.services.alerts import send_out_of_band_alert
            send_out_of_band_alert(
                f"Gmail token deactivated: {row.talent_key}",
                f"{row.email} failed {row.consecutive_failures} times in a row. "
                f"Polling, triage and drafting are OFF for this inbox until it is reconnected.\n"
                f"Last error: {row.last_error}",
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not post deactivation alert for %s", row.talent_key, exc_info=True)
    return deactivated


def proactive_refresh_all_tokens(db) -> dict:
    """
    Proactively refresh all active tokens expiring within 30 minutes.
    Called every 10 minutes by the scheduler. Returns a summary dict.
    """
    from backend.models.db import TalentToken
    summary = {"refreshed": 0, "failed": 0, "deactivated": 0}
    cutoff = datetime.utcnow() + timedelta(minutes=30)
    candidates = (
        db.query(TalentToken)
        .filter(
            TalentToken.active == True,  # noqa: E712
            TalentToken.token_expiry != None,  # noqa: E711
            TalentToken.token_expiry < cutoff,
        )
        .all()
    )
    for row in candidates:
        creds = credentials_from_token_row(row)
        try:
            creds.refresh(Request())
            row.access_token = creds.token
            if creds.expiry:
                row.token_expiry = creds.expiry.replace(tzinfo=None)
            row.consecutive_failures = 0
            row.last_error = None
            db.add(row)
            summary["refreshed"] += 1
        except Exception as exc:  # noqa: BLE001
            # Only Google rejecting the refresh token (RefreshError) counts toward
            # deactivation; a network/transport error is a failure but not a dead token.
            if record_token_failure(db, row, exc, auth_rejected=isinstance(exc, RefreshError)):
                summary["deactivated"] += 1
            else:
                summary["failed"] += 1
    db.commit()
    return summary


def reset_token_failure(db, talent_key: str) -> None:
    """Reset failure counters after a successful reconnect."""
    from backend.models.db import TalentToken
    row = db.query(TalentToken).filter(TalentToken.talent_key.ilike(talent_key)).first()
    if row:
        row.consecutive_failures = 0
        row.last_error = None
        row.deactivated_at = None
        db.add(row)
        db.commit()


def refresh_if_needed(creds: Credentials) -> Credentials:
    """Refresh the access token if expired or expiring within 5 minutes. Raises TokenRefreshError if Google rejects it."""
    expiring_soon = (
        creds.expiry is not None
        and (creds.expiry.replace(tzinfo=None) - datetime.utcnow()) < timedelta(minutes=5)
    )
    if creds.expired or not creds.valid or expiring_soon:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise TokenRefreshError(f"Google rejected token refresh: {exc}") from exc
        except Exception as exc:
            logger.error("Token refresh failed: %s", exc)
            raise
    return creds
