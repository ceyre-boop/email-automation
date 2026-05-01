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
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy.orm import Session

import time

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, PollHealth, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services import reply as reply_svc
from backend.services import sheets as sheets_svc
from backend.services import triage as triage_svc
from backend.services.inbox_sync import fetch_pending_bodies, sync_inbox_for_talent
from backend.services.oauth import TokenRefreshError

logger = logging.getLogger(__name__)

BODY_FETCH_BATCH = 50       # max body-fetch pending rows per cycle (was 20)

# Concurrency: process this many emails per talent in parallel.
# Each worker opens its own DB session and makes independent Gmail + OpenAI calls.
# Keep at 3 to stay within Supabase connection pool limits (MAX_TALENT_WORKERS×3=12 max).
MAX_CONCURRENT_EMAILS = 3

# Concurrency: process this many talents in parallel.
# 4 concurrent talents × 3 email workers = 12 max DB connections — safe for Supabase.
MAX_TALENT_WORKERS = 4

# Per-talent poll lock — prevents a slow poll from overlapping the next cycle
_poll_locks: dict[str, bool] = {}

# Module-level session factory — created once and reused by all worker threads.
# Avoids the cost of building a fresh SQLAlchemy engine on every _process_message_in_thread call.
_session_factory = None
_session_factory_lock = threading.Lock()


def _get_session_factory():
    """Return the module-level session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        with _session_factory_lock:
            if _session_factory is None:
                from backend.models.db import get_session_factory
                _session_factory = get_session_factory()
    return _session_factory


def _talent_config_map(settings) -> dict[str, dict]:
    """Return a dict of talent_key → talent config dict from settings.json.

    Keys are normalised to lowercase so DB keys ('katrina') match config keys ('Katrina').
    """
    return {t["key"].lower(): t for t in settings.app_config.get("talents", [])}


def _batch_already_processed_ids(db: Session, message_ids: list[str]) -> set[str]:
    """Return the subset of message_ids already present in ProcessedEmail (single query)."""
    if not message_ids:
        return set()
    rows = (
        db.query(ProcessedEmail.gmail_message_id)
        .filter(ProcessedEmail.gmail_message_id.in_(message_ids))
        .all()
    )
    return {r.gmail_message_id for r in rows}


def _record_poll_health(db: Session, talent_key: str, emails_found: int, emails_processed: int,
                        error_message: str | None, duration_ms: int) -> None:
    """Insert a PollHealth row. Never raises."""
    try:
        db.add(PollHealth(
            talent_key=talent_key,
            emails_found=emails_found,
            emails_processed=emails_processed,
            error_message=error_message,
            duration_ms=duration_ms,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write PollHealth for %s: %s", talent_key, exc)


def poll_all_inboxes(db: Session) -> dict:
    """
    Main polling function. Processes all active connected talents concurrently.
    Returns a summary dict for logging/monitoring.
    """
    settings = get_settings()
    talent_map = _talent_config_map(settings)
    draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)

    active_tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712

    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    if not active_tokens:
        logger.info("No active tokens — nothing to poll")
        return summary

    # Build per-talent job list, skipping talents with no config
    jobs: list[tuple[int, dict, bool]] = []
    for token_row in active_tokens:
        talent_cfg = talent_map.get(token_row.talent_key.lower())
        if not talent_cfg:
            logger.warning("No config for talent_key=%s — skipping", token_row.talent_key)
            continue
        jobs.append((token_row.id, talent_cfg, draft_mode))

    if not jobs:
        return summary

    summary_lock = threading.Lock()

    # Process all talents concurrently — each gets its own DB session via _poll_one_talent
    with ThreadPoolExecutor(max_workers=min(len(jobs), MAX_TALENT_WORKERS)) as executor:
        future_to_key = {
            executor.submit(_poll_one_talent, tid, cfg, dm): cfg["key"]
            for tid, cfg, dm in jobs
        }
        for future in as_completed(future_to_key):
            talent_key = future_to_key[future]
            try:
                result = future.result()
                if result:
                    with summary_lock:
                        for k, v in result.items():
                            if k in summary:
                                summary[k] += v
            except Exception as exc:  # noqa: BLE001
                logger.error("Talent poll future error for %s: %s", talent_key, exc)
                with summary_lock:
                    summary["errors"] += 1

    logger.info("Poll complete: %s", summary)
    return summary


def _poll_one_talent(token_row_id: int, talent_cfg: dict, draft_mode: bool) -> dict:
    """Process one talent's inbox in its own DB session. Returns per-talent summary."""
    talent_key = talent_cfg["key"]

    if _poll_locks.get(talent_key):
        logger.info("Poll still running for %s — skipping cycle", talent_key)
        return {}

    Session = _get_session_factory()
    db = Session()
    _poll_locks[talent_key] = True
    poll_start = time.monotonic()
    emails_found = 0
    emails_processed_count = 0
    summary: dict[str, int] = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    try:
        token_row = db.query(TalentToken).filter(TalentToken.id == token_row_id).first()
        if not token_row:
            logger.error("TalentToken not found for id=%s (key=%s)", token_row_id, talent_key)
            return {}

        talent_name = talent_cfg.get("full_name", talent_key)
        minimum_rate = talent_cfg.get("minimum_rate_usd", 0)
        max_drafts = talent_cfg.get("max_drafts")

        # Enforce per-talent draft cap if configured
        if max_drafts is not None:
            existing_drafts = (
                db.query(Draft)
                .filter(Draft.talent_key == talent_key.lower(), Draft.status == DraftStatus.pending)
                .count()
            )
            if existing_drafts >= max_drafts:
                logger.info("Draft cap (%d) reached for %s — skipping poll", max_drafts, talent_key)
                return {}

        # ── Inbox cache sync ──────────────────────────────────────────────────
        try:
            sync_result = sync_inbox_for_talent(token_row, db)
            logger.info("Inbox sync %s: %s", talent_key, sync_result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Inbox sync failed for %s (non-fatal): %s", talent_key, exc)

        try:
            fetch_pending_bodies(token_row, db, limit=BODY_FETCH_BATCH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Body fetch failed for %s (non-fatal): %s", talent_key, exc)

        try:
            messages = gmail_svc.list_unread_inbox_messages(token_row, db=db)
        except TokenRefreshError as exc:
            logger.error("Token refresh rejected for %s — marking inactive: %s", talent_key, exc)
            token_row.active = False
            token_row.consecutive_failures = (token_row.consecutive_failures or 0) + 1
            token_row.last_error = str(exc)
            db.add(token_row)
            db.commit()
            summary["errors"] += 1
            _record_poll_health(db, talent_key, 0, 0, str(exc), int((time.monotonic() - poll_start) * 1000))
            return summary
        except Exception as exc:  # noqa: BLE001
            logger.error("Gmail list failed for %s: %s", talent_key, exc)
            token_row.consecutive_failures = (token_row.consecutive_failures or 0) + 1
            token_row.last_error = str(exc)
            db.add(token_row)
            db.commit()
            summary["errors"] += 1
            _record_poll_health(db, talent_key, 0, 0, str(exc), int((time.monotonic() - poll_start) * 1000))
            return summary

        emails_found = len(messages)

        if not get_settings().app_config.get("ai_enabled", True):
            logger.info("AI disabled — inbox synced for %s but triage skipped (ai_enabled=false)", talent_key)
        else:
            # Batch dedup: one IN query instead of N individual queries
            all_ids = [m["id"] for m in messages]
            already_done = _batch_already_processed_ids(db, all_ids)
            pending_ids = [mid for mid in all_ids if mid not in already_done]
            logger.info(
                "%s: %d unread, %d new to process (concurrent workers=%d)",
                talent_key, emails_found, len(pending_ids), MAX_CONCURRENT_EMAILS,
            )

            futures: dict[Future, str] = {}
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_EMAILS) as executor:
                for message_id in pending_ids:
                    future = executor.submit(
                        _process_message_in_thread,
                        token_row.id,
                        message_id,
                        talent_key,
                        talent_name,
                        minimum_rate,
                        draft_mode,
                    )
                    futures[future] = message_id

                for future in as_completed(futures):
                    message_id = futures[future]
                    try:
                        result = future.result()
                        emails_processed_count += 1
                        if result["status"] == "ok":
                            for k, v in result.get("summary", {}).items():
                                summary[k] += v
                        else:
                            logger.error(
                                "Worker error for %s / %s: %s",
                                talent_key, message_id, result.get("reason"),
                            )
                            summary["errors"] += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Future error for %s / %s: %s", talent_key, message_id, exc)
                        summary["errors"] += 1

        # Success — reset failure counters, record last_poll_at
        token_row.consecutive_failures = 0
        token_row.last_error = None
        token_row.last_poll_at = datetime.utcnow()
        db.add(token_row)
        db.commit()
        _record_poll_health(db, talent_key, emails_found, emails_processed_count, None,
                            int((time.monotonic() - poll_start) * 1000))
        return summary

    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled poll error for %s: %s", talent_key, exc)
        summary["errors"] += 1
        _record_poll_health(db, talent_key, emails_found, emails_processed_count, str(exc),
                            int((time.monotonic() - poll_start) * 1000))
        return summary
    finally:
        _poll_locks[talent_key] = False
        db.close()


def _process_message_in_thread(
    token_row_id: int,
    message_id: str,
    talent_key: str,
    talent_name: str,
    minimum_rate: float,
    draft_mode: bool,
) -> dict:
    """Process one email in a worker thread with its own DB session and TalentToken."""
    Session = _get_session_factory()
    db = Session()
    try:
        token_row = db.query(TalentToken).filter(TalentToken.id == token_row_id).first()
        if not token_row:
            return {"status": "error", "reason": f"TalentToken not found for {talent_key} (id={token_row_id})"}

        # Build the Gmail service once for this message's entire processing chain.
        # Subsequent gmail calls receive it directly — avoids two extra build() roundtrips.
        try:
            service = gmail_svc.build_service(token_row, db)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "reason": f"Gmail service build failed: {exc}"}

        summary: dict[str, int] = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}
        _process_one_message(
            db=db,
            token_row=token_row,
            service=service,
            message_id=message_id,
            talent_key=talent_key,
            talent_name=talent_name,
            minimum_rate=minimum_rate,
            draft_mode=draft_mode,
            summary=summary,
        )
        return {"status": "ok", "summary": summary}
    except Exception as exc:  # noqa: BLE001
        logger.error("Thread error for %s / %s: %s", talent_key, message_id, exc)
        return {"status": "error", "reason": str(exc)}
    finally:
        db.close()


def _process_one_message(
    db: Session,
    token_row,
    service,
    message_id: str,
    talent_key: str,
    talent_name: str,
    minimum_rate: float,
    draft_mode: bool,
    summary: dict,
):
    detail = gmail_svc.get_message_detail(token_row, message_id, db=db, service=service)
    if not detail:
        logger.warning("Empty detail for %s / %s — skipping", talent_key, message_id)
        return

    thread_id = detail.get("thread_id", "")
    subject = detail.get("subject", "")
    sender = detail.get("sender", "")
    sender_domain = detail.get("sender_domain", "")
    body = detail.get("body_text", "")
    email_date = detail.get("email_date")
    message_id_header = detail.get("message_id_header", "")

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
        gmail_svc.archive_message(token_row, message_id, db=db, service=service)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.archived,
            body_text=body, email_date=email_date,
        )
        db.commit()
        _safe_log_sheet(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, "archived", reason)
        summary["archived"] += 1

    # ── Score 2 or 3 → Draft reply ──────────────────────────────────────────────
    elif score >= 2:
        # Guard against duplicate drafts (e.g. if a previous poll cycle's DB commit
        # failed after the Gmail draft was saved, leaving the email unprocessed).
        existing_draft = (
            db.query(Draft)
            .filter(Draft.gmail_message_id == message_id)
            .first()
        )
        if existing_draft:
            logger.info(
                "Draft already exists for %s / %s (draft_id=%s) — recording processed to prevent re-evaluation",
                talent_key, message_id, existing_draft.id,
            )
            # Record a ProcessedEmail so _batch_already_processed_ids excludes this message
            # on the next poll cycle; without this the email would be re-evaluated forever.
            processed_already = (
                db.query(ProcessedEmail)
                .filter(ProcessedEmail.gmail_message_id == message_id)
                .first()
            )
            if not processed_already:
                _record_processed(
                    db, talent_key, message_id, thread_id, sender, subject,
                    score, brand_name, proposed_rate, offer_type, reason,
                    EmailStatus.draft_saved, body_text=body, email_date=email_date,
                )
                db.commit()
            summary["processed"] += 1
            return

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
                db=db,
                in_reply_to=message_id_header or None,
                service=service,
            )

        # Persist draft + processed record together, then commit once
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
            message_id_header=message_id_header or None,  # stored for In-Reply-To on approve
            status=DraftStatus.pending,
            is_escalate=is_escalate,
            escalate_reason=escalate_reason,
        )
        db.add(draft_row)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.draft_saved,
            body_text=body, email_date=email_date,
        )
        db.commit()

        status_label = "escalated" if is_escalate else "draft_saved"
        _safe_log_sheet(
            talent_key, sender, subject, score, brand_name, proposed_rate,
            offer_type, status_label, escalate_reason or reason,
        )
        summary["drafted"] += 1

    summary["processed"] += 1


def _safe_log_sheet(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, status_label, reason):
    """Log to Google Sheets — failure is non-fatal."""
    try:
        sheets_svc.log_email(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, status_label, reason)
    except Exception as exc:
        logger.warning("Sheets log failed for %s / %s (non-fatal): %s", talent_key, subject, exc)


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
    try:
        db.add(row)
        db.flush()
    except Exception:
        db.rollback()
