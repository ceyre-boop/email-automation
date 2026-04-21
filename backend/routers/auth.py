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
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
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
  <title>Gmail Connected — TABOOST</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    html, body {{ height: 100%; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif;
      background-color: #07070f;
      background-image:
        radial-gradient(ellipse at 18% 25%, rgba(34,197,94,.15)  0%, transparent 55%),
        radial-gradient(ellipse at 82% 78%, rgba(245,200,66,.08) 0%, transparent 55%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      color: #ffffff;
    }}

    .card {{
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.09);
      border-radius: 28px;
      padding: 52px 48px;
      max-width: 460px;
      width: 100%;
      text-align: center;
      backdrop-filter: blur(28px);
      -webkit-backdrop-filter: blur(28px);
      box-shadow:
        0 0 0 1px rgba(255,255,255,0.04) inset,
        0 40px 80px -16px rgba(0,0,0,.75),
        0 0 100px rgba(34,197,94,.10);
      animation: fadeUp .55s cubic-bezier(.22,.68,0,1.2) both;
    }}

    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(28px) scale(.97); }}
      to   {{ opacity: 1; transform: translateY(0)    scale(1);   }}
    }}

    /* ── Animated checkmark ── */
    .checkmark-wrap {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 20px;
    }}
    .checkmark {{
      width: 72px;
      height: 72px;
      filter: drop-shadow(0 0 16px rgba(34,197,94,.55));
    }}
    .check-circle {{
      stroke: #22c55e;
      stroke-width: 2;
      stroke-dasharray: 163;
      stroke-dashoffset: 163;
      animation: drawCircle .6s ease-out .15s forwards;
      transform-origin: center;
      fill: none;
    }}
    .check-path {{
      stroke: #22c55e;
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-dasharray: 50;
      stroke-dashoffset: 50;
      animation: drawCheck .4s ease-out .7s forwards;
      fill: none;
    }}
    @keyframes drawCircle {{ to {{ stroke-dashoffset: 0; }} }}
    @keyframes drawCheck  {{ to {{ stroke-dashoffset: 0; }} }}

    .brand {{
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .18em;
      text-transform: uppercase;
      color: #f5c842;
      margin-bottom: 14px;
    }}

    h1 {{
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -.02em;
      margin-bottom: 12px;
    }}

    p {{
      font-size: 15px;
      color: rgba(255,255,255,.6);
      line-height: 1.65;
    }}

    strong {{ color: #ffffff; }}

    @media (max-width: 520px) {{
      .card {{ padding: 36px 24px; border-radius: 22px; }}
      h1    {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="checkmark-wrap">
      <svg class="checkmark" viewBox="0 0 52 52" aria-hidden="true">
        <circle class="check-circle" cx="26" cy="26" r="25"/>
        <path   class="check-path"   d="M14 27l8 8 16-16"/>
      </svg>
    </div>
    <div class="brand">TABOOST</div>
    <h1>You're connected!</h1>
    <p>
      <strong>{talent_name}</strong>'s Gmail is now linked.<br />
      Drafts will appear automatically — you don't need to do anything else.
    </p>
  </div>
</body>
</html>"""
