"""
Auth router — Google OAuth flow. Open to any user, no pre-registration needed.

GET  /auth/connect   → redirect to Google consent screen
GET  /auth/callback  → receive code, auto-create or update user, show success
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import secrets
import unicodedata

import requests
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import OAuthState, TalentToken
from backend.routers.deps import get_db
from backend.services.oauth import build_authorization_url, exchange_code

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _generate_talent_key(name: str, email: str, db: Session) -> str:
    """Derive a unique talent_key from the user's name or email."""
    # Normalise: strip accents, lowercase, keep only letters/digits
    base = unicodedata.normalize("NFKD", name or email.split("@")[0])
    base = base.encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]", "", base.lower()) or "user"
    base = base[:24]

    # Make unique — append a number if key already exists
    key = base
    n = 2
    while db.query(TalentToken).filter(TalentToken.talent_key == key).first():
        key = f"{base}{n}"
        n += 1
    return key


@router.get("/connect")
def connect_gmail(talent_key: str | None = Query(None), db: Session = Depends(get_db)):
    """Redirect any user to the Google consent screen. Pin to a talent_key if provided."""
    state = secrets.token_urlsafe(32)
    db.add(OAuthState(state=state, pinned_talent_key=talent_key))
    db.commit()
    auth_url = build_authorization_url(state)
    return RedirectResponse(url=auth_url)


@router.get("/callback")
def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    try:
        return _oauth_callback_inner(code=code, state=state, error=error, db=db)
    except Exception as exc:
        logger.exception("Unhandled error in oauth_callback: %s", exc)
        return HTMLResponse(content=_error_page("token_exchange_failed"), status_code=200)


def _oauth_callback_inner(
    code: str | None,
    state: str | None,
    error: str | None,
    db,
):
    if error:
        return HTMLResponse(content=_error_page(error))

    if not code or not state:
        return HTMLResponse(content=_error_page("missing_params"))

    # Validate CSRF state (DB-backed so restarts don't invalidate it)
    state_row = db.query(OAuthState).filter(OAuthState.state == state).first()
    if not state_row:
        return HTMLResponse(content=_error_page("invalid_state"))
    pinned_talent_key = state_row.pinned_talent_key
    db.delete(state_row)
    db.commit()

    settings = get_settings()

    try:
        token_data = exchange_code(code)
    except Exception as exc:
        logger.error("Token exchange failed: %s", exc)
        return HTMLResponse(content=_error_page("token_exchange_failed"))

    # Fetch Google profile via userinfo endpoint (simpler than googleapiclient.discovery)
    try:
        resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=10,
        )
        resp.raise_for_status()
        userinfo = resp.json()
        email = userinfo.get("email", "")
        google_user_id = userinfo.get("sub", "")
        full_name = userinfo.get("name", "") or email.split("@")[0] or "there"
    except Exception as exc:
        logger.warning("Could not fetch userinfo: %s", exc)
        email = ""
        google_user_id = ""
        full_name = "there"

    # Upsert:
    # A. If we pinned a talent_key (from /connect?talent_key=X), use that.
    # B. Else try to find existing by google_user_id or email.
    # C. Else create new with auto-key.
    
    existing = None
    if pinned_talent_key:
        # Case-insensitive lookup so reconnects always find the existing row
        existing = db.query(TalentToken).filter(
            TalentToken.talent_key.ilike(pinned_talent_key)
        ).first()

    if not existing and google_user_id:
        existing = db.query(TalentToken).filter(TalentToken.google_user_id == google_user_id).first()

    if not existing and email:
        existing = db.query(TalentToken).filter(TalentToken.email == email).first()

    if existing:
        existing.access_token = token_data["access_token"]
        existing.refresh_token = token_data.get("refresh_token") or existing.refresh_token
        existing.token_expiry = token_data.get("expiry")
        existing.email = email or existing.email
        existing.google_user_id = google_user_id or existing.google_user_id
        existing.active = True
        db.add(existing)
        talent_key = existing.talent_key
    else:
        # Create new — use pinned key if available, else generate
        talent_key = pinned_talent_key or _generate_talent_key(full_name, email, db)
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
    logger.info("User connected: talent_key=%s email=%s", talent_key, email)

    # Kick off an immediate inbox sync in the background so the dashboard
    # is populated before the manager even opens it
    try:
        import threading
        from backend.models.db import get_session_factory, TalentToken as _TT
        from backend.services.inbox_sync import fetch_pending_bodies, sync_inbox_for_talent
        connected_key = talent_key
        def _initial_sync(tk=connected_key):
            _db = get_session_factory()()
            try:
                tok = _db.query(_TT).filter(_TT.talent_key.ilike(tk), _TT.active == True).first()  # noqa: E712
                if tok:
                    sync_inbox_for_talent(tok, _db)
                    fetch_pending_bodies(tok, _db, limit=50)
            except Exception as exc:
                logger.warning("Initial inbox sync failed for %s: %s", tk, exc)
            finally:
                _db.close()
        threading.Thread(target=_initial_sync, daemon=True).start()
    except Exception as exc:
        logger.warning("Could not start initial sync thread: %s", exc)

    return HTMLResponse(content=_success_page(html_lib.escape(full_name)))


def _success_page(name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gmail Connected — TABOOST</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh; background: #080b14;
      display: flex; align-items: center; justify-content: center;
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
      overflow: hidden; color: #e2e8f0;
    }}
    .bg {{ position: fixed; inset: 0; pointer-events: none; z-index: 0; }}
    .orb {{ position: absolute; border-radius: 50%; filter: blur(100px); opacity: 0.22; }}
    .orb-1 {{ width: 600px; height: 600px; background: #e91e8c; top: -200px; left: -150px; }}
    .orb-2 {{ width: 500px; height: 500px; background: #6d28d9; bottom: -150px; right: -100px; }}
    .card {{
      position: relative; z-index: 1;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.09);
      backdrop-filter: blur(32px); -webkit-backdrop-filter: blur(32px);
      border-radius: 28px; padding: 56px 44px 48px;
      max-width: 420px; width: calc(100% - 32px); text-align: center;
      box-shadow: 0 40px 80px rgba(0,0,0,0.6);
    }}
    .check-wrap {{ position: relative; width: 92px; height: 92px; margin: 0 auto 32px; }}
    .check-glow {{
      position: absolute; inset: -18px; border-radius: 50%;
      background: radial-gradient(circle, rgba(34,197,94,0.4) 0%, transparent 68%);
      animation: pulse 3s ease-in-out infinite;
    }}
    .check-circle {{
      width: 92px; height: 92px; border-radius: 50%;
      background: rgba(34,197,94,0.1); border: 2px solid rgba(34,197,94,0.4);
      display: flex; align-items: center; justify-content: center;
      font-size: 40px; color: #4ade80; position: relative; z-index: 2;
      animation: pop 0.45s cubic-bezier(0.34,1.56,0.64,1) both;
    }}
    @keyframes pulse {{ 0%,100% {{ opacity:0.5; transform:scale(1); }} 50% {{ opacity:0.9; transform:scale(1.14); }} }}
    @keyframes pop {{ from {{ transform:scale(0.4); opacity:0; }} to {{ transform:scale(1); opacity:1; }} }}
    h1 {{ font-size: 28px; font-weight: 700; color: #f8fafc; letter-spacing: -0.02em; margin-bottom: 12px; line-height: 1.2; }}
    .accent {{ color: #f472b6; }}
    p {{ font-size: 15px; color: rgba(255,255,255,0.42); line-height: 1.7; }}
    .footer {{ font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(233,30,140,0.3); margin-top: 32px; }}
  </style>
</head>
<body>
  <div class="bg"><div class="orb orb-1"></div><div class="orb orb-2"></div></div>
  <main class="card">
    <div class="check-wrap">
      <div class="check-glow"></div>
      <div class="check-circle">✓</div>
    </div>
    <h1>You're all set,<br/><span class="accent">{name}!</span></h1>
    <p>Your Gmail is connected to TABOOST.<br/>Brand deal emails will be handled automatically.<br/>You don't need to do anything else.</p>
    <div class="footer">Powered by TABOOST</div>
  </main>
</body>
</html>"""


def _error_page(error: str) -> str:
    messages = {
        "access_denied": ("Permission Declined", "You cancelled the sign-in. Close this tab and try again."),
        "missing_params": ("Something went wrong", "The sign-in response was incomplete. Please try again."),
        "invalid_state": ("Session expired", "Your sign-in session expired. Please try again."),
        "token_exchange_failed": ("Connection failed", "Could not connect to Google. Please try again in a moment."),
    }
    title, body = messages.get(error, ("Sign-in failed", f"Google returned an error: {html_lib.escape(error)}."))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{html_lib.escape(title)} — TABOOST</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{min-height:100vh;background:#080b14;display:flex;align-items:center;justify-content:center;
      font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;padding:24px;color:#e2e8f0}}
    .card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);
      backdrop-filter:blur(32px);border-radius:28px;padding:52px 44px 44px;
      max-width:420px;width:100%;text-align:center;box-shadow:0 40px 80px rgba(0,0,0,0.6)}}
    .icon{{font-size:48px;margin-bottom:24px}}
    h1{{font-size:24px;font-weight:700;color:#f8fafc;margin-bottom:12px;letter-spacing:-0.02em}}
    p{{font-size:14px;color:rgba(255,255,255,0.42);line-height:1.7;margin-bottom:28px}}
    a{{display:inline-flex;align-items:center;justify-content:center;
      background:linear-gradient(135deg,#e91e8c,#c2185b);color:#fff;
      border-radius:12px;padding:13px 28px;font-size:14px;font-weight:600;text-decoration:none}}
    .footer{{font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:rgba(233,30,140,0.3);margin-top:28px}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⚠️</div>
    <h1>{html_lib.escape(title)}</h1>
    <p>{html_lib.escape(body)}</p>
    <a href="/">Try again</a>
    <div class="footer">Powered by TABOOST</div>
  </div>
</body>
</html>"""
