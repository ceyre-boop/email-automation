"""
Cron + status routes.

GET  /cron/poll-inboxes   → triggered by Railway cron every 5 minutes
GET  /health              → health check
GET  /api/status          → talent connection status overview
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services.oauth import proactive_refresh_all_tokens
from backend.services.poller import poll_all_inboxes


router = APIRouter(tags=["internal"])
logger = logging.getLogger(__name__)

# Mutex preventing draft_queue and backlog_blaster from overlapping.
# Both jobs run 50-worker thread pools against the same NOT-IN subquery —
# without this lock they race to create duplicate Gmail drafts.
_draft_queue_lock = threading.Lock()


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
    Protected by _draft_queue_lock so backlog_blaster can never overlap this job.
    """
    if not _draft_queue_lock.acquire(blocking=False):
        logger.info("Draft queue skipped — already running")
        return
    try:
        _run_draft_queue_inner(batch_size)
    finally:
        _draft_queue_lock.release()


def _run_draft_queue_inner(batch_size: int = 60):
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
            _mgr = talent_cfg.get("manager", "")
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
                    manager_name=_mgr,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Draft queue error %s/%s: %s", _tk, row.gmail_message_id, exc)
            finally:
                thread_db.close()

        # 5 workers → 6 peak DB sessions (1 main + 5 workers). Was 10 (11 sessions).
        # Commit 3 target: release session before Gmail I/O to reduce lock hold time.
        with ThreadPoolExecutor(max_workers=5) as executor:
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
    Runs every 30s. Processes up to 300 backlogged emails per cycle — no cap on
    total backlog; each scheduler tick just keeps biting chunks until it's empty.
    """
    _run_draft_queue(batch_size=300)


def _run_guardian():
    """Guardian self-healing watchdog — runs every 60 seconds."""
    from backend.models.db import get_session_factory
    import backend.main as _main_module
    from backend.services.guardian import GuardianWatchdog
    from sqlalchemy import text as _text
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        GuardianWatchdog(scheduler=_main_module._scheduler).run(db)
        # Cleanup score=0 ghost rows from crashed poll cycles.
        # Moved here from create_tables() startup — locking processed_emails at boot
        # was delaying port binding and triggering Render R10 restart timeouts.
        try:
            db.execute(_text(
                "DELETE FROM processed_emails WHERE score = 0 "
                "AND processed_at < NOW() - INTERVAL '10 minutes'"
            ))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.error("Guardian watchdog failed: %s", exc)
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


def _run_reconcile():
    """Reconcile pending drafts against Gmail every 5 minutes."""
    from backend.models.db import get_session_factory, Draft, DraftStatus, ProcessedEmail, TalentToken, EmailStatus
    from backend.services import gmail as gmail_svc
    from datetime import datetime as _dt

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        pending = (
            db.query(Draft)
            .filter(Draft.status == DraftStatus.pending, Draft.gmail_draft_id.isnot(None))
            .all()
        )
        sent = discarded = skipped = 0
        for draft in pending:
            token = db.query(TalentToken).filter(
                TalentToken.talent_key.ilike(draft.talent_key),
                TalentToken.active == True,  # noqa: E712
            ).first()
            if not token:
                continue
            try:
                if gmail_svc.draft_exists_in_gmail(token, draft.gmail_draft_id, db=db):
                    skipped += 1
                    continue
                # Draft gone from Gmail — check if it was manually sent
                if draft.thread_id and gmail_svc.thread_has_sent_reply(
                    token, draft.thread_id, draft.gmail_message_id, db=db
                ):
                    draft.status = DraftStatus.sent
                    draft.reviewed_at = _dt.utcnow()
                    draft.reviewed_by = "gmail-reconciler"
                    pe = db.query(ProcessedEmail).filter(
                        ProcessedEmail.gmail_message_id == draft.gmail_message_id
                    ).first()
                    if pe:
                        pe.status = EmailStatus.sent
                        db.add(pe)
                    gmail_svc.mark_initial_response_sent(token, draft.gmail_message_id, db=db)
                    sent += 1
                else:
                    draft.status = DraftStatus.discarded
                    draft.reviewed_at = _dt.utcnow()
                    draft.reviewed_by = "gmail-reconciler"
                    discarded += 1
                db.add(draft)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconcile: error on draft %s: %s", draft.id, exc)
        # Second pass: discard pending drafts that were never saved to Gmail
        no_id_drafts = db.query(Draft).filter(
            Draft.status == DraftStatus.pending,
            Draft.gmail_draft_id.is_(None),
            Draft.is_escalate == False,  # noqa: E712
        ).all()
        for draft in no_id_drafts:
            draft.status = DraftStatus.discarded
            draft.reviewed_at = _dt.utcnow()
            draft.reviewed_by = "reconciler-no-draft-id"
            db.add(draft)
        discarded += len(no_id_drafts)
        db.commit()
        if sent or discarded:
            logger.info("Draft reconciliation: sent=%d discarded=%d skipped=%d", sent, discarded, skipped)
    except Exception as exc:  # noqa: BLE001
        logger.error("Draft reconciliation failed: %s", exc)
    finally:
        db.close()


def _run_inbox_reconcile():
    """
    Compare InboxEmail DB rows against actual Gmail INBOX contents for each talent.
    Delete rows whose message is no longer in INBOX (archived, replied, or moved manually).
    Runs hourly via scheduler; also triggered by POST /api/sync/reconcile.
    """
    from backend.models.db import get_session_factory, InboxEmail, TalentToken
    from backend.services import gmail as gmail_svc

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
        total_removed = 0
        for token in tokens:
            try:
                service = gmail_svc.build_service(token, db)
                # Fetch all current INBOX message IDs for this talent in one paginated pass
                inbox_ids: set[str] = set()
                page_token = None
                while True:
                    kwargs: dict = {"userId": "me", "labelIds": ["INBOX"], "maxResults": 500}
                    if page_token:
                        kwargs["pageToken"] = page_token
                    result = service.users().messages().list(**kwargs).execute()
                    for msg in result.get("messages", []):
                        inbox_ids.add(msg["id"])
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break

                # Delete InboxEmail rows no longer in Gmail INBOX
                db_rows = db.query(InboxEmail).filter(
                    InboxEmail.talent_key == token.talent_key.lower()
                ).all()
                removed = 0
                for row in db_rows:
                    if row.gmail_message_id not in inbox_ids:
                        db.delete(row)
                        removed += 1
                if removed:
                    db.commit()
                    total_removed += removed
                    logger.info(
                        "inbox_reconcile: %s removed=%d kept=%d",
                        token.talent_key, removed, len(db_rows) - removed,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("inbox_reconcile: error for %s: %s", token.talent_key, exc)
        if total_removed:
            logger.info("inbox_reconcile complete: total_removed=%d", total_removed)
    except Exception as exc:  # noqa: BLE001
        logger.error("inbox_reconcile failed: %s", exc)
    finally:
        db.close()


def _run_full_reconcile():
    """Run draft reconcile + inbox reconcile together. Called by the sync endpoint."""
    _run_reconcile()
    _run_inbox_reconcile()


def _run_auto_send():
    """Auto-send qualifying pending drafts after the configured hold period."""
    from backend.models.db import get_session_factory
    from backend.services.auto_send import run_auto_send
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        run_auto_send(db)
    except Exception as exc:  # noqa: BLE001
        logger.error("Auto-send job failed: %s", exc)
    finally:
        db.close()


@router.post("/api/sync/reconcile", dependencies=[Depends(verify_api_key)])
def trigger_reconcile(background_tasks: BackgroundTasks):
    """On-demand Gmail sync: reconcile pending drafts and inbox emails against real Gmail state."""
    background_tasks.add_task(_run_full_reconcile)
    return {"status": "reconciliation started"}


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


@router.post("/cron/reconcile-drafts", dependencies=[Depends(verify_api_key)])
def trigger_reconcile(background_tasks: BackgroundTasks):
    """Manually trigger draft reconciliation (detects drafts sent directly from Gmail)."""
    background_tasks.add_task(_run_reconcile)
    return {"ok": True, "queued": True}


@router.get("/cron/poll-inboxes", dependencies=[Depends(verify_api_key)])
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


