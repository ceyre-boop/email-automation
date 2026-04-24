"""
Cron + status routes.

GET  /cron/poll-inboxes   → triggered by Railway cron every 5 minutes
GET  /health              → health check
GET  /api/status          → talent connection status overview
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
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


def _run_poll():
    """Run the poll in a background thread with its own DB session."""
    from backend.models.db import get_session_factory
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        summary = poll_all_inboxes(db)
        logger.info("Background poll complete: %s", summary)
    except Exception as exc:  # noqa: BLE001
        logger.error("Background poll failed: %s", exc)
    finally:
        db.close()


@router.get("/cron/poll-inboxes")
def cron_poll(background_tasks: BackgroundTasks):
    """
    Poll all connected talent inboxes in the background.
    Returns immediately — poll result appears in logs and DB.
    """
    background_tasks.add_task(_run_poll)
    return {"ok": True, "status": "poll started in background"}


@router.get("/api/db-check", dependencies=[Depends(verify_api_key)])
def db_check(db: Session = Depends(get_db)):
    """Quick DB connectivity check — returns row counts or the error."""
    try:
        talent_count = db.query(TalentToken).count()
        draft_count = db.query(Draft).count()
        return {"ok": True, "talent_rows": talent_count, "draft_rows": draft_count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/api/status", dependencies=[Depends(verify_api_key)])
def get_status(db: Session = Depends(get_db)):
    """
    Return connection status for every talent defined in settings.json.
    Used by the agency dashboard to show who is connected.
    """
    settings = get_settings()
    talents = settings.app_config.get("talents", [])
    connected = {
        row.talent_key.lower(): {
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
                "connected": t["key"].lower() in connected,
                **connected.get(t["key"].lower(), {}),
            }
            for t in talents
        ],
        "pending_drafts": pending_count,
    }
