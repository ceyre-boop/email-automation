"""Auto-send service — sends qualifying pending drafts after a hold period.

Controlled entirely by config/settings.json:
  auto_send_enabled: false          → function returns immediately, nothing fires
  auto_send_talents: [...]          → pilot talent list
  auto_send_hold_minutes: 15        → drafts younger than this are never auto-sent
  auto_send_window_enabled: true    → only send between window_start and window_end in the configured timezone

Safeguards enforced per draft:
  - Velocity cap: no more than N auto-sends per talent per hour
  - Thread count: skip if the Gmail thread already has > 1 message (prior activity)
  - Already-sent guard: skip if reviewed_at is already set
  - Human-touch guard: skip if human_edited=True or dismissed=True
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services.oauth import TokenRefreshError

logger = logging.getLogger(__name__)


def _is_within_send_window(now_utc: datetime, cfg: dict) -> bool:
    """Return True if auto-send is allowed right now per the configured time window.

    Isolated so per-talent or per-timezone logic can be added here later
    without touching the send loop. When window is disabled, always returns True.
    Fails open on any config/tz error so a bad timezone string never silently blocks sends.
    """
    if not cfg.get("auto_send_window_enabled", False):
        return True
    tz_name = cfg.get("auto_send_timezone", "America/Detroit")
    start_str = cfg.get("auto_send_window_start", "07:00")
    end_str = cfg.get("auto_send_window_end", "19:00")
    try:
        tz = ZoneInfo(tz_name)
        now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        window_start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
        window_end   = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
        return window_start <= now_local < window_end
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto_send: window check failed (%s) — allowing send", exc)
        return True


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

    if not _is_within_send_window(datetime.utcnow(), cfg):
        logger.info(
            "auto_send: outside send window [%s–%s %s] — skipping cycle",
            cfg.get("auto_send_window_start", "07:00"),
            cfg.get("auto_send_window_end", "19:00"),
            cfg.get("auto_send_timezone", "America/Detroit"),
        )
        return

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
    velocity_cap: int = int(get_settings().app_config.get("auto_send_velocity_cap", 25))
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
    if sent_last_hour >= velocity_cap:
        logger.warning(
            "auto_send: velocity cap reached for %s (%d sent in last hour, cap=%d) — skipping cycle",
            talent_key, sent_last_hour, velocity_cap,
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
            Draft.is_escalate == False,  # noqa: E712
            Draft.validation_failed != True,  # noqa: E712
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
        if sent_last_hour + sent_this_cycle >= velocity_cap:
            logger.info("auto_send: velocity cap hit mid-cycle for %s — stopping", talent_key)
            break

        # Already-sent guard
        if draft.reviewed_at is not None:
            continue

        # Pre-send validation gate
        from backend.services.validation import run_pre_send_checks
        ok, err = run_pre_send_checks(draft, db)
        if not ok:
            draft.validation_failed = True
            draft.validation_error = err
            db.add(draft)
            db.commit()
            logger.warning(
                "auto_send: validation failed for draft %d (%s): %s",
                draft.id, draft.talent_key, err,
            )
            continue

        # Thread count guard: skip if thread already has a real sent reply (not just our draft)
        if draft.thread_id:
            try:
                thread = service.users().threads().get(
                    userId="me", id=draft.thread_id, format="metadata"
                ).execute()
                messages = thread.get("messages", [])
                sent_messages = [m for m in messages if "DRAFT" not in m.get("labelIds", [])]
                if len(sent_messages) > 1:
                    logger.info(
                        "auto_send: thread %s has %d sent messages — dismissing draft %d",
                        draft.thread_id, len(sent_messages), draft.id,
                    )
                    draft.dismissed = True
                    db.add(draft)
                    db.commit()
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_send: thread check failed for draft %d (%s): %s — proceeding without check",
                    draft.id, draft.talent_key, exc,
                )
                # Fall through to _send_draft. reviewed_at guard above prevents double-send.

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
