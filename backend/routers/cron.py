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


def _run_draft_queue(batch_size: int = 60):
    """
    Continuous drafting worker — finds Score-3 emails with no draft and processes them.
    Uses a subquery to avoid full table scans, 50 parallel workers per run.
    Runs every 20s normally; when backlog > 0 it re-queues itself immediately.
    """
    from backend.models.db import get_session_factory, Draft, ProcessedEmail, TalentToken
    from backend.services.poller import _process_one_message
    from backend.core.config import get_settings as _gs
    from backend.services import gmail as gmail_svc
    from concurrent.futures import ThreadPoolExecutor
    from collections import defaultdict
    from sqlalchemy import select

    SessionLocal = get_session_factory()
    db = SessionLocal()
    drafted_count = 0
    try:
        settings = _gs()
        if not settings.app_config.get("ai_enabled", True):
            return
        draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)
        talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}

        # Subquery: find Score-3 ProcessedEmails that have no matching Draft row
        # Uses NOT EXISTS instead of loading all draft IDs into Python memory
        drafted_subq = select(Draft.gmail_message_id)
        candidates = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.score == 3,
                ProcessedEmail.status != "archived",
                ProcessedEmail.gmail_message_id.not_in(drafted_subq),
            )
            .order_by(ProcessedEmail.processed_at.desc())  # newest first
            .limit(batch_size)
            .all()
        )

        if not candidates:
            return

        logger.info("Draft queue: %d Score-3 emails need drafts (batch=%d)", len(candidates), batch_size)

        # Pre-fetch one token per talent so we don't re-query inside every thread
        token_cache: dict[str, object] = {}
        for talent_key in {row.talent_key.lower() for row in candidates}:
            tok = db.query(TalentToken).filter(
                TalentToken.talent_key.ilike(talent_key),
                TalentToken.active == True,  # noqa: E712
            ).first()
            if tok:
                token_cache[talent_key] = tok

        def _draft_one(row):
            _tk = row.talent_key.lower()
            talent_cfg = talent_map.get(_tk, {})
            if talent_cfg.get("paused"):
                return
            if _tk not in token_cache:
                return
            _tn = talent_cfg.get("full_name", _tk)
            _mr = talent_cfg.get("minimum_rate_usd", 0)
            thread_db = SessionLocal()
            try:
                thread_token = thread_db.query(TalentToken).filter(
                    TalentToken.talent_key.ilike(_tk),
                    TalentToken.active == True,  # noqa: E712
                ).first()
                if not thread_token:
                    return
                if thread_db.query(Draft).filter(Draft.gmail_message_id == row.gmail_message_id).first():
                    return  # race-condition guard
                service = gmail_svc.build_service(thread_token, thread_db)
                _process_one_message(
                    db=thread_db,
                    token_row=thread_token,
                    service=service,
                    message_id=row.gmail_message_id,
                    talent_key=_tk,
                    talent_name=_tn,
                    minimum_rate=_mr,
                    draft_mode=draft_mode,
                    summary={},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Draft queue error %s/%s: %s", _tk, row.gmail_message_id, exc)
            finally:
                thread_db.close()

        # All candidates submitted to a single pool — no per-talent serialisation
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(_draft_one, row) for row in candidates]
            for f in futures:
                try:
                    f.result()
                    drafted_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Draft queue future error: %s", exc)

        logger.info("Draft queue batch complete: %d processed", drafted_count)
    except Exception as exc:  # noqa: BLE001
        logger.error("Draft queue worker failed: %s", exc)
    finally:
        try:
            from backend.services.health import record_queue_heartbeat
            record_queue_heartbeat(db)
        except Exception:  # noqa: BLE001
            pass
        db.close()


def _run_backlog_blaster():
    """
    Idle-time worker — runs every 30s and processes up to 100 backlogged Score-3
    emails per cycle. Larger batch than the regular queue; designed to clear the
    backlog quickly when the system isn't busy with fresh incoming emails.
    Skips if the regular draft queue is currently active (max_instances=1 guard).
    """
    _run_draft_queue(batch_size=100)


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


@router.post("/api/admin/blast-backlog", dependencies=[Depends(verify_api_key)])
def blast_backlog(background_tasks: BackgroundTasks):
    """Blast ALL backlogged Score-3 emails with no draft — no batch limit, runs until empty."""
    background_tasks.add_task(_blast_all_until_empty)
    return {"ok": True, "message": "Full backlog blast started — will run until empty."}


def _blast_all_until_empty():
    """Keep calling the draft queue with large batches until there's nothing left."""
    import time
    rounds = 0
    while rounds < 20:  # safety cap: max 20 rounds × 200 emails = 4000 emails
        rounds += 1
        from backend.models.db import get_session_factory, Draft, ProcessedEmail
        from sqlalchemy import select
        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            drafted_subq = select(Draft.gmail_message_id)
            remaining = db.query(ProcessedEmail).filter(
                ProcessedEmail.score == 3,
                ProcessedEmail.status != "archived",
                ProcessedEmail.gmail_message_id.not_in(drafted_subq),
            ).count()
        finally:
            db.close()
        if remaining == 0:
            logger.info("Backlog blast complete after %d round(s).", rounds)
            break
        logger.info("Backlog blast round %d — %d remaining", rounds, remaining)
        _run_draft_queue(batch_size=200)
        time.sleep(2)  # brief pause between rounds to let DB settle


@router.get("/cron/poll-inboxes")
def cron_poll(background_tasks: BackgroundTasks):
    """
    Poll all connected talent inboxes in the background.
    Returns immediately — poll result appears in logs and DB.
    """
    background_tasks.add_task(_run_poll)
    return {"ok": True, "status": "poll started in background"}


@router.post("/api/admin/clear-cache", dependencies=[Depends(verify_api_key)])
def clear_cache():
    """Force reload of SOP and triage prompt caches on next email. Call after editing sop.md."""
    from backend.services.reply import clear_sop_cache
    from backend.services.triage import clear_triage_cache
    from backend.services.health import check_and_store_sop_hash
    from backend.models.db import get_session_factory
    clear_sop_cache()
    clear_triage_cache()
    db = get_session_factory()()
    try:
        sop_status = check_and_store_sop_hash(db)
    finally:
        db.close()
    logger.info("Cache cleared — SOP and triage prompt will reload on next email")
    return {"ok": True, "message": "SOP and triage caches cleared. New rules load on next email.", "sop": sop_status}


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


@router.post("/api/test/gpt-demo-reply", dependencies=[Depends(verify_api_key)])
def gpt_demo_reply(db: Session = Depends(get_db)):
    """
    Demo: GPT drafts a reply as Katrina to a fake low-ball offer, sends to colineyre222@gmail.com.
    Bypasses ai_enabled — this is an explicit one-off test only.
    """
    from openai import OpenAI
    from backend.services import gmail as gmail_svc

    token_row = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike("katrina"),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token_row:
        raise HTTPException(status_code=503, detail="Katrina's Gmail not connected")

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Katrina, a fashion content creator managed by TABOOST talent agency. "
                    "You write short, warm, conversational emails — never stiff or corporate. "
                    "Your minimum rate is $300 per video. When a brand low-balls you, you decline "
                    "the rate politely but leave the door open for them to come back with a real offer. "
                    "2-4 sentences max. Sign off as just 'Katrina'."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Reply to this email from a brand:\n\n"
                    "From: partnerships@budgetbeauty.com\n"
                    "Subject: Collab opportunity!\n\n"
                    "Hey Katrina! Huge fan of your content — your style is exactly what we're "
                    "looking for. We'd love to send you our new skincare line and pay $50 per video. "
                    "Let us know if you're in!\n\n"
                    "— BudgetBeauty team"
                ),
            },
        ],
        max_tokens=250,
        temperature=0.5,
    )

    draft_text = response.choices[0].message.content.strip()

    success = gmail_svc.send_standalone_message(
        token_row,
        to="colineyre222@gmail.com",
        subject="Re: Collab opportunity! (BudgetBeauty)",
        body=draft_text,
        db=db,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Gmail send failed — check logs")

    return {"ok": True, "sent_from": "katrina@taboost.me", "draft_text": draft_text}


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
        cc = gmail_svc.parse_cc_recipients(draft.cc_recipients)
        success = gmail_svc.send_reply(
            token_row=token,
            thread_id=draft.thread_id or "",
            reply_to=draft.sender or "",
            subject=draft.subject or "",
            body=draft.draft_text,
            db=db,
            in_reply_to=getattr(draft, "message_id_header", None),
            cc=cc or None,
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

    # Sync status on ProcessedEmail so the Sent tab reflects this
    from backend.models.db import EmailStatus, ProcessedEmail
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == draft.gmail_message_id
    ).first()
    if pe:
        pe.status = EmailStatus.sent
        db.add(pe)

    db.commit()
    logger.info("Draft %d approved via n8n by %s", body.draft_id, body.reviewed_by)
    return {"ok": True, "status": "sent", "draft_id": body.draft_id}
