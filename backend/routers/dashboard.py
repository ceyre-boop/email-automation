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
    ManagerContext,
    ProcessedEmail,
    TalentToken,
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
        .limit(200)
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


# ── Live Gmail inbox ───────────────────────────────────────────────────────────

@router.get("/talents/{talent_key}/inbox/live")
def live_inbox(talent_key: str, db: Session = Depends(get_db)):
    """
    Fetch the talent's real Gmail inbox live, enriched with our triage scores.
    Returns the actual inbox so the dashboard always matches Gmail exactly.
    """
    from backend.services import gmail as gmail_svc
    _validate_talent(talent_key)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key == talent_key.lower(), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=400, detail="Gmail not connected for this talent.")

    stubs = gmail_svc.list_inbox_messages(token, max_results=50)
    if not stubs:
        return []

    # Bulk-fetch our triage records for these message IDs
    msg_ids = [s["id"] for s in stubs]
    db_rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id.in_(msg_ids))
        .all()
    )
    db_map = {row.gmail_message_id: row for row in db_rows}

    results = []
    for stub in stubs:
        mid = stub["id"]
        detail = gmail_svc.get_message_detail(token, mid)
        if not detail:
            continue
        db_row = db_map.get(mid)
        results.append({
            "id": db_row.id if db_row else None,
            "gmail_message_id": mid,
            "thread_id": detail.get("thread_id", ""),
            "sender": detail.get("sender", ""),
            "subject": detail.get("subject", ""),
            "body_text": detail.get("body_text", ""),
            "email_date": detail.get("email_date").isoformat() if detail.get("email_date") else None,
            "processed_at": db_row.processed_at.isoformat() if db_row else None,
            "score": db_row.score if db_row else None,
            "brand_name": db_row.brand_name if db_row else None,
            "proposed_rate": db_row.proposed_rate if db_row else None,
            "offer_type": db_row.offer_type if db_row else None,
            "triage_reason": db_row.triage_reason if db_row else None,
            "status": db_row.status if db_row else "unprocessed",
            "is_unread": "UNREAD" in detail.get("label_ids", []),
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
        .filter(TalentToken.talent_key == talent_key.lower(), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Talent Gmail not connected.")
    gmail_svc.archive_message(token, gmail_message_id)
    # Update status in DB if record exists
    row = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if row:
        from backend.models.db import EmailStatus
        row.status = EmailStatus.archived
        db.commit()
    return {"ok": True}


# ── Email body (live fetch from Gmail) ────────────────────────────────────────

@router.get("/talents/{talent_key}/emails/{gmail_message_id}/body")
def email_body(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Fetch the full email body live from Gmail for the reading pane."""
    from backend.services import gmail as gmail_svc
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key == talent_key.lower(), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Talent Gmail not connected.")
    detail = gmail_svc.get_message_detail(token, gmail_message_id)
    return {"body": detail.get("body_text", "") or ""}


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
            TalentToken.talent_key == talent_key.lower(),
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
