"""Pipeline stall alarm.

Why this exists
---------------
On 2026-09-03 drafting died at 16:00 UTC and stayed dead until 19:53. Nothing
surfaced it: `/health` returned "ok", and APScheduler logged
`_run_poll executed successfully` every cycle because the job wrapper catches
the exception and reports success anyway. The outage was found by a human
noticing a Gmail tab had gone quiet, hours later.

The lesson is that "no error" is not the same as "working". This module watches
for the *absence of expected work* instead of the presence of errors, which is
the only signal that would have caught that failure — and the one before it
(2026-08-04, a varchar overflow that crash-looped the poller for ~7 hours while
the scheduler still logged success).

Two independent conditions, either of which trips the alarm:

  A. NOTHING PROCESSED — no ProcessedEmail rows for `stall_minutes` while UNPROCESSED
     unread mail is sitting in the inbox cache. Triage/polling is down.
  B. NOTHING DRAFTED — triage scored at least one Score 3 in the window but no
     Draft row was created. Triage works, drafting is broken.

Condition A deliberately requires unread mail present: a quiet 3 a.m. with no
inbound is not a stall, and an alarm that cries wolf overnight gets ignored by
morning.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, InboxEmail, ProcessedEmail, TalentToken

logger = logging.getLogger(__name__)

_KEY_ALARM_SENT_AT = "stall_alarm_last_sent_at"

DEFAULT_STALL_MINUTES = 30
DEFAULT_COOLDOWN_MINUTES = 60


def _cfg() -> dict:
    return get_settings().app_config.get("stall_alarm", {}) or {}


def _dead_tokens(db: Session) -> tuple[list[str], list[dict]]:
    """Gmail tokens that were switched off after failing — never raises.

    A dead token is inactive AND carries evidence of failure: deactivated_at (set by
    oauth.record_token_failure), consecutive_failures > 0, or a recorded last_error.
    The 13 shared-inbox talents are also active=False, but deliberately so — never
    polled individually, zero failures, no error — and must not trip this.

    Returns (keys, details); details carry the reason and when it died.
    """
    try:
        rows = (
            db.query(TalentToken)
            .filter(
                TalentToken.active.is_(False),
                (TalentToken.deactivated_at.isnot(None))
                | (TalentToken.consecutive_failures > 0)
                | (TalentToken.last_error.isnot(None)),
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stall_alarm: dead-token query failed: %s", exc)
        return [], []
    details = [
        {
            "talent_key": t.talent_key,
            "email": t.email,
            "reason": t.last_error,
            "deactivated_at": t.deactivated_at.isoformat() if t.deactivated_at else None,
            "consecutive_failures": t.consecutive_failures or 0,
        }
        for t in rows
    ]
    return [t.talent_key for t in rows], details


def check_pipeline_stall(db: Session) -> dict:
    """Return the pipeline liveness picture. Never raises."""
    now = datetime.utcnow()
    cfg = _cfg()
    stall_minutes = int(cfg.get("stall_minutes", DEFAULT_STALL_MINUTES))
    since = now - timedelta(minutes=stall_minutes)
    hour_ago = now - timedelta(minutes=60)

    day_ago = now - timedelta(hours=24)
    unrouted_warn = int(cfg.get("unrouted_warn_per_24h", 15))

    try:
        processed_in_window = (
            db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since).count()
        )
        unread_waiting = db.query(InboxEmail).filter(InboxEmail.is_unread.is_(True)).count()
        # Unread in Gmail != waiting on us. Handled mail routinely stays unread
        # (archived junk, replies sent from Gmail), so on 2026-10-24 the poller
        # logged "1657 unread, 0 new to process" while this alarm paged "polling
        # is down" over a 40-minute lull in inbound. Only unread mail with no
        # ProcessedEmail row is work the pipeline owes.
        unprocessed_unread = (
            db.query(InboxEmail)
            .outerjoin(ProcessedEmail, ProcessedEmail.gmail_message_id == InboxEmail.gmail_message_id)
            .filter(InboxEmail.is_unread.is_(True), ProcessedEmail.id.is_(None))
            .count()
        )
        score3_last_hour = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.processed_at >= hour_ago, ProcessedEmail.score == 3)
            .count()
        )
        drafts_last_hour = db.query(Draft).filter(Draft.created_at >= hour_ago).count()
        sends_last_hour = (
            db.query(Draft)
            .filter(Draft.reviewed_at >= hour_ago, Draft.status == "sent")
            .count()
        )
        # Revenue-relevant, not just liveness: a routing regression does not stop
        # the pipeline (poll/triage/draft/send all keep looking healthy), it just
        # quietly drops real brand deals into the UNROUTED pile instead of a
        # talent's dashboard. That is exactly how the 2026-09-14 thread-continuity
        # gap ran for weeks unnoticed — every other metric here stayed green the
        # whole time. This is the check that would have caught it in a day.
        unrouted_last_24h = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.processed_at >= day_ago, ProcessedEmail.talent_key == "UNROUTED")
            .count()
        )
        # retry_pending rows the poller will never re-claim (due > 15 min ago, or out
        # of retries). The claim_reaper sweep should clear these every 5 minutes; a
        # non-zero count here means it is not.
        from backend.services.retry_sweep import OVERDUE_MINUTES, count_overdue
        retry_pending_overdue = count_overdue(db, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stall_alarm: metric query failed: %s", exc)
        # A failed query leaves the session's transaction aborted; without this
        # rollback the dead-token query below fails too (InFailedSqlTransaction).
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        # Dead tokens are independent of the metric queries above — report them anyway
        # so /health never silently loses them just because one count query failed.
        dead_keys, dead_details = _dead_tokens(db)
        # stalled is None ("unknown"), never False: on 2026-11-12 these queries hit
        # statement_timeout every cycle while /health and the dashboard banner read
        # "not stalled", so a crawling database looked perfectly healthy.
        return {"stalled": None, "reason": METRICS_UNAVAILABLE_REASON, "error": str(exc)[:200],
                "dead_tokens": dead_keys, "dead_token_details": dead_details}

    dead_tokens, dead_token_details = _dead_tokens(db)

    # Guardian-paused talents: mail isn't even ingested while paused (poller.py
    # skips the Gmail account outright), so nothing about a stuck pause shows up
    # anywhere else. guardian_pause_sent_at_<key> is the timestamp the guardian
    # itself writes when it pauses a talent.
    paused_warn_minutes = int(cfg.get("paused_talent_warn_minutes", 180))
    paused_talents: list[tuple[str, float]] = []
    try:
        from backend.services.guardian import _get_state
        for key, profile in get_settings().talent_profiles.items():
            if not profile.paused:
                continue
            ts = _get_state(db, f"guardian_pause_sent_at_{key}")
            if not ts:
                continue
            try:
                paused_since = datetime.fromisoformat(ts)
            except ValueError:
                continue
            minutes = (now - paused_since).total_seconds() / 60
            if minutes > paused_warn_minutes:
                paused_talents.append((key, minutes))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stall_alarm: paused-talent check failed (non-fatal): %s", exc)

    # Escalated drafts ("no matching approved response") can NEVER auto-send
    # regardless of the talent's Auto Send setting (auto_send.py excludes
    # is_escalate rows outright) and have no dedicated review view anywhere.
    escalation_warn_hours = int(cfg.get("escalation_warn_hours", 24))
    stale_escalations = (
        db.query(Draft)
        .filter(
            Draft.is_escalate.is_(True),
            Draft.status == "pending",
            Draft.created_at < now - timedelta(hours=escalation_warn_hours),
        )
        .count()
    )

    # Score-2 ("human review required") backlog. By design these never auto-send —
    # a live negotiation reply needs a human judgment call — but the queue has no
    # staleness signal anywhere else.
    #
    # ProcessedEmail.status is written once at triage time and never reconciled
    # with what actually happens to the message afterward — managers routinely
    # handle a flagged negotiation directly in Gmail (reply, archive) rather than
    # through the dashboard's archive/keep buttons, which is the only thing that
    # flips status to "archived". That left status='flagged' accumulating
    # forever. Checked live 2026-09-17: 18,158 rows read "flagged" in this table,
    # but only 1,344 of those messages were still actually sitting in the Gmail
    # inbox (inbox_emails, synced from Gmail's INBOX label every 5 min) — the
    # other ~93% were already resolved outside the app. Counting the raw ledger
    # tripped this alarm hourly on a number ~15x the real backlog.
    #
    # inbox_emails is the source of truth for "still needs a human" — only count
    # a flagged row whose message is still present there.
    score2_warn = int(cfg.get("score2_backlog_warn", 5000))
    # Join on gmail_message_id only — it's globally unique on processed_emails,
    # and InboxEmail.talent_key is lowercased by inbox_sync.py while
    # ProcessedEmail.talent_key isn't, so an added talent_key equality here
    # silently matches nothing.
    score2_still_pending = (
        db.query(ProcessedEmail)
        .join(InboxEmail, InboxEmail.gmail_message_id == ProcessedEmail.gmail_message_id)
        .filter(ProcessedEmail.score == 2, ProcessedEmail.status == "flagged")
    )
    score2_total = score2_still_pending.count()
    score2_older_than_7d = score2_still_pending.filter(
        ProcessedEmail.processed_at < now - timedelta(days=7)
    ).count()
    score2_backlog_growth_flag = score2_older_than_7d > score2_warn

    metrics = {
        "window_minutes": stall_minutes,
        "processed_in_window": processed_in_window,
        "unread_waiting": unread_waiting,
        "unprocessed_unread": unprocessed_unread,
        "score3_last_hour": score3_last_hour,
        "drafts_last_hour": drafts_last_hour,
        "sends_last_hour": sends_last_hour,
        "unrouted_last_24h": unrouted_last_24h,
        "retry_pending_overdue": retry_pending_overdue,
        "dead_tokens": dead_tokens,
        "dead_token_details": dead_token_details,
        "paused_talents": [{"talent_key": k, "minutes_paused": round(m)} for k, m in paused_talents],
        "stale_escalations": stale_escalations,
        "score2_backlog_total": score2_total,
        "score2_backlog_older_than_7d": score2_older_than_7d,
    }

    reason = None
    if processed_in_window == 0 and unprocessed_unread > 0:
        reason = (
            f"NOTHING PROCESSED — 0 emails triaged in {stall_minutes} min "
            f"while {unprocessed_unread} unprocessed unread sit in the inbox "
            f"({unread_waiting} unread total). Polling or triage is down."
        )
    elif score3_last_hour > 0 and drafts_last_hour == 0:
        reason = (
            f"NOTHING DRAFTED — {score3_last_hour} Score 3 email(s) in the last hour "
            f"produced 0 drafts. Triage is working, draft creation is not."
        )
    elif retry_pending_overdue > 0:
        reason = (
            f"STRANDED RETRIES — {retry_pending_overdue} email(s) are stuck in retry_pending, "
            f"due more than {OVERDUE_MINUTES} min ago or out of retries. The claim_reaper sweep "
            "should re-process these every 5 minutes; it is not (AI disabled, Gmail token "
            "dead, or the sweep is failing). Those brand emails are neither triaged nor "
            "flagged for a human."
        )
    elif unrouted_last_24h > unrouted_warn:
        reason = (
            f"ROUTING REGRESSION — {unrouted_last_24h} emails landed UNROUTED in the last "
            f"24h (warn at {unrouted_warn}). The pipeline looks healthy on every other metric, "
            "but real brand mail may be silently missing its talent's dashboard. Check "
            "/api/dashboard/debug/routing-headers and /api/dashboard/admin/reroute-unrouted."
        )
    elif dead_tokens:
        reason = (
            f"TALENT INBOX DOWN — {len(dead_tokens)} Gmail token(s) deactivated after repeated "
            f"refresh failures and never reconnected: {', '.join(dead_tokens[:5])}. Polling, "
            "triage and drafting are silently excluded for these talents — every other metric "
            "here stays green because the rest of the roster keeps working. Reconnect Gmail "
            "for the affected talent(s)."
        )
    elif paused_talents:
        reason = (
            f"TALENT PAUSED — {', '.join(f'{k} ({int(mins)}min)' for k, mins in paused_talents[:5])} "
            f"paused by the guardian for over {paused_warn_minutes} minutes with no auto-recovery. "
            "Mail for a paused talent is not even ingested, so nothing shows in the dashboard "
            "while it stays paused. Un-pause via the SOP admin once the underlying trigger is "
            "understood, or confirm the pause is intentional."
        )
    elif stale_escalations:
        reason = (
            f"{stale_escalations} escalated draft(s) ('no matching approved response') are "
            f"older than {escalation_warn_hours}h. These can NEVER auto-send regardless of the "
            "talent's Auto Send setting and have no dedicated review view — a real brand deal "
            "sits waiting on a human to write a reply from scratch."
        )
    elif score2_backlog_growth_flag:
        reason = (
            f"SCORE-2 BACKLOG — {score2_older_than_7d} 'human review required' emails "
            f"(ongoing negotiations, event invites) are older than 7 days, out of "
            f"{score2_total} total. This queue has no staleness tracking; a real counter-offer "
            "can sit unanswered indefinitely with nothing surfacing it. Not necessarily a bug — "
            "confirm someone is actively working this queue."
        )

    return {"stalled": reason is not None, "reason": reason, **metrics}


METRICS_UNAVAILABLE_REASON = "DATABASE SLOW — pipeline metrics timed out, status unknown"
# Consecutive "unknown" checks (job runs every 5 min) before alerting: 3 ≈ 15 min.
UNKNOWN_ALERT_AFTER = 3
_unknown_streak = 0


def run_stall_alarm(db: Session) -> dict:
    """Scheduled entry point: check, log loudly, and email once per cooldown."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"stalled": False, "reason": None, "disabled": True}

    global _unknown_streak
    result = check_pipeline_stall(db)
    if result.get("stalled") is None:
        # Metrics unavailable (DB timing out). Never announce "recovered" on an
        # unknown, and alert once it persists — a slow DB is itself an outage.
        _unknown_streak += 1
        if _unknown_streak >= UNKNOWN_ALERT_AFTER:
            alert = {**result, "stalled": True,
                     "reason": f"DATABASE TOO SLOW — pipeline metrics failed {_unknown_streak} checks in a row: "
                               f"{result.get('error') or 'unknown error'}"}
            logger.error("STALL ALARM: %s", alert["reason"])
            _maybe_send_alert(db, alert, cfg)
        return result
    _unknown_streak = 0
    if not result.get("stalled"):
        _maybe_send_recovery(db)
        return result

    logger.error("STALL ALARM: %s | metrics=%s", result["reason"], json.dumps(
        {k: v for k, v in result.items() if k not in ("stalled", "reason")}
    ))
    _maybe_send_alert(db, result, cfg)
    return result


def _maybe_send_recovery(db: Session) -> None:
    """If a stall alert was sent and the pipeline is healthy again, say so once. Never raises.

    The transition marker is the stall-alarm "last sent" state: set when a stall alert
    delivered, blanked here so recovery fires once per incident.
    """
    try:
        from backend.services.alerts import send_out_of_band_alert
        from backend.services.guardian import _get_state, _set_state

        if not _get_state(db, _KEY_ALARM_SENT_AT):
            return
        _set_state(db, _KEY_ALARM_SENT_AT, "")
        channels = send_out_of_band_alert("✅ Pipeline recovered", "The stall alarm has cleared.")
        logger.info("stall_alarm: recovery notice sent via %s", ", ".join(channels) or "no channel")
    except Exception as exc:  # noqa: BLE001
        logger.error("stall_alarm: recovery notice failed: %s", exc)


def _maybe_send_alert(db: Session, result: dict, cfg: dict) -> None:
    """Alert on the stall, at most once per cooldown. Never raises.

    Channels: ALERT_WEBHOOK_URL and Telegram (independent of any Gmail token) and email. The
    cooldown only starts once at least one channel delivered, so a total delivery
    failure is retried on the next 5-minute run instead of being suppressed for an hour.
    """
    from backend.services.alerts import send_out_of_band_alert
    from backend.services.guardian import _get_state, _set_state

    try:
        cooldown = int(cfg.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
        last_sent_str = _get_state(db, _KEY_ALARM_SENT_AT)
        if last_sent_str:
            try:
                last_sent = datetime.fromisoformat(last_sent_str)
                if (datetime.utcnow() - last_sent).total_seconds() < cooldown * 60:
                    logger.info("stall_alarm: alert suppressed (cooldown active)")
                    return
            except ValueError:
                pass

        settings = get_settings()
        guardian_cfg = settings.app_config.get("guardian", {}) or {}
        alert_email = cfg.get("alert_email") or guardian_cfg.get("alert_email", "colineyre222@gmail.com")
        base_url = settings.app_base_url or ""

        body = (
            "AUTOMATED ALERT — TABOOST Email Pipeline Stall\n\n"
            f"{result['reason']}\n\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "Metrics:\n"
            f"  emails triaged in last {result['window_minutes']} min: {result['processed_in_window']}\n"
            f"  unread waiting in inbox:                {result['unread_waiting']}\n"
            f"  Score 3 emails in last hour:            {result['score3_last_hour']}\n"
            f"  drafts created in last hour:            {result['drafts_last_hour']}\n"
            f"  replies sent in last hour:              {result['sends_last_hour']}\n\n"
            "Note: the scheduler logs jobs as 'executed successfully' even when they\n"
            "throw, so Render logs will look normal. Check for QueryCanceled /\n"
            "OperationalError lines and the drafts-created-per-hour count instead.\n\n"
            f"Dashboard: {base_url}/dashboard\n"
            f"Health:    {base_url}/health\n"
        )

        delivered = []

        # 1. Webhook — works even when every Gmail token is dead.
        delivered.extend(send_out_of_band_alert(
            "ALERT: email automation pipeline stalled",
            f"{result['reason']}\nHealth: {base_url}/health",
        ))

        # 2. Email, through the first active talent token.
        token_row = db.query(TalentToken).filter(TalentToken.active.is_(True)).first()
        if not token_row:
            logger.warning("stall_alarm: no active token to send alert email")
        else:
            try:
                from backend.services.gmail import send_standalone_message
                send_standalone_message(
                    token_row, to=alert_email,
                    subject="ALERT: email automation pipeline stalled", body=body, db=db,
                )
                delivered.append(f"email:{alert_email}")
            except Exception as exc:  # noqa: BLE001
                logger.error("stall_alarm: alert email failed: %s", exc)

        if delivered:
            _set_state(db, _KEY_ALARM_SENT_AT, datetime.utcnow().isoformat())
            logger.info("stall_alarm: alert sent via %s", ", ".join(delivered))
        else:
            logger.error("stall_alarm: alert NOT delivered on any channel (no webhook/telegram, no active token)")
    except Exception as exc:  # noqa: BLE001
        logger.error("stall_alarm: alert send failed: %s", exc)
