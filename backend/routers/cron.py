"""
Cron + status routes.

GET  /cron/poll-inboxes   → triggered by Railway cron every 5 minutes
GET  /health              → health check
GET  /api/status          → talent connection status overview
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services.oauth import proactive_refresh_all_tokens
from backend.services.poller import poll_all_inboxes

router = APIRouter(tags=["internal"])
logger = logging.getLogger(__name__)


from datetime import datetime as _dt
_DEPLOY_TIME = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

@router.get("/health")
def health():
    return {"status": "ok", "deployed_at": _DEPLOY_TIME}


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


def _run_proactive_refresh():
    """Proactively refresh tokens expiring within 30 minutes. Runs every 10 min."""
    from backend.models.db import get_session_factory
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        summary = proactive_refresh_all_tokens(db)
        if summary["refreshed"] or summary["failed"] or summary["deactivated"]:
            logger.info("Proactive token refresh: %s", summary)
    except Exception as exc:  # noqa: BLE001
        logger.error("Proactive token refresh failed: %s", exc)
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


@router.get("/api/n8n/new-escalations", dependencies=[Depends(verify_api_key)])
def new_escalations(since_minutes: int = 5, db: Session = Depends(get_db)):
    """
    Returns escalations created in the last N minutes.
    n8n polls this after each poll cycle to detect new escalations needing manager attention.
    """
    since = datetime.utcnow() - timedelta(minutes=since_minutes)
    rows = (
        db.query(Draft)
        .filter(Draft.is_escalate == True, Draft.created_at >= since)  # noqa: E712
        .order_by(Draft.created_at.desc())
        .all()
    )
    return {
        "count": len(rows),
        "escalations": [
            {
                "id": r.id,
                "talent_key": r.talent_key,
                "sender": r.sender,
                "subject": r.subject,
                "escalate_reason": r.escalate_reason,
                "brand_name": r.brand_name,
                "proposed_rate": r.proposed_rate,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.post("/api/test/send-test-email", dependencies=[Depends(verify_api_key)])
def send_test_email(db: Session = Depends(get_db)):
    """
    Send a test email to colineyre222@gmail.com using the first active talent token.
    Verifies that Gmail OAuth + sending is fully wired up.
    """
    from backend.services import gmail as gmail_svc

    token_row = db.query(TalentToken).filter(TalentToken.active == True).first()  # noqa: E712
    if not token_row:
        raise HTTPException(status_code=503, detail="No active talent tokens — connect a Gmail account first")

    success = gmail_svc.send_standalone_message(
        token_row,
        to="colineyre222@gmail.com",
        subject="TABOOST System Test — Email Sending Works",
        body=(
            "This is a test email from the TABOOST email automation system.\n\n"
            f"Sent from: {token_row.talent_key} inbox ({token_row.email})\n"
            "System status: Gmail OAuth connected and sending operational\n\n"
            "You can safely delete this email."
        ),
        db=db,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Email send failed — check server logs for Gmail API error")

    return {"ok": True, "sent_from": token_row.talent_key, "sent_from_email": token_row.email, "sent_to": "colineyre222@gmail.com"}


class N8nApproveBody(BaseModel):
    draft_id: int
    reviewed_by: str = "n8n"


@router.post("/api/n8n/approve-draft")
def n8n_approve_draft(
    body: N8nApproveBody,
    x_n8n_secret: str = Header(None),
    db: Session = Depends(get_db),
):
    """
    Webhook endpoint called by n8n when a manager approves a draft.
    Protected by X-N8N-Secret header (set N8N_WEBHOOK_SECRET in Render env vars).
    Idempotent: returns 200 silently if draft is already sent.
    """
    expected = os.environ.get("N8N_WEBHOOK_SECRET", "")
    if expected and x_n8n_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid n8n webhook secret.")

    draft = db.query(Draft).filter(Draft.id == body.draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail=f"Draft {body.draft_id} not found.")

    # Idempotent — already sent is a success, not an error
    if draft.status == DraftStatus.sent:
        return {"ok": True, "status": "already_sent", "draft_id": body.draft_id}

    if draft.status != DraftStatus.pending:
        raise HTTPException(status_code=400, detail=f"Draft is '{draft.status}' — cannot approve.")

    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(draft.talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail=f"No active Gmail token for {draft.talent_key}.")

    from backend.services import gmail as gmail_svc
    from backend.services.oauth import TokenRefreshError

    try:
        success = gmail_svc.send_reply(
            token_row=token,
            thread_id=draft.thread_id or "",
            reply_to=draft.sender or "",
            subject=draft.subject or "",
            body=draft.draft_text,
            db=db,
        )
    except TokenRefreshError:
        token.active = False
        db.add(token)
        db.commit()
        raise HTTPException(status_code=401, detail=f"Gmail token expired for {draft.talent_key} — reconnect needed.")

    if not success:
        raise HTTPException(status_code=502, detail="Gmail send failed.")

    if draft.gmail_draft_id:
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)

    draft.status = DraftStatus.sent
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)
    db.commit()
    logger.info("Draft %d approved via n8n by %s", body.draft_id, body.reviewed_by)
    return {"ok": True, "status": "sent", "draft_id": body.draft_id}
