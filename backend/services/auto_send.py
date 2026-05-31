"""Auto-send service — sends qualifying pending drafts after a hold period.

Controlled entirely by config/settings.json:
  auto_send_enabled: false   → function returns immediately, nothing fires
  auto_send_talents: [...]   → pilot talent list
  auto_send_hold_minutes: 15 → drafts younger than this are never auto-sent

Safeguards enforced per draft:
  - Velocity cap: no more than 5 auto-sends per talent per hour
  - Thread count: skip if the Gmail thread already has > 1 message (prior activity)
  - Already-sent guard: skip if reviewed_at is already set
  - Human-touch guard: skip if human_edited=True or dismissed=True
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services.oauth import TokenRefreshError

logger = logging.getLogger(__name__)


def run_auto_send(db: Session) -> None:
    settings = get_settings()
    cfg = settings.app_config

    if not cfg.get("auto_send_enabled", False):
        return

    talents: list[str] = cfg.get("auto_send_talents", [])
    if not talents:
        return

    hold_minutes: int = int(cfg.get("auto_send_hold_minutes", 15))
    cutoff = datetime.utcnow() - timedelta(minutes=hold_minutes)

    for talent_key in talents:
        try:
            _process_talent(db, talent_key, cutoff)
        except Exception as exc:  # noqa: BLE001
            logger.error("auto_send: unexpected error for %s: %s", talent_key, exc)


def _process_talent(db: Session, talent_key: str, cutoff: datetime) -> None:
    token = (
        db.query(TalentToken)
        .filter(TalentToken.talent_key.ilike(talent_key), TalentToken.active == True)  # noqa: E712
        .first()
    )
    if not token:
        logger.warning("auto_send: no active token for %s — skipping", talent_key)
        return

    # Velocity guard: count auto-sends in last hour for this talent
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    sent_last_hour = (
        db.query(Draft)
        .filter(
            Draft.talent_key.ilike(talent_key),
            Draft.triggered_by_job == "auto_send",
            Draft.reviewed_at >= one_hour_ago,
        )
        .count()
    )
    if sent_last_hour >= 5:
        logger.warning(
            "auto_send: velocity cap reached for %s (%d sent in last hour) — skipping cycle",
            talent_key, sent_last_hour,
        )
        return

    drafts = (
        db.query(Draft)
        .filter(
            Draft.talent_key.ilike(talent_key),
            Draft.status == DraftStatus.pending,
            or_(Draft.triggered_by_job == None, Draft.triggered_by_job != "auto_send"),  # noqa: E711
            Draft.created_at < cutoff,
            Draft.human_edited == False,  # noqa: E712
            Draft.dismissed == False,  # noqa: E712
        )
        .order_by(Draft.created_at.asc())
        .all()
    )

    if not drafts:
        return

    try:
        service = gmail_svc.build_service(token, db)
    except TokenRefreshError as exc:
        logger.warning("auto_send: token refresh failed for %s — skipping: %s", talent_key, exc)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto_send: could not build Gmail service for %s: %s", talent_key, exc)
        return

    sent_this_cycle = 0
    for draft in drafts:
        if sent_last_hour + sent_this_cycle >= 5:
            logger.info("auto_send: velocity cap hit mid-cycle for %s — stopping", talent_key)
            break

        # Already-sent guard
        if draft.reviewed_at is not None:
            continue

        # Thread count guard: skip if the thread has prior activity
        if draft.thread_id:
            try:
                thread = service.users().threads().get(
                    userId="me", id=draft.thread_id, format="minimal"
                ).execute()
                if len(thread.get("messages", [])) > 1:
                    logger.info(
                        "auto_send: thread %s has %d messages — skipping draft %d",
                        draft.thread_id, len(thread.get("messages", [])), draft.id,
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_send: thread check failed for draft %d: %s — skipping", draft.id, exc)
                continue

        _send_draft(db, draft, token, service)
        sent_this_cycle += 1


def _send_draft(db: Session, draft: Draft, token: TalentToken, service) -> None:
    from backend.services.gmail import parse_cc_recipients
    cc = parse_cc_recipients(draft.cc_recipients)

    try:
        success, send_error = gmail_svc.send_reply(
            token_row=token,
            thread_id=draft.thread_id or "",
            reply_to=draft.sender or "",
            subject=draft.subject or "",
            body=draft.draft_text,
            db=db,
            in_reply_to=draft.message_id_header,
            cc=cc or None,
        )
    except TokenRefreshError as exc:
        logger.error("auto_send: token refresh failed sending draft %d for %s: %s", draft.id, draft.talent_key, exc)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("auto_send: unexpected error sending draft %d for %s: %s", draft.id, draft.talent_key, exc)
        return

    if not success:
        logger.error("auto_send: Gmail send failed for draft %d (%s): %s", draft.id, draft.talent_key, send_error)
        return

    now = datetime.utcnow()
    draft.status = DraftStatus.sent
    draft.reviewed_at = now
    draft.reviewed_by = "auto_send"
    draft.triggered_by_job = "auto_send"
    db.add(draft)

    # Sync ProcessedEmail status
    if draft.gmail_message_id:
        pe = db.query(ProcessedEmail).filter(
            ProcessedEmail.gmail_message_id == draft.gmail_message_id
        ).first()
        if pe:
            pe.status = EmailStatus.sent
            db.add(pe)

        try:
            gmail_svc.mark_initial_response_sent(token, draft.gmail_message_id, db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_send: mark_initial_response_sent failed for draft %d: %s", draft.id, exc)

    if draft.gmail_draft_id:
        try:
            gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_send: delete_gmail_draft failed for draft %d: %s", draft.id, exc)

    db.commit()
    logger.info(
        "auto_send: sent draft %d for %s — subject: %s",
        draft.id, draft.talent_key, (draft.subject or "")[:60],
    )
