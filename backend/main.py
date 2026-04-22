"""
FastAPI application entry point.
"""
from __future__ import annotations

import html as html_lib
import logging
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from backend.core.config import get_settings
from backend.models.db import create_tables
try:
    from backend.routers import auth, cron, drafts
except Exception as _import_exc:
    print(f"FATAL: router import failed — {_import_exc}", file=sys.stderr, flush=True)
    import traceback; traceback.print_exc(file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Talent Inbox Automation API",
    description="Centralized agency inbox manager: multi-talent Gmail polling, GPT drafting, unified review queue.",
    version="1.0.0",
)

# ── CORS ─────────────────────────────────────────────────────────────────────
try:
    settings = get_settings()
except Exception as _exc:  # pydantic ValidationError or similar
    print(
        f"FATAL: could not load settings — check required env vars: {_exc}",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(drafts.router)
app.include_router(cron.router)


# ── Onboarding page at /connect?talent=<key> ─────────────────────────────────
_connect_html_path = Path(__file__).parent / "static" / "connect.html"
_index_html_path = Path(__file__).parent / "static" / "index.html"
_home_html_path = Path(__file__).parent / "static" / "home.html"


@app.get("/api/talents", include_in_schema=False)
def api_talents():
    """Public endpoint — returns talent list for the onboarding page."""
    talents = [
        {"key": t["key"], "full_name": t.get("full_name", t["key"])}
        for t in get_settings().app_config.get("talents", [])
    ]
    return JSONResponse({"status": "ok", "talents": talents})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page():
    return HTMLResponse(content=_home_html_path.read_text(encoding="utf-8"))


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy():
    return HTMLResponse(content=_PRIVACY_HTML)


_PRIVACY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Privacy Policy — TABOOST</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{background:#080b14;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;padding:48px 24px;line-height:1.7}
    .wrap{max-width:720px;margin:0 auto}
    h1{font-size:32px;font-weight:700;color:#f8fafc;margin-bottom:8px;letter-spacing:-0.02em}
    .brand{color:#e91e8c}
    .updated{font-size:13px;color:rgba(255,255,255,0.35);margin-bottom:40px}
    h2{font-size:18px;font-weight:600;color:#f8fafc;margin:36px 0 10px}
    p,li{font-size:15px;color:rgba(255,255,255,0.6)}
    ul{padding-left:20px;margin-top:8px}
    li{margin-bottom:6px}
    a{color:#e91e8c;text-decoration:none}
    footer{margin-top:56px;font-size:12px;color:rgba(255,255,255,0.2);border-top:1px solid rgba(255,255,255,0.06);padding-top:24px}
  </style>
</head>
<body>
<div class="wrap">
  <h1><span class="brand">TABOOST</span> Privacy Policy</h1>
  <p class="updated">Last updated: April 21, 2025</p>

  <h2>What we are</h2>
  <p>TABOOST is a talent management platform. This application ("Email Automation") connects to Gmail accounts of talent we represent in order to manage brand deal email communications on their behalf.</p>

  <h2>What data we access</h2>
  <ul>
    <li>Gmail messages in the connected inbox (read access)</li>
    <li>Ability to create draft replies and modify labels</li>
    <li>Your Google account email address and name (for identification only)</li>
  </ul>

  <h2>How we use your data</h2>
  <ul>
    <li>Read inbound brand partnership emails and classify them automatically</li>
    <li>Generate draft replies using AI for human review before any sending occurs</li>
    <li>Log email metadata (sender, subject, classification) to an internal management dashboard</li>
  </ul>
  <p>We do <strong>not</strong> sell, share, or monetise your data. No emails are sent automatically — all drafts require explicit human approval.</p>

  <h2>Data storage</h2>
  <p>OAuth tokens are stored securely in an encrypted database hosted on Render.com. Email content is processed in memory and not persisted beyond the classification step.</p>

  <h2>Third-party services</h2>
  <ul>
    <li>Google Gmail API — to read and draft emails</li>
    <li>OpenAI API — to classify emails and generate draft text (email content is sent to OpenAI for this purpose)</li>
    <li>Google Sheets API — to log email metadata to an internal spreadsheet</li>
  </ul>

  <h2>Your rights</h2>
  <p>You may revoke access at any time by visiting <a href="https://myaccount.google.com/permissions" target="_blank">Google Account Permissions</a> and removing TABOOST. Contact us at <a href="mailto:info@taboost.me">info@taboost.me</a> to request deletion of stored data.</p>

  <h2>Contact</h2>
  <p>Email: <a href="mailto:info@taboost.me">info@taboost.me</a></p>

  <footer>© 2025 TABOOST. All rights reserved.</footer>
</div>
</body>
</html>"""


@app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
def onboarding_page(talent: str = Query(..., description="Talent key from settings.json")):
    """
    Serve the one-time Gmail onboarding page for a talent.
    Returns 404 if the talent_key is not defined in settings.json.
    """
    talent_map = {t["key"]: t for t in get_settings().app_config.get("talents", [])}
    if talent not in talent_map:
        raise HTTPException(status_code=404, detail=f"Unknown talent: {talent}")
    return HTMLResponse(content=_connect_html_path.read_text(encoding="utf-8"))


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    settings = get_settings()
    missing = [k for k in ("google_client_id", "google_client_secret", "openai_api_key", "database_url")
               if not getattr(settings, k)]
    if missing:
        logger.warning("Missing required env vars: %s — set these in Render dashboard → Environment", missing)

    if settings.database_url:
        logger.info("Creating database tables if they don't exist…")
        try:
            create_tables()
            logger.info("Startup complete.")
        except Exception:
            logger.exception("Could not create/verify database tables — check DATABASE_URL")
    else:
        logger.warning("DATABASE_URL not set — skipping table creation. App will start but DB routes will fail.")
