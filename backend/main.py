"""
FastAPI application entry point.
"""
from __future__ import annotations

import html as html_lib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from backend.core.config import get_settings
from backend.models.db import create_tables
from backend.routers import auth, cron, drafts

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
settings = get_settings()
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
    logger.info("Creating database tables if they don't exist…")
    create_tables()
    logger.info("Startup complete.")
