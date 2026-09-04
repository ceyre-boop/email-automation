"""Stale send-claim reaper.

`auto_send` claims a draft (`send_claimed_at`) before calling Gmail, so two
workers can never send the same reply. If the worker then dies — a redeploy, an
OOM, a DB timeout mid-flight — the claim is never cleared and the draft is stuck
as `pending` forever. 77 drafts had accumulated this way, the oldest from Aug 3,
one or two a day.

The existing design refuses to auto-clear them, and it is right to: the worker
may have died AFTER Gmail accepted the send, so blindly releasing the claim and
retrying would send a brand a second copy of the same reply. That is a worse
failure than a stuck draft.

So this does not clear claims — it RESOLVES them, by asking Gmail what actually
happened:

  draft still in Gmail Drafts   → the send never went out → release the claim,
                                  the draft goes back in the queue
  draft gone + SENT in thread   → the send DID go out → mark it sent, so it is
                                  never re-sent and the record matches reality
  anything else / API error     → leave it alone and log for a human

Only the first two are actionable, and neither can produce a duplicate reply.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus
from backend.services import gmail as gmail_svc
from backend.services.inbox_routing import resolve_token_for_talent

logger = logging.getLogger(__name__)

DEFAULT_LEASE_MINUTES = 15
DEFAULT_BATCH = 25


def _cfg() -> dict:
    return get_settings().app_config.get("claim_reaper", {}) or {}


def reap_stale_claims(db: Session) -> dict:
    """Resolve expired send claims against Gmail. Never raises."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"enabled": False}

    lease = int(cfg.get("lease_minutes", DEFAULT_LEASE_MINUTES))
    batch = int(cfg.get("batch_size", DEFAULT_BATCH))
    cutoff = datetime.utcnow() - timedelta(minutes=lease)
    summary = {"examined": 0, "released": 0, "marked_sent": 0, "ambiguous": 0, "errors": 0}

    try:
        rows = (
            db.query(Draft)
            .filter(
                Draft.status == DraftStatus.pending,
                Draft.send_claimed_at.isnot(None),
                Draft.send_claimed_at < cutoff,
            )
            .order_by(Draft.send_claimed_at.asc())
            .limit(max(1, batch))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("claim_reaper: query failed: %s", exc)
        return {**summary, "error": str(exc)[:200]}

    for draft in rows:
        summary["examined"] += 1
        try:
            token = resolve_token_for_talent(db, draft.talent_key)
            if not token:
                summary["ambiguous"] += 1
                logger.warning(
                    "claim_reaper: no token for %s — draft #%s left claimed",
                    draft.talent_key, draft.id,
                )
                continue

            service = gmail_svc.build_service(token, db)

            if draft.gmail_draft_id and gmail_svc.draft_exists_in_gmail(
                token, draft.gmail_draft_id, db=db
            ):
                # The draft is still sitting in Gmail, so nothing was ever sent.
                # Releasing the claim is safe and puts it back in the queue.
                draft.send_claimed_at = None
                db.add(draft)
                db.commit()
                summary["released"] += 1
                logger.info(
                    "claim_reaper: released stale claim on draft #%s (%s) — Gmail draft "
                    "still exists, so no reply was sent",
                    draft.id, draft.talent_key,
                )
                continue

            if draft.thread_id and gmail_svc.thread_has_prior_sent_reply(service, draft.thread_id):
                # Gmail draft is gone AND the thread carries a SENT message: the
                # send completed and the worker died before recording it. Mark it
                # sent so it is never re-sent.
                draft.status = DraftStatus.sent
                draft.reviewed_at = draft.send_claimed_at or datetime.utcnow()
                draft.reviewed_by = "claim_reaper"
                draft.send_claimed_at = None
                db.add(draft)
                db.commit()
                summary["marked_sent"] += 1
                logger.info(
                    "claim_reaper: draft #%s (%s) was actually SENT before the worker "
                    "died — recorded as sent, not retried",
                    draft.id, draft.talent_key,
                )
                continue

            summary["ambiguous"] += 1
            logger.warning(
                "claim_reaper: draft #%s (%s) is ambiguous — no Gmail draft and no SENT "
                "message in thread %s. Left claimed for a human to check.",
                draft.id, draft.talent_key, draft.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            summary["errors"] += 1
            logger.error("claim_reaper: draft #%s failed: %s", draft.id, exc)

    if summary["examined"]:
        logger.info("claim_reaper: %s", summary)
    return summary
