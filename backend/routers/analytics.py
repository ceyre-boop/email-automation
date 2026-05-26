"""
Analytics API — 5 dashboard panel endpoints.

Panel 1  GET /api/analytics/talent-health         → per-talent volume, escalations, risk
Panel 2  GET /api/analytics/scenario-performance  → offer type distribution, escalation rates
Panel 3  GET /api/analytics/operational-load      → hourly throughput, automation rate, time saved
Panel 4  GET /api/analytics/anomalies             → volume spikes, high-risk emails, repeated issues
Panel 5  GET /api/analytics/marco/messages        → AI narrative messages for the manager
         POST /api/analytics/marco/generate       → trigger message generation
         POST /api/analytics/marco/{id}/dismiss   → dismiss a message
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import (
    Draft,
    DraftStatus,
    MarcoMessage,
    PollHealth,
    ProcessedEmail,
    TalentToken,
)
from backend.routers.deps import get_db, verify_api_key

from collections import Counter

router = APIRouter(
    prefix="/api/analytics",
    tags=["analytics"],
    dependencies=[Depends(verify_api_key)],
)
logger = logging.getLogger(__name__)


def _window_start(days: int = 7) -> datetime:
    return datetime.utcnow() - timedelta(days=days)


# ── Triage Intelligence ───────────────────────────────────────────────────────

@router.get("/triage-intelligence")
def triage_intelligence(days: int = 1, db: Session = Depends(get_db)):
    """Today's triage decision breakdown + top Score 2 reasons for the dashboard."""
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since).all()

    total = len(rows)
    score3 = [r for r in rows if r.score == 3]
    score2 = [r for r in rows if r.score == 2]
    score1 = [r for r in rows if r.score == 1]
    fallbacks = [r for r in rows if r.triage_reason and r.triage_reason.startswith("Triage fallback")]

    # Top Score 2 reasons — cluster by common phrases
    reason_counter: Counter = Counter()
    for r in score2:
        if r.triage_reason:
            # Truncate to ~80 chars for display
            key = r.triage_reason[:90].rstrip()
            reason_counter[key] += 1

    top_reasons = [
        {"reason": reason, "count": count}
        for reason, count in reason_counter.most_common(5)
    ]

    # Top Score 3 wins — most common brands being drafted
    brand_counter: Counter = Counter()
    for r in score3:
        if r.brand_name:
            brand_counter[r.brand_name] += 1

    return {
        "period_days": days,
        "total": total,
        "score3_count": len(score3),
        "score2_count": len(score2),
        "score1_count": len(score1),
        "fallback_count": len(fallbacks),
        "draft_rate": round(len(score3) / total, 3) if total else 0.0,
        "top_score2_reasons": top_reasons,
        "top_drafted_brands": [{"brand": b, "count": c} for b, c in brand_counter.most_common(5)],
    }


# ── Panel 1 — Talent Health ───────────────────────────────────────────────────

@router.get("/talent-health")
def talent_health(days: int = 7, db: Session = Depends(get_db)):
    """Per-talent volume, response load, escalation rate, spam rate, risk flags."""
    settings = get_settings()
    talent_configs = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
    since = _window_start(days)

    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= since)
        .all()
    )

    by_talent: dict[str, list] = defaultdict(list)
    for r in rows:
        by_talent[r.talent_key.lower()].append(r)

    # Connected tokens
    connected = {
        r.talent_key.lower()
        for r in db.query(TalentToken).filter(TalentToken.active == True).all()  # noqa: E712
    }

    pending_drafts = {
        r.talent_key.lower(): r.cnt
        for r in db.query(Draft.talent_key, func.count(Draft.id).label("cnt"))
        .filter(Draft.status == DraftStatus.pending)
        .group_by(Draft.talent_key)
        .all()
    }

    results = []
    for key, cfg in talent_configs.items():
        emails = by_talent.get(key, [])
        total = len(emails)
        score3 = sum(1 for e in emails if e.score == 3)
        score2 = sum(1 for e in emails if e.score == 2)
        score1 = sum(1 for e in emails if e.score == 1)
        high_risk = sum(1 for e in emails if (e.risk_score or 0) >= 7)
        avg_risk = round(sum((e.risk_score or 0) for e in emails) / total, 1) if total else 0.0

        results.append({
            "talent_key": key,
            "full_name": cfg.get("full_name", key),
            "manager": cfg.get("manager"),
            "connected": key in connected,
            "total_emails": total,
            "score3_deals": score3,
            "score2_review": score2,
            "score1_spam": score1,
            "pending_drafts": pending_drafts.get(key, 0),
            "high_risk_count": high_risk,
            "avg_risk_score": avg_risk,
            "spam_rate": round(score1 / total, 3) if total else 0.0,
            "automation_rate": round((score3 + score1) / total, 3) if total else 0.0,
            "days": days,
        })

    results.sort(key=lambda x: x["total_emails"], reverse=True)
    return results


# ── Panel 2 — Scenario Performance ───────────────────────────────────────────

@router.get("/scenario-performance")
def scenario_performance(days: int = 7, db: Session = Depends(get_db)):
    """Which offer types fire most, which escalate, which score highest."""
    since = _window_start(days)

    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= since, ProcessedEmail.offer_type != None)  # noqa: E711
        .all()
    )

    by_type: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "score1": 0, "score2": 0, "score3": 0,
        "total_rate": 0.0, "rate_count": 0,
        "avg_sentiment": 0.0, "avg_urgency": 0.0, "avg_risk": 0.0,
        "overrides": 0,
    })

    for r in rows:
        ot = r.offer_type or "Unknown"
        d = by_type[ot]
        d["count"] += 1
        if r.score == 1:
            d["score1"] += 1
        elif r.score == 2:
            d["score2"] += 1
        elif r.score == 3:
            d["score3"] += 1
        if r.proposed_rate:
            d["total_rate"] += r.proposed_rate
            d["rate_count"] += 1
        d["avg_sentiment"] += (r.sentiment_score or 5)
        d["avg_urgency"] += (r.urgency_score or 0)
        d["avg_risk"] += (r.risk_score or 0)
        if r.human_override_occurred:
            d["overrides"] += 1

    result = []
    for ot, d in by_type.items():
        n = d["count"]
        result.append({
            "offer_type": ot,
            "count": n,
            "score1_pct": round(d["score1"] / n, 3) if n else 0.0,
            "score2_pct": round(d["score2"] / n, 3) if n else 0.0,
            "score3_pct": round(d["score3"] / n, 3) if n else 0.0,
            "avg_rate_usd": round(d["total_rate"] / d["rate_count"], 2) if d["rate_count"] else 0.0,
            "avg_sentiment": round(d["avg_sentiment"] / n, 1) if n else 0.0,
            "avg_urgency": round(d["avg_urgency"] / n, 1) if n else 0.0,
            "avg_risk": round(d["avg_risk"] / n, 1) if n else 0.0,
            "override_count": d["overrides"],
            "days": days,
        })

    result.sort(key=lambda x: x["count"], reverse=True)
    return result


# ── Panel 3 — Operational Load ────────────────────────────────────────────────

@router.get("/operational-load")
def operational_load(days: int = 7, db: Session = Depends(get_db)):
    """Emails per hour/day, automation rate, time saved, human interventions."""
    since = _window_start(days)

    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= since)
        .all()
    )

    total = len(rows)
    automated = sum(1 for r in rows if r.score in (1, 3))
    overrides = sum(1 for r in rows if r.human_override_occurred)
    avg_classify_ms = (
        sum(r.time_to_classify_ms for r in rows if r.time_to_classify_ms)
        / max(1, sum(1 for r in rows if r.time_to_classify_ms))
    )
    avg_draft_ms = (
        sum(r.time_to_draft_ms for r in rows if r.time_to_draft_ms)
        / max(1, sum(1 for r in rows if r.time_to_draft_ms))
    )

    # Hourly distribution (UTC hour bucket)
    hourly: dict[int, int] = defaultdict(int)
    daily: dict[str, int] = defaultdict(int)
    for r in rows:
        hourly[r.processed_at.hour] += 1
        daily[r.processed_at.strftime("%Y-%m-%d")] += 1

    # Estimate time saved: assume each deal email takes 5 min to read + reply manually
    # Automated handling of score-3 = full reply drafted (~5 min saved)
    # Automated handling of score-1 = spam filtered (~1 min saved)
    score3_count = sum(1 for r in rows if r.score == 3)
    score1_count = sum(1 for r in rows if r.score == 1)
    estimated_minutes_saved = score3_count * 5 + score1_count * 1

    # Poll health stats
    poll_rows = (
        db.query(PollHealth)
        .filter(PollHealth.polled_at >= since)
        .all()
    )
    poll_errors = sum(1 for p in poll_rows if p.error_message)
    avg_poll_ms = (
        sum(p.duration_ms for p in poll_rows if p.duration_ms)
        / max(1, sum(1 for p in poll_rows if p.duration_ms))
    )

    return {
        "period_days": days,
        "total_emails": total,
        "automated_count": automated,
        "automation_rate": round(automated / total, 3) if total else 0.0,
        "human_overrides": overrides,
        "avg_classify_ms": round(avg_classify_ms),
        "avg_draft_ms": round(avg_draft_ms),
        "estimated_minutes_saved": estimated_minutes_saved,
        "hourly_distribution": [{"hour": h, "count": hourly[h]} for h in range(24)],
        "daily_volume": [
            {"date": d, "count": daily[d]}
            for d in sorted(daily.keys())
        ],
        "poll_error_count": poll_errors,
        "avg_poll_duration_ms": round(avg_poll_ms),
    }


# ── Panel 4 — Anomaly Detection ───────────────────────────────────────────────

@router.get("/anomalies")
def anomaly_detection(db: Session = Depends(get_db)):
    """Volume spikes/drops, high-risk emails, repeated escalations, suspicious senders."""
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=7)

    today_emails = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= today_start).all()
    yesterday_count = db.query(ProcessedEmail).filter(
        ProcessedEmail.processed_at >= yesterday_start,
        ProcessedEmail.processed_at < today_start,
    ).count()
    week_emails = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= week_start).all()

    today_count = len(today_emails)
    week_avg_per_day = len(week_emails) / 7

    anomalies = []

    # Volume spike/drop — only compare after 6+ UTC hours so partial-day doesn't trigger false alarms
    hours_into_day = now.hour + now.minute / 60
    if week_avg_per_day > 0 and hours_into_day >= 6:
        # Scale today's count to a full-day projection before comparing
        projected_today = today_count * (24 / max(hours_into_day, 1))
        ratio = projected_today / week_avg_per_day
        raw_ratio = today_count / week_avg_per_day
        if ratio >= 2.0:
            anomalies.append({
                "type": "volume_spike",
                "severity": "warning",
                "message": f"Today's volume ({today_count} so far) is trending {ratio:.1f}x the 7-day average ({week_avg_per_day:.0f}/day).",
                "talent_key": None,
            })
        elif raw_ratio <= 0.2 and week_avg_per_day >= 5 and hours_into_day >= 18:
            # Only fire volume-drop after 18:00 UTC (10am-11am Pacific) — full business day
            anomalies.append({
                "type": "volume_drop",
                "severity": "warning",
                "message": f"Today's volume ({today_count}) is only {raw_ratio:.0%} of the 7-day average — possible poll failure.",
                "talent_key": None,
            })

    # High-risk emails today
    high_risk_today = [e for e in today_emails if (e.risk_score or 0) >= 8]
    if high_risk_today:
        anomalies.append({
            "type": "high_risk_emails",
            "severity": "critical" if len(high_risk_today) >= 5 else "warning",
            "message": f"{len(high_risk_today)} high-risk emails detected today (risk score ≥ 8). Possible phishing campaign.",
            "talent_key": None,
        })

    # Repeated escalations from same sender (last 7 days)
    sender_escalations: dict[str, int] = defaultdict(int)
    for e in week_emails:
        if e.score == 3 and e.human_override_occurred:
            if e.sender:
                sender_escalations[e.sender] += 1
    for sender, count in sender_escalations.items():
        if count >= 3:
            anomalies.append({
                "type": "repeated_override",
                "severity": "info",
                "message": f"Sender '{sender}' has triggered {count} human overrides in the last 7 days — scenario may need improvement.",
                "talent_key": None,
            })

    # Per-talent spam rate spike
    by_talent_today: dict[str, list] = defaultdict(list)
    by_talent_week: dict[str, list] = defaultdict(list)
    for e in today_emails:
        by_talent_today[e.talent_key.lower()].append(e)
    for e in week_emails:
        by_talent_week[e.talent_key.lower()].append(e)

    for key in set(list(by_talent_today.keys()) + list(by_talent_week.keys())):
        td = by_talent_today.get(key, [])
        wk = by_talent_week.get(key, [])
        if len(td) < 5 or len(wk) < 10:
            continue
        today_spam = sum(1 for e in td if e.score == 1) / len(td)
        week_spam = sum(1 for e in wk if e.score == 1) / len(wk)
        if today_spam >= week_spam * 1.5 and today_spam >= 0.4:
            anomalies.append({
                "type": "spam_spike",
                "severity": "warning",
                "message": f"{key}: spam rate today is {today_spam:.0%} vs 7-day avg {week_spam:.0%} — possible spam campaign targeting this inbox.",
                "talent_key": key,
            })

    # Token failures
    for row in db.query(TalentToken).filter(TalentToken.consecutive_failures >= 2).all():
        anomalies.append({
            "type": "token_failure",
            "severity": "critical" if row.consecutive_failures >= 5 else "warning",
            "message": f"{row.talent_key}: Gmail token has failed {row.consecutive_failures} consecutive times. Last error: {row.last_error or 'unknown'}",
            "talent_key": row.talent_key,
        })

    return {
        "generated_at": now.isoformat(),
        "today_count": today_count,
        "yesterday_count": yesterday_count,
        "week_avg_per_day": round(week_avg_per_day, 1),
        "anomalies": anomalies,
    }


# ── Panel 5 — Marco Messages ──────────────────────────────────────────────────

@router.get("/marco/messages")
def list_marco_messages(include_dismissed: bool = False, db: Session = Depends(get_db)):
    """Active AI narrative messages for the manager."""
    q = db.query(MarcoMessage)
    if not include_dismissed:
        q = q.filter(MarcoMessage.dismissed == False)  # noqa: E712
    rows = q.order_by(MarcoMessage.created_at.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "message": r.message,
            "category": r.category,
            "talent_key": r.talent_key,
            "severity": r.severity,
            "created_at": r.created_at.isoformat(),
            "dismissed": r.dismissed,
        }
        for r in rows
    ]


@router.post("/marco/{message_id}/dismiss")
def dismiss_marco_message(message_id: int, db: Session = Depends(get_db)):
    """Dismiss a Marco message."""
    row = db.query(MarcoMessage).filter(MarcoMessage.id == message_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found.")
    row.dismissed = True
    row.dismissed_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/retriage-backfill")
def retriage_backfill(db: Session = Depends(get_db)):
    """Release false-positive Score-2 records (no Re: subject, not a real thread) so next poll re-triages them."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=24)
    rows = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.score == 2,
            ProcessedEmail.processed_at >= cutoff,
            ProcessedEmail.triage_reason.notlike("Ongoing thread%"),
            ProcessedEmail.subject.notlike("Re:%"),
            ProcessedEmail.subject.notlike("RE:%"),
        )
        .all()
    )
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    logger.info("retriage-backfill: released %d false-positive Score-2 records", count)
    return {"ok": True, "released": count}


@router.get("/email-feed")
def email_feed(hours: int = 24, limit: int = 100, db: Session = Depends(get_db)):
    """Recent processed emails regardless of score — for the Inbox Feed dashboard panel."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.processed_at >= cutoff, ProcessedEmail.score > 0)
        .order_by(ProcessedEmail.processed_at.desc())
        .limit(limit)
        .all()
    )
    # Look up pending drafts for these emails in one query
    message_ids = [r.gmail_message_id for r in rows if r.gmail_message_id]
    draft_map: dict[str, int] = {}
    if message_ids:
        drafts = (
            db.query(Draft.gmail_message_id, Draft.id)
            .filter(Draft.gmail_message_id.in_(message_ids), Draft.status == DraftStatus.pending)
            .all()
        )
        draft_map = {d.gmail_message_id: d.id for d in drafts}

    return [
        {
            "gmail_message_id": r.gmail_message_id,
            "talent_key": r.talent_key,
            "sender": r.sender,
            "subject": r.subject,
            "score": r.score,
            "triage_reason": r.triage_reason,
            "status": r.status if isinstance(r.status, str) else r.status.value,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            "draft_id": draft_map.get(r.gmail_message_id),
        }
        for r in rows
    ]


@router.get("/sop-audit")
def sop_audit(db: Session = Depends(get_db)):
    """Character-level verbatim compliance check of all pending, non-human-edited drafts against SOP."""
    import json
    import re
    from pathlib import Path

    sop_path = Path(__file__).parent.parent.parent / "sheets" / "sop_data.json"
    sop_data = json.loads(sop_path.read_text())

    drafts = (
        db.query(Draft)
        .filter(Draft.status == DraftStatus.pending, Draft.human_edited == False)  # noqa: E712
        .all()
    )

    compliant, deviations = [], []
    for d in drafts:
        talent = sop_data.get(d.talent_key)
        if not talent:
            continue
        rules = talent.get("rules", [])
        rule = next((r for r in rules if r.get("offer_type") == d.offer_type), None)
        if not rule:
            rule = next((r for r in rules if r.get("is_default")), None)
        if not rule:
            continue

        expected = rule["response"].strip().replace("\r\n", "\n")
        actual = re.sub(r"^CC:.*\n", "", d.draft_text or "").strip().replace("\r\n", "\n")

        entry = {"draft_id": d.id, "talent_key": d.talent_key, "subject": d.subject}
        if actual == expected:
            compliant.append(entry)
        else:
            deviations.append({
                **entry,
                "offer_type": d.offer_type,
                "expected_preview": expected[:200],
                "actual_preview": actual[:200],
            })

    return {
        "compliant_count": len(compliant),
        "deviation_count": len(deviations),
        "compliant": compliant,
        "deviations": deviations,
    }


@router.post("/marco/dismiss-all")
def dismiss_all_marco_messages(category: str | None = None, db: Session = Depends(get_db)):
    """Bulk-dismiss all undismissed Marco messages, optionally filtered by category."""
    q = db.query(MarcoMessage).filter(MarcoMessage.dismissed == False)  # noqa: E712
    if category:
        q = q.filter(MarcoMessage.category == category)
    rows = q.all()
    now = datetime.utcnow()
    for row in rows:
        row.dismissed = True
        row.dismissed_at = now
    db.commit()
    return {"ok": True, "dismissed": len(rows)}


@router.post("/marco/generate")
def generate_marco_messages(db: Session = Depends(get_db)):
    """Trigger GPT-4o to analyze system state and generate Marco narrative messages."""
    from backend.services.marco import generate_messages
    try:
        count = generate_messages(db)
        return {"ok": True, "generated": count}
    except Exception as exc:
        logger.error("Marco generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
