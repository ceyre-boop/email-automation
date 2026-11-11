"""Sweep for stranded ``retry_pending`` rows.

Why this exists
---------------
A triage failure that is transient is parked as ``retry_pending`` and re-claimed by the
poller when it is due. But the poller only sees a message again if it shows up in the
Gmail ``INBOX + UNREAD`` listing. If a human reads or archives the email in the
meantime, the parked row is never re-claimed: it sits in ``retry_pending`` forever and
the brand email is neither triaged nor flagged. The same happens to a row whose
retries are exhausted but whose message left the listing.

``sweep_stranded_retries`` runs inside the ``claim_reaper`` job. For every row that is
overdue by more than ``OVERDUE_MINUTES`` (or has exhausted its retries) it re-fetches
the message BY ID, so the listing does not matter, and re-processes it. A row that is
still not finalised afterwards and has exhausted its retries is finalised directly as a
score-2 flagged row with an explicit ``triage_reason``.

``stranded_retry_clause`` / ``count_overdue`` are shared with the stall alarm and
``/health`` (``pipeline.retry_pending_overdue``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.db import EmailStatus, ProcessedEmail

logger = logging.getLogger(__name__)

OVERDUE_MINUTES = 15
DEFAULT_BATCH = 25


def _max_retries() -> int:
    from backend.services.poller import _triage_retry_config
    return _triage_retry_config()[0]


def stranded_retry_clause(now: datetime | None = None, max_retries: int | None = None):
    """SQL clause: retry_pending rows due > OVERDUE_MINUTES ago, never scheduled, or exhausted."""
    now = now or datetime.utcnow()
    if max_retries is None:
        max_retries = _max_retries()
    return (
        (ProcessedEmail.status == EmailStatus.retry_pending)
        & or_(
            ProcessedEmail.triage_next_retry_at.is_(None),
            ProcessedEmail.triage_next_retry_at < now - timedelta(minutes=OVERDUE_MINUTES),
            ProcessedEmail.triage_retry_count >= max_retries,
        )
    )


def count_overdue(db: Session, now: datetime | None = None) -> int:
    return db.query(ProcessedEmail).filter(stranded_retry_clause(now)).count()


def _finalize_exhausted(db: Session, message_id: str, max_retries: int) -> None:
    """Terminal score-2 flagged, with a reason a human can act on."""
    db.rollback()
    row = db.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == message_id).first()
    if row is None or row.status not in (EmailStatus.retry_pending, EmailStatus.processing):
        return
    row.score = 2
    row.status = EmailStatus.flagged
    row.triage_next_retry_at = None
    row.triage_reason = (
        f"Triage retries exhausted ({row.triage_retry_count or 0}/{max_retries} attempts) "
        "and the message could not be re-triaged — human review required."
    )
    row.processed_at = datetime.utcnow()
    db.add(row)
    db.commit()


def sweep_stranded_retries(db: Session, batch: int = DEFAULT_BATCH, now: datetime | None = None) -> dict:
    """Re-process stranded retry_pending rows by message id. Never raises."""
    from backend.core.config import get_settings
    from backend.services import gmail as gmail_svc
    from backend.services import poller
    from backend.services.inbox_routing import resolve_token_for_talent
    from backend.services.runtime_state import is_ai_enabled

    now = now or datetime.utcnow()
    summary = {"examined": 0, "reprocessed": 0, "finalized_exhausted": 0, "errors": 0, "skipped": 0}
    try:
        max_retries = _max_retries()
        rows = (
            db.query(ProcessedEmail.gmail_message_id, ProcessedEmail.talent_key,
                     ProcessedEmail.triage_retry_count)
            .filter(stranded_retry_clause(now, max_retries))
            .order_by(ProcessedEmail.processed_at.asc())
            .limit(max(1, batch))
            .all()
        )
        db.rollback()  # release the read transaction before the Gmail round-trips
    except Exception as exc:  # noqa: BLE001
        logger.error("retry_sweep: query failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {**summary, "error": str(exc)[:200]}

    if not rows:
        return summary

    ai_on = is_ai_enabled()
    settings = get_settings()
    profiles = {k.lower(): p for k, p in settings.talent_profiles.items()}
    draft_mode = settings.app_config.get("reply", {}).get("draft_mode", True)
    services: dict[str, tuple] = {}

    for message_id, talent_key, retry_count in rows:
        summary["examined"] += 1
        exhausted = (retry_count or 0) >= max_retries
        try:
            profile = profiles.get((talent_key or "").lower())
            if not ai_on or profile is None or profile.paused:
                # Cannot triage right now. An exhausted row would only be re-parked
                # forever, so give it a human; otherwise leave it for a later sweep.
                if exhausted:
                    _finalize_exhausted(db, message_id, max_retries)
                    summary["finalized_exhausted"] += 1
                else:
                    summary["skipped"] += 1
                continue

            if talent_key not in services:
                token = resolve_token_for_talent(db, talent_key)
                services[talent_key] = (token, gmail_svc.build_service(token, db) if token else None)
            token, service = services[talent_key]
            if token is None:
                if exhausted:
                    _finalize_exhausted(db, message_id, max_retries)
                    summary["finalized_exhausted"] += 1
                else:
                    summary["skipped"] += 1
                continue

            # Make the row due so the poller's atomic re-claim wins it (a concurrent
            # poll that wins instead is fine: we just skip below).
            db.query(ProcessedEmail).filter(
                ProcessedEmail.gmail_message_id == message_id,
                ProcessedEmail.status == EmailStatus.retry_pending,
            ).update({ProcessedEmail.triage_next_retry_at: now - timedelta(seconds=1)},
                     synchronize_session=False)
            db.commit()

            poller._process_one_message(
                db=db, token_row=token, service=service, message_id=message_id,
                talent_key=talent_key, talent_name=profile.full_name or talent_key,
                minimum_rate=profile.minimum_rate_usd, draft_mode=draft_mode,
                summary={"processed": 0, "archived": 0, "flagged": 0, "drafted": 0,
                         "errors": 0, "unrouted": 0},
                manager_name=profile.manager or "",
            )
            summary["reprocessed"] += 1
            logger.info("retry_sweep: re-processed stranded %s / %s by id", talent_key, message_id)

            db.expire_all()
            row = db.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == message_id).first()
            if exhausted and row is not None and row.status == EmailStatus.retry_pending:
                _finalize_exhausted(db, message_id, max_retries)
                summary["finalized_exhausted"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            logger.error("retry_sweep: %s / %s failed: %s", talent_key, message_id, exc, exc_info=True)
            try:
                db.rollback()
                row = db.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == message_id).first()
                if row is not None and row.status in (EmailStatus.processing, EmailStatus.retry_pending):
                    # Count the failed attempt so a poison message converges on the
                    # exhausted path instead of being retried forever.
                    row.triage_retry_count = (row.triage_retry_count or 0) + 1
                    if row.triage_retry_count >= max_retries:
                        _finalize_exhausted(db, message_id, max_retries)
                        summary["finalized_exhausted"] += 1
                    else:
                        row.status = EmailStatus.retry_pending
                        row.triage_next_retry_at = datetime.utcnow() + timedelta(
                            minutes=poller._triage_backoff_minutes(
                                row.triage_retry_count, poller._triage_retry_config()[1]
                            )
                        )
                        db.add(row)
                        db.commit()
            except Exception:  # noqa: BLE001
                logger.error("retry_sweep: could not record failure for %s", message_id, exc_info=True)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass

    if summary["reprocessed"] or summary["finalized_exhausted"] or summary["errors"]:
        logger.warning("retry_sweep: %s", summary)
    return summary
