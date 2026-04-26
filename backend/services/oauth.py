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
