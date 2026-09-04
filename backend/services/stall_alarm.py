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

  A. NOTHING PROCESSED — no ProcessedEmail rows for `stall_minutes` while unread
     mail is sitting in the inbox cache. Triage/polling is down.
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


def check_pipeline_stall(db: Session) -> dict:
    """Return the pipeline liveness picture. Never raises."""
    now = datetime.utcnow()
    cfg = _cfg()
    stall_minutes = int(cfg.get("stall_minutes", DEFAULT_STALL_MINUTES))
    since = now - timedelta(minutes=stall_minutes)
    hour_ago = now - timedelta(minutes=60)

    try:
        processed_in_window = (
            db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since).count()
        )
        unread_waiting = db.query(InboxEmail).filter(InboxEmail.is_unread.is_(True)).count()
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("stall_alarm: metric query failed: %s", exc)
        return {"stalled": False, "reason": None, "error": str(exc)[:200]}

    metrics = {
        "window_minutes": stall_minutes,
        "processed_in_window": processed_in_window,
        "unread_waiting": unread_waiting,
        "score3_last_hour": score3_last_hour,
        "drafts_last_hour": drafts_last_hour,
        "sends_last_hour": sends_last_hour,
    }

    reason = None
    if processed_in_window == 0 and unread_waiting > 0:
        reason = (
            f"NOTHING PROCESSED — 0 emails triaged in {stall_minutes} min "
            f"while {unread_waiting} unread sit in the inbox. Polling or triage is down."
        )
    elif score3_last_hour > 0 and drafts_last_hour == 0:
        reason = (
            f"NOTHING DRAFTED — {score3_last_hour} Score 3 email(s) in the last hour "
            f"produced 0 drafts. Triage is working, draft creation is not."
        )

    return {"stalled": reason is not None, "reason": reason, **metrics}


def run_stall_alarm(db: Session) -> dict:
    """Scheduled entry point: check, log loudly, and email once per cooldown."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"stalled": False, "reason": None, "disabled": True}

    result = check_pipeline_stall(db)
    if not result.get("stalled"):
        return result

    logger.error("STALL ALARM: %s | metrics=%s", result["reason"], json.dumps(
        {k: v for k, v in result.items() if k not in ("stalled", "reason")}
    ))
    _maybe_send_alert(db, result, cfg)
    return result


def _maybe_send_alert(db: Session, result: dict, cfg: dict) -> None:
    """Email the alarm, at most once per cooldown. Never raises."""
    from backend.services.guardian import _get_state, _set_state

    try:
        cooldown = int(cfg.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
        last_sent_str = _get_state(db, _KEY_ALARM_SENT_AT)
        if last_sent_str:
            try:
                last_sent = datetime.fromisoformat(last_sent_str)
                if (datetime.utcnow() - last_sent).total_seconds() < cooldown * 60:
                    logger.info("stall_alarm: email suppressed (cooldown active)")
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

        token_row = db.query(TalentToken).filter(TalentToken.active.is_(True)).first()
        if not token_row:
            logger.warning("stall_alarm: no active token to send alert email")
            return

        from backend.services.gmail import send_standalone_message
        send_standalone_message(
            token_row, to=alert_email,
            subject="ALERT: email automation pipeline stalled", body=body, db=db,
        )
        _set_state(db, _KEY_ALARM_SENT_AT, datetime.utcnow().isoformat())
        logger.info("stall_alarm: alert emailed to %s", alert_email)
    except Exception as exc:  # noqa: BLE001
        logger.error("stall_alarm: alert send failed: %s", exc)
