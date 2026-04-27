"""
Internal API router — endpoints consumed by n8n automation workflows.

All routes are protected by x-api-key and prefixed with /internal.

Endpoints
---------
GET  /internal/gmail/accounts-to-poll
GET  /internal/gmail/fetch-new-messages
POST /internal/gmail/send-reply
POST /internal/gpt/generate-draft
GET  /internal/drafts/{draft_id}
POST /internal/logs/automation-error
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, ProcessedEmail, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services import gmail as gmail_svc
from backend.services import reply as reply_svc
from backend.services import triage as triage_svc
from backend.services.oauth import TokenRefreshError

router = APIRouter(
    prefix="/internal",
    tags=["internal-n8n"],
    dependencies=[Depends(verify_api_key)],
)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_active_token(db: Session, gmail_account_id: str) -> TalentToken:
    """
    Resolve a gmail_account_id (talent_key or email) to an active TalentToken.
    Raises 404 if not found or inactive.
    """
    token = (
        db.query(TalentToken)
        .filter(
            TalentToken.active == True,  # noqa: E712
            (TalentToken.talent_key == gmail_account_id)
            | (TalentToken.email == gmail_account_id),
        )
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=404,
            detail=f"No active Gmail account found for id='{gmail_account_id}'.",
        )
    return token


# ── Schemas ───────────────────────────────────────────────────────────────────


class SendReplyBody(BaseModel):
    gmail_account_id: str
    thread_id: str
    reply_to: str
    subject: str
    body: str


class GenerateDraftBody(BaseModel):
    gmail_account_id: str
    gmail_message_id: str
    thread_id: str
    subject: str
    sender: str
    body_text: str
    # Optional pre-computed triage fields — if omitted, triage runs automatically
    offer_type: Optional[str] = None
    brand_name: Optional[str] = None
    proposed_rate: Optional[float] = None


class AutomationErrorBody(BaseModel):
    workflow: Optional[str] = None
    node: Optional[str] = None
    error: str
    context: Optional[dict[str, Any]] = None


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/gmail/accounts-to-poll")
def accounts_to_poll(db: Session = Depends(get_db)):
    """
    Return all active Gmail accounts connected to the system.
    n8n uses this list to know which inboxes to poll each cycle.

    Response: [{ gmail_account_id, email, talent_key, connected_at }]
    """
    tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    return [
        {
            "gmail_account_id": t.talent_key,
            "email": t.email,
            "talent_key": t.talent_key,
            "connected_at": t.connected_at.isoformat() if t.connected_at else None,
        }
        for t in tokens
    ]


@router.get("/gmail/fetch-new-messages")
def fetch_new_messages(
    gmail_account_id: str = Query(..., description="talent_key or email of the connected Gmail account"),
    max_results: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Fetch unread inbox messages for a specific Gmail account.
    Returns normalized message objects including headers and body text.

    Response: [{ message_id, thread_id, subject, sender, body_text, snippet, label_ids, email_date }]
    """
    token = _get_active_token(db, gmail_account_id)

    try:
        stubs = gmail_svc.list_unread_inbox_messages(token, db=db, max_results=max_results)
    except TokenRefreshError as exc:
        token.active = False
        token.last_error = str(exc)
        db.add(token)
        db.commit()
        raise HTTPException(
            status_code=401,
            detail="Gmail OAuth token expired — talent must reconnect.",
        ) from exc
    except Exception as exc:
        logger.error("fetch-new-messages failed for %s: %s", gmail_account_id, exc)
        raise HTTPException(status_code=502, detail=f"Gmail API error: {exc}") from exc

    # Fetch full detail for each stub and return normalised messages
    messages = []
    for stub in stubs:
        detail = gmail_svc.get_message_detail(token, stub["id"], db=db)
        if not detail:
            continue
        messages.append(
            {
                "message_id": detail.get("id", stub["id"]),
                "thread_id": detail.get("thread_id", ""),
                "subject": detail.get("subject", ""),
                "sender": detail.get("sender", ""),
                "sender_domain": detail.get("sender_domain", ""),
                "body_text": detail.get("body_text", ""),
                "snippet": detail.get("snippet", ""),
                "label_ids": detail.get("label_ids", []),
                "email_date": (
                    detail["email_date"].isoformat()
                    if detail.get("email_date")
                    else None
                ),
            }
        )
    return messages


@router.post("/gmail/send-reply")
def send_reply(body: SendReplyBody, db: Session = Depends(get_db)):
    """
    Send an email reply as the talent.
    Used by n8n after a manager approves a draft.

    Request body: { gmail_account_id, thread_id, reply_to, subject, body }
    Response: { ok: true }
    """
    token = _get_active_token(db, body.gmail_account_id)

    try:
        success = gmail_svc.send_reply(
            token_row=token,
            thread_id=body.thread_id,
            reply_to=body.reply_to,
            subject=body.subject,
            body=body.body,
            db=db,
        )
    except TokenRefreshError as exc:
        token.active = False
        token.last_error = str(exc)
        db.add(token)
        db.commit()
        raise HTTPException(
            status_code=401,
            detail="Gmail OAuth token expired — talent must reconnect.",
        ) from exc
    except Exception as exc:
        logger.error("send-reply failed for %s: %s", body.gmail_account_id, exc)
        raise HTTPException(status_code=502, detail=f"Gmail send error: {exc}") from exc

    if not success:
        raise HTTPException(status_code=502, detail="Gmail send failed — check token and try again.")

    return {"ok": True}


@router.post("/gpt/generate-draft")
def generate_draft(body: GenerateDraftBody, db: Session = Depends(get_db)):
    """
    Run triage + GPT reply drafting for an inbound email and persist the draft.

    Steps:
    1. Look up talent config (name, minimum_rate)
    2. Run triage (unless offer_type/brand_name/proposed_rate are already supplied)
    3. Call GPT-4o to generate a reply draft
    4. Save the draft to the DB
    5. Return { draft_id, draft_body }

    Request body: { gmail_account_id, gmail_message_id, thread_id, subject, sender, body_text,
                    offer_type?, brand_name?, proposed_rate? }
    Response: { draft_id, draft_body }
    """
    # ── 1. Resolve talent config ──────────────────────────────────────────────
    settings = get_settings()
    talent_map = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    talent_cfg = talent_map.get(body.gmail_account_id.lower())
    if not talent_cfg:
        # Fall back to token lookup to get the talent_key
        token = _get_active_token(db, body.gmail_account_id)
        talent_cfg = talent_map.get(token.talent_key.lower(), {})

    talent_key: str = talent_cfg.get("key", body.gmail_account_id).lower()
    talent_name: str = talent_cfg.get("full_name", talent_key)
    minimum_rate: float = float(talent_cfg.get("minimum_rate_usd", 0))

    # ── 2. Skip if already processed ─────────────────────────────────────────
    already = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id == body.gmail_message_id)
        .first()
    )
    # If a draft already exists for this message, return it instead of regenerating
    existing_draft = (
        db.query(Draft)
        .filter(
            Draft.gmail_message_id == body.gmail_message_id,
            Draft.talent_key == talent_key,
        )
        .first()
    )
    if existing_draft:
        return {"draft_id": existing_draft.id, "draft_body": existing_draft.draft_text}

    # ── 3. Triage (if not pre-supplied) ──────────────────────────────────────
    offer_type = body.offer_type
    brand_name = body.brand_name
    proposed_rate = body.proposed_rate if body.proposed_rate is not None else 0.0

    if not offer_type or not brand_name:
        try:
            import re as _re
            sender_domain = ""
            match = _re.search(r"@([\w.\-]+)", body.sender)
            if match:
                sender_domain = match.group(1).lower()

            triage_result = triage_svc.triage_email(
                talent_key=talent_key,
                talent_name=talent_name,
                minimum_rate=minimum_rate,
                subject=body.subject,
                sender=body.sender,
                sender_domain=sender_domain,
                body=body.body_text,
            )
            offer_type = offer_type or triage_result.get("offer_type", "Unknown")
            brand_name = brand_name or triage_result.get("brand_name", "Unknown Brand")
            if body.proposed_rate is None:
                proposed_rate = triage_result.get("proposed_rate_usd", 0.0) or 0.0
            triage_reason = triage_result.get("reason", "")
        except Exception as exc:
            logger.warning("Triage failed in generate-draft for %s: %s", talent_key, exc)
            offer_type = offer_type or "Unknown"
            brand_name = brand_name or "Unknown Brand"
            triage_reason = f"Triage error: {exc}"
    else:
        triage_reason = ""

    # ── 4. Generate GPT reply draft ───────────────────────────────────────────
    try:
        reply_result = reply_svc.draft_reply(
            talent_key=talent_key,
            talent_name=talent_name,
            minimum_rate=minimum_rate,
            subject=body.subject,
            sender=body.sender,
            offer_type=offer_type,
            brand_name=brand_name,
            proposed_rate=proposed_rate,
            triage_reason=triage_reason,
            db=db,
        )
    except Exception as exc:
        logger.error("GPT draft generation failed for %s: %s", talent_key, exc)
        raise HTTPException(status_code=502, detail=f"GPT draft generation failed: {exc}") from exc

    draft_text: str = reply_result["draft_text"]
    is_escalate: bool = reply_result.get("is_escalate", False)
    escalate_reason: str | None = reply_result.get("escalate_reason")

    # ── 5. Persist draft ──────────────────────────────────────────────────────
    draft_row = Draft(
        talent_key=talent_key,
        gmail_message_id=body.gmail_message_id,
        thread_id=body.thread_id,
        sender=body.sender,
        subject=body.subject,
        brand_name=brand_name,
        proposed_rate=proposed_rate,
        offer_type=offer_type,
        draft_text=draft_text,
        status=DraftStatus.pending,
        is_escalate=is_escalate,
        escalate_reason=escalate_reason,
        created_at=datetime.utcnow(),
    )
    db.add(draft_row)
    db.commit()
    db.refresh(draft_row)

    logger.info(
        "generate-draft: created draft %d for %s (message=%s, escalate=%s)",
        draft_row.id,
        talent_key,
        body.gmail_message_id,
        is_escalate,
    )

    return {"draft_id": draft_row.id, "draft_body": draft_text}


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    """
    Retrieve a single draft by its DB ID.
    n8n uses this after generate-draft to fetch the full draft details.

    Response: { id, talent_key, sender, subject, brand_name, proposed_rate,
                offer_type, draft_text, status, is_escalate, escalate_reason, created_at }
    """
    draft = db.query(Draft).filter(Draft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return {
        "id": draft.id,
        "talent_key": draft.talent_key,
        "gmail_message_id": draft.gmail_message_id,
        "thread_id": draft.thread_id,
        "sender": draft.sender,
        "subject": draft.subject,
        "brand_name": draft.brand_name,
        "proposed_rate": draft.proposed_rate,
        "offer_type": draft.offer_type,
        "draft_text": draft.draft_text,
        "status": draft.status,
        "is_escalate": draft.is_escalate,
        "escalate_reason": draft.escalate_reason,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


@router.post("/logs/automation-error")
def log_automation_error(body: AutomationErrorBody):
    """
    Receive and log an automation error reported by n8n.
    Errors are written to the application log at ERROR level.

    Request body: { workflow?, node?, error, context? }
    Response: { ok: true }
    """
    logger.error(
        "n8n automation error — workflow=%s node=%s error=%s context=%s",
        body.workflow,
        body.node,
        body.error,
        body.context,
    )
    return {"ok": True}
