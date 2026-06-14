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
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Date, case, func, text
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
    # Snap to midnight UTC so daily_volume bars sum exactly to total_emails.
    # Rolling timedelta would cut the first day mid-day, making sum(bars) < total.
    today = datetime.utcnow().date()
    return datetime.combine(today - timedelta(days=days), dt_time.min)


# ── Triage Intelligence ───────────────────────────────────────────────────────

@router.get("/triage-intelligence")
def triage_intelligence(days: int = 1, db: Session = Depends(get_db)):
    """Today's triage decision breakdown + top Score 2 reasons for the dashboard."""
    since = datetime.utcnow() - timedelta(days=days)

    agg = db.query(
        ProcessedEmail.score,
        func.count().label("cnt"),
    ).filter(ProcessedEmail.processed_at >= since).group_by(ProcessedEmail.score).all()

    score_map = {row.score: row.cnt for row in agg}
    total = sum(score_map.values())
    score3_count = score_map.get(3, 0)
    score2_count = score_map.get(2, 0)
    score1_count = score_map.get(1, 0)

    fallback_count = db.query(func.count()).filter(
        ProcessedEmail.processed_at >= since,
        ProcessedEmail.triage_reason.ilike("Triage fallback%"),
    ).scalar() or 0

    reason_rows = db.query(ProcessedEmail.triage_reason).filter(
        ProcessedEmail.processed_at >= since,
        ProcessedEmail.score == 2,
        ProcessedEmail.triage_reason.isnot(None),
    ).all()
    reason_counter: Counter = Counter()
    for (reason,) in reason_rows:
        reason_counter[reason[:90].rstrip()] += 1

    brand_rows = db.query(ProcessedEmail.brand_name).filter(
        ProcessedEmail.processed_at >= since,
        ProcessedEmail.score == 3,
        ProcessedEmail.brand_name.isnot(None),
    ).all()
    brand_counter: Counter = Counter(b for (b,) in brand_rows)

    return {
        "period_days": days,
        "total": total,
        "score3_count": score3_count,
        "score2_count": score2_count,
        "score1_count": score1_count,
        "fallback_count": fallback_count,
        "draft_rate": round(score3_count / total, 3) if total else 0.0,
        "top_score2_reasons": [
            {"reason": r, "count": c} for r, c in reason_counter.most_common(5)
        ],
        "top_drafted_brands": [
            {"brand": b, "count": c} for b, c in brand_counter.most_common(5)
        ],
    }


# ── Panel 1 — Talent Health ───────────────────────────────────────────────────

@router.get("/talent-health")
def talent_health(days: int = 7, db: Session = Depends(get_db)):
    """Per-talent volume, response load, escalation rate, spam rate, risk flags."""
    settings = get_settings()
    talent_configs = {t["key"].lower(): t for t in settings.talent_list}
    since = _window_start(days)

    agg_rows = db.query(
        ProcessedEmail.talent_key,
        func.count().label("total"),
        func.sum(case((ProcessedEmail.score == 3, 1), else_=0)).label("score3"),
        func.sum(case((ProcessedEmail.score == 2, 1), else_=0)).label("score2"),
        func.sum(case((ProcessedEmail.score == 1, 1), else_=0)).label("score1"),
        func.sum(case((ProcessedEmail.risk_score >= 7, 1), else_=0)).label("high_risk"),
        func.avg(func.coalesce(ProcessedEmail.risk_score, 0)).label("avg_risk"),
    ).filter(ProcessedEmail.processed_at >= since).group_by(ProcessedEmail.talent_key).all()

    by_talent = {row.talent_key.lower(): row for row in agg_rows if row.talent_key}

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
        row = by_talent.get(key)
        total = row.total if row else 0
        score3 = row.score3 if row else 0
        score2 = row.score2 if row else 0
        score1 = row.score1 if row else 0
        high_risk = row.high_risk if row else 0
        avg_risk = round(float(row.avg_risk or 0), 1) if row else 0.0

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

    agg_rows = db.query(
        ProcessedEmail.offer_type,
        func.count().label("total"),
        func.sum(case((ProcessedEmail.score == 1, 1), else_=0)).label("score1"),
        func.sum(case((ProcessedEmail.score == 2, 1), else_=0)).label("score2"),
        func.sum(case((ProcessedEmail.score == 3, 1), else_=0)).label("score3"),
        func.avg(ProcessedEmail.proposed_rate).label("avg_rate"),
        func.avg(func.coalesce(ProcessedEmail.sentiment_score, 5)).label("avg_sentiment"),
        func.avg(func.coalesce(ProcessedEmail.urgency_score, 0)).label("avg_urgency"),
        func.avg(func.coalesce(ProcessedEmail.risk_score, 0)).label("avg_risk"),
        func.sum(case((ProcessedEmail.human_override_occurred == True, 1), else_=0)).label("overrides"),  # noqa: E712
    ).filter(
        ProcessedEmail.processed_at >= since,
        ProcessedEmail.offer_type.isnot(None),
    ).group_by(ProcessedEmail.offer_type).all()

    result = []
    for row in agg_rows:
        n = row.total or 0
        result.append({
            "offer_type": row.offer_type or "Unknown",
            "count": n,
            "score1_pct": round(row.score1 / n, 3) if n else 0.0,
            "score2_pct": round(row.score2 / n, 3) if n else 0.0,
            "score3_pct": round(row.score3 / n, 3) if n else 0.0,
            "avg_rate_usd": round(float(row.avg_rate or 0), 2),
            "avg_sentiment": round(float(row.avg_sentiment or 0), 1),
            "avg_urgency": round(float(row.avg_urgency or 0), 1),
            "avg_risk": round(float(row.avg_risk or 0), 1),
            "override_count": row.overrides or 0,
            "days": days,
        })

    result.sort(key=lambda x: x["count"], reverse=True)
    return result


# ── Panel 3 — Operational Load ────────────────────────────────────────────────

@router.get("/operational-load")
def operational_load(days: int = 7, db: Session = Depends(get_db)):
    """Emails per hour/day, automation rate, time saved, human interventions."""
    since = _window_start(days)

    scalar = db.query(
        func.count().label("total"),
        func.sum(case((ProcessedEmail.score.in_([1, 3]), 1), else_=0)).label("automated"),
        func.sum(case((ProcessedEmail.human_override_occurred == True, 1), else_=0)).label("overrides"),  # noqa: E712
        func.avg(ProcessedEmail.time_to_classify_ms).label("avg_classify_ms"),
        func.avg(ProcessedEmail.time_to_draft_ms).label("avg_draft_ms"),
        func.sum(case((ProcessedEmail.score == 3, 5), (ProcessedEmail.score == 1, 1), else_=0)).label("minutes_saved"),
    ).filter(ProcessedEmail.processed_at >= since).one()

    total = scalar.total or 0
    automated = scalar.automated or 0
    overrides = scalar.overrides or 0
    avg_classify_ms = round(float(scalar.avg_classify_ms or 0))
    avg_draft_ms = round(float(scalar.avg_draft_ms or 0))
    estimated_minutes_saved = scalar.minutes_saved or 0

    hourly_rows = db.query(
        func.extract("hour", ProcessedEmail.processed_at).label("hour"),
        func.count().label("cnt"),
    ).filter(ProcessedEmail.processed_at >= since).group_by(
        func.extract("hour", ProcessedEmail.processed_at)
    ).all()
    hourly: dict[int, int] = {int(r.hour): r.cnt for r in hourly_rows}

    daily_rows = db.query(
        func.cast(ProcessedEmail.processed_at, Date).label("day"),
        func.count().label("cnt"),
    ).filter(ProcessedEmail.processed_at >= since).group_by(
        func.cast(ProcessedEmail.processed_at, Date)
    ).order_by(func.cast(ProcessedEmail.processed_at, Date)).all()

    poll_scalar = db.query(
        func.count().label("total"),
        func.sum(case((PollHealth.error_message.isnot(None), 1), else_=0)).label("errors"),
        func.avg(PollHealth.duration_ms).label("avg_ms"),
    ).filter(PollHealth.polled_at >= since).one()

    return {
        "period_days": days,
        "total_emails": total,
        "automated_count": automated,
        "automation_rate": round(automated / total, 3) if total else 0.0,
        "human_overrides": overrides,
        "avg_classify_ms": avg_classify_ms,
        "avg_draft_ms": avg_draft_ms,
        "estimated_minutes_saved": estimated_minutes_saved,
        "hourly_distribution": [{"hour": h, "count": hourly.get(h, 0)} for h in range(24)],
        "daily_volume": [{"date": str(r.day), "count": r.cnt} for r in daily_rows],
        "poll_error_count": poll_scalar.errors or 0,
        "avg_poll_duration_ms": round(float(poll_scalar.avg_ms or 0)),
    }


# ── Panel 4 — Anomaly Detection ───────────────────────────────────────────────

@router.get("/anomalies")
def anomaly_detection(db: Session = Depends(get_db)):
    """Volume spikes/drops, high-risk emails, repeated escalations, suspicious senders."""
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=7)

    # Today: small window, keep as .all() for high-risk + spam checks
    today_emails = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= today_start).all()
    today_count = len(today_emails)

    yesterday_count = db.query(func.count()).filter(
        ProcessedEmail.processed_at >= yesterday_start,
        ProcessedEmail.processed_at < today_start,
    ).scalar() or 0

    week_total = db.query(func.count()).filter(ProcessedEmail.processed_at >= week_start).scalar() or 0
    week_avg_per_day = week_total / 7

    # Sender override escalations — only the two columns needed
    override_sender_rows = db.query(ProcessedEmail.sender).filter(
        ProcessedEmail.processed_at >= week_start,
        ProcessedEmail.score == 3,
        ProcessedEmail.human_override_occurred == True,  # noqa: E712
        ProcessedEmail.sender.isnot(None),
    ).all()
    sender_escalations: dict[str, int] = defaultdict(int)
    for (sender,) in override_sender_rows:
        sender_escalations[sender] += 1

    # Per-talent spam rate (week) — aggregated
    week_talent_agg = db.query(
        ProcessedEmail.talent_key,
        func.count().label("total"),
        func.sum(case((ProcessedEmail.score == 1, 1), else_=0)).label("spam"),
    ).filter(ProcessedEmail.processed_at >= week_start).group_by(ProcessedEmail.talent_key).all()
    week_by_talent = {row.talent_key.lower(): row for row in week_talent_agg if row.talent_key}

    anomalies = []

    hours_into_day = now.hour + now.minute / 60
    if week_avg_per_day > 0 and hours_into_day >= 6:
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
            anomalies.append({
                "type": "volume_drop",
                "severity": "warning",
                "message": f"Today's volume ({today_count}) is only {raw_ratio:.0%} of the 7-day average — possible poll failure.",
                "talent_key": None,
            })

    high_risk_today = [e for e in today_emails if (e.risk_score or 0) >= 8]
    if high_risk_today:
        anomalies.append({
            "type": "high_risk_emails",
            "severity": "critical" if len(high_risk_today) >= 5 else "warning",
            "message": f"{len(high_risk_today)} high-risk emails detected today (risk score ≥ 8). Possible phishing campaign.",
            "talent_key": None,
        })

    for sender, count in sender_escalations.items():
        if count >= 3:
            anomalies.append({
                "type": "repeated_override",
                "severity": "info",
                "message": f"Sender '{sender}' has triggered {count} human overrides in the last 7 days — scenario may need improvement.",
                "talent_key": None,
            })

    # Per-talent spam spike: compare today rate vs 7-day rate
    by_talent_today: dict[str, list] = defaultdict(list)
    for e in today_emails:
        by_talent_today[e.talent_key.lower()].append(e)

    for key, wk in week_by_talent.items():
        td = by_talent_today.get(key, [])
        if len(td) < 5 or (wk.total or 0) < 10:
            continue
        today_spam = sum(1 for e in td if e.score == 1) / len(td)
        week_spam = (wk.spam or 0) / wk.total
        if today_spam >= week_spam * 1.5 and today_spam >= 0.4:
            anomalies.append({
                "type": "spam_spike",
                "severity": "warning",
                "message": f"{key}: spam rate today is {today_spam:.0%} vs 7-day avg {week_spam:.0%} — possible spam campaign targeting this inbox.",
                "talent_key": key,
            })

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
def email_feed(hours: int | None = None, limit: int = 500, db: Session = Depends(get_db)):
    """Inbox Feed rows: scores 1 & 2 (current behaviour) plus Score-3 LOST rows —
    Score 3 emails with no draft and no 'A Initial Response' label in Gmail.
    LOST rows carry is_lost=true so the dashboard can route the [A] button to the
    Regenerate endpoint instead of force-draft."""
    from sqlalchemy import exists, select
    from backend.models.db import InboxEmail
    from backend.services import gmail as gmail_svc

    cutoff = datetime.utcnow() - timedelta(hours=hours) if hours is not None else None

    # Score 1 / 2 rows — existing behaviour
    base_q = db.query(ProcessedEmail).filter(
        ProcessedEmail.score > 0, ProcessedEmail.score != 3,
        exists().where(InboxEmail.gmail_message_id == ProcessedEmail.gmail_message_id),
    )
    if cutoff is not None:
        base_q = base_q.filter(ProcessedEmail.processed_at >= cutoff)
    base_rows = base_q.order_by(ProcessedEmail.processed_at.desc()).limit(limit).all()

    # Score 3 LOST candidates — no draft, not archived, still in INBOX (joined for label_ids)
    drafted_subq = select(Draft.gmail_message_id)
    lost_q = (
        db.query(ProcessedEmail, InboxEmail.label_ids)
        .join(InboxEmail, InboxEmail.gmail_message_id == ProcessedEmail.gmail_message_id)
        .filter(
            ProcessedEmail.score == 3,
            ProcessedEmail.status != "archived",
            ProcessedEmail.gmail_message_id.not_in(drafted_subq),
        )
    )
    if cutoff is not None:
        lost_q = lost_q.filter(ProcessedEmail.processed_at >= cutoff)
    lost_candidates = lost_q.order_by(ProcessedEmail.processed_at.desc()).limit(limit).all()

    # Resolve "A Initial Response" label ID per talent (one labels.list call each).
    # On failure or absent label → None, which means no rows get filtered out for that
    # talent (safer default — surface them as LOST so Marco sees them).
    air_label_by_talent: dict[str, str | None] = {}
    talent_keys = {pe.talent_key for pe, _ in lost_candidates if pe.talent_key}
    if talent_keys:
        tokens = (
            db.query(TalentToken)
            .filter(TalentToken.talent_key.in_(talent_keys), TalentToken.active == True)  # noqa: E712
            .all()
        )
        for token in tokens:
            try:
                svc = gmail_svc.build_service(token, db)
                air_label_by_talent[token.talent_key] = gmail_svc.get_label_id_by_name(svc, "A Initial Response")
            except Exception as exc:  # noqa: BLE001
                logger.warning("email-feed: label lookup failed for %s: %s", token.talent_key, exc)
                air_label_by_talent[token.talent_key] = None

    lost_rows: list[ProcessedEmail] = []
    for pe, label_ids in lost_candidates:
        air_id = air_label_by_talent.get(pe.talent_key)
        ids = set((label_ids or "").split(",")) if label_ids else set()
        if air_id and air_id in ids:
            continue
        lost_rows.append(pe)

    # One draft-id lookup for score 1/2 rows (LOST rows by definition have none)
    message_ids = [r.gmail_message_id for r in base_rows if r.gmail_message_id]
    draft_map: dict[str, int] = {}
    if message_ids:
        drafts = (
            db.query(Draft.gmail_message_id, Draft.id)
            .filter(Draft.gmail_message_id.in_(message_ids), Draft.status == DraftStatus.pending)
            .all()
        )
        draft_map = {d.gmail_message_id: d.id for d in drafts}

    def _serialize(r: ProcessedEmail, is_lost: bool) -> dict:
        return {
            "gmail_message_id": r.gmail_message_id,
            "talent_key": r.talent_key,
            "sender": r.sender,
            "subject": r.subject,
            "score": r.score,
            "triage_reason": r.triage_reason,
            "status": r.status if isinstance(r.status, str) else r.status.value,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            "draft_id": draft_map.get(r.gmail_message_id),
            "is_lost": is_lost,
        }

    merged = [_serialize(r, False) for r in base_rows] + [_serialize(r, True) for r in lost_rows]
    merged.sort(key=lambda x: x["processed_at"] or "", reverse=True)
    return merged[:limit]


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
