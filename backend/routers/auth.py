"""
Auth router — Google OAuth flow for talent Gmail onboarding.

GET  /auth/connect?talent_key=katrina  → redirect to Google consent screen
GET  /auth/callback                    → receive code, store tokens, show success
"""
from __future__ import annotations

import html as html_lib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import TalentToken
from backend.routers.deps import get_db
from backend.services.oauth import build_authorization_url, exchange_code

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.get("/connect")
def connect_gmail(talent_key: str = Query(..., description="Talent identifier from settings.json")):
    """
    Step 1 — Redirect the talent to Google's consent screen.
    The talent_key is encoded in the OAuth `state` parameter so we can identify
    which talent's tokens to store when Google redirects back.
    """
    settings = get_settings()
    # Verify talent exists in config
    talent_map = {t["key"]: t for t in settings.app_config.get("talents", [])}
    if talent_key not in talent_map:
        raise HTTPException(status_code=404, detail=f"Unknown talent_key: {talent_key}")

    auth_url = build_authorization_url(talent_key)
    return RedirectResponse(url=auth_url)


@router.get("/callback")
def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    Step 2 — Google redirects here after the talent consents.
    Exchange the authorization code for tokens and store them.
    """
    talent_key = state  # We encoded talent_key as the state param
    settings = get_settings()
    talent_map = {t["key"]: t for t in settings.app_config.get("talents", [])}

    if talent_key not in talent_map:
        raise HTTPException(status_code=400, detail=f"Unknown talent_key in state: {talent_key}")

    try:
        token_data = exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        logger.error("Token exchange failed for %s: %s", talent_key, exc)
        raise HTTPException(status_code=500, detail="Token exchange failed") from exc

    # Fetch the Gmail email address from Google's userinfo endpoint
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    )
    try:
        oauth2_service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        userinfo = oauth2_service.userinfo().get().execute()
        email = userinfo.get("email", "")
        google_user_id = userinfo.get("id", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch userinfo for %s: %s", talent_key, exc)
        email = ""
        google_user_id = ""

    # Upsert the token row
    existing = db.query(TalentToken).filter(TalentToken.talent_key == talent_key).first()
    if existing:
        existing.access_token = token_data["access_token"]
        existing.refresh_token = token_data.get("refresh_token") or existing.refresh_token
        existing.token_expiry = token_data.get("expiry")
        existing.email = email or existing.email
        existing.google_user_id = google_user_id or existing.google_user_id
        existing.active = True
        db.add(existing)
    else:
        row = TalentToken(
            talent_key=talent_key,
            email=email,
            google_user_id=google_user_id,
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            token_expiry=token_data.get("expiry"),
            active=True,
        )
        db.add(row)

    db.commit()
    logger.info("Token stored for talent_key=%s email=%s", talent_key, email)

    talent_name = talent_map[talent_key].get("full_name", talent_key)
    # HTML-escape before interpolating into the success page
    talent_name_escaped = html_lib.escape(talent_name)
    return HTMLResponse(content=_success_page(talent_name_escaped))


def _success_page(talent_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gmail Connected</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; margin: 0; background: #f0fdf4; }}
    .card {{ background: white; border-radius: 16px; padding: 48px 40px;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); max-width: 420px; text-align: center; }}
    .icon {{ font-size: 56px; margin-bottom: 16px; }}
    h1 {{ color: #15803d; font-size: 24px; margin: 0 0 12px; }}
    p {{ color: #555; line-height: 1.6; margin: 0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>You're connected!</h1>
    <p>
      <strong>{talent_name}</strong>'s Gmail is now linked.<br />
      Drafts will appear automatically — you don't need to do anything else.
    </p>
  </div>
</body>
</html>"""
