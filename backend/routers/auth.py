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
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent = talent_map.get(talent_key.lower())
    if not talent:
        raise HTTPException(status_code=404, detail=f"Unknown talent_key: {talent_key}")

    auth_url = build_authorization_url(talent["key"])
    return RedirectResponse(url=auth_url)


@router.get("/callback")
def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error:
        return HTMLResponse(content=_error_page(error))
    """
    Step 2 — Google redirects here after the talent consents.
    Exchange the authorization code for tokens and store them.
    """
    if not code or not state:
        return HTMLResponse(content=_error_page("missing_params"))

    talent_key = state
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
    body {{
      min-height: 100vh;
      background: #080b14;
      display: flex; align-items: center; justify-content: center;
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif;
      overflow: hidden; color: #e2e8f0;
    }}
    .bg-orbs {{ position: fixed; inset: 0; pointer-events: none; z-index: 0; }}
    .orb {{
      position: absolute; border-radius: 50%;
      filter: blur(90px); opacity: 0.28;
    }}
    .orb-1 {{ width: 520px; height: 520px; background: #7c3aed; top: -140px; left: -120px; }}
    .orb-2 {{ width: 420px; height: 420px; background: #1d4ed8; bottom: -100px; right: -100px; }}
    .orb-3 {{ width: 320px; height: 320px; background: #be185d; top: 45%; left: 58%; }}
    .glass-card {{
      position: relative; z-index: 1;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.10);
      backdrop-filter: blur(28px) saturate(180%);
      -webkit-backdrop-filter: blur(28px) saturate(180%);
      border-radius: 28px;
      padding: 56px 44px 48px;
      max-width: 420px; width: calc(100% - 32px); text-align: center;
      box-shadow: 0 0 0 1px rgba(255,255,255,0.05) inset, 0 40px 80px rgba(0,0,0,0.65);
    }}
    .check-wrapper {{
      position: relative; width: 92px; height: 92px;
      margin: 0 auto 32px;
    }}
    .check-glow {{
      position: absolute; inset: -18px; border-radius: 50%;
      background: radial-gradient(circle, rgba(34,197,94,0.45) 0%, transparent 68%);
      animation: pulse-glow 3s ease-in-out infinite; z-index: 1;
    }}
    .check-circle {{
      width: 92px; height: 92px; border-radius: 50%;
      background: rgba(34,197,94,0.12);
      border: 2px solid rgba(34,197,94,0.45);
      display: flex; align-items: center; justify-content: center;
      font-size: 40px; color: #4ade80; position: relative; z-index: 2;
      animation: check-pop 0.45s cubic-bezier(0.34, 1.56, 0.64, 1) both;
    }}
    @keyframes pulse-glow {{
      0%, 100% {{ opacity: 0.55; transform: scale(1); }}
      50%       {{ opacity: 0.9;  transform: scale(1.14); }}
    }}
    @keyframes check-pop {{
      from {{ transform: scale(0.4); opacity: 0; }}
      to   {{ transform: scale(1);   opacity: 1; }}
    }}
    h1 {{
      font-size: 30px; font-weight: 700; color: #f8fafc;
      line-height: 1.2; letter-spacing: -0.02em; margin-bottom: 12px;
    }}
    .name-accent {{ color: #a78bfa; }}
    .body-text {{
      font-size: 15px; color: rgba(255,255,255,0.45);
      line-height: 1.65; margin-bottom: 0;
    }}
    .powered-by {{
      font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase;
      color: rgba(167,139,250,0.4); margin-top: 32px;
    }}
  </style>
</head>
<body>
  <div class="bg-orbs">
    <div class="orb orb-1"></div>
    <div class="orb orb-2"></div>
    <div class="orb orb-3"></div>
  </div>
  <main class="glass-card">
    <div class="check-wrapper">
      <div class="check-glow"></div>
      <div class="check-circle">✓</div>
    </div>
    <h1>You're all set,<br/><span class="name-accent">{talent_name}</span></h1>
    <p class="body-text">
      Your Gmail is now connected.<br/>
      Drafts will appear automatically —<br/>you don't need to do anything else.
    </p>
    <p class="powered-by">Powered by TABOOST</p>
  </main>
</body>
</html>"""


def _error_page(error: str) -> str:
    messages = {
        "access_denied": ("Permission declined", "You cancelled the sign-in or your account isn't approved yet. Ask your manager to add your Gmail to the authorised list, then try again."),
        "missing_params": ("Something went wrong", "The sign-in response was incomplete. Please close this tab and try again from the start."),
    }
    title, body = messages.get(error, ("Sign-in failed", f"Google returned an error: {html_lib.escape(error)}. Please try again."))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — TABOOST</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ min-height: 100vh; background: #080b14; display: flex; align-items: center; justify-content: center; font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif; overflow: hidden; color: #e2e8f0; }}
    .bg-orbs {{ position: fixed; inset: 0; pointer-events: none; z-index: 0; }}
    .orb {{ position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.28; }}
    .orb-1 {{ width: 520px; height: 520px; background: #7c3aed; top: -140px; left: -120px; }}
    .orb-2 {{ width: 420px; height: 420px; background: #1d4ed8; bottom: -100px; right: -100px; }}
    .glass-card {{ position: relative; z-index: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10); backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px); border-radius: 28px; padding: 52px 44px 44px; max-width: 420px; width: calc(100% - 32px); text-align: center; box-shadow: 0 40px 80px rgba(0,0,0,0.65); }}
    .icon {{ font-size: 52px; margin-bottom: 24px; }}
    h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; margin-bottom: 14px; letter-spacing: -0.02em; }}
    p {{ font-size: 15px; color: rgba(255,255,255,0.45); line-height: 1.65; margin-bottom: 28px; }}
    a.btn {{ display: inline-flex; align-items: center; justify-content: center; background: rgba(167,139,250,0.15); border: 1px solid rgba(167,139,250,0.35); color: #a78bfa; border-radius: 12px; padding: 13px 28px; font-size: 14px; font-weight: 600; text-decoration: none; transition: background 0.15s; }}
    a.btn:hover {{ background: rgba(167,139,250,0.25); }}
    .powered-by {{ font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase; color: rgba(167,139,250,0.35); margin-top: 24px; }}
  </style>
</head>
<body>
  <div class="bg-orbs"><div class="orb orb-1"></div><div class="orb orb-2"></div></div>
  <main class="glass-card">
    <div class="icon">⚠️</div>
    <h1>{html_lib.escape(title)}</h1>
    <p>{html_lib.escape(body)}</p>
    <a class="btn" href="/">← Try again</a>
    <p class="powered-by">Powered by TABOOST</p>
  </main>
</body>
</html>"""
