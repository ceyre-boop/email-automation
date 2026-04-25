"""
Inbox polling engine.

Called by the cron route (GET /cron/poll-inboxes) every 5 minutes.
Iterates over every connected talent, processes unread emails end-to-end:
  1. Fetch unread messages from Gmail
  2. Triage (GPT-4o-mini)
  3. Score 1 → archive + log
     Score 2 → log as flagged
     Score 3 → draft reply (GPT-4o) + save Gmail Draft + log
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services import reply as reply_svc
from backend.services import sheets as sheets_svc
from backend.services import triage as triage_svc

logger = logging.getLogger(__name__)


def _talent_config_map(settings) -> dict[str, dict]:
    """Return a dict of talent_key → talent config dict from settings.json.

    Keys are normalised to lowercase so DB keys ('katrina') match config keys ('Katrina').
    """
    return {t["key"].lower(): t for t in settings.app_config.get("talents", [])}


def _already_processed(db: Session, message_id: str) -> bool:
    return (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id == message_id)
        .first()
        is not None
    )


def poll_all_inboxes(db: Session) -> dict:
    """
    Main polling function. Processes all active connected talents.
    Returns a summary dict for logging/monitoring.
    """
    settings = get_settings()
    talent_map = _talent_config_map(settings)
    policy = settings.confidence_policy
    draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)

    active_tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712

    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    for token_row in active_tokens:
        talent_key = token_row.talent_key
        talent_cfg = talent_map.get(talent_key.lower())
        if not talent_cfg:
            logger.warning("No config for talent_key=%s — skipping", talent_key)
            continue

        talent_name = talent_cfg.get("full_name", talent_key)
        minimum_rate = talent_cfg.get("minimum_rate_usd", 0)

        try:
            messages = gmail_svc.list_unread_inbox_messages(token_row)
            # Persist refreshed token back to DB
            db.add(token_row)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Gmail list failed for %s: %s", talent_key, exc)
            summary["errors"] += 1
            continue

        for msg_stub in messages:
            message_id = msg_stub["id"]
            if _already_processed(db, message_id):
                continue

            try:
                _process_one_message(
                    db=db,
                    token_row=token_row,
                    message_id=message_id,
                    talent_key=talent_key,
                    talent_name=talent_name,
                    minimum_rate=minimum_rate,
                    draft_mode=draft_mode,
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Error processing %s / %s: %s", talent_key, message_id, exc
                )
                summary["errors"] += 1
                # Record the failure so we don't retry forever on a broken message
                _record_processed(
                    db=db,
                    talent_key=talent_key,
                    message_id=message_id,
                    thread_id="",
                    sender="",
                    subject="",
                    score=2,
                    brand_name="",
                    proposed_rate=0,
                    offer_type="Unknown",
                    reason=f"Error: {exc}",
                    status=EmailStatus.error,
                    email_date=None,
                )

    logger.info("Poll complete: %s", summary)
    return summary


def _process_one_message(
    db: Session,
    token_row,
    message_id: str,
    talent_key: str,
    talent_name: str,
    minimum_rate: float,
    draft_mode: bool,
    summary: dict,
):
    detail = gmail_svc.get_message_detail(token_row, message_id)
    if not detail:
        logger.warning("Empty detail for %s / %s — skipping", talent_key, message_id)
        return

    thread_id = detail.get("thread_id", "")
    subject = detail.get("subject", "")
    sender = detail.get("sender", "")
    sender_domain = detail.get("sender_domain", "")
    body = detail.get("body_text", "")
    email_date = detail.get("email_date")

    # ── Triage ───────────────────────────────────────────────────────────────
    triage_result = triage_svc.triage_email(
        talent_key=talent_key,
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        sender_domain=sender_domain,
        body=body,
    )
    score = triage_result["score"]
    reason = triage_result["reason"]
    offer_type = triage_result["offer_type"]
    proposed_rate = triage_result["proposed_rate_usd"]
    brand_name = triage_result["brand_name"]

    # ── Score 1 → Archive ────────────────────────────────────────────────────
    if score == 1:
        gmail_svc.archive_message(token_row, message_id)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.archived,
            body_text=body, email_date=email_date,
        )
        sheets_svc.log_email(
            talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, "archived", reason
        )
        summary["archived"] += 1

    # ── Score 2 → Flag for review ────────────────────────────────────────────
    elif score == 2:
        gmail_svc.mark_as_read(token_row, message_id)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.flagged,
            body_text=body, email_date=email_date,
        )
        sheets_svc.log_email(
            talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, "flagged", reason
        )
        summary["flagged"] += 1

    # ── Score 3 → Draft reply ────────────────────────────────────────────────
    else:  # score == 3
        reply_result = reply_svc.draft_reply(
            talent_key=talent_key,
            talent_name=talent_name,
            minimum_rate=minimum_rate,
            subject=subject,
            sender=sender,
            offer_type=offer_type,
            brand_name=brand_name,
            proposed_rate=proposed_rate,
            triage_reason=reason,
            db=db,
        )
        draft_text = reply_result["draft_text"]
        is_escalate = reply_result["is_escalate"]
        escalate_reason = reply_result.get("escalate_reason")

        # Save as Gmail Draft in the talent's inbox (unless GPT escalated)
        gmail_draft_id: str | None = None
        if not is_escalate and draft_mode:
            gmail_draft_id = gmail_svc.create_gmail_draft(
                token_row,
                thread_id=thread_id,
                reply_to=sender,
                subject=subject,
                body=draft_text,
            )

        # Persist draft to DB
        draft_row = Draft(
            talent_key=talent_key,
            gmail_message_id=message_id,
            thread_id=thread_id,
            sender=sender,
            subject=subject,
            brand_name=brand_name,
            proposed_rate=proposed_rate,
            offer_type=offer_type,
            draft_text=draft_text,
            gmail_draft_id=gmail_draft_id,
            status=DraftStatus.pending,
            is_escalate=is_escalate,
            escalate_reason=escalate_reason,
        )
        db.add(draft_row)

        status_label = "escalated" if is_escalate else "draft_saved"
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.draft_saved,
            body_text=body, email_date=email_date,
        )
        sheets_svc.log_email(
            talent_key, sender, subject, score, brand_name, proposed_rate,
            offer_type, status_label,
            escalate_reason or reason,
        )
        summary["drafted"] += 1

    db.commit()
    summary["processed"] += 1


def _record_processed(
    db: Session,
    talent_key: str,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    score: int,
    brand_name: str,
    proposed_rate: float,
    offer_type: str,
    reason: str,
    status: EmailStatus,
    body_text: str = "",
    email_date=None,
):
    row = ProcessedEmail(
        talent_key=talent_key,
        gmail_message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        score=score,
        brand_name=brand_name,
        proposed_rate=proposed_rate,
        offer_type=offer_type,
        triage_reason=reason,
        body_text=body_text or None,
        email_date=email_date,
        processed_at=datetime.utcnow(),
        status=status,
    )
    db.add(row)
