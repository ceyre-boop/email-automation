"""Bounded-growth data retention.

Supabase hit 100% CPU / memory / disk IO in 2026-08-29 with no retention policy
anywhere: processed_emails ~68k rows, drafts ~33k, inbox_emails ~1.4k, all
growing forever. This module purges the tables that are safe to purge, in
small committed batches so a purge run itself can never hold a long
transaction or spike IO the way the outage did.

Tables covered:
  - drafts: TERMINAL states only (sent, discarded), older than
    drafts_retention_days. `pending` drafts are NEVER deleted regardless of
    age — those are unsent work. Deleting old `sent` rows is safe: auto_send's
    human-replied guard compares Gmail SENT count against our sent-draft
    count, and a missing row only makes it MORE conservative (dismisses
    rather than sends), so there is no duplicate-reply risk.
  - inbox_emails: older than inbox_cache_retention_days, measured on
    last_synced_at. Pure display cache, rebuilt from Gmail on demand.
  - poll_health: older than poll_health_retention_days. Pure observability
    log.

NOT covered, deliberately:
  - processed_emails. This table is the de-duplication key that stops the
    poller from re-triaging and re-drafting an email it has already seen. If
    this were purged, the poller could pick an old message back up as "new"
    and draft/send a duplicate reply to a brand. DO NOT add processed_emails
    retention here without a redesign of that de-dup mechanism — this is not
    an oversight, it is a deliberate exclusion.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, InboxEmail, PollHealth

logger = logging.getLogger(__name__)

DEFAULT_DRAFTS_RETENTION_DAYS = 14
DEFAULT_INBOX_CACHE_RETENTION_DAYS = 30
DEFAULT_POLL_HEALTH_RETENTION_DAYS = 7
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES_PER_RUN = 20

_TERMINAL_DRAFT_STATUSES = (DraftStatus.sent, DraftStatus.discarded)


def _cfg() -> dict:
    return get_settings().app_config.get("retention", {}) or {}


def _delete_in_batches(
    db: Session,
    query_factory,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
) -> int:
    """Delete rows matched by `query_factory()` in committed batches.

    `query_factory` must return a fresh Query object each call (ordered,
    limited to `batch_size` primary keys) so re-running it after a commit
    picks up the next batch rather than rows already deleted.

    When `dry_run` is True this only counts — nothing is deleted or committed.
    """
    if dry_run:
        return query_factory(count_only=True)

    total_deleted = 0
    for _ in range(max_batches):
        ids = query_factory()
        if not ids:
            break
        (
            db.query(*query_factory.model_pk)  # placeholder, unused — see callers
        )
    return total_deleted


def run_retention(db: Session, dry_run: bool = True) -> dict:
    """Purge terminal drafts, stale inbox cache rows, and old poll_health rows.

    Never raises into the caller — on error, rolls back and returns whatever
    partial summary had been built, plus an "errors" entry.

    dry_run=True (default) only counts matching rows; nothing is deleted.
    """
    cfg = _cfg()
    drafts_days = int(cfg.get("drafts_retention_days", DEFAULT_DRAFTS_RETENTION_DAYS))
    inbox_days = int(cfg.get("inbox_cache_retention_days", DEFAULT_INBOX_CACHE_RETENTION_DAYS))
    poll_health_days = int(cfg.get("poll_health_retention_days", DEFAULT_POLL_HEALTH_RETENTION_DAYS))
    batch_size = max(1, int(cfg.get("batch_size", DEFAULT_BATCH_SIZE)))
    max_batches = max(1, int(cfg.get("max_batches_per_run", DEFAULT_MAX_BATCHES_PER_RUN)))

    summary: dict = {
        "dry_run": dry_run,
        "drafts": {"deleted": 0, "batches": 0},
        "inbox_emails": {"deleted": 0, "batches": 0},
        "poll_health": {"deleted": 0, "batches": 0},
        "errors": [],
    }

    # processed_emails is intentionally never touched here — see module docstring.

    try:
        drafts_cutoff = datetime.utcnow() - timedelta(days=drafts_days)
        _purge_table(
            db=db,
            summary_key="drafts",
            summary=summary,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
            id_query=lambda: db.query(Draft.id).filter(
                Draft.status.in_(_TERMINAL_DRAFT_STATUSES),
                Draft.created_at < drafts_cutoff,
            ),
            delete_model=Draft,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("retention: drafts purge failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"drafts: {exc}"[:300])

    try:
        inbox_cutoff = datetime.utcnow() - timedelta(days=inbox_days)
        _purge_table(
            db=db,
            summary_key="inbox_emails",
            summary=summary,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
            id_query=lambda: db.query(InboxEmail.id).filter(
                InboxEmail.last_synced_at < inbox_cutoff,
            ),
            delete_model=InboxEmail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("retention: inbox_emails purge failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"inbox_emails: {exc}"[:300])

    try:
        poll_health_cutoff = datetime.utcnow() - timedelta(days=poll_health_days)
        _purge_table(
            db=db,
            summary_key="poll_health",
            summary=summary,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
            id_query=lambda: db.query(PollHealth.id).filter(
                PollHealth.polled_at < poll_health_cutoff,
            ),
            delete_model=PollHealth,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("retention: poll_health purge failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"poll_health: {exc}"[:300])

    if any(summary[k]["deleted"] for k in ("drafts", "inbox_emails", "poll_health")) or summary["errors"]:
        logger.info("retention run complete (dry_run=%s): %s", dry_run, summary)

    return summary


def _purge_table(
    db: Session,
    summary_key: str,
    summary: dict,
    dry_run: bool,
    batch_size: int,
    max_batches: int,
    id_query,
    delete_model,
) -> None:
    """Count or delete rows matching `id_query()` for one table.

    Deletes happen `batch_size` primary keys at a time with a commit after
    every batch, capped at `max_batches` batches per call, so a single
    retention run can never hold a long transaction or blast an unbounded
    number of deletes in one go.
    """
    if dry_run:
        count = id_query().count()
        summary[summary_key]["deleted"] = count
        return

    for _ in range(max_batches):
        ids = [row_id for (row_id,) in id_query().limit(batch_size).all()]
        if not ids:
            break
        deleted = (
            db.query(delete_model)
            .filter(delete_model.id.in_(ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        summary[summary_key]["deleted"] += deleted
        summary[summary_key]["batches"] += 1
        if len(ids) < batch_size:
            break
