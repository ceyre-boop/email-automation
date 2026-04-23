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

from fastapi import APIRouter, Depends, HTTPException
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
    """Daily email processing report — counts and best deals per talent for today."""
    settings = get_settings()
    talent_configs = settings.app_config.get("talents", [])

    today_utc = datetime.utcnow().date()
    today_start = datetime.combine(today_utc, datetime.min.time())
    today_end = today_start + timedelta(days=1)

    rows = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.processed_at >= today_start,
            ProcessedEmail.processed_at < today_end,
        )
        .all()
    )

    talent_emails: dict[str, list] = defaultdict(list)
    for row in rows:
        talent_emails[row.talent_key].append(row)

    pending_query = (
        db.query(Draft.talent_key, func.count(Draft.id).label("cnt"))
        .filter(Draft.status == DraftStatus.pending)
        .group_by(Draft.talent_key)
        .all()
    )
    pending_map: dict[str, int] = {r.talent_key: r.cnt for r in pending_query}

    total_good = total_uncertain = total_trash = 0
    cards: list[TalentReportCard] = []

    for t_cfg in talent_configs:
        key = t_cfg["key"]
        emails = talent_emails.get(key, [])

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
            pending_drafts=pending_map.get(key, 0),
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
        row.talent_key: row
        for row in db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    }

    return [
        TalentOut(
            key=t["key"],
            full_name=t.get("full_name", t["key"]),
            manager=t.get("manager"),
            category=t.get("category"),
            minimum_rate_usd=t.get("minimum_rate_usd"),
            connected=t["key"] in connected,
            email=connected[t["key"]].email if t["key"] in connected else None,
            connected_at=connected[t["key"]].connected_at.isoformat() if t["key"] in connected else None,
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
        .filter(ProcessedEmail.talent_key == talent_key)
        .order_by(ProcessedEmail.processed_at.desc())
        .limit(50)
        .all()
    )


@router.get("/talents/{talent_key}/drafts", response_model=list[DraftOut])
def talent_drafts(talent_key: str, db: Session = Depends(get_db)):
    """Pending drafts for a talent, newest first."""
    _validate_talent(talent_key)
    return (
        db.query(Draft)
        .filter(Draft.talent_key == talent_key, Draft.status == DraftStatus.pending)
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
