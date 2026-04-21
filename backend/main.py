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
    return HTMLResponse(content=_index_html_path.read_text(encoding="utf-8"))


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
