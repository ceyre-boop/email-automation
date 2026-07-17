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
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

import time

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, ExternalChannelReview, PollHealth, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services import reply as reply_svc
from backend.services import sheets as sheets_svc
from backend.services import triage as triage_svc
from backend.services.external_channel import detect_external_channel
from backend.services.inbox_sync import fetch_pending_bodies, sync_inbox_for_talent
from backend.services.oauth import TokenRefreshError
from backend.services.sop_parser import TalentProfile, get_active_profiles

logger = logging.getLogger(__name__)

BODY_FETCH_BATCH = 50       # max body-fetch pending rows per cycle

# Concurrency — reduced to prevent QueuePool exhaustion.
# Peak DB connections per poll: 1 parent + (MAX_TALENT_WORKERS × (1+MAX_CONCURRENT_EMAILS))
# = 1 + (5 × 4) = 21. Combined with draft queue (6) + other jobs (5) + HTTP (3) = ~35 peak,
# which fits within pool_size=10 + max_overflow=15 = 25 hard cap (connections time-share).
# Commit 3 target: release DB session before Gmail API I/O to drop this to ~10 peak.
MAX_CONCURRENT_EMAILS = 3    # was 10 — each holds a session during Gmail API I/O
MAX_TALENT_WORKERS = 5       # was 10 — halves talent-level parallelism

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


def _talent_profile_map(settings) -> dict[str, TalentProfile]:
    """Return a dict of lowercase talent_key → TalentProfile, excluding paused talents."""
    return {k.lower(): p for k, p in get_active_profiles(settings.talent_profiles).items()}


def _already_processed(db: Session, message_id: str) -> bool:
    """Return True if this gmail_message_id already has a ProcessedEmail record."""
    return bool(_batch_already_processed_ids(db, [message_id]))


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
    talent_map = _talent_profile_map(settings)
    draft_mode: bool = settings.app_config.get("reply", {}).get("draft_mode", True)

    active_tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712

    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0}

    if not active_tokens:
        logger.info("No active tokens — nothing to poll")
        return summary

    # Build per-talent job list, skipping talents with no sop.md profile
    jobs: list[tuple[int, TalentProfile, bool]] = []
    for token_row in active_tokens:
        profile = talent_map.get(token_row.talent_key.lower())
        if not profile:
            logger.warning("No sop.md profile for talent_key=%s — skipping", token_row.talent_key)
            continue
        jobs.append((token_row.id, profile, draft_mode))

    if not jobs:
        return summary

    summary_lock = threading.Lock()

    # Process all talents concurrently — each gets its own DB session via _poll_one_talent
    with ThreadPoolExecutor(max_workers=min(len(jobs), MAX_TALENT_WORKERS)) as executor:
        future_to_key = {
            executor.submit(_poll_one_talent, tid, profile, dm): profile.key
            for tid, profile, dm in jobs
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


def _poll_one_talent(token_row_id: int, profile: TalentProfile, draft_mode: bool) -> dict:
    """Process one talent's inbox in its own DB session. Returns per-talent summary."""
    talent_key = profile.key

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

        # Sweep ghost claim rows (score=0) older than 10 min — from crashed prior poll cycles
        try:
            db.query(ProcessedEmail).filter(
                ProcessedEmail.talent_key == talent_key,
                ProcessedEmail.score == 0,
                ProcessedEmail.processed_at < datetime.utcnow() - timedelta(minutes=10),
            ).delete(synchronize_session=False)
            db.commit()
        except Exception as _e:
            logger.warning("Ghost cleanup failed for %s: %s", talent_key, _e)
            db.rollback()

        talent_name = profile.full_name or talent_key
        minimum_rate = profile.minimum_rate_usd
        max_drafts = get_settings().app_config.get("guardian", {}).get("default_max_drafts_per_day")

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

            manager_name = profile.manager or ""
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
                        manager_name,
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


def _spam_sweep_for_talent(token_row, profile: TalentProfile, db: Session) -> int:
    """
    Scan the talent's SPAM folder and draft replies for score=3 emails.
    Score=1/2: write ProcessedEmail row to skip on next cycle.
    Score=3: triage + reply + Gmail draft, no label changes.
    Returns count of drafts created.
    """
    talent_key = profile.key
    messages = gmail_svc.list_spam_messages(token_row, db=db)
    if not messages:
        return 0

    all_ids = [m["id"] for m in messages]
    already_done = _batch_already_processed_ids(db, all_ids)
    pending = [m for m in messages if m["id"] not in already_done]
    if not pending:
        return 0

    talent_name = profile.full_name or talent_key
    minimum_rate = float(profile.minimum_rate_usd)
    service = gmail_svc._gmail_service(token_row, db)
    drafted = 0

    for msg in pending:
        message_id = msg["id"]
        try:
            detail = gmail_svc.get_message_detail(token_row, message_id, db=db, service=service)
            if not detail:
                continue
            subject = detail.get("subject", "")
            sender = detail.get("sender", "")
            sender_domain = detail.get("sender_domain", "")
            body = detail.get("body_text", "")
            thread_id = detail.get("thread_id", msg.get("threadId", ""))
            message_id_header = detail.get("message_id_header", "")
            email_date = detail.get("email_date")

            triage_result = triage_svc.triage_email(
                talent_key=talent_key,
                talent_name=talent_name,
                minimum_rate=minimum_rate,
                subject=subject,
                sender=sender,
                sender_domain=sender_domain,
                body=body,
            )
            score = triage_result.get("score", 1)
            brand_name = triage_result.get("brand_name")
            proposed_rate = triage_result.get("proposed_rate")
            offer_type = triage_result.get("offer_type")
            reason = triage_result.get("reason", "")

            # External Channel Review — informational only, independent of score.
            _record_external_channel(db, talent_key, message_id, thread_id, sender, subject, body, email_date, service=service)

            if score != 3:
                _record_processed(
                    db, talent_key, message_id, thread_id, sender, subject,
                    score, brand_name, proposed_rate, offer_type, reason,
                    EmailStatus.archived if score == 1 else EmailStatus.flagged,
                    body_text=body, email_date=email_date, sender_domain=sender_domain,
                )
                db.commit()
                continue

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
                body_text=body,
            )
            if reply_result.get("is_escalate"):
                _record_processed(
                    db, talent_key, message_id, thread_id, sender, subject,
                    score, brand_name, proposed_rate, offer_type, reason,
                    EmailStatus.flagged,
                    body_text=body, email_date=email_date, sender_domain=sender_domain,
                )
                db.commit()
                continue

            draft_text = reply_result["draft_text"]
            cc_str = reply_result.get("cc_recipients")
            cc_list = [e.strip() for e in cc_str.split(",")] if cc_str else None

            try:
                gmail_draft_id = gmail_svc.create_gmail_draft(
                    token_row,
                    thread_id=thread_id,
                    reply_to=sender,
                    subject=subject,
                    body=draft_text,
                    cc=cc_list,
                    db=db,
                    in_reply_to=message_id_header or None,
                    service=service,
                )
            except gmail_svc.GmailDraftError as exc:
                logger.warning(
                    "spam_sweep: draft creation failed for %s / %s — status=%s reason=%s",
                    talent_key, message_id, exc.status, exc.reason,
                )
                continue

            db.add(Draft(
                talent_key=talent_key,
                gmail_message_id=message_id,
                thread_id=thread_id,
                sender=sender,
                subject=subject,
                brand_name=brand_name,
                proposed_rate=proposed_rate,
                offer_type=offer_type,
                draft_text=draft_text,
                cc_recipients=cc_str,
                gmail_draft_id=gmail_draft_id,
                message_id_header=message_id_header or None,
                status=DraftStatus.pending,
                triggered_by_job="spam_sweep",
            ))
            _record_processed(
                db, talent_key, message_id, thread_id, sender, subject,
                score, brand_name, proposed_rate, offer_type, reason,
                EmailStatus.draft_saved,
                body_text=body, email_date=email_date, sender_domain=sender_domain,
            )
            db.commit()
            drafted += 1
            logger.info("spam_sweep: drafted reply for %s / %s (%s)", talent_key, message_id, sender)

        except Exception as exc:  # noqa: BLE001
            logger.warning("spam_sweep error for %s / %s: %s", talent_key, message_id, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return drafted


def _process_message_in_thread(
    token_row_id: int,
    message_id: str,
    talent_key: str,
    talent_name: str,
    minimum_rate: float,
    draft_mode: bool,
    manager_name: str = "",
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
            manager_name=manager_name,
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
    message_id: str,
    talent_key: str,
    talent_name: str,
    minimum_rate: float,
    draft_mode: bool,
    summary: dict,
    manager_name: str = "",
    service=None,
):
    import time as _time
    from sqlalchemy.exc import IntegrityError

    # Atomically claim this message before any GPT/Gmail API calls.
    # If a concurrent worker already claimed it the INSERT fails on the unique constraint
    # and we skip immediately — this prevents duplicate drafts from overlapping poll workers.
    claim = ProcessedEmail(
        talent_key=talent_key,
        gmail_message_id=message_id,
        thread_id="",
        sender="",
        subject="",
        score=0,
        status=EmailStatus.processing,
    )
    try:
        db.add(claim)
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("Skipping %s / %s — already claimed by another worker", talent_key, message_id)
        return

    detail = gmail_svc.get_message_detail(token_row, message_id, db=db, service=service)
    if not detail:
        logger.warning("Empty detail for %s / %s — recording error to prevent infinite retry", talent_key, message_id)
        _record_processed(
            db, talent_key, message_id, "", "", "",
            2, "", 0.0, "Unknown", "Gmail fetch failed — message unavailable or deleted",
            EmailStatus.error,
        )
        db.commit()
        summary["errors"] += 1
        return

    # SOP Rule 1: only process emails currently in INBOX.
    # Re-check after fetch — the email may have been moved by another worker or manually.
    if "INBOX" not in detail.get("label_ids", []):
        logger.info(
            "Skipping %s / %s — no longer in INBOX (labels: %s)",
            talent_key, message_id, detail.get("label_ids", []),
        )
        # Update the claim row so we don't retry this message next cycle
        claim_row = db.query(ProcessedEmail).filter(
            ProcessedEmail.gmail_message_id == message_id,
            ProcessedEmail.status == EmailStatus.processing,
        ).first()
        if claim_row:
            claim_row.status = EmailStatus.archived
            claim_row.triage_reason = "Skipped — not in INBOX at processing time (SOP Rule 1)"
            db.add(claim_row)
            db.commit()
        summary["processed"] += 1
        return

    thread_id = detail.get("thread_id", "")
    subject = detail.get("subject", "")
    sender = detail.get("sender", "")
    sender_domain = detail.get("sender_domain", "")
    body = detail.get("body_text", "")
    email_date = detail.get("email_date")
    message_id_header = detail.get("message_id_header", "")

    # Computed context fields
    email_length = len(body) if body else 0
    has_links = bool(body and ("http://" in body or "https://" in body))
    has_attachments = bool(detail.get("has_attachments", False))

    # External Channel Review — informational only, independent of score routing.
    # Runs BEFORE the ongoing-thread guardrail so thread replies (where brands most
    # often ask to move to WhatsApp/Discord) are scanned too. Own try/except inside.
    # Filtered to initial inbound / first response (thread size <= 2).
    _record_external_channel(db, talent_key, message_id, thread_id, sender, subject, body, email_date, service=service)

    # ── Guardrail: ongoing thread with existing draft/sent work → manual only ──
    if thread_id:
        existing_thread_activity = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.thread_id == thread_id,
                ProcessedEmail.gmail_message_id != message_id,
                ProcessedEmail.status.in_([EmailStatus.draft_saved, EmailStatus.sent]),
            )
            .first()
        )
        existing_thread_draft = (
            db.query(Draft)
            .filter(
                Draft.thread_id == thread_id,
                Draft.gmail_message_id != message_id,
                Draft.status.in_([DraftStatus.pending, DraftStatus.sent, DraftStatus.approved]),
            )
            .first()
        )
        # Also check Gmail directly: if the thread has any SENT message the talent
        # or a manager already replied manually (no DB record exists for those threads).
        thread_manually_handled = gmail_svc.thread_has_prior_sent_reply(service, thread_id)

        if existing_thread_activity or existing_thread_draft or thread_manually_handled:
            reason = "Ongoing thread — prior sent activity detected. Human review required."
            # SOP Rule 10B: leave Gmail untouched — no labels, no inbox removal
            _record_processed(
                db, talent_key, message_id, thread_id, sender, subject,
                2, "", 0.0, "Human Admin Required", reason, EmailStatus.flagged,
                body_text=body, email_date=email_date,
            )
            db.commit()
            _safe_log_sheet(talent_key, sender, subject, 2, "", 0.0, "Human Admin Required", "flagged", reason)
            summary["flagged"] += 1
            summary["processed"] += 1
            return

    # ── Triage ───────────────────────────────────────────────────────────────
    _triage_start = _time.monotonic()
    triage_result = triage_svc.triage_email(
        talent_key=talent_key,
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        sender_domain=sender_domain,
        body=body,
    )
    time_to_classify_ms = int((_time.monotonic() - _triage_start) * 1000)
    score = triage_result["score"]
    reason = triage_result["reason"]

    offer_type = triage_result["offer_type"]
    proposed_rate = triage_result["proposed_rate_usd"]
    brand_name = triage_result["brand_name"]
    sentiment_score = triage_result.get("sentiment_score")
    urgency_score = triage_result.get("urgency_score")
    risk_score = triage_result.get("risk_score")
    alternatives_considered = triage_result.get("alternatives_considered", "")

    _extra = dict(
        sender_domain=sender_domain,
        email_length=email_length,
        sentiment_score=sentiment_score,
        urgency_score=urgency_score,
        risk_score=risk_score,
        is_thread=bool(thread_id),
        has_attachments=has_attachments,
        has_links=has_links,
        alternatives_considered=alternatives_considered,
        time_to_classify_ms=time_to_classify_ms,
    )

    # ── Score 1 → Spam (Option C) ───────────────────────────────────────────
    if score == 1:
        logger.info(
            "SPAM: %s / %s from %s — reason: %s",
            talent_key, message_id, sender, reason,
        )
        # SOP Rule 10C: atomic — INBOX removal + Spam label in one API call
        gmail_svc.archive_as_spam(token_row, message_id, db=db, service=service)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.archived,
            body_text=body, email_date=email_date, **_extra,
        )
        db.commit()
        _safe_log_sheet(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, "archived", reason)
        summary["archived"] += 1

    # ── Score 2 → Human review (Option B) ───────────────────────────────────
    elif score == 2:
        # SOP Rule 10B: leave Gmail completely untouched — no labels, no inbox removal.
        # Event invites (ignore_leave_inbox=True) are already covered by this same rule.
        if triage_result.get("ignore_leave_inbox"):
            logger.info("ignore_leave_inbox for %s / %s — leaving in INBOX (SOP Rules 7 & 8)", talent_key, message_id)
        else:
            logger.info("Score 2 for %s / %s — leaving in INBOX untouched (SOP Rule 10B)", talent_key, message_id)
        _record_processed(
            db, talent_key, message_id, thread_id, sender, subject,
            score, brand_name, proposed_rate, offer_type, reason, EmailStatus.flagged,
            body_text=body, email_date=email_date, **_extra,
        )
        db.commit()
        _safe_log_sheet(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, "flagged", reason)
        summary["flagged"] += 1

    # ── Score 3 → Draft reply ───────────────────────────────────────────────────
    elif score == 3:
        # Guard 1: duplicate draft in DB
        existing_draft = (
            db.query(Draft)
            .filter(Draft.gmail_message_id == message_id)
            .first()
        )
        # Guard 2: thread already has a human-sent reply in Gmail (covers conversations
        # the system never saw — no DB records exist for manually-handled threads).
        thread_already_replied = gmail_svc.thread_has_prior_sent_reply(service, thread_id)
        if thread_already_replied:
            logger.info(
                "Thread %s for %s already has a sent reply — leaving in INBOX untouched (SOP Rule 10B)",
                thread_id, talent_key,
            )
            # SOP Rule 10B: leave Gmail untouched
            _record_processed(
                db, talent_key, message_id, thread_id, sender, subject,
                2, brand_name, proposed_rate, "Human Admin Required",
                "Ongoing thread — prior reply detected. Human review required.",
                EmailStatus.flagged, body_text=body, email_date=email_date,
            )
            db.commit()
            summary["flagged"] += 1
            summary["processed"] += 1
            return

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

        _draft_start = _time.monotonic()
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
            body_text=body,
        )
        time_to_draft_ms = int((_time.monotonic() - _draft_start) * 1000)
        draft_text = reply_result["draft_text"]
        is_escalate = reply_result["is_escalate"]
        escalate_reason = reply_result.get("escalate_reason")
        cc_str = reply_result.get("cc_recipients")
        cc_list = gmail_svc.parse_cc_recipients(cc_str) if cc_str else None

        # Save as Gmail Draft in the talent's inbox (unless GPT escalated)
        gmail_draft_id: str | None = None
        if not is_escalate and draft_mode:
            try:
                gmail_draft_id = gmail_svc.create_gmail_draft(
                    token_row,
                    thread_id=thread_id,
                    reply_to=sender,
                    subject=subject,
                    body=draft_text,
                    cc=cc_list or None,
                    db=db,
                    in_reply_to=message_id_header or None,
                    service=service,
                )
            except gmail_svc.GmailDraftError as exc:
                # Gmail API failed — escalate so it routes to human review instead of
                # sitting as a phantom "pending" draft that will fail on approve.
                logger.warning(
                    "Gmail draft creation failed for %s / %s — status=%s reason=%s — escalating",
                    talent_key, message_id, exc.status, exc.reason,
                )
                is_escalate = True
                escalate_reason = f"Gmail draft creation failed (status={exc.status} reason={exc.reason})"

        if is_escalate:
            # ── Option B: GPT escalated or Gmail draft creation failed ──────────
            # SOP Rule 10B: leave Gmail completely untouched — no labels, no inbox removal.
            # Do NOT create a DB Draft row (avoids phantom unsendable drafts on the dashboard).
            logger.info(
                "Score 3 escalated for %s / %s (%s) — leaving in INBOX untouched (SOP Rule 10B)",
                talent_key, message_id, escalate_reason,
            )
            _record_processed(
                db, talent_key, message_id, thread_id, sender, subject,
                score, brand_name, proposed_rate, offer_type,
                escalate_reason or reason, EmailStatus.flagged,
                body_text=body, email_date=email_date,
                time_to_draft_ms=time_to_draft_ms, **_extra,
            )
            db.commit()
            _safe_log_sheet(
                talent_key, sender, subject, score, brand_name, proposed_rate,
                offer_type, "escalated", escalate_reason or reason,
            )
            summary["flagged"] += 1

        else:
            # ── Option A: real Gmail draft created ──────────────────────────────
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
                cc_recipients=cc_str,
                gmail_draft_id=gmail_draft_id,
                message_id_header=message_id_header or None,
                status=DraftStatus.pending,
                is_escalate=False,
                escalate_reason=None,
            )
            db.add(draft_row)
            _record_processed(
                db, talent_key, message_id, thread_id, sender, subject,
                score, brand_name, proposed_rate, offer_type, reason, EmailStatus.draft_saved,
                body_text=body, email_date=email_date,
                time_to_draft_ms=time_to_draft_ms, **_extra,
            )
            db.commit()

            # Record successful draft for system health score
            try:
                from backend.services.health import record_successful_draft
                record_successful_draft(db)
            except Exception:  # noqa: BLE001
                pass

            # SOP Rule 11 Option A: remove INBOX at draft creation (label applied post-send only)
            gmail_svc.remove_from_inbox(token_row, message_id, db=db, service=service)
            _safe_log_sheet(
                talent_key, sender, subject, score, brand_name, proposed_rate,
                offer_type, "draft_saved", reason,
            )
            summary["drafted"] += 1

    summary["processed"] += 1


def _safe_log_sheet(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, status_label, reason):
    """Log to Google Sheets — failure is non-fatal."""
    try:
        sheets_svc.log_email(talent_key, sender, subject, score, brand_name, proposed_rate, offer_type, status_label, reason)
    except Exception as exc:
        logger.warning("Sheets log failed for %s / %s (non-fatal): %s", talent_key, subject, exc)


def _record_external_channel(db, talent_key, message_id, thread_id, sender, subject, body, email_date, service=None):
    """Upsert an ExternalChannelReview row when the sender asks to move to WhatsApp/Discord.

    Informational only — separate from _record_processed and the score routing. Fully
    isolated: writes to its own table, never touches ProcessedEmail/drafts/labels/send.
    Own try/except so a failure here can never disrupt triage or draft handling.

    Only flags EARLY conversation — thread message count 1 (initial inbound) or 2
    (first response). Deals already several replies deep are skipped, so the review
    queue surfaces new handoff requests, not conversations already moving forward.
    """
    try:
        channel = detect_external_channel(subject, sender, body)
        if not channel:
            return
        # Initial inbound / first response only. Count is fetched lazily (only after a
        # channel match) to avoid an extra Gmail call on every email. On error (None)
        # we default to flagging — better to over-surface than silently drop.
        if service is not None and thread_id:
            count = gmail_svc.get_thread_message_count(service, thread_id)
            if count is not None and count > 2:
                return
        exists = (
            db.query(ExternalChannelReview)
            .filter(ExternalChannelReview.gmail_message_id == message_id)
            .first()
        )
        if exists:
            return
        db.add(ExternalChannelReview(
            gmail_message_id=message_id,
            thread_id=thread_id,
            talent_key=talent_key,
            sender=sender,
            subject=subject,
            body_text=body or None,
            channel_requested=channel,
            received_at=email_date,
        ))
        db.commit()
        logger.info("External Channel Review flagged (%s) for %s / %s", channel, talent_key, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("External channel flag failed for %s / %s (non-fatal): %s", talent_key, message_id, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


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
    sender_domain: str | None = None,
    email_length: int | None = None,
    sentiment_score: int | None = None,
    urgency_score: int | None = None,
    risk_score: int | None = None,
    is_thread: bool | None = None,
    has_attachments: bool | None = None,
    has_links: bool | None = None,
    alternatives_considered: str | None = None,
    time_to_classify_ms: int | None = None,
    time_to_draft_ms: int | None = None,
    human_override_occurred: bool = False,
):
    # The claim row was pre-inserted at the start of _process_one_message.
    # Update it in-place rather than inserting a duplicate.
    existing = db.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == message_id).first()
    if existing:
        existing.talent_key = talent_key
        existing.thread_id = thread_id
        existing.sender = sender
        existing.subject = subject
        existing.score = score
        existing.brand_name = brand_name
        existing.proposed_rate = proposed_rate
        existing.offer_type = offer_type
        existing.triage_reason = reason
        existing.body_text = body_text or None
        existing.email_date = email_date
        existing.processed_at = datetime.utcnow()
        existing.status = status
        existing.sender_domain = sender_domain
        existing.email_length = email_length
        existing.sentiment_score = sentiment_score
        existing.urgency_score = urgency_score
        existing.risk_score = risk_score
        existing.is_thread = is_thread
        existing.has_attachments = has_attachments
        existing.has_links = has_links
        existing.alternatives_considered = alternatives_considered or None
        existing.time_to_classify_ms = time_to_classify_ms
        existing.time_to_draft_ms = time_to_draft_ms
        existing.human_override_occurred = human_override_occurred
    else:
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
            sender_domain=sender_domain,
            email_length=email_length,
            sentiment_score=sentiment_score,
            urgency_score=urgency_score,
            risk_score=risk_score,
            is_thread=is_thread,
            has_attachments=has_attachments,
            has_links=has_links,
            alternatives_considered=alternatives_considered or None,
            time_to_classify_ms=time_to_classify_ms,
            time_to_draft_ms=time_to_draft_ms,
            human_override_occurred=human_override_occurred,
        )
        db.add(row)
