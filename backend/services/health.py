"""
System health score engine.

Computes a 0.0–1.0 score reflecting how well the automation pipeline is running.
Checks: triage reliability, draft queue liveness, last successful draft, token health,
poll errors, and SOP integrity.

Score guide:
  1.0   Everything nominal
  0.8+  Minor issues, system still working
  0.6+  Degraded — some automation failing
  < 0.6 Critical — drafting likely broken, human review needed
"""
from __future__ import annotations

import hashlib
import logging
import pathlib
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models.db import AppState, Draft, DraftStatus, PollHealth, ProcessedEmail, TalentToken

logger = logging.getLogger(__name__)

_SOP_MD_PATH = pathlib.Path(__file__).resolve().parents[2] / "sheets" / "sop.md"

# AppState keys
_KEY_QUEUE_HEARTBEAT = "draft_queue_last_run_at"
_KEY_LAST_DRAFT = "last_successful_draft_at"
_KEY_SOP_HASH = "sop_md_hash"


# ── Heartbeat writers (called by poller/cron) ─────────────────────────────────

def record_queue_heartbeat(db: Session) -> None:
    """Call at the end of every _run_draft_queue execution."""
    _set_state(db, _KEY_QUEUE_HEARTBEAT, datetime.utcnow().isoformat())


def record_successful_draft(db: Session) -> None:
    """Call whenever a Draft row is successfully created."""
    _set_state(db, _KEY_LAST_DRAFT, datetime.utcnow().isoformat())


def _set_state(db: Session, key: str, value: str) -> None:
    try:
        row = db.query(AppState).filter(AppState.key == key).first()
        if not row:
            row = AppState(key=key)
        row.value_text = value
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health._set_state failed for %s: %s", key, exc)


def _get_state(db: Session, key: str) -> str | None:
    row = db.query(AppState).filter(AppState.key == key).first()
    return row.value_text if row else None


# ── SOP hash ──────────────────────────────────────────────────────────────────

def get_sop_hash() -> str | None:
    if not _SOP_MD_PATH.exists():
        return None
    return hashlib.md5(_SOP_MD_PATH.read_bytes()).hexdigest()


def check_and_store_sop_hash(db: Session) -> dict:
    """Check if SOP file has changed since last recorded hash. Returns status dict."""
    current = get_sop_hash()
    stored = _get_state(db, _KEY_SOP_HASH)

    if current is None:
        return {"ok": False, "reason": "sop.md file not found", "changed": False}

    if stored is None:
        _set_state(db, _KEY_SOP_HASH, current)
        return {"ok": True, "reason": "hash stored for first time", "changed": False}

    if current != stored:
        logger.warning("SOP file changed — stored hash %s, current %s", stored[:8], current[:8])
        _set_state(db, _KEY_SOP_HASH, current)
        return {"ok": True, "reason": "sop.md changed — cache cleared", "changed": True}

    return {"ok": True, "reason": "sop.md unchanged", "changed": False}


# ── Health score computation ──────────────────────────────────────────────────

def compute_health_score(db: Session) -> dict:
    """
    Return a dict with score (0.0–1.0), component breakdown, and status strings.
    Each component is 0.0–1.0. Final score is weighted average.
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_ago = now - timedelta(hours=1)
    six_hours_ago = now - timedelta(hours=6)

    components = {}
    issues = []

    # ── 1. Triage reliability (weight: 0.30) ──────────────────────────────────
    emails_today = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= today_start).count()
    fallbacks_today = db.query(ProcessedEmail).filter(
        ProcessedEmail.processed_at >= today_start,
        ProcessedEmail.triage_reason.like("Triage fallback%"),
    ).count()
    if emails_today == 0:
        triage_score = 1.0  # no data yet today, not a fault
    else:
        fallback_rate = fallbacks_today / emails_today
        triage_score = max(0.0, 1.0 - (fallback_rate * 3))  # 33% fallback rate = 0.0
        if fallback_rate > 0.1:
            issues.append(f"High triage fallback rate: {fallback_rate:.0%} of today's emails failed GPT parsing")
    components["triage_reliability"] = round(triage_score, 3)

    # ── 2. Draft queue liveness (weight: 0.25) ────────────────────────────────
    heartbeat_str = _get_state(db, _KEY_QUEUE_HEARTBEAT)
    if heartbeat_str:
        try:
            last_hb = datetime.fromisoformat(heartbeat_str)
            minutes_since = (now - last_hb).total_seconds() / 60
            if minutes_since <= 5:
                queue_score = 1.0
            elif minutes_since <= 15:
                queue_score = 0.8
                issues.append(f"Draft queue hasn't run in {minutes_since:.0f} minutes")
            elif minutes_since <= 60:
                queue_score = 0.4
                issues.append(f"Draft queue stalled — last run {minutes_since:.0f} minutes ago")
            else:
                queue_score = 0.0
                issues.append(f"DRAFT QUEUE DOWN — no heartbeat for {minutes_since/60:.1f} hours")
        except ValueError:
            queue_score = 0.5
    else:
        queue_score = 0.5  # no data yet (fresh deploy)
    components["queue_liveness"] = round(queue_score, 3)

    # ── 3. Last successful draft freshness (weight: 0.25) ─────────────────────
    last_draft_str = _get_state(db, _KEY_LAST_DRAFT)
    if last_draft_str:
        try:
            last_draft = datetime.fromisoformat(last_draft_str)
            hours_since = (now - last_draft).total_seconds() / 3600
            if hours_since <= 2:
                draft_score = 1.0
            elif hours_since <= 6:
                draft_score = 0.8
                issues.append(f"No new drafts in {hours_since:.1f} hours")
            elif hours_since <= 24:
                draft_score = 0.4
                issues.append(f"No new drafts in {hours_since:.1f} hours — drafting may be broken")
            else:
                draft_score = 0.0
                issues.append(f"DRAFTING STOPPED — no new drafts for {hours_since:.0f} hours")
        except ValueError:
            draft_score = 0.5
    else:
        # Fall back to checking the DB directly
        recent_draft = db.query(Draft).filter(
            Draft.created_at >= six_hours_ago,
            Draft.status != DraftStatus.discarded,
        ).first()
        draft_score = 1.0 if recent_draft else 0.4
        if not recent_draft:
            issues.append("No recent drafts found in the last 6 hours")
    components["draft_freshness"] = round(draft_score, 3)

    # ── 4. Gmail token health (weight: 0.10) ──────────────────────────────────
    all_tokens = db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    failing = [t for t in all_tokens if (t.consecutive_failures or 0) >= 3]
    if not all_tokens:
        token_score = 0.5
    else:
        token_score = max(0.0, 1.0 - (len(failing) / len(all_tokens)))
        if failing:
            issues.append(f"{len(failing)} Gmail token(s) failing: {', '.join(t.talent_key for t in failing)}")
    components["token_health"] = round(token_score, 3)

    # ── 5. Poll error rate (weight: 0.10) ─────────────────────────────────────
    poll_rows = db.query(PollHealth).filter(PollHealth.polled_at >= today_start).all()
    if not poll_rows:
        poll_score = 1.0
    else:
        error_rate = sum(1 for p in poll_rows if p.error_message) / len(poll_rows)
        poll_score = max(0.0, 1.0 - (error_rate * 2))
        if error_rate > 0.2:
            issues.append(f"Poll error rate today: {error_rate:.0%}")
    components["poll_health"] = round(poll_score, 3)

    # ── Weighted final score ──────────────────────────────────────────────────
    weights = {
        "triage_reliability": 0.30,
        "queue_liveness": 0.25,
        "draft_freshness": 0.25,
        "token_health": 0.10,
        "poll_health": 0.10,
    }
    score = sum(components[k] * weights[k] for k in weights)

    if score >= 0.9:
        status = "healthy"
    elif score >= 0.7:
        status = "degraded"
    elif score >= 0.5:
        status = "warning"
    else:
        status = "critical"

    return {
        "score": round(score, 3),
        "status": status,
        "components": components,
        "issues": issues,
        "emails_today": emails_today,
        "fallback_rate": round(fallbacks_today / max(emails_today, 1), 3),
        "computed_at": now.isoformat(),
    }
