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

from backend.models.db import Draft, DraftStatus, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services import gmail as gmail_svc
from backend.services.oauth import TokenRefreshError

router = APIRouter(prefix="/api/drafts", tags=["drafts"], dependencies=[Depends(verify_api_key)])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────


class DraftOut(BaseModel):
    id: int
    talent_key: str
    sender: Optional[str]
    subject: Optional[str]
    brand_name: Optional[str]
    proposed_rate: Optional[float]
    offer_type: Optional[str]
    draft_text: str
    status: str
    is_escalate: bool
    escalate_reason: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class EditBody(BaseModel):
    draft_text: str
    reviewed_by: Optional[str] = None


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
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key == talent_key, TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=404,
            detail=f"No active Gmail token for talent '{talent_key}'. Have them reconnect.",
        )
    return token


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[DraftOut])
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
        q = q.filter(Draft.talent_key == talent_key)
    return q.order_by(Draft.created_at.desc()).all()


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    return _get_draft_or_404(db, draft_id)


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
        success = gmail_svc.send_reply(
            token_row=token,
            thread_id=draft.thread_id or "",
            reply_to=draft.sender or "",
            subject=draft.subject or "",
            body=draft.draft_text,
            db=db,
            in_reply_to=getattr(draft, "message_id_header", None),
        )
    except TokenRefreshError:
        token.active = False
        db.add(token)
        db.commit()
        raise HTTPException(status_code=401, detail="Gmail token expired — talent must reconnect.")

    if not success:
        raise HTTPException(status_code=502, detail="Gmail send failed — check token and try again.")

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

    draft.draft_text = body.draft_text
    draft.reviewed_by = body.reviewed_by
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
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id)
        db.add(token)

    draft.status = DraftStatus.discarded
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)
    db.commit()
    logger.info("Draft %s discarded by %s", draft_id, body.reviewed_by)
    return {"ok": True, "message": "Draft discarded."}
