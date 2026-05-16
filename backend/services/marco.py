"""
Marco message generation service.

Reads analytics data, runs GPT-4o to generate natural-language system
status messages for the manager. Saves results to marco_messages table.

Marco talks to the manager the same way a smart analyst would — direct,
specific, actionable. Not generic alerts; specific insights.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, MarcoMessage, PollHealth, ProcessedEmail, TalentToken

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are Marco, an AI operations analyst for TABOOST talent management.
You monitor the email automation system and report meaningful insights to the manager.

Write 3-6 short, direct messages based on the system data provided.
Each message should be one or two sentences. Be specific — cite numbers, talent names, trends.
Avoid generic platitudes. Only flag things that matter.

Categorize each message as one of: volume, quality, spam, escalation, health

Rate severity as: info, warning, critical

Return a JSON array:
[{"message": "...", "category": "volume|quality|spam|escalation|health", "talent_key": "<key or null>", "severity": "info|warning|critical"}]

Only include messages that are genuinely noteworthy. Fewer strong signals beat many weak ones."""


def generate_messages(db: Session) -> int:
    """Generate Marco messages from current system state. Returns count of messages saved."""
    settings = get_settings()
    if not settings.openai_api_key:
        logger.warning("Marco: no OpenAI key — skipping generation")
        return 0

    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key)

    snapshot = _build_snapshot(db, settings)
    user_content = f"System snapshot (last 7 days):\n{json.dumps(snapshot, indent=2, default=str)}"

    try:
        response = client.chat.completions.create(
            model=settings.app_config.get("openai", {}).get("draft_model", "gpt-4o"),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=800,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        # Handle both {"messages": [...]} and [...]
        if isinstance(parsed, dict):
            messages = parsed.get("messages", [])
        else:
            messages = parsed
    except Exception as exc:
        logger.error("Marco GPT call failed: %s", exc)
        return 0

    count = 0
    for item in messages:
        if not isinstance(item, dict) or not item.get("message"):
            continue
        row = MarcoMessage(
            message=str(item["message"])[:1000],
            category=str(item.get("category", "health"))[:64],
            talent_key=item.get("talent_key") or None,
            severity=str(item.get("severity", "info"))[:16],
            created_at=datetime.utcnow(),
        )
        db.add(row)
        count += 1

    if count:
        db.commit()
    logger.info("Marco: generated %d messages", count)
    return count


def _build_snapshot(db: Session, settings) -> dict:
    """Build a data snapshot for GPT to analyze."""
    now = datetime.utcnow()
    since_7d = now - timedelta(days=7)
    since_1d = now - timedelta(hours=24)

    emails_7d = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since_7d).all()
    emails_1d = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since_1d).all()

    pending_drafts = db.query(Draft).filter(Draft.status == DraftStatus.pending).count()
    escalations_7d = db.query(Draft).filter(
        Draft.created_at >= since_7d, Draft.is_escalate == True  # noqa: E712
    ).count()

    tokens = db.query(TalentToken).all()
    failing_tokens = [t.talent_key for t in tokens if (t.consecutive_failures or 0) >= 2]

    poll_errors_7d = db.query(PollHealth).filter(
        PollHealth.polled_at >= since_7d, PollHealth.error_message != None  # noqa: E711
    ).count()

    # Per-talent summary
    talent_summary = {}
    for e in emails_7d:
        key = e.talent_key.lower()
        if key not in talent_summary:
            talent_summary[key] = {"total": 0, "score1": 0, "score2": 0, "score3": 0,
                                   "high_risk": 0, "overrides": 0}
        talent_summary[key]["total"] += 1
        if e.score == 1:
            talent_summary[key]["score1"] += 1
        elif e.score == 2:
            talent_summary[key]["score2"] += 1
        elif e.score == 3:
            talent_summary[key]["score3"] += 1
        if (e.risk_score or 0) >= 7:
            talent_summary[key]["high_risk"] += 1
        if e.human_override_occurred:
            talent_summary[key]["overrides"] += 1

    # Offer type distribution
    offer_counts: dict[str, int] = {}
    for e in emails_7d:
        ot = e.offer_type or "Unknown"
        offer_counts[ot] = offer_counts.get(ot, 0) + 1

    avg_classify = (
        sum(e.time_to_classify_ms for e in emails_7d if e.time_to_classify_ms)
        / max(1, sum(1 for e in emails_7d if e.time_to_classify_ms))
    )

    return {
        "timestamp": now.isoformat(),
        "last_7_days": {
            "total_emails": len(emails_7d),
            "score1_spam": sum(1 for e in emails_7d if e.score == 1),
            "score2_review": sum(1 for e in emails_7d if e.score == 2),
            "score3_deals": sum(1 for e in emails_7d if e.score == 3),
            "escalations": escalations_7d,
            "poll_errors": poll_errors_7d,
            "avg_classify_ms": round(avg_classify),
        },
        "last_24_hours": {
            "total_emails": len(emails_1d),
            "score1_spam": sum(1 for e in emails_1d if e.score == 1),
            "score3_deals": sum(1 for e in emails_1d if e.score == 3),
        },
        "current_state": {
            "pending_drafts": pending_drafts,
            "failing_tokens": failing_tokens,
        },
        "per_talent": talent_summary,
        "offer_type_distribution": offer_counts,
    }
