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


@router.post("/orphaned/trash-all")
def trash_all_orphaned(
    talent_key: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Trash all orphaned score-3 emails in Gmail and mark them archived in DB."""
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
    rows = q.all()

    trashed = 0
    failed = 0
    for row in rows:
        try:
            token_row = db.query(TalentToken).filter(
                TalentToken.talent_key.ilike(row.talent_key)
            ).first()
            if token_row:
                service = gmail_svc.build_service(token_row, db=db)
                service.users().messages().trash(userId="me", id=row.gmail_message_id).execute()
            row.status = "archived"
            trashed += 1
        except Exception as e:
            logger.warning("trash_all_orphaned: failed to trash %s: %s", row.gmail_message_id, e)
            row.status = "archived"  # mark archived in DB even if Gmail call failed
            failed += 1
    db.commit()
    return {"ok": True, "trashed": trashed, "failed": failed}


@router.post("/orphaned/regenerate-all")
def regenerate_all_orphaned(
    talent_key: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Bulk-regenerate drafts for all orphaned Score-3 emails with no draft. Clears any stale Draft record so the draft-queue worker recreates it within 20 seconds."""
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
    orphans = q.all()

    queued = 0
    for pe in orphans:
        existing = db.query(Draft).filter(Draft.gmail_message_id == pe.gmail_message_id).first()
        if existing:
            db.delete(existing)
        queued += 1

    db.commit()
    return {"ok": True, "queued": queued, "message": f"{queued} orphaned draft(s) cleared — will regenerate within 20 seconds."}


@router.post("/orphaned/{gmail_message_id}/regenerate")
def regenerate_draft(gmail_message_id: str, db: Session = Depends(get_db)):
    """Force-regenerate a draft for an orphaned score-3 email synchronously."""
    from backend.models.db import InboxEmail, ProcessedEmail
    from backend.core.config import get_settings
    from backend.services.reply import draft_reply

    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if not pe:
        raise HTTPException(status_code=404, detail="Email not found in processed records.")
    if pe.score != 3:
        raise HTTPException(status_code=400, detail="Email is not Score 3 — cannot regenerate draft.")

    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(pe.talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Talent Gmail not connected.")

    settings = get_settings()
    talent_cfg = next(
        (t for t in settings.app_config.get("talents", []) if t["key"].lower() == pe.talent_key.lower()),
        None,
    )
    talent_name = talent_cfg.get("full_name", pe.talent_key) if talent_cfg else pe.talent_key
    minimum_rate = float(talent_cfg.get("minimum_rate_usd", 0)) if talent_cfg else 0.0

    inbox_row = db.query(InboxEmail).filter(
        InboxEmail.gmail_message_id == gmail_message_id
    ).first()
    body_text = (inbox_row.body_text if inbox_row else None) or ""
    subject = pe.subject or (inbox_row.subject if inbox_row else "") or ""
    sender = pe.sender or (inbox_row.sender if inbox_row else "") or ""
    thread_id = pe.thread_id or (inbox_row.thread_id if inbox_row else None) or gmail_message_id

    if not body_text:
        try:
            detail = gmail_svc.get_message_detail(token, gmail_message_id, db=db)
            body_text = detail.get("body_text") or ""
            subject = subject or detail.get("subject") or ""
            sender = sender or detail.get("sender") or ""
            thread_id = thread_id or detail.get("thread_id") or gmail_message_id
        except Exception as exc:
            logger.warning("regenerate: could not fetch body for %s: %s", gmail_message_id, exc)

    result = draft_reply(
        talent_key=pe.talent_key.lower(),
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        offer_type=pe.offer_type or "",
        brand_name=pe.brand_name or "",
        proposed_rate=pe.proposed_rate or 0.0,
        triage_reason=pe.triage_reason or "",
        db=db,
        body_text=body_text,
    )

    if result.get("is_escalate"):
        raise HTTPException(
            status_code=422,
            detail=f"Escalated — no SOP match: {result.get('escalate_reason', 'unknown reason')}",
        )

    draft_text = result["draft_text"]
    cc_str = result.get("cc_recipients") or None
    cc_list = [c.strip() for c in cc_str.split(",")] if cc_str else None

    gmail_draft_id = gmail_svc.create_gmail_draft(
        token,
        thread_id=thread_id,
        reply_to=sender,
        subject=subject,
        body=draft_text,
        db=db,
        in_reply_to=None,
        cc=cc_list,
    )
    if not gmail_draft_id:
        raise HTTPException(status_code=502, detail="Gmail draft creation failed — token may need refresh.")

    # Discard old pending drafts for this email before saving new one
    old_drafts = db.query(Draft).filter(
        Draft.gmail_message_id == gmail_message_id,
        Draft.status == DraftStatus.pending,
    ).all()
    for old in old_drafts:
        old.status = DraftStatus.discarded
        db.add(old)

    draft_row = Draft(
        talent_key=pe.talent_key.lower(),
        gmail_message_id=gmail_message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        brand_name=pe.brand_name,
        proposed_rate=pe.proposed_rate,
        offer_type=pe.offer_type,
        draft_text=draft_text,
        cc_recipients=cc_str,
        gmail_draft_id=gmail_draft_id,
        message_id_header=None,
        status=DraftStatus.pending,
        is_escalate=False,
        triggered_by_job="regenerate-button",
    )
    db.add(draft_row)

    labeled = gmail_svc.mark_initial_response_sent(token, gmail_message_id, db=db)
    if not labeled:
        logger.warning("regenerate: mark_initial_response_sent returned False for %s", gmail_message_id)

    db.commit()
    return {"ok": True, "draft_id": draft_row.id, "gmail_draft_id": gmail_draft_id}


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
        success, send_error = gmail_svc.send_reply(
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
        raise HTTPException(status_code=502, detail=f"Gmail send failed: {send_error}")

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


@router.post("/{draft_id}/move-to-inbox")
def move_draft_to_inbox(draft_id: int, body: DiscardBody = DiscardBody(), db: Session = Depends(get_db)):
    """Activity Hub ↩ Inbox button. Discards draft, restores INBOX label, downgrades score to 2."""
    from backend.models.db import EmailStatus
    draft = _get_draft_or_404(db, draft_id)
    if draft.status not in (DraftStatus.pending,):
        raise HTTPException(status_code=400, detail=f"Cannot move draft with status '{draft.status}' to inbox.")
    token = _get_token_or_404(db, draft.talent_key)
    if draft.gmail_draft_id:
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)
    if draft.gmail_message_id:
        gmail_svc.move_to_inbox(token, draft.gmail_message_id, db=db)
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == draft.gmail_message_id
    ).first()
    if pe:
        pe.score = 2
        pe.status = EmailStatus.flagged
        pe.processed_at = datetime.utcnow()  # refresh so email appears in 24h feed window
        db.add(pe)
    draft.status = DraftStatus.discarded
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)
    db.commit()
    logger.info("Draft %s moved to inbox by %s", draft_id, body.reviewed_by)
    return {"ok": True}


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
