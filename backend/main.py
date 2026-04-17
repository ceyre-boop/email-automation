"""
FastAPI application entry point.
"""
from __future__ import annotations

import logging

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

# Serve the talent onboarding page at /connect?talent=<key>
_static_dir = Path(__file__).parent / "static"
app.mount("/connect", StaticFiles(directory=str(_static_dir), html=True), name="connect")

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    logger.info("Creating database tables if they don't exist…")
    create_tables()
    logger.info("Startup complete.")
