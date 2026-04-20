"""
Cron + status routes.

GET  /cron/poll-inboxes   → triggered by Railway cron every 5 minutes
GET  /health              → health check
GET  /api/status          → talent connection status overview
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services.poller import poll_all_inboxes

router = APIRouter(tags=["internal"])
logger = logging.getLogger(__name__)


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/cron/poll-inboxes")
def cron_poll(db: Session = Depends(get_db)):
    """
    Poll all connected talent inboxes.
    Called by Railway cron every POLL_INTERVAL_MINUTES.
    Returns a summary JSON — errors are logged but never raise (keeps cron stable).
    """
    try:
        summary = poll_all_inboxes(db)
        return {"ok": True, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        logger.error("Poll failed: %s", exc)
        return {"ok": False, "error": "Polling failed — check server logs for details."}


@router.get("/api/status", dependencies=[Depends(verify_api_key)])
def get_status(db: Session = Depends(get_db)):
    """
    Return connection status for every talent defined in settings.json.
    Used by the agency dashboard to show who is connected.
    """
    settings = get_settings()
    talents = settings.app_config.get("talents", [])
    connected = {
        row.talent_key: {
            "email": row.email,
            "connected_at": row.connected_at.isoformat(),
            "active": row.active,
        }
        for row in db.query(TalentToken).all()
    }
    pending_count = db.query(Draft).filter(Draft.status == DraftStatus.pending).count()

    return {
        "talents": [
            {
                "key": t["key"],
                "full_name": t.get("full_name", t["key"]),
                "manager": t.get("manager"),
                "connected": t["key"] in connected,
                **connected.get(t["key"], {}),
            }
            for t in talents
        ],
        "pending_drafts": pending_count,
    }
