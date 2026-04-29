"""
Manager Dashboard API router.

GET  /api/dashboard/report                        → Daily email stats per talent
GET  /api/dashboard/talents                       → All configured talents + connection status
GET  /api/dashboard/talents/{talent_key}/emails   → Last 50 processed emails for a talent
GET  /api/dashboard/talents/{talent_key}/drafts   → Pending drafts for a talent
GET  /api/dashboard/context                       → Active manager context entries
POST /api/dashboard/context                       → Add a new context entry
DELETE /api/dashboard/context/{context_id}        → Soft-delete a context entry
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import (
    Draft,
    DraftStatus,
    EmailStatus,
    InboxEmail,
    ManagerContext,
    PollHealth,
    ProcessedEmail,
    TalentToken,
    TriageAudit,
)
from backend.routers.deps import get_db, verify_api_key

router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(verify_api_key)],
)
logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class TalentReportCard(BaseModel):
    talent_key: str
    full_name: str
    manager: Optional[str] = None
    count_good: int
    count_uncertain: int
    count_trash: int
    total: int
    best_deal_brand: Optional[str] = None
    best_deal_rate: Optional[float] = None
    pending_drafts: int


class DailyReportOut(BaseModel):
    report_date: str
    total_good: int
    total_uncertain: int
    total_trash: int
    total_emails: int
    talents: list[TalentReportCard]


class TalentOut(BaseModel):
    key: str
    full_name: str
    manager: Optional[str] = None
    category: Optional[str] = None
    minimum_rate_usd: Optional[float] = None
    connected: bool
    email: Optional[str] = None
    inbox_email: Optional[str] = None
    connected_at: Optional[str] = None


class EmailOut(BaseModel):
    id: int
    gmail_message_id: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    score: Optional[int] = None
    brand_name: Optional[str] = None
    proposed_rate: Optional[float] = None
    offer_type: Optional[str] = None
    triage_reason: Optional[str] = None
    body_text: Optional[str] = None
    email_date: Optional[datetime] = None
    processed_at: datetime
    status: str

    class Config:
        from_attributes = True


class DraftOut(BaseModel):
    id: int
    talent_key: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    brand_name: Optional[str] = None
    proposed_rate: Optional[float] = None
    offer_type: Optional[str] = None
    draft_text: str
    gmail_message_id: str
    status: str
    is_escalate: bool
    escalate_reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ContextOut(BaseModel):
    id: int
    text: str
    added_at: datetime
    added_by: Optional[str] = None
    active: bool

    class Config:
        from_attributes = True


class ContextIn(BaseModel):
    text: str
    added_by: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/report", response_model=DailyReportOut)
def daily_report(db: Session = Depends(get_db)):
    """Report — shows last 7 days so data is always visible regardless of time zone."""
    settings = get_settings()
    talent_configs = settings.app_config.get("talents", [])

    today_utc = datetime.utcnow().date()
    window_start = datetime.combine(today_utc, datetime.min.time()) - timedelta(days=7)

    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= window_start)
        .all()
    )

    # group by lowercase key so 'katrina' matches config key 'Katrina'
    talent_emails: dict[str, list] = defaultdict(list)
    for row in rows:
        talent_emails[row.talent_key.lower()].append(row)

    pending_query = (
        db.query(Draft.talent_key, func.count(Draft.id).label("cnt"))
        .filter(Draft.status == DraftStatus.pending)
        .group_by(Draft.talent_key)
        .all()
    )
    pending_map: dict[str, int] = {r.talent_key.lower(): r.cnt for r in pending_query}

    total_good = total_uncertain = total_trash = 0
    cards: list[TalentReportCard] = []

    for t_cfg in talent_configs:
        key = t_cfg["key"]
        emails = talent_emails.get(key.lower(), [])

        count_good = sum(1 for e in emails if e.score == 3)
        count_uncertain = sum(1 for e in emails if e.score == 2)
        count_trash = sum(1 for e in emails if e.score == 1)
        total_good += count_good
        total_uncertain += count_uncertain
        total_trash += count_trash

        good_with_rate = [e for e in emails if e.score == 3 and e.proposed_rate]
        best = max(good_with_rate, key=lambda e: e.proposed_rate, default=None)

        cards.append(TalentReportCard(
            talent_key=key,
            full_name=t_cfg.get("full_name", key),
            manager=t_cfg.get("manager"),
            count_good=count_good,
            count_uncertain=count_uncertain,
            count_trash=count_trash,
            total=len(emails),
            best_deal_brand=best.brand_name if best else None,
            best_deal_rate=best.proposed_rate if best else None,
            pending_drafts=pending_map.get(key.lower(), 0),
        ))

    return DailyReportOut(
        report_date=today_utc.isoformat(),
        total_good=total_good,
        total_uncertain=total_uncertain,
        total_trash=total_trash,
        total_emails=total_good + total_uncertain + total_trash,
        talents=cards,
    )


@router.get("/talents", response_model=list[TalentOut])
def list_talents(db: Session = Depends(get_db)):
    """All configured talents with OAuth connection status."""
    settings = get_settings()
    talent_configs = settings.app_config.get("talents", [])

    connected = {
        row.talent_key.lower(): row
        for row in db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    }

    return [
        TalentOut(
            key=t["key"],
            full_name=t.get("full_name", t["key"]),
            manager=t.get("manager"),
            category=t.get("category"),
            minimum_rate_usd=t.get("minimum_rate_usd"),
            connected=t["key"].lower() in connected,
            email=connected[t["key"].lower()].email if t["key"].lower() in connected else None,
            inbox_email=t.get("inbox_email"),
            connected_at=connected[t["key"].lower()].connected_at.isoformat() if t["key"].lower() in connected else None,
        )
        for t in talent_configs
    ]


def _validate_talent(talent_key: str) -> None:
    talent_keys = {t["key"].lower() for t in get_settings().app_config.get("talents", [])}
    if talent_key.lower() not in talent_keys:
        raise HTTPException(status_code=404, detail=f"Unknown talent: {talent_key}")


@router.get("/talents/{talent_key}/emails", response_model=list[EmailOut])
def talent_emails(talent_key: str, db: Session = Depends(get_db)):
    """Last 50 processed emails for a talent, newest first."""
    _validate_talent(talent_key)
    return (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.talent_key == talent_key.lower())
        .order_by(ProcessedEmail.processed_at.desc())
        .limit(250)
        .all()
    )


@router.get("/talents/{talent_key}/drafts", response_model=list[DraftOut])
def talent_drafts(talent_key: str, db: Session = Depends(get_db)):
    """Pending drafts for a talent, newest first."""
    _validate_talent(talent_key)
    return (
        db.query(Draft)
        .filter(Draft.talent_key == talent_key.lower(), Draft.status == DraftStatus.pending)
        .order_by(Draft.created_at.desc())
        .all()
    )


@router.get("/context", response_model=list[ContextOut])
def list_context(db: Session = Depends(get_db)):
    """Active manager context entries, oldest first (applied in that order)."""
    return (
        db.query(ManagerContext)
        .filter(ManagerContext.active == True)  # noqa: E712
        .order_by(ManagerContext.added_at.asc())
        .all()
    )


@router.post("/context", response_model=ContextOut, status_code=201)
def add_context(body: ContextIn, db: Session = Depends(get_db)):
    """Add a new manager context entry."""
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text cannot be empty.")
    row = ManagerContext(text=body.text.strip(), added_by=body.added_by, active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Manager context added by %s: %.80s", body.added_by, body.text)
    return row


@router.delete("/context/{context_id}")
def delete_context(context_id: int, db: Session = Depends(get_db)):
    """Soft-delete (deactivate) a manager context entry."""
    row = db.query(ManagerContext).filter(ManagerContext.id == context_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Context entry not found.")
    row.active = False
    db.commit()
    logger.info("Manager context %d deactivated.", context_id)
    return {"ok": True}


# ── Health & observability ────────────────────────────────────────────────────


@router.get("/health/tokens")
def token_health(db: Session = Depends(get_db)):
    """Per-talent token health: consecutive failures, last error, last poll time."""
    rows = db.query(TalentToken).all()
    return [
        {
            "talent_key": r.talent_key,
            "email": r.email,
            "active": r.active,
            "consecutive_failures": r.consecutive_failures or 0,
            "last_error": r.last_error,
            "last_poll_at": r.last_poll_at.isoformat() if r.last_poll_at else None,
            "token_expiry": r.token_expiry.isoformat() if r.token_expiry else None,
        }
        for r in rows
    ]


@router.get("/health/poll-log")
def poll_log(talent_key: str, limit: int = 20, db: Session = Depends(get_db)):
    """Recent poll history for a talent — shows errors and durations."""
    rows = (
        db.query(PollHealth)
        .filter(PollHealth.talent_key == talent_key)
        .order_by(PollHealth.polled_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "polled_at": r.polled_at.isoformat(),
            "emails_found": r.emails_found,
            "emails_processed": r.emails_processed,
            "error_message": r.error_message,
            "duration_ms": r.duration_ms,
        }
        for r in rows
    ]


@router.get("/health/summary")
def health_summary(db: Session = Depends(get_db)):
    """Today's stats across all talents: emails, drafts, escalations, errors."""
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    emails_today = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= today).count()
    drafts_today = db.query(Draft).filter(Draft.created_at >= today).count()
    escalations_today = db.query(Draft).filter(Draft.created_at >= today, Draft.is_escalate == True).count()  # noqa: E712
    errors_today = db.query(PollHealth).filter(PollHealth.polled_at >= today, PollHealth.error_message != None).count()  # noqa: E711
    pending_drafts = db.query(Draft).filter(Draft.status == DraftStatus.pending).count()
    return {
        "emails_today": emails_today,
        "drafts_today": drafts_today,
        "escalations_today": escalations_today,
        "errors_today": errors_today,
        "pending_drafts": pending_drafts,
    }


@router.get("/audit/triage")
def triage_audit_for_email(email_id: str, db: Session = Depends(get_db)):
    """Return the triage audit record for a specific email — shows AI reasoning."""
    row = db.query(TriageAudit).filter(TriageAudit.gmail_message_id == email_id).order_by(TriageAudit.created_at.desc()).first()
    if not row:
        raise HTTPException(status_code=404, detail="No triage audit found for this email.")
    return {
        "gmail_message_id": row.gmail_message_id,
        "talent_key": row.talent_key,
        "parsed_score": row.parsed_score,
        "brand_detected": row.brand_detected,
        "rate_detected": row.rate_detected,
        "confidence": row.confidence,
        "reasoning": row.reasoning,
        "model_used": row.model_used,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/audit/recent")
def recent_triage_audits(talent_key: str, limit: int = 50, db: Session = Depends(get_db)):
    """Recent triage audit records for a talent — useful for spotting misclassifications."""
    rows = (
        db.query(TriageAudit)
        .filter(TriageAudit.talent_key == talent_key)
        .order_by(TriageAudit.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "gmail_message_id": r.gmail_message_id,
            "parsed_score": r.parsed_score,
            "brand_detected": r.brand_detected,
            "confidence": r.confidence,
            "reasoning": r.reasoning,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/talents/{talent_key}/sent")
def talent_sent_emails(talent_key: str, limit: int = 50, db: Session = Depends(get_db)):
    """Emails where a reply was sent — the missing 'Sent' tab."""
    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.talent_key == talent_key.lower(), ProcessedEmail.status == EmailStatus.sent)
        .order_by(ProcessedEmail.processed_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "gmail_message_id": r.gmail_message_id,
            "sender": r.sender,
            "subject": r.subject,
            "brand_name": r.brand_name,
            "proposed_rate": r.proposed_rate,
            "processed_at": r.processed_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/talents/{talent_key}/settings")
def get_talent_settings(talent_key: str, db: Session = Depends(get_db)):
    """Talent voice profile and manager instructions."""
    settings = get_settings()
    talent_cfg = next((t for t in settings.app_config.get("talents", []) if t["key"].lower() == talent_key.lower()), None)
    ctx = db.query(ManagerContext).filter(
        ManagerContext.talent_key == talent_key.lower(),
        ManagerContext.active == True,  # noqa: E712
    ).order_by(ManagerContext.added_at.desc()).first()
    return {
        "talent_key": talent_key,
        "full_name": talent_cfg.get("full_name") if talent_cfg else talent_key,
        "minimum_rate_usd": talent_cfg.get("minimum_rate_usd") if talent_cfg else None,
        "manager": talent_cfg.get("manager") if talent_cfg else None,
        "voice_profile": ctx.voice_profile if ctx else None,
        "manager_instructions": ctx.text if ctx else None,
        "context_id": ctx.id if ctx else None,
    }


class TalentSettingsIn(BaseModel):
    voice_profile: Optional[str] = None
    manager_instructions: Optional[str] = None
    added_by: Optional[str] = None


@router.put("/talents/{talent_key}/settings")
def update_talent_settings(talent_key: str, body: TalentSettingsIn, db: Session = Depends(get_db)):
    """Update voice profile / manager instructions for a talent without code changes."""
    ctx = db.query(ManagerContext).filter(
        ManagerContext.talent_key == talent_key.lower(),
        ManagerContext.active == True,  # noqa: E712
    ).order_by(ManagerContext.added_at.desc()).first()

    if ctx:
        if body.voice_profile is not None:
            ctx.voice_profile = body.voice_profile
        if body.manager_instructions is not None:
            ctx.text = body.manager_instructions
        db.add(ctx)
    else:
        ctx = ManagerContext(
            text=body.manager_instructions or "",
            voice_profile=body.voice_profile,
            talent_key=talent_key.lower(),
            added_by=body.added_by,
            active=True,
        )
        db.add(ctx)
    db.commit()
    return {"ok": True}


# ── n8n webhook: process a single email in real-time ─────────────────────────


class ProcessEmailIn(BaseModel):
    talent_key: str
    gmail_message_id: str


@router.post("/process-email")
def process_single_email(body: ProcessEmailIn, db: Session = Depends(get_db)):
    """
    Process one email through triage + reply. Called by n8n Gmail trigger
    for near-real-time processing (replaces waiting for the 60s poll cycle).
    """
    from backend.services.poller import _already_processed, _process_one_message
    from backend.core.config import get_settings as _gs

    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(body.talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=404, detail=f"No active token for {body.talent_key}")

    if _already_processed(db, body.gmail_message_id):
        return {"ok": True, "status": "already_processed"}

    settings = _gs()
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent_cfg = talent_map.get(body.talent_key.lower(), {})
    draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)
    summary: dict = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    _process_one_message(
        db=db,
        token_row=token,
        message_id=body.gmail_message_id,
        talent_key=body.talent_key,
        talent_name=talent_cfg.get("full_name", body.talent_key),
        minimum_rate=talent_cfg.get("minimum_rate_usd", 0),
        draft_mode=draft_mode,
        summary=summary,
    )
    return {"ok": True, "summary": summary}


@router.post("/talents/{talent_key}/process-batch")
def process_batch(
    talent_key: str,
    background_tasks: BackgroundTasks,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    """
    Run the full triage + reply pipeline on up to `limit` cached inbox emails
    that haven't been processed yet. Runs in the background so the response
    returns immediately.
    """
    from backend.services.poller import _already_processed
    _validate_talent(talent_key)

    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    # Find cached inbox emails that have body text and haven't been processed
    candidates = (
        db.query(InboxEmail)
        .filter(
            InboxEmail.talent_key == talent_key.lower(),
            InboxEmail.body_text != None,  # noqa: E711
        )
        .order_by(InboxEmail.email_date.desc().nullslast())
        .limit(limit * 3)  # fetch extra so we can skip already-processed ones
        .all()
    )

    # Filter out already-processed
    unprocessed = [c for c in candidates if not _already_processed(db, c.gmail_message_id)]
    batch = unprocessed[:limit]

    if not batch:
        return {"ok": True, "message": "No unprocessed emails with body text found.", "queued": 0}

    msg_ids = [e.gmail_message_id for e in batch]
    background_tasks.add_task(_run_process_batch, talent_key, msg_ids)
    return {"ok": True, "message": f"Processing {len(batch)} emails in background.", "queued": len(batch)}


def _run_process_batch(talent_key: str, msg_ids: list):
    """Background task: run full triage + reply on a list of message IDs."""
    from backend.models.db import get_session_factory, TalentToken
    from backend.services.poller import _already_processed, _process_one_message
    from backend.core.config import get_settings as _gs

    SessionLocal = get_session_factory()
    _db = SessionLocal()
    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}
    try:
        token = _db.query(TalentToken).filter(
            TalentToken.talent_key.ilike(talent_key),
            TalentToken.active == True,  # noqa: E712
        ).first()
        if not token:
            logger.error("Batch: no token for %s", talent_key)
            return

        settings = _gs()
        talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
        talent_cfg = talent_map.get(talent_key.lower(), {})
        draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)

        for msg_id in msg_ids:
            if _already_processed(_db, msg_id):
                continue
            try:
                _process_one_message(
                    db=_db,
                    token_row=token,
                    message_id=msg_id,
                    talent_key=talent_key,
                    talent_name=talent_cfg.get("full_name", talent_key),
                    minimum_rate=talent_cfg.get("minimum_rate_usd", 0),
                    draft_mode=draft_mode,
                    summary=summary,
                )
            except Exception as exc:
                logger.warning("Batch error on %s / %s: %s", talent_key, msg_id, exc)
                summary["errors"] += 1

        logger.info("Batch complete for %s: %s", talent_key, summary)
    finally:
        _db.close()


# ── Live Gmail inbox ───────────────────────────────────────────────────────────

@router.get("/talents/{talent_key}/inbox/live")
def live_inbox(talent_key: str, db: Session = Depends(get_db)):
    """
    Return the talent's inbox from the server-side cache (1 DB query, instant).
    Falls back to live Gmail fetch if the cache hasn't been populated yet.
    """
    from backend.models.db import InboxEmail
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)

    # ── Primary: read from cache ──────────────────────────────────────────────
    cached = (
        db.query(InboxEmail)
        .filter(InboxEmail.talent_key == talent_key.lower())
        .order_by(InboxEmail.email_date.desc().nullslast())
        .limit(250)
        .all()
    )

    if cached:
        # If cache is stale (>2 min), kick off a background sync so next
        # refresh shows fresh data without blocking this response
        from datetime import timedelta
        from backend.models.db import InboxEmail as _IE
        most_recent_sync = max((r.last_synced_at for r in cached), default=None)
        if most_recent_sync and (datetime.utcnow() - most_recent_sync) > timedelta(minutes=2):
            token = (
                db.query(TalentToken)
                .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
                .first()
            )
            if token:
                import threading
                from backend.services.inbox_sync import sync_inbox_for_talent, fetch_pending_bodies
                from backend.models.db import get_session_factory
                def _bg_sync(tk=talent_key, tok_id=token.id):
                    _db = get_session_factory()()
                    try:
                        tok = _db.query(TalentToken).filter(TalentToken.id == tok_id).first()
                        if tok:
                            sync_inbox_for_talent(tok, _db)
                            fetch_pending_bodies(tok, _db)
                    except Exception as exc:
                        logger.warning("Background sync failed for %s: %s", tk, exc)
                    finally:
                        _db.close()
                threading.Thread(target=_bg_sync, daemon=True).start()
        return [
            {
                "id": None,
                "gmail_message_id": r.gmail_message_id,
                "thread_id": r.thread_id or "",
                "sender": r.sender or "",
                "subject": r.subject or "",
                "body_text": None,
                "snippet": r.snippet or "",
                "email_date": r.email_date.isoformat() if r.email_date else None,
                "processed_at": None,
                "score": r.score,
                "brand_name": r.brand_name,
                "proposed_rate": r.proposed_rate,
                "offer_type": r.offer_type,
                "triage_reason": r.triage_reason,
                "status": r.triage_status or "unprocessed",
                "is_unread": r.is_unread,
                "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            }
            for r in cached
        ]

    # ── Fallback: live Gmail fetch (cache not yet populated) ──────────────────
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    try:
        stubs = gmail_svc.list_inbox_messages(token, max_results=25)
    except Exception as e:
        logger.error(f"Live inbox fetch failed: {e}")
        # Return a custom error instead of throwing a generic 500, or just return empty cache?
        # Actually we want a specific error so frontend shows 'Gmail error, try again'
        raise HTTPException(status_code=503, detail="Gmail API is currently unavailable. Please try again in a few minutes.")
    if not stubs:
        return []

    msg_ids = [s["id"] for s in stubs]
    db_rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id.in_(msg_ids))
        .all()
    )
    db_map = {row.gmail_message_id: row for row in db_rows}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    headers_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        future_to_id = {pool.submit(gmail_svc.get_message_headers, token, s["id"]): s["id"] for s in stubs}
        for future in as_completed(future_to_id):
            mid = future_to_id[future]
            try:
                result = future.result()
                if result:
                    headers_map[mid] = result
            except Exception:
                pass

    results = []
    for stub in stubs:
        mid = stub["id"]
        headers = headers_map.get(mid)
        if not headers:
            continue
        db_row = db_map.get(mid)
        email_date = headers.get("email_date")
        results.append({
            "id": db_row.id if db_row else None,
            "gmail_message_id": mid,
            "thread_id": headers.get("thread_id", ""),
            "sender": headers.get("sender", ""),
            "subject": headers.get("subject", ""),
            "body_text": None,
            "snippet": "",
            "email_date": email_date.isoformat() if email_date else None,
            "processed_at": db_row.processed_at.isoformat() if db_row else None,
            "score": db_row.score if db_row else None,
            "brand_name": db_row.brand_name if db_row else None,
            "proposed_rate": db_row.proposed_rate if db_row else None,
            "offer_type": db_row.offer_type if db_row else None,
            "triage_reason": db_row.triage_reason if db_row else None,
            "status": db_row.status if db_row else "unprocessed",
            "is_unread": "UNREAD" in headers.get("label_ids", []),
            "last_synced_at": None,
        })

    return results


# ── Live Gmail drafts ──────────────────────────────────────────────────────────

@router.get("/talents/{talent_key}/drafts/live")
def live_drafts(talent_key: str, db: Session = Depends(get_db)):
    """
    Fetch the talent's real Gmail drafts folder, cross-referenced with our DB
    so the dashboard shows the actual draft text and the DB draft ID for approve/discard.
    """
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    try:
        gmail_drafts = gmail_svc.list_gmail_drafts(token, max_results=25)
    except Exception as e:
        logger.error(f"Live drafts fetch failed: {e}")
        raise HTTPException(status_code=503, detail="Gmail API is currently unavailable. Please try again in a few minutes.")
    db.add(token)
    db.commit()

    # Build lookup: gmail_draft_id → DB Draft row
    gmail_draft_ids = [d["gmail_draft_id"] for d in gmail_drafts]
    db_drafts = (
        db.query(Draft)
        .filter(Draft.gmail_draft_id.in_(gmail_draft_ids))
        .all()
    ) if gmail_draft_ids else []
    db_map = {row.gmail_draft_id: row for row in db_drafts}

    results = []
    for gd in gmail_drafts:
        db_row = db_map.get(gd["gmail_draft_id"])
        results.append({
            "gmail_draft_id": gd["gmail_draft_id"],
            "db_draft_id": db_row.id if db_row else None,
            "thread_id": gd["thread_id"],
            "to": gd["to"],
            "subject": gd["subject"],
            "body_text": db_row.draft_text if db_row else gd["body_text"],
            "snippet": gd["snippet"],
            "is_escalate": db_row.is_escalate if db_row else False,
            "escalate_reason": db_row.escalate_reason if db_row else None,
            "brand_name": db_row.brand_name if db_row else None,
            "proposed_rate": db_row.proposed_rate if db_row else None,
            "offer_type": db_row.offer_type if db_row else None,
            "status": db_row.status if db_row else "gmail_only",
            "sender": db_row.sender if db_row else None,
        })
    return results


# ── Archive email ─────────────────────────────────────────────────────────────

@router.post("/talents/{talent_key}/emails/{gmail_message_id}/archive")
def archive_email(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Archive a specific email in the talent's Gmail account and mark it in DB."""
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Talent Gmail not connected.")
    gmail_svc.archive_message(token, gmail_message_id)
    # Update status in ProcessedEmail if record exists
    row = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if row:
        from backend.models.db import EmailStatus
        row.status = EmailStatus.archived
    # Remove from inbox cache so it doesn't reappear
    from backend.models.db import InboxEmail
    db.query(InboxEmail).filter(
        InboxEmail.gmail_message_id == gmail_message_id,
        InboxEmail.talent_key == talent_key.lower(),
    ).delete()
    db.commit()
    return {"ok": True}


# ── Email body (live fetch from Gmail) ────────────────────────────────────────

@router.get("/talents/{talent_key}/emails/{gmail_message_id}/body")
def email_body(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Return email body — from cache if available, else live from Gmail."""
    from backend.models.db import InboxEmail
    from backend.services import gmail as gmail_svc

    # Check inbox cache first
    cached = db.query(InboxEmail).filter(
        InboxEmail.gmail_message_id == gmail_message_id,
        InboxEmail.talent_key == talent_key.lower(),
    ).first()
    if cached and cached.body_text:
        return {"body": cached.body_text}

    # Check ProcessedEmail (triage also stores body_text)
    processed = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if processed and processed.body_text:
        return {"body": processed.body_text}

    # Fall back to live Gmail fetch
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Talent Gmail not connected.")
    detail = gmail_svc.get_message_detail(token, gmail_message_id)
    body = detail.get("body_text", "") or "" if detail else ""

    # Opportunistically populate the cache
    if cached and not cached.body_text:
        cached.body_text = body
        cached.body_fetched_at = datetime.utcnow()
        db.commit()

    return {"body": body}


# ── 30-day backfill ───────────────────────────────────────────────────────────

def _run_backfill(talent_key: str, days: int):
    """Background task: read all Gmail messages from the last N days and store them."""
    from backend.models.db import get_session_factory, ProcessedEmail, TalentToken, EmailStatus
    from backend.services import gmail as gmail_svc
    from datetime import datetime

    SessionLocal = get_session_factory()
    db = SessionLocal()
    stored = skipped = errors = 0
    try:
        token = db.query(TalentToken).filter(
            TalentToken.talent_key.ilike(talent_key),
            TalentToken.active == True,  # noqa: E712
        ).first()
        if not token:
            logger.error("Backfill: no connected token for %s", talent_key)
            return

        logger.info("Backfill started for %s — fetching last %d days", talent_key, days)
        message_stubs = gmail_svc.list_all_messages_since(token, days_back=days)
        logger.info("Backfill: %d messages found for %s", len(message_stubs), talent_key)

        for stub in message_stubs:
            msg_id = stub["id"]
            exists = db.query(ProcessedEmail).filter(
                ProcessedEmail.gmail_message_id == msg_id
            ).first()
            if exists:
                skipped += 1
                continue
            try:
                detail = gmail_svc.get_message_detail(token, msg_id)
                if not detail:
                    errors += 1
                    continue
                row = ProcessedEmail(
                    talent_key=talent_key.lower(),
                    gmail_message_id=msg_id,
                    thread_id=detail.get("thread_id", ""),
                    sender=detail.get("sender", ""),
                    subject=detail.get("subject", ""),
                    score=None,
                    brand_name=None,
                    proposed_rate=None,
                    offer_type=None,
                    triage_reason=None,
                    body_text=detail.get("body_text", "") or None,
                    email_date=detail.get("email_date"),
                    processed_at=datetime.utcnow(),
                    status=EmailStatus.flagged,
                )
                db.add(row)
                db.commit()
                stored += 1
                if stored % 50 == 0:
                    logger.info("Backfill %s: %d stored so far", talent_key, stored)
            except Exception as exc:
                logger.warning("Backfill error on msg %s: %s", msg_id, exc)
                errors += 1
                db.rollback()

        logger.info("Backfill complete for %s: %d stored, %d skipped, %d errors",
                    talent_key, stored, skipped, errors)
    finally:
        db.close()


@router.post("/backfill-all")
def start_backfill_all(
    background_tasks: BackgroundTasks,
    days: int = 30,
    db: Session = Depends(get_db),
):
    """Start a background 30-day backfill for ALL connected talents at once."""
    tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    if not tokens:
        raise HTTPException(status_code=400, detail="No connected Gmail accounts found.")
    for token in tokens:
        background_tasks.add_task(_run_backfill, token.talent_key, days)
    keys = [t.talent_key for t in tokens]
    logger.info("Backfill-all triggered for %s talents: %s", len(keys), keys)
    return {"ok": True, "talents": keys, "message": f"Backfill started for {len(keys)} talent(s) — last {days} days"}


@router.post("/talents/{talent_key}/backfill")
def start_backfill(
    talent_key: str,
    background_tasks: BackgroundTasks,
    days: int = 30,
    db: Session = Depends(get_db),
):
    """Start a background backfill of the last N days of Gmail history."""
    _validate_talent(talent_key)
    token = db.query(TalentToken).filter(
        TalentToken.talent_key == talent_key.lower(),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")
    background_tasks.add_task(_run_backfill, talent_key, days)
    return {"ok": True, "message": f"Backfill started — fetching last {days} days for {talent_key}"}


def _run_triage_unscored(talent_key: str, batch_size: int = 20):
    """Background job: triage InboxEmail rows that have no score yet, in batches."""
    from backend.models.db import get_session_factory
    from backend.services import triage as triage_svc
    from backend.core.config import get_settings as _gs

    settings = _gs()
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent_cfg = talent_map.get(talent_key.lower(), {})
    talent_name = talent_cfg.get("full_name", talent_key)
    minimum_rate = talent_cfg.get("minimum_rate_usd", 0)

    _db = get_session_factory()()
    total = 0
    try:
        while True:
            rows = (
                _db.query(InboxEmail)
                .filter(
                    InboxEmail.talent_key == talent_key.lower(),
                    InboxEmail.score == None,  # noqa: E711
                    InboxEmail.body_text != None,  # noqa: E711
                )
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            for row in rows:
                try:
                    result = triage_svc.triage_email(
                        talent_key=talent_key,
                        talent_name=talent_name,
                        minimum_rate=minimum_rate,
                        subject=row.subject or "",
                        sender=row.sender or "",
                        sender_domain=(row.sender or "").split("@")[-1].split(">")[0] if "@" in (row.sender or "") else "",
                        body=row.body_text or "",
                    )
                    row.score = result["score"]
                    row.brand_name = result.get("brand_name")
                    row.proposed_rate = result.get("proposed_rate_usd")
                    row.offer_type = result.get("offer_type")
                    row.triage_reason = result.get("reason")
                    row.triage_status = "triaged"
                    _db.add(row)
                    total += 1
                except Exception as exc:
                    logger.warning("Triage failed for %s / %s: %s", talent_key, row.gmail_message_id, exc)
            _db.commit()
    except Exception as exc:
        logger.error("Triage-unscored job failed for %s: %s", talent_key, exc)
    finally:
        _db.close()
    logger.info("Triage-unscored complete for %s: %d emails scored", talent_key, total)


@router.post("/talents/{talent_key}/triage-unscored")
def triage_unscored(
    talent_key: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Triage all InboxEmail rows that have body text but no score yet. Runs in background."""
    unscored_count = (
        db.query(InboxEmail)
        .filter(
            InboxEmail.talent_key == talent_key.lower(),
            InboxEmail.score == None,  # noqa: E711
            InboxEmail.body_text != None,  # noqa: E711
        )
        .count()
    )
    if unscored_count == 0:
        return {"ok": True, "message": "No unscored emails with body text found."}
    background_tasks.add_task(_run_triage_unscored, talent_key)
    return {"ok": True, "message": f"Triaging {unscored_count} unscored emails for {talent_key} in background."}


@router.post("/triage-all-unscored")
def triage_all_unscored(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Triage unscored emails for ALL connected talents."""
    tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    keys = [t.talent_key for t in tokens]
    for key in keys:
        background_tasks.add_task(_run_triage_unscored, key)
    return {"ok": True, "talents": keys, "message": f"Triage started for {len(keys)} talent(s)"}
