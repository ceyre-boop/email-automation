"""
Draft review router — unified agency review queue.

GET  /api/drafts                    → list all pending drafts across all talents
POST /api/drafts/{id}/approve       → send the draft via Gmail + mark sent
POST /api/drafts/{id}/edit          → update draft text, keep pending
POST /api/drafts/{id}/discard       → mark discarded (deletes Gmail draft too)
GET  /api/drafts/{id}               → get single draft detail
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models.db import Draft, DraftEditLog, DraftStatus, ProcessedEmail, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services import gmail as gmail_svc
from backend.services.gmail import parse_cc_recipients
from backend.services.oauth import TokenRefreshError
from backend.services.talent_access import ensure_talent_gmail_enabled

router = APIRouter(prefix="/api/drafts", tags=["drafts"], dependencies=[Depends(verify_api_key)])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────


class DraftOut(BaseModel):
    id: int
    talent_key: str
    gmail_message_id: Optional[str] = None
    sender: Optional[str]
    subject: Optional[str]
    brand_name: Optional[str]
    proposed_rate: Optional[float]
    offer_type: Optional[str]
    triage_reason: Optional[str] = None
    draft_text: str
    status: str
    is_escalate: bool
    escalate_reason: Optional[str]
    created_at: datetime
    human_edited: bool = False
    human_edited_at: Optional[datetime] = None
    human_edited_by: Optional[str] = None

    class Config:
        from_attributes = True


class EditBody(BaseModel):
    draft_text: str
    reviewed_by: Optional[str] = None
    edit_note: Optional[str] = None


class ApproveBody(BaseModel):
    reviewed_by: Optional[str] = None


class DiscardBody(BaseModel):
    reviewed_by: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_draft_or_404(db: Session, draft_id: int) -> Draft:
    draft = db.query(Draft).filter(Draft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


def _get_token_or_404(db: Session, talent_key: str) -> TalentToken:
    ensure_talent_gmail_enabled(talent_key)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=404,
            detail=f"No active Gmail token for talent '{talent_key}'. Have them reconnect.",
        )
    return token


# ── Routes ────────────────────────────────────────────────────────────────────


def _draft_to_dict(r: Draft, triage_reason: str | None = None) -> dict:
    return {
        "id": r.id,
        "talent_key": r.talent_key,
        "gmail_message_id": r.gmail_message_id,
        "sender": r.sender,
        "subject": r.subject,
        "brand_name": r.brand_name,
        "proposed_rate": r.proposed_rate,
        "offer_type": r.offer_type,
        "triage_reason": triage_reason,
        "draft_text": r.draft_text,
        "status": r.status if isinstance(r.status, str) else r.status.value,
        "is_escalate": bool(r.is_escalate),
        "escalate_reason": r.escalate_reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "human_edited": bool(getattr(r, "human_edited", False) or False),
        "human_edited_at": r.human_edited_at.isoformat() if getattr(r, "human_edited_at", None) else None,
        "human_edited_by": getattr(r, "human_edited_by", None),
    }


@router.get("")
def list_drafts(
    status: Optional[str] = Query(None, description="Filter by status: pending, approved, sent, discarded"),
    talent_key: Optional[str] = Query(None, description="Filter by talent"),
    db: Session = Depends(get_db),
):
    """Return all drafts, newest first. Defaults to pending only."""
    q = db.query(Draft)
    if status:
        q = q.filter(Draft.status == status)
    else:
        q = q.filter(Draft.status == DraftStatus.pending)
    if talent_key:
        q = q.filter(Draft.talent_key.ilike(talent_key))
    rows = q.order_by(Draft.created_at.desc()).all()
    msg_ids = [r.gmail_message_id for r in rows if r.gmail_message_id]
    processed_rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id.in_(msg_ids))
        .all()
    ) if msg_ids else []
    triage_map = {r.gmail_message_id: r.triage_reason for r in processed_rows}
    return [_draft_to_dict(r, triage_map.get(r.gmail_message_id)) for r in rows]


@router.get("/human-edited")
def list_human_edited_drafts(
    talent_key: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """All drafts that a human has touched — across all statuses."""
    q = db.query(Draft).filter(Draft.human_edited == True)  # noqa: E712
    if talent_key:
        q = q.filter(Draft.talent_key.ilike(talent_key))
    rows = q.order_by(Draft.human_edited_at.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "talent_key": r.talent_key,
            "gmail_message_id": r.gmail_message_id,
            "sender": r.sender,
            "subject": r.subject,
            "brand_name": r.brand_name,
            "proposed_rate": r.proposed_rate,
            "status": r.status,
            "human_edited_at": r.human_edited_at.isoformat() if r.human_edited_at else None,
            "human_edited_by": r.human_edited_by,
            "draft_text": r.draft_text,
            "original_draft_text": r.original_draft_text,
        }
        for r in rows
    ]


@router.get("/orphaned")
def list_orphaned_emails(
    talent_key: Optional[str] = Query(None),
    limit: int = Query(100),
    db: Session = Depends(get_db),
):
    """Score-3 emails that have no draft — real deals that fell through the cracks."""
    from backend.models.db import ProcessedEmail
    from sqlalchemy import select

    drafted_subq = select(Draft.gmail_message_id)
    q = db.query(ProcessedEmail).filter(
        ProcessedEmail.score == 3,
        ProcessedEmail.status != "archived",
        ProcessedEmail.gmail_message_id.not_in(drafted_subq),
    )
    if talent_key:
        q = q.filter(ProcessedEmail.talent_key.ilike(talent_key))
    rows = q.order_by(ProcessedEmail.processed_at.desc()).limit(limit).all()
    return [
        {
            "gmail_message_id": r.gmail_message_id,
            "talent_key": r.talent_key,
            "sender": r.sender,
            "subject": r.subject,
            "brand_name": r.brand_name,
            "proposed_rate": r.proposed_rate,
            "offer_type": r.offer_type,
            "triage_reason": r.triage_reason,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        }
        for r in rows
    ]


@router.post("/orphaned/{gmail_message_id}/regenerate")
def regenerate_draft(gmail_message_id: str, db: Session = Depends(get_db)):
    """Force-regenerate a draft for an orphaned score-3 email."""
    from backend.models.db import ProcessedEmail
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if not pe:
        raise HTTPException(status_code=404, detail="Email not found in processed records.")
    if pe.score != 3:
        raise HTTPException(status_code=400, detail="Email is not Score 3 — cannot regenerate draft.")

    existing = db.query(Draft).filter(Draft.gmail_message_id == gmail_message_id).first()
    if existing:
        db.delete(existing)
        db.commit()

    return {"ok": True, "message": "Draft cleared — will be regenerated within 20 seconds."}


@router.get("/{draft_id}")
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = _get_draft_or_404(db, draft_id)
    processed = None
    if draft.gmail_message_id:
        processed = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.gmail_message_id == draft.gmail_message_id)
            .first()
        )
    return _draft_to_dict(draft, processed.triage_reason if processed else None)


@router.post("/{draft_id}/approve")
def approve_draft(draft_id: int, body: ApproveBody = ApproveBody(), db: Session = Depends(get_db)):
    """
    Send the draft reply via Gmail as the talent, then mark as sent.
    """
    draft = _get_draft_or_404(db, draft_id)
    if draft.status != DraftStatus.pending:
        raise HTTPException(status_code=400, detail=f"Draft is already '{draft.status}' — cannot approve.")

    token = _get_token_or_404(db, draft.talent_key)

    try:
        cc = parse_cc_recipients(draft.cc_recipients)
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
        raise HTTPException(status_code=401, detail="Gmail token expired — talent must reconnect.")

    if not success:
        raise HTTPException(status_code=502, detail="Gmail send failed — check token and try again.")

    if draft.gmail_message_id:
        gmail_svc.mark_initial_response_sent(token, draft.gmail_message_id, db=db)

    # Delete the Gmail draft copy (sent from the Sent folder now)
    if draft.gmail_draft_id:
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)

    draft.status = DraftStatus.sent
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)

    # Sync status on the ProcessedEmail record so the Sent tab shows this email
    from backend.models.db import EmailStatus, ProcessedEmail
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == draft.gmail_message_id
    ).first()
    if pe:
        pe.status = EmailStatus.sent
        db.add(pe)

    db.commit()
    logger.info("Draft %s approved and sent by %s", draft_id, body.reviewed_by)
    return {"ok": True, "message": "Reply sent successfully."}


@router.post("/{draft_id}/edit")
def edit_draft(draft_id: int, body: EditBody, db: Session = Depends(get_db)):
    """
    Update the draft text. Keeps status as pending so it can still be approved.
    Also updates the Gmail Draft copy if one exists.
    """
    draft = _get_draft_or_404(db, draft_id)
    if draft.status not in (DraftStatus.pending,):
        raise HTTPException(status_code=400, detail=f"Cannot edit a draft with status '{draft.status}'.")

    if not body.draft_text.strip():
        raise HTTPException(status_code=422, detail="draft_text cannot be empty.")

    # Log the human edit before overwriting
    log = DraftEditLog(
        draft_id=draft.id,
        talent_key=draft.talent_key,
        gmail_message_id=draft.gmail_message_id,
        edited_by=body.reviewed_by,
        edit_note=body.edit_note,
        text_before=draft.draft_text,
        text_after=body.draft_text,
        edited_at=datetime.utcnow(),
    )
    db.add(log)

    # Preserve the original AI text on first edit
    if not draft.human_edited:
        draft.original_draft_text = draft.draft_text

    draft.draft_text = body.draft_text
    draft.reviewed_by = body.reviewed_by
    draft.human_edited = True
    draft.human_edited_at = datetime.utcnow()
    draft.human_edited_by = body.reviewed_by
    # If there's an existing Gmail draft, delete it and recreate with new text
    if draft.gmail_draft_id:
        token = _get_token_or_404(db, draft.talent_key)
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)
        new_gmail_draft_id = gmail_svc.create_gmail_draft(
            token,
            thread_id=draft.thread_id or "",
            reply_to=draft.sender or "",
            subject=draft.subject or "",
            body=body.draft_text,
            db=db,
            cc=parse_cc_recipients(draft.cc_recipients) or None,
        )
        draft.gmail_draft_id = new_gmail_draft_id

    db.add(draft)
    db.commit()
    return {"ok": True, "message": "Draft updated."}


@router.post("/{draft_id}/discard")
def discard_draft(draft_id: int, body: DiscardBody = DiscardBody(), db: Session = Depends(get_db)):
    """
    Discard the draft and delete the Gmail Draft copy.
    """
    draft = _get_draft_or_404(db, draft_id)
    if draft.status not in (DraftStatus.pending,):
        raise HTTPException(status_code=400, detail=f"Cannot discard a draft with status '{draft.status}'.")

    if draft.gmail_draft_id:
        token = _get_token_or_404(db, draft.talent_key)
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)

    draft.status = DraftStatus.discarded
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)
    db.commit()
    logger.info("Draft %s discarded by %s", draft_id, body.reviewed_by)
    return {"ok": True, "message": "Draft discarded."}


@router.get("/{draft_id}/edit-history")
def get_edit_history(draft_id: int, db: Session = Depends(get_db)):
    """Full edit log for a single draft."""
    logs = (
        db.query(DraftEditLog)
        .filter(DraftEditLog.draft_id == draft_id)
        .order_by(DraftEditLog.edited_at.asc())
        .all()
    )
    return [
        {
            "id": l.id,
            "edited_by": l.edited_by,
            "edit_note": l.edit_note,
            "text_before": l.text_before,
            "text_after": l.text_after,
            "edited_at": l.edited_at.isoformat(),
        }
        for l in logs
    ]


@router.post("/discard-all")
def discard_all_pending(db: Session = Depends(get_db)):
    """Discard every pending draft — wipes the badge counts to zero for a clean start."""
    from backend.services import gmail as gmail_svc
    from backend.models.db import TalentToken

    pending = db.query(Draft).filter(Draft.status == DraftStatus.pending).all()
    cleared = 0
    for draft in pending:
        # Delete the Gmail draft copy if one exists
        if draft.gmail_draft_id:
            try:
                ensure_talent_gmail_enabled(draft.talent_key)
            except HTTPException:
                draft.status = DraftStatus.discarded
                db.add(draft)
                cleared += 1
                continue
            token = db.query(TalentToken).filter(
                TalentToken.talent_key == draft.talent_key,
                TalentToken.active == True,  # noqa: E712
            ).first()
            if token:
                try:
                    gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)
                except Exception:
                    pass
        draft.status = DraftStatus.discarded
        db.add(draft)
        cleared += 1

    db.commit()
    return {"ok": True, "cleared": cleared, "message": f"{cleared} pending drafts discarded."}
