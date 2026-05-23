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
    AppState,
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
from backend.services.talent_access import ensure_talent_gmail_enabled, is_talent_paused

router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(verify_api_key)],
)
logger = logging.getLogger(__name__)

_DASHBOARD_RESET_KEY = "dashboard_reset_started_at"


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class TalentReportCard(BaseModel):
    talent_key: str
    full_name: str
    manager: Optional[str] = None
    count_good: int
    count_uncertain: int
    count_trash: int
    count_sent: int          # sent today
    count_drafts: int        # all-time pending (backlog) — used for sidebar badge
    new_drafts_today: int    # pending drafts created today — used for sidebar badge
    count_ignore: int        # score=1 today
    total: int
    best_deal_brand: Optional[str] = None
    best_deal_rate: Optional[float] = None
    pending_drafts: int
    pending_real_drafts: int


class DailyReportOut(BaseModel):
    report_date: str
    total_good: int
    total_uncertain: int
    total_trash: int
    total_emails: int
    total_sent: int
    total_draft_backlog: int
    total_new_drafts_today: int
    total_ignore: int
    total_deal_value_today: float  # sum of proposed_rate for Score-3 emails today
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


def _get_reset_at(db: Session, talent_key: str | None = None) -> datetime | None:
    """Return the stored reset baseline. Per-talent key takes priority over global."""
    keys_to_try = []
    if talent_key:
        keys_to_try.append(f"reset_at_{talent_key.lower()}")
    keys_to_try.append(_DASHBOARD_RESET_KEY)
    for k in keys_to_try:
        row = db.query(AppState).filter(AppState.key == k).first()
        if row and row.value_text:
            try:
                return datetime.fromisoformat(row.value_text)
            except ValueError:
                pass
    return None


def _set_reset_at(db: Session, when: datetime, talent_key: str | None = None) -> None:
    key = f"reset_at_{talent_key.lower()}" if talent_key else _DASHBOARD_RESET_KEY
    row = db.query(AppState).filter(AppState.key == key).first()
    if not row:
        row = AppState(key=key)
    row.value_text = when.isoformat()
    db.add(row)


# Keep old names as aliases so existing callers don't break
def _get_dashboard_reset_at(db: Session) -> datetime | None:
    return _get_reset_at(db)


def _set_dashboard_reset_at(db: Session, when: datetime) -> None:
    _set_reset_at(db, when)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/report", response_model=DailyReportOut)
def daily_report(db: Session = Depends(get_db)):
    """Report — counts since last manual reset. No automatic daily reset."""
    settings = get_settings()
    talent_configs = settings.app_config.get("talents", [])

    today_utc = datetime.utcnow().date()
    # Fallback window: 30 days back if no reset has ever been set
    _fallback_start = datetime.utcnow() - timedelta(days=30)

    def _safe_lkey(value: str | None) -> str | None:
        """Normalize a potential talent key to lowercase; return None for empty/invalid values."""
        if not isinstance(value, str):
            return None
        v = value.strip().lower()
        return v or None

    talent_keys_lower = [
        t["key"].lower()
        for t in talent_configs
        if isinstance(t.get("key"), str) and t.get("key").strip()
    ]
    reset_rows = db.query(AppState).filter(
        AppState.key.in_(
            [_DASHBOARD_RESET_KEY] + [f"reset_at_{k}" for k in talent_keys_lower]
        )
    ).all()
    reset_map: dict[str, datetime] = {}
    global_reset: datetime | None = None
    for row in reset_rows:
        if not row.value_text:
            continue
        try:
            ts = datetime.fromisoformat(row.value_text)
        except ValueError:
            continue
        if row.key == _DASHBOARD_RESET_KEY:
            global_reset = ts
        else:
            k = row.key.replace("reset_at_", "")
            reset_map[k] = ts

    def _window_for(talent_key: str) -> datetime:
        """Latest manual reset for this talent, global reset, or 30-day fallback."""
        candidates = []
        if global_reset:
            candidates.append(global_reset)
        if talent_key in reset_map:
            candidates.append(reset_map[talent_key])
        return max(candidates) if candidates else _fallback_start

    # Load all processed emails since the earliest window we need
    earliest = min(_window_for(k) for k in talent_keys_lower) if talent_keys_lower else _fallback_start
    all_emails = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= earliest)
        .all()
    )
    emails_by_talent: dict[str, list] = defaultdict(list)
    for e in all_emails:
        lkey = _safe_lkey(getattr(e, "talent_key", None))
        if not lkey or not getattr(e, "processed_at", None):
            continue
        emails_by_talent[lkey].append(e)

    # Sent drafts per talent (status='sent') — since today or reset
    sent_drafts = db.query(Draft).filter(
        Draft.status == DraftStatus.sent,
        Draft.reviewed_at >= earliest,
    ).all()
    sent_by_talent: dict[str, int] = defaultdict(int)
    for d in sent_drafts:
        lkey = _safe_lkey(getattr(d, "talent_key", None))
        if not lkey:
            continue
        sent_by_talent[lkey] += 1

    # Draft backlog — all pending non-escalation drafts regardless of date
    all_pending = db.query(Draft).filter(
        Draft.status == DraftStatus.pending,
        Draft.is_escalate == False,  # noqa: E712
    ).all()
    backlog_by_talent: dict[str, int] = defaultdict(int)
    for d in all_pending:
        lkey = _safe_lkey(getattr(d, "talent_key", None))
        if not lkey:
            continue
        backlog_by_talent[lkey] += 1

    # New drafts today — pending drafts created since each talent's window start
    all_pending_with_date = db.query(Draft).filter(
        Draft.status == DraftStatus.pending,
        Draft.is_escalate == False,  # noqa: E712
        Draft.created_at >= earliest,
    ).all()
    new_today_by_talent: dict[str, list] = defaultdict(list)
    for d in all_pending_with_date:
        lkey = _safe_lkey(getattr(d, "talent_key", None))
        if not lkey:
            continue
        new_today_by_talent[lkey].append(d)

    total_good = total_uncertain = total_trash = 0
    total_sent = total_draft_backlog = total_new_drafts_today = total_ignore = 0
    total_deal_value_today: float = 0.0
    cards: list[TalentReportCard] = []

    for t_cfg in talent_configs:
        key = t_cfg["key"]
        lkey = key.lower()
        window = _window_for(lkey)
        emails = [
            e for e in emails_by_talent.get(lkey, [])
            if getattr(e, "processed_at", None) and e.processed_at >= window
        ]

        count_good = sum(1 for e in emails if e.score == 3)
        count_uncertain = sum(1 for e in emails if e.score == 2)
        count_trash = sum(1 for e in emails if e.score == 1)
        count_sent = sent_by_talent.get(lkey, 0)
        count_backlog = backlog_by_talent.get(lkey, 0)
        count_new_today = sum(
            1 for d in new_today_by_talent.get(lkey, [])
            if getattr(d, "created_at", None) and d.created_at >= window
        )
        count_ignore = count_trash

        total_good += count_good
        total_uncertain += count_uncertain
        total_trash += count_trash
        total_sent += count_sent
        total_draft_backlog += count_backlog
        total_new_drafts_today += count_new_today
        total_ignore += count_ignore
        total_deal_value_today += sum(
            (e.proposed_rate or 0) for e in emails if e.score == 3 and e.proposed_rate
        )

        good_with_rate = [e for e in emails if e.score == 3 and e.proposed_rate]
        best = max(good_with_rate, key=lambda e: e.proposed_rate, default=None)

        cards.append(TalentReportCard(
            talent_key=key,
            full_name=t_cfg.get("full_name", key),
            manager=t_cfg.get("manager"),
            count_good=count_good,
            count_uncertain=count_uncertain,
            count_trash=count_trash,
            count_sent=count_sent,
            count_drafts=count_backlog,
            new_drafts_today=count_new_today,
            count_ignore=count_ignore,
            total=len(emails),
            best_deal_brand=best.brand_name if best else None,
            best_deal_rate=best.proposed_rate if best else None,
            pending_drafts=count_backlog,
            pending_real_drafts=count_new_today,  # sidebar badge = new today
        ))

    return DailyReportOut(
        report_date=today_utc.isoformat(),
        total_good=total_good,
        total_uncertain=total_uncertain,
        total_trash=total_trash,
        total_emails=total_good + total_uncertain + total_trash,
        total_sent=total_sent,
        total_draft_backlog=total_draft_backlog,
        total_new_drafts_today=total_new_drafts_today,
        total_ignore=total_ignore,
        total_deal_value_today=round(total_deal_value_today, 2),
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
        .filter(ProcessedEmail.talent_key.ilike(talent_key))
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
        .filter(Draft.talent_key.ilike(talent_key), Draft.status == DraftStatus.pending)
        .order_by(Draft.created_at.desc())
        .all()
    )

@router.post("/reset-badges")
def reset_all_badges(db: Session = Depends(get_db)):
    """Zero out all badge counts from now — no drafts touched, no data deleted."""
    now = datetime.utcnow()
    _set_reset_at(db, now)
    db.commit()
    logger.info("Global badge reset at %s", now.isoformat())
    return {"ok": True, "reset_at": now.isoformat()}


@router.post("/talents/{talent_key}/reset-badges")
def reset_talent_badges(talent_key: str, db: Session = Depends(get_db)):
    """Zero out badge counts for a single talent — no drafts touched, no data deleted."""
    _validate_talent(talent_key)
    now = datetime.utcnow()
    _set_reset_at(db, now, talent_key=talent_key)
    db.commit()
    logger.info("Badge reset for %s at %s", talent_key, now.isoformat())
    return {"ok": True, "talent_key": talent_key, "reset_at": now.isoformat()}


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


@router.post("/recover-fallbacks")
def recover_fallbacks(
    background_tasks: BackgroundTasks,
    days: int = 7,
    db: Session = Depends(get_db),
):
    """
    Find every email that silently fell back to Score 2 due to a GPT/JSON error,
    delete those ProcessedEmail records, and re-queue all connected talents for
    fresh triage. This is the recovery path after a silent failure period.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Count and delete all fallback-scored ProcessedEmail rows
    deleted = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.processed_at >= cutoff,
            ProcessedEmail.triage_reason.like("Triage fallback%"),
        )
        .delete(synchronize_session=False)
    )

    # Also delete score=2 rows where reason indicates a known bad classification
    # (non-JSON, truncated, schema mismatch) to be safe
    deleted_truncated = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.processed_at >= cutoff,
            ProcessedEmail.triage_reason.like("%truncated%"),
        )
        .delete(synchronize_session=False)
    )
    deleted += deleted_truncated

    db.commit()
    logger.info("Recovery: deleted %d fallback ProcessedEmail records", deleted)

    # Re-queue triage for every connected active talent
    tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    queued = [t.talent_key for t in tokens]
    for key in queued:
        background_tasks.add_task(_run_triage_unscored, key)

    return {
        "ok": True,
        "deleted_fallback_records": deleted,
        "queued_talents": queued,
        "message": (
            f"Cleared {deleted} failed triage records and re-queued {len(queued)} talent(s). "
            "Fresh drafts will appear in Gmail within ~2 minutes."
        ),
    }


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
        .filter(PollHealth.talent_key.ilike(talent_key))
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
    """Today's stats across all talents: emails, drafts, escalations, errors, fallback rate."""
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    emails_today = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= today).count()
    drafts_today = db.query(Draft).filter(Draft.created_at >= today).count()
    escalations_today = db.query(Draft).filter(Draft.created_at >= today, Draft.is_escalate == True).count()  # noqa: E712
    errors_today = db.query(PollHealth).filter(PollHealth.polled_at >= today, PollHealth.error_message != None).count()  # noqa: E711
    pending_drafts = db.query(Draft).filter(Draft.status == DraftStatus.pending).count()
    # Fallback count: real GPT failures only — excludes manual admin resets (SOP-pending re-queue)
    fallbacks_today = db.query(ProcessedEmail).filter(
        ProcessedEmail.processed_at >= today,
        ProcessedEmail.triage_reason.like("Triage fallback%"),
        ProcessedEmail.triage_reason.notlike("%SOP pending%"),
    ).count()
    score2_today = db.query(ProcessedEmail).filter(
        ProcessedEmail.processed_at >= today,
        ProcessedEmail.score == 2,
    ).count()
    return {
        "emails_today": emails_today,
        "drafts_today": drafts_today,
        "escalations_today": escalations_today,
        "errors_today": errors_today,
        "pending_drafts": pending_drafts,
        "triage_fallbacks_today": fallbacks_today,
        "score2_today": score2_today,
        "fallback_rate": round(fallbacks_today / max(emails_today, 1), 3),
    }


@router.get("/health/score")
def system_health_score(db: Session = Depends(get_db)):
    """System health score (0.0–1.0) with component breakdown and active issues."""
    from backend.services.health import compute_health_score
    return compute_health_score(db)


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
        .filter(ProcessedEmail.talent_key.ilike(talent_key), ProcessedEmail.status == EmailStatus.sent)
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
    ensure_talent_gmail_enabled(body.talent_key)
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
    ensure_talent_gmail_enabled(talent_key)

    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    # Pull all cached inbox emails and find ones not yet in ProcessedEmail
    # Fetch up to 500 so we can skip already-processed ones and still fill the batch
    from sqlalchemy import select
    processed_ids = {
        row[0] for row in db.execute(
            select(ProcessedEmail.gmail_message_id).where(
                ProcessedEmail.talent_key.ilike(talent_key)
            )
        ).fetchall()
    }

    candidates = (
        db.query(InboxEmail)
        .filter(InboxEmail.talent_key == talent_key.lower())
        .order_by(InboxEmail.email_date.desc().nullslast())
        .limit(500)
        .all()
    )

    batch = [c for c in candidates if c.gmail_message_id not in processed_ids][:limit]

    if not batch:
        return {"ok": True, "message": f"All {len(candidates)} cached emails already processed.", "queued": 0}

    msg_ids = [e.gmail_message_id for e in batch]
    background_tasks.add_task(_run_process_batch, talent_key, msg_ids)
    return {"ok": True, "message": f"Processing {len(batch)} emails in background.", "queued": len(batch)}


@router.post("/talents/{talent_key}/force-blast")
def force_blast(
    talent_key: str,
    background_tasks: BackgroundTasks,
    limit: int = 1000,
    db: Session = Depends(get_db),
):
    """
    Fetch up to `limit` inbox messages from Gmail directly, then run full
    triage + reply on every one that doesn't already have a draft.
    Ignores 'already processed' status — treats everything as fresh.
    Returns immediately; all work happens in the background.
    """
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)
    ensure_talent_gmail_enabled(talent_key)

    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    # Fetch up to limit message stubs directly from Gmail (bypasses inbox cache)
    stubs = gmail_svc.list_inbox_messages(token, max_results=limit, db=db)
    if not stubs:
        return {"ok": True, "message": "No inbox messages found.", "queued": 0}

    # Skip any that already have a draft — don't regenerate what's already done
    drafted_ids = {d.gmail_message_id for d in db.query(Draft.gmail_message_id).filter(
        Draft.talent_key.ilike(talent_key)
    ).all()}

    msg_ids = [s["id"] for s in stubs if s["id"] not in drafted_ids]

    logger.info("Force blast for %s: %d total stubs, %d already drafted, %d to process",
                talent_key, len(stubs), len(stubs) - len(msg_ids), len(msg_ids))

    background_tasks.add_task(_run_force_blast, talent_key, msg_ids)
    return {
        "ok": True,
        "message": f"Blast queued: {len(msg_ids)} emails to process ({len(stubs) - len(msg_ids)} already drafted).",
        "queued": len(msg_ids),
        "already_drafted": len(stubs) - len(msg_ids),
    }


def _run_force_blast(talent_key: str, msg_ids: list):
    """
    Background worker for force-blast. Processes every message ID with full
    triage + reply, 15 workers in parallel, logging progress every 50 emails.
    """
    if is_talent_paused(talent_key):
        logger.info("Force blast skipped for %s — Gmail automation disabled", talent_key)
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.models.db import get_session_factory, Draft, TalentToken
    from backend.services.poller import _process_one_message
    from backend.core.config import get_settings as _gs

    settings = _gs()
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent_cfg = talent_map.get(talent_key.lower(), {})
    talent_name = talent_cfg.get("full_name", talent_key)
    minimum_rate = talent_cfg.get("minimum_rate_usd", 0)
    draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)

    SessionLocal = get_session_factory()
    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    def _process_one(msg_id: str):
        thread_db = SessionLocal()
        try:
            # Re-check — another worker may have drafted this while we were waiting
            already = thread_db.query(Draft).filter(Draft.gmail_message_id == msg_id).first()
            if already:
                return
            thread_token = thread_db.query(TalentToken).filter(
                TalentToken.talent_key.ilike(talent_key),
                TalentToken.active == True,  # noqa: E712
            ).first()
            if not thread_token:
                return
            _process_one_message(
                db=thread_db,
                token_row=thread_token,
                message_id=msg_id,
                talent_key=talent_key,
                talent_name=talent_name,
                minimum_rate=minimum_rate,
                draft_mode=draft_mode,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Force blast error on %s: %s", msg_id, exc)
            summary["errors"] += 1
        finally:
            thread_db.close()

    total = len(msg_ids)
    logger.info("Force blast START for %s: %d emails, 15 workers", talent_key, total)

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(_process_one, mid): mid for mid in msg_ids}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Force blast future error: %s", exc)
            if done % 50 == 0 or done == total:
                logger.info("Force blast %s: %d/%d done — %s", talent_key, done, total, summary)

    logger.info("Force blast COMPLETE for %s: %d/%d — %s", talent_key, done, total, summary)


def _run_process_batch(talent_key: str, msg_ids: list):
    """Background task: run full triage + reply on a list of message IDs."""
    if is_talent_paused(talent_key):
        logger.info("Batch processing skipped for %s — Gmail automation disabled", talent_key)
        return
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

        # Each thread gets its own DB session AND summary — SQLAlchemy sessions are not
        # thread-safe, and sharing a mutable dict across threads causes race conditions.
        def _process_in_thread(msg_id: str) -> dict:
            thread_db = SessionLocal()
            thread_summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}
            try:
                thread_token = thread_db.query(TalentToken).filter(
                    TalentToken.talent_key.ilike(talent_key),
                    TalentToken.active == True,  # noqa: E712
                ).first()
                if not thread_token:
                    return thread_summary
                _process_one_message(
                    db=thread_db,
                    token_row=thread_token,
                    message_id=msg_id,
                    talent_key=talent_key,
                    talent_name=talent_cfg.get("full_name", talent_key),
                    minimum_rate=talent_cfg.get("minimum_rate_usd", 0),
                    draft_mode=draft_mode,
                    summary=thread_summary,
                )
            finally:
                thread_db.close()
            return thread_summary

        from concurrent.futures import ThreadPoolExecutor
        unqueued = [m for m in msg_ids if not _already_processed(_db, m)]
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(_process_in_thread, mid) for mid in unqueued]
            for f in futures:
                try:
                    result = f.result()
                    for k, v in result.items():
                        summary[k] += v
                except Exception as exc:
                    logger.warning("Batch error on %s: %s", talent_key, exc)
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
    ensure_talent_gmail_enabled(talent_key)

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
    with ThreadPoolExecutor(max_workers=15) as pool:
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
    ensure_talent_gmail_enabled(talent_key)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    try:
        gmail_drafts = gmail_svc.list_gmail_drafts(token, max_results=25, db=db)
    except Exception as e:
        logger.error(f"Live drafts fetch failed: {e}")
        raise HTTPException(status_code=503, detail="Gmail API is currently unavailable. Please try again in a few minutes.")
    # token refresh already persisted by _gmail_service inside list_gmail_drafts

    # Build lookup: gmail_draft_id → DB Draft row
    gmail_draft_ids = [d["gmail_draft_id"] for d in gmail_drafts]
    db_drafts = (
        db.query(Draft)
        .filter(Draft.gmail_draft_id.in_(gmail_draft_ids))
        .all()
    ) if gmail_draft_ids else []
    db_map = {row.gmail_draft_id: row for row in db_drafts}
    message_ids = [row.gmail_message_id for row in db_drafts if row.gmail_message_id]
    processed_rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id.in_(message_ids))
        .all()
    ) if message_ids else []
    processed_map = {row.gmail_message_id: row for row in processed_rows}

    results = []
    for gd in gmail_drafts:
        db_row = db_map.get(gd["gmail_draft_id"])
        processed_row = processed_map.get(db_row.gmail_message_id) if db_row and db_row.gmail_message_id else None
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
            "triage_reason": processed_row.triage_reason if processed_row else None,
            "status": db_row.status if db_row else "gmail_only",
            "sender": db_row.sender if db_row else None,
        })
    return results


# ── Archive email ─────────────────────────────────────────────────────────────

@router.post("/talents/{talent_key}/force-draft/{gmail_message_id}")
def force_draft_email(
    talent_key: str,
    gmail_message_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Manual override: force GPT to write a draft for any email regardless of its score.
    Used when the team spots a missed opportunity in the No Draft or trash view.
    """
    _validate_talent(talent_key)
    ensure_talent_gmail_enabled(talent_key)
    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key), TalentToken.active == True  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")
    background_tasks.add_task(_run_force_blast, talent_key, [gmail_message_id])
    return {"ok": True, "queued": gmail_message_id}


@router.post("/talents/{talent_key}/emails/{gmail_message_id}/archive")
def archive_email(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Archive a specific email in the talent's Gmail account and mark it in DB."""
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)
    ensure_talent_gmail_enabled(talent_key)
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
    ensure_talent_gmail_enabled(talent_key)

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
    if is_talent_paused(talent_key):
        logger.info("Backfill skipped for %s — Gmail automation disabled", talent_key)
        return
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
    ensure_talent_gmail_enabled(talent_key)
    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")
    background_tasks.add_task(_run_backfill, talent_key, days)
    return {"ok": True, "message": f"Backfill started — fetching last {days} days for {talent_key}"}


def _run_triage_unscored(talent_key: str, batch_size: int = 20):
    """Background job: sync inbox then triage all untriaged and undrafted emails in batches.

    Steps:
      1. Sync the Gmail inbox → inbox_emails cache (picks up read *and* unread messages).
      2. Fetch body text for any cached emails that don't have it yet.
      3. Delete ProcessedEmail stubs with score=NULL (created by backfill, never actually
         triaged) so they are reprocessed properly here.
      4. Iterate inbox_emails in batches, skipping emails that:
           - already have a ProcessedEmail record (already triaged as TRASH or DRAFT), OR
           - already have a pending Draft record (draft exists even if ProcessedEmail is missing)
         Run full triage + reply on everything else.

    Each email runs in its own thread with its own DB session — mirrors _run_process_batch.
    """
    if is_talent_paused(talent_key):
        logger.info("Triage-unscored skipped for %s — Gmail automation disabled", talent_key)
        return
    from concurrent.futures import ThreadPoolExecutor

    from backend.core.config import get_settings as _gs
    from backend.models.db import Draft, DraftStatus, ProcessedEmail, get_session_factory
    from backend.services.inbox_sync import fetch_pending_bodies, sync_inbox_for_talent
    from backend.services.poller import _already_processed, _process_one_message

    settings = _gs()
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent_cfg = talent_map.get(talent_key.lower(), {})
    talent_name = talent_cfg.get("full_name", talent_key)
    minimum_rate = talent_cfg.get("minimum_rate_usd", 0)

    SessionLocal = get_session_factory()
    _db = SessionLocal()
    total = 0
    try:
        # ── Step 1: Resolve Gmail token ───────────────────────────────────────
        # ilike so we match regardless of how the talent_key was stored (e.g. "Katrina" vs "katrina")
        token = _db.query(TalentToken).filter(
            TalentToken.talent_key.ilike(talent_key),
            TalentToken.active == True,  # noqa: E712
        ).first()
        if not token:
            logger.error("Triage-unscored: no active token for %s", talent_key)
            return

        # ── Step 2: Sync inbox cache (all inbox messages, read + unread) ──────
        try:
            sync_inbox_for_talent(token, _db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Triage-unscored: inbox sync failed for %s (continuing): %s", talent_key, exc)

        # ── Step 3: Fetch body text for any emails that don't have it yet ─────
        try:
            fetched = fetch_pending_bodies(token, _db, limit=200)
            if fetched:
                logger.info("Triage-unscored: fetched %d email bodies for %s", fetched, talent_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Triage-unscored: body fetch failed for %s (continuing): %s", talent_key, exc)

        # ── Step 4: Reset ProcessedEmail stubs with score=NULL ─────────────────
        # Backfill creates rows with score=NULL/status=flagged to log history, but
        # GPT never actually ran. Deleting them lets this job re-triage them properly.
        stubs_deleted = _db.query(ProcessedEmail).filter(
            ProcessedEmail.talent_key.ilike(talent_key),
            ProcessedEmail.score == None,  # noqa: E711
        ).delete(synchronize_session=False)
        if stubs_deleted:
            _db.commit()
            logger.info(
                "Triage-unscored: cleared %d unscored ProcessedEmail stubs for %s — will re-triage",
                stubs_deleted, talent_key,
            )

        # ── Step 5: Process emails not yet triaged and not already drafted ──
        while True:
            # Skip emails that:
            #   (a) already have a ProcessedEmail record (TRASH archived or DRAFT created), OR
            #   (b) already have a pending Draft record (safety net if ProcessedEmail is missing)
            rows = (
                _db.query(InboxEmail)
                .outerjoin(
                    ProcessedEmail,
                    (ProcessedEmail.gmail_message_id == InboxEmail.gmail_message_id)
                    & (func.lower(ProcessedEmail.talent_key) == talent_key.lower()),
                )
                .outerjoin(
                    Draft,
                    (Draft.gmail_message_id == InboxEmail.gmail_message_id)
                    & (func.lower(Draft.talent_key) == talent_key.lower())
                    & (Draft.status == DraftStatus.pending),
                )
                .filter(
                    InboxEmail.talent_key.ilike(talent_key),
                    ProcessedEmail.id == None,  # noqa: E711 — not yet triaged
                    Draft.id == None,            # not already drafted
                )
                .limit(batch_size)
                .all()
            )
            if not rows:
                break

            summary: dict[str, int] = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

            # Give each thread its own session + summary dict — sessions are not thread-safe
            # and sharing a mutable dict across threads causes race conditions.
            def _process_in_thread(msg_id: str) -> dict:
                thread_db = SessionLocal()
                thread_summary: dict[str, int] = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}
                try:
                    thread_token = thread_db.query(TalentToken).filter(
                        TalentToken.talent_key.ilike(talent_key),
                        TalentToken.active == True,  # noqa: E712
                    ).first()
                    if not thread_token:
                        return thread_summary
                    if _already_processed(thread_db, msg_id):
                        return thread_summary
                    _process_one_message(
                        db=thread_db,
                        token_row=thread_token,
                        message_id=msg_id,
                        talent_key=talent_key,
                        talent_name=talent_name,
                        minimum_rate=minimum_rate,
                        draft_mode=_gs().app_config.get("reply", {}).get("draft_mode", True),
                        summary=thread_summary,
                    )
                finally:
                    thread_db.close()
                return thread_summary

            with ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(_process_in_thread, r.gmail_message_id) for r in rows]
                for f in futures:
                    try:
                        result = f.result()
                        for k, v in result.items():
                            summary[k] += v
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Triage-unscored thread error for %s: %s", talent_key, exc)
                        summary["errors"] += 1

            total += summary.get("processed", 0)
            logger.info("Triage-unscored batch complete for %s: %s", talent_key, summary)
    except Exception as exc:  # noqa: BLE001
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
    """
    Sync inbox then triage all unscored emails for a talent. Runs fully in background.

    Called by the 'Run AI Draft' button in the dashboard. Always starts the background
    job as long as the talent has an active Gmail token — the job handles its own
    empty-inbox case gracefully.
    """
    _validate_talent(talent_key)
    ensure_talent_gmail_enabled(talent_key)
    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key),
        TalentToken.active == True,  # noqa: E712
    ).first()
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"Gmail not connected for {talent_key}. Connect their inbox first.",
        )
    background_tasks.add_task(_run_triage_unscored, talent_key)
    return {
        "ok": True,
        "message": (
            f"AI pipeline started for {talent_key} — syncing inbox and triaging emails. "
            "Drafts will appear in Gmail within ~30 seconds."
        ),
    }


@router.post("/triage-all-unscored")
def triage_all_unscored(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Triage unscored emails for ALL connected talents."""
    tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    keys = [t.talent_key for t in tokens]
    for key in keys:
        background_tasks.add_task(_run_triage_unscored, key)
    return {"ok": True, "talents": keys, "message": f"Triage started for {len(keys)} talent(s)"}


@router.get("/sop-html")
def get_sop_html():
    """Build SOP HTML from sop_data.json — no file-parsing dependencies needed."""
    import html as html_lib

    def esc(s: str) -> str:
        return html_lib.escape(str(s or ""))

    parts: list[str] = []

    # ── Global Rules ─────────────────────────────────────────────────────────
    parts.append("<h2>Global Rules — Mandatory</h2>")
    global_rules = [
        ("1. Follow the SOP explicitly",
         "Do not deviate from approved responses. Do not rewrite, improve, shorten, expand, or personalize approved responses unless specifically instructed by an admin."),
        ("2. Talent matching is mandatory",
         "Each talent has different rates, terms, and response language. Always identify the correct talent before selecting a response. Never use one talent's response for another talent."),
        ("3. Initial inbound emails only",
         "Draft responses only for first-time inbound emails or new deal inquiries. If the email is part of an ongoing thread, follow-up, or negotiation — do not draft. Return: Classification: Human Admin Required."),
        ("4. Default to the Initial Approved Response",
         "Each talent has a default Initial Approved Response. Use it for all valid inbound opportunities unless the email clearly matches a more specific scenario."),
        ("5. Err on the side of responding",
         "Only classify as Spam/Trash if the email is clearly and truly spam. If there is any reasonable chance it is a real brand, agency, PR, event, collab, gifting, or paid inquiry — use the Initial Approved Response. It is better to reply to a questionable email than to miss a real opportunity."),
        ("6. Spam handling must be conservative",
         "Do not classify as Spam because an email is vague, low-budget, poorly written, or from an unfamiliar sender. Non-English emails (e.g. Chinese market) are NOT spam. Classify Spam only for: phishing, scams, suspicious links, SEO/web/design pitches, fake invoices, malware, adult/illegal content. Known spam senders: Superordinary, Grail, Nextwave."),
        ("7. Output must clearly state the action",
         "Use exactly one of: Approved Response / Human Admin Required / Spam / Ignore"),
        ("8. Return approved responses verbatim",
         "Return the exact approved response only. Do not modify, combine, or add commentary."),
    ]
    for title, body in global_rules:
        parts.append(f"<p><strong>{esc(title)}</strong><br>{esc(body)}</p>")

    # ── SOP Status index ──────────────────────────────────────────────────────
    sop = get_settings().sop_data
    approved_names = [v.get("full_name", k) for k, v in sop.items() if v.get("sop_status") == "approved"]
    pending_names  = [v.get("full_name", k) for k, v in sop.items() if v.get("sop_status") != "approved"]

    parts.append("<h2>SOP Status</h2>")
    parts.append(f"<p><strong>✅ AI will draft ({len(approved_names)}):</strong> {esc(', '.join(approved_names))}</p>")
    parts.append(f"<p><strong>⏳ Pending — Human Admin Required ({len(pending_names)}):</strong> {esc(', '.join(pending_names))}</p>")

    # ── Per-talent approved SOPs ──────────────────────────────────────────────
    parts.append("<h2>Talent SOPs</h2>")
    for talent_key, talent_data in sop.items():
        status = talent_data.get("sop_status", "pending")
        full_name = talent_data.get("full_name", talent_key)
        manager = talent_data.get("manager", "")
        manager_email = talent_data.get("manager_email", "")
        mgr_str = f"{manager} ({manager_email})" if manager_email else manager
        rules = talent_data.get("rules", [])

        if status != "approved":
            parts.append(f"<h3>{esc(full_name)} <span style='color:#888;font-weight:400;font-size:12px;'>⏳ SOP Pending</span></h3>")
            continue

        parts.append(f"<h3>{esc(full_name)}</h3>")
        parts.append(f"<p style='color:#888;font-size:12px;margin-top:-8px;margin-bottom:12px;'>Manager: {esc(mgr_str)}</p>")

        for rule in rules:
            scenario = rule.get("scenario", "")
            label = rule.get("label", "")
            is_default = rule.get("is_default", False)
            default_tag = " &nbsp;<span style='background:#1a3a1a;color:#00d68f;font-size:10px;padding:2px 6px;border-radius:4px;'>DEFAULT</span>" if is_default else ""
            parts.append(f"<h4>Scenario {esc(scenario)}: {esc(label)}{default_tag}</h4>")

            use_when = rule.get("use_when", [])
            if use_when:
                parts.append(f"<p><strong>Use when:</strong> {esc(' · '.join(use_when))}</p>")

            do_not = rule.get("do_not_use_when", [])
            if do_not:
                parts.append(f"<p><strong>Do not use when:</strong> {esc(' · '.join(do_not))}</p>")

            cc = rule.get("cc")
            if cc:
                parts.append(f"<p><strong>CC:</strong> {esc(cc)}</p>")

            response = rule.get("response", "")
            response_html = esc(response).replace("\n", "<br>")
            parts.append(
                f"<div style='background:#0d1a0d;border:1px solid #1a3a1a;border-radius:8px;"
                f"padding:12px 14px;margin:8px 0 16px;font-size:12px;line-height:1.7;'>"
                f"{response_html}</div>"
            )

    return {"html": "\n".join(parts)}


@router.post("/reset-counters")
def reset_counters(db: Session = Depends(get_db)):
    """Set the dashboard baseline to now — badges reset to 0, old data preserved."""
    now = datetime.utcnow()
    _set_dashboard_reset_at(db, now)
    db.commit()
    return {"ok": True, "reset_at": now.isoformat(), "message": "Counters reset. Badges now count from this moment forward."}


# ── Retry triage fallbacks ─────────────────────────────────────────────────────

def _retry_one_fallback(processed_email_id: int) -> None:
    """
    Background task: re-triage one fallback email using its stored body_text.
    Updates the ProcessedEmail record in-place and creates a Draft if score=3.
    Does NOT create a Gmail draft (email is already marked read).
    """
    from backend.models.db import get_session_factory
    from backend.services import triage as triage_svc
    from backend.services import reply as reply_svc

    SessionLocal = get_session_factory()
    db = SessionLocal()
    logger_rt = logging.getLogger(__name__ + ".retry")
    try:
        row = db.query(ProcessedEmail).filter(ProcessedEmail.id == processed_email_id).first()
        if not row or not row.body_text:
            logger_rt.warning("Retry fallback %s: row missing or no body_text", processed_email_id)
            return

        settings = get_settings()
        talent_cfg = next(
            (t for t in settings.app_config.get("talents", [])
             if t.get("key", "").lower() == (row.talent_key or "").lower()),
            None,
        )
        if not talent_cfg:
            logger_rt.warning("Retry fallback %s: no talent config for %s", processed_email_id, row.talent_key)
            return

        sender_domain = row.sender.split("@")[-1] if row.sender and "@" in row.sender else ""

        result = triage_svc.triage_email(
            talent_key=row.talent_key,
            talent_name=talent_cfg.get("full_name", ""),
            minimum_rate=talent_cfg.get("minimum_rate_usd", 0),
            subject=row.subject or "",
            sender=row.sender or "",
            sender_domain=sender_domain,
            body=row.body_text,
        )

        new_score = result["score"]
        row.score = new_score
        row.triage_reason = result["reason"]
        row.offer_type = result.get("offer_type", "")
        row.proposed_rate = result.get("proposed_rate_usd", 0)
        row.brand_name = result.get("brand_name", "")

        if new_score == 1:
            row.status = EmailStatus.archived

        elif new_score == 3:
            # Skip if a draft already exists for this message
            existing = db.query(Draft).filter(Draft.gmail_message_id == row.gmail_message_id).first()
            if not existing:
                draft_result = reply_svc.draft_reply(
                    talent_key=row.talent_key,
                    talent_name=talent_cfg.get("full_name", ""),
                    minimum_rate=talent_cfg.get("minimum_rate_usd", 0),
                    subject=row.subject or "",
                    sender=row.sender or "",
                    offer_type=result.get("offer_type", ""),
                    brand_name=result.get("brand_name", ""),
                    proposed_rate=float(result.get("proposed_rate_usd") or 0),
                    triage_reason=result["reason"],
                    db=db,
                    body_text=row.body_text,
                )
                if not draft_result["is_escalate"]:
                    db.add(Draft(
                        talent_key=row.talent_key,
                        gmail_message_id=row.gmail_message_id,
                        thread_id=row.thread_id or "",
                        sender=row.sender or "",
                        subject=row.subject or "",
                        brand_name=result.get("brand_name", ""),
                        proposed_rate=float(result.get("proposed_rate_usd") or 0),
                        offer_type=result.get("offer_type", ""),
                        draft_text=draft_result["draft_text"],
                        status=DraftStatus.pending,
                        is_escalate=False,
                        escalate_reason=None,
                    ))
                    row.status = EmailStatus.draft_saved
            else:
                row.status = EmailStatus.draft_saved

        db.commit()
        logger_rt.info(
            "Retry fallback %s (%s / %s): score 2 → %s",
            processed_email_id, row.talent_key, row.gmail_message_id, new_score,
        )
    except Exception as exc:  # noqa: BLE001
        logger_rt.error("Retry fallback error for id=%s: %s", processed_email_id, exc)
        db.rollback()
    finally:
        db.close()


@router.post("/retry-fallbacks")
def retry_triage_fallbacks(
    background_tasks: BackgroundTasks,
    hours: int = 24,
    db: Session = Depends(get_db),
):
    """
    Re-triage all emails that failed with a triage fallback in the last N hours.
    Uses body_text stored in processed_emails — no Gmail API calls needed.
    Generates new drafts for any that score 3 on re-triage.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    fallbacks = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.processed_at >= cutoff,
            ProcessedEmail.triage_reason.like("Triage fallback%"),
            ProcessedEmail.body_text.isnot(None),
        )
        .all()
    )
    if not fallbacks:
        return {"queued": 0, "message": f"No triage fallbacks with body text found in last {hours}h"}

    for row in fallbacks:
        background_tasks.add_task(_retry_one_fallback, row.id)

    talent_counts: dict[str, int] = {}
    for row in fallbacks:
        talent_counts[row.talent_key] = talent_counts.get(row.talent_key, 0) + 1

    return {
        "queued": len(fallbacks),
        "hours_back": hours,
        "by_talent": talent_counts,
        "message": f"Re-triaging {len(fallbacks)} fallback emails in background.",
    }


@router.post("/talents/{talent_key}/repush-drafts")
def repush_drafts(
    talent_key: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Re-create Gmail drafts from DB draft rows whose gmail_draft_id is missing or was deleted.
    Safe to call multiple times — skips drafts that already have a valid gmail_draft_id.
    """
    from backend.services import gmail as gmail_svc

    token = db.query(TalentToken).filter(TalentToken.talent_key == talent_key).first()
    if not token:
        raise HTTPException(status_code=404, detail="Talent not found")

    pending = (
        db.query(Draft)
        .filter(Draft.talent_key == talent_key, Draft.status == "pending")
        .all()
    )

    pushed, skipped, errors = 0, 0, 0
    for draft in pending:
        # gmail_draft_id may reference a now-deleted draft — clear it so we repush
        draft.gmail_draft_id = None
        try:
            cc_list = gmail_svc.parse_cc_recipients(draft.cc_recipients) if draft.cc_recipients else None
            gmail_draft_id = gmail_svc.create_gmail_draft(
                token,
                thread_id=draft.thread_id or "",
                reply_to=draft.sender or "",
                subject=draft.subject or "",
                body=draft.draft_text,
                cc=cc_list or None,
                db=db,
                in_reply_to=draft.message_id_header or None,
            )
            if gmail_draft_id:
                draft.gmail_draft_id = gmail_draft_id
                pushed += 1
            else:
                errors += 1
        except Exception as exc:
            logger.error("repush failed for draft %s: %s", draft.id, exc)
            errors += 1

    db.commit()
    return {"pushed": pushed, "skipped": skipped, "errors": errors}


@router.post("/talents/{talent_key}/purge-duplicate-drafts")
def purge_duplicate_drafts(
    talent_key: str,
    keep: int = 1,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Delete all but `keep` Gmail drafts per unique thread/subject from the talent's Gmail account.
    Keeps the newest draft for each thread and deletes the rest.
    Use after a runaway poller loop creates thousands of duplicates.
    """
    from backend.services import gmail as gmail_svc

    token = db.query(TalentToken).filter(TalentToken.talent_key == talent_key).first()
    if not token:
        raise HTTPException(status_code=404, detail="Talent not found")

    service = gmail_svc.build_service(token, db)

    # Paginate through ALL drafts (Gmail API caps list at 500 per page)
    all_draft_stubs: list[dict] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().drafts().list(**kwargs).execute()
        stubs = result.get("drafts", [])
        all_draft_stubs.extend(stubs)
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if not all_draft_stubs:
        return {"deleted": 0, "message": "No drafts found"}

    # Fetch subject/threadId for each draft to group duplicates
    # Do this in batches to avoid rate limits
    from collections import defaultdict
    thread_to_drafts: dict[str, list[str]] = defaultdict(list)
    deleted = 0
    errors = 0

    for stub in all_draft_stubs:
        draft_id = stub.get("id")
        if not draft_id:
            continue
        try:
            # Use minimal=True equivalent via fields param — threadId is all we need
            full = service.users().drafts().get(
                userId="me", id=draft_id,
                fields="id,message/threadId",
            ).execute()
            thread_id = full.get("message", {}).get("threadId", draft_id)
            thread_to_drafts[thread_id].append(draft_id)
        except Exception:
            errors += 1

    for thread_id, draft_ids in thread_to_drafts.items():
        if len(draft_ids) <= keep:
            continue
        # Delete all but the last `keep` (assume list order = insertion order, keep last)
        to_delete = draft_ids if keep == 0 else draft_ids[:-keep]
        for draft_id in to_delete:
            try:
                service.users().drafts().delete(userId="me", id=draft_id).execute()
                deleted += 1
            except Exception:
                errors += 1

    return {
        "total_drafts_found": len(all_draft_stubs),
        "threads_with_duplicates": sum(1 for ids in thread_to_drafts.values() if len(ids) > keep),
        "deleted": deleted,
        "errors": errors,
        "message": f"Deleted {deleted} duplicate drafts, kept {keep} per thread.",
    }
