"""
Inbox cache sync service.

Keeps the inbox_emails table as a server-side mirror of each talent's Gmail inbox.
Called from the poller every 5 minutes — the dashboard then reads from the DB
instead of hitting Gmail on every load.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy.orm import Session

from backend.models.db import InboxEmail, ProcessedEmail
from backend.services import gmail as gmail_svc

logger = logging.getLogger(__name__)

MAX_INBOX_RESULTS = 100   # sync up to 100 messages per cycle (was 50)
BODY_FETCH_BATCH = 20
HEADER_WORKERS = 20       # parallel header fetches (was 10)
BODY_WORKERS = 20         # parallel body fetches (was 10)


def sync_inbox_for_talent(token_row, db: Session) -> dict:
    """
    Sync one talent's Gmail inbox into the inbox_emails cache table.
    - New messages: fetch headers in parallel, insert row.
    - Existing messages: refresh is_unread / label_ids / last_synced_at.
    - Both: backfill triage data from ProcessedEmail if score was NULL.
    Returns {"upserted": N, "updated": N, "errors": N}
    """
    talent_key = token_row.talent_key.lower()
    summary = {"upserted": 0, "updated": 0, "errors": 0}

    stubs = gmail_svc.list_inbox_messages(token_row, max_results=MAX_INBOX_RESULTS)
    # Persist any token refresh back to DB
    db.add(token_row)

    if not stubs:
        db.commit()
        return summary

    current_ids = {s["id"] for s in stubs}
    stub_map = {s["id"]: s for s in stubs}

    # Load existing cache rows for these IDs
    existing_rows = (
        db.query(InboxEmail)
        .filter(
            InboxEmail.talent_key == talent_key,
            InboxEmail.gmail_message_id.in_(current_ids),
        )
        .all()
    )
    existing_map: dict[str, InboxEmail] = {r.gmail_message_id: r for r in existing_rows}
    new_ids = current_ids - set(existing_map.keys())

    # Fetch headers for new messages in parallel
    headers_map: dict[str, dict] = {}
    if new_ids:
        with ThreadPoolExecutor(max_workers=HEADER_WORKERS) as pool:
            future_to_id = {
                pool.submit(gmail_svc.get_message_headers, token_row, mid): mid
                for mid in new_ids
            }
            for future in as_completed(future_to_id):
                mid = future_to_id[future]
                try:
                    result = future.result()
                    if result:
                        headers_map[mid] = result
                except Exception as exc:
                    logger.warning("Header fetch failed for %s / %s: %s", talent_key, mid, exc)
                    summary["errors"] += 1

    # Load triage data from ProcessedEmail for all current IDs
    triage_rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.gmail_message_id.in_(current_ids))
        .all()
    )
    triage_map = {r.gmail_message_id: r for r in triage_rows}

    now = datetime.utcnow()

    for mid in current_ids:
        triage = triage_map.get(mid)
        existing = existing_map.get(mid)

        if existing:
            existing.last_synced_at = now
            hdr = headers_map.get(mid)
            if hdr:
                existing.is_unread = "UNREAD" in hdr.get("label_ids", [])
                existing.label_ids = ",".join(hdr.get("label_ids", []))
            if triage:
                # Always sync triage fields so TRASH/DRAFT status stays current
                if existing.score is None:
                    existing.score = triage.score
                    existing.brand_name = triage.brand_name
                    existing.proposed_rate = triage.proposed_rate
                    existing.offer_type = triage.offer_type
                    existing.triage_reason = triage.triage_reason
                existing.triage_status = str(triage.status) if triage.status else None
            summary["updated"] += 1
        else:
            hdr = headers_map.get(mid, {})
            stub = stub_map.get(mid, {})
            row = InboxEmail(
                talent_key=talent_key,
                gmail_message_id=mid,
                thread_id=hdr.get("thread_id") or stub.get("threadId", ""),
                sender=hdr.get("sender"),
                subject=hdr.get("subject"),
                snippet=hdr.get("snippet"),
                email_date=hdr.get("email_date"),
                is_unread="UNREAD" in hdr.get("label_ids", []),
                label_ids=",".join(hdr.get("label_ids", [])),
                body_text=None,
                body_fetched_at=None,
                score=triage.score if triage else None,
                brand_name=triage.brand_name if triage else None,
                proposed_rate=triage.proposed_rate if triage else None,
                offer_type=triage.offer_type if triage else None,
                triage_reason=triage.triage_reason if triage else None,
                triage_status=str(triage.status) if triage and triage.status else "unprocessed",
                first_seen_at=now,
                last_synced_at=now,
            )
            db.add(row)
            summary["upserted"] += 1

    db.commit()

    # ── Prune stale rows (emails no longer in inbox) ──────────────────────────
    # Only prune when Gmail returned a complete result set smaller than MAX_INBOX_RESULTS,
    # meaning we definitively know the full inbox contents. If we got a full page
    # (len == MAX_INBOX_RESULTS) there could be more messages we didn't fetch, so
    # we leave older cached rows alone to avoid false deletions.
    # Guard: never prune if current_ids is empty (would delete all cached rows).
    if current_ids and len(stubs) < MAX_INBOX_RESULTS:
        pruned = (
            db.query(InboxEmail)
            .filter(
                InboxEmail.talent_key == talent_key,
                InboxEmail.gmail_message_id.not_in(current_ids),
            )
            .delete(synchronize_session=False)
        )
        if pruned:
            db.commit()
            logger.info("Inbox sync %s: pruned %d stale cache rows", talent_key, pruned)
            summary["pruned"] = pruned

    return summary


def fetch_pending_bodies(token_row, db: Session, limit: int = BODY_FETCH_BATCH) -> int:
    """
    Lazy body fetch: grab up to `limit` cached emails that have no body yet
    and populate them from Gmail. Newest first.
    Returns number of bodies successfully fetched.
    """
    talent_key = token_row.talent_key.lower()

    pending = (
        db.query(InboxEmail)
        .filter(
            InboxEmail.talent_key == talent_key,
            InboxEmail.body_text.is_(None),
            InboxEmail.body_fetched_at.is_(None),
        )
        .order_by(InboxEmail.email_date.desc().nullslast())
        .limit(limit)
        .all()
    )

    if not pending:
        return 0

    row_map = {r.gmail_message_id: r for r in pending}
    fetched = 0

    with ThreadPoolExecutor(max_workers=BODY_WORKERS) as pool:
        future_to_id = {
            pool.submit(gmail_svc.get_message_detail, token_row, r.gmail_message_id): r.gmail_message_id
            for r in pending
        }
        for future in as_completed(future_to_id):
            mid = future_to_id[future]
            row = row_map[mid]
            try:
                detail = future.result()
                row.body_text = detail.get("body_text") or "" if detail else ""
                row.body_fetched_at = datetime.utcnow()
                row.body_fetch_attempts = (row.body_fetch_attempts or 0) + 1
                if detail:
                    fetched += 1
                else:
                    row.body_fetch_failed = True
            except Exception as exc:
                logger.warning("Body fetch failed for %s / %s: %s", talent_key, mid, exc)
                row.body_fetched_at = datetime.utcnow()
                row.body_fetch_attempts = (row.body_fetch_attempts or 0) + 1
                row.body_fetch_failed = True

    db.commit()
    return fetched
