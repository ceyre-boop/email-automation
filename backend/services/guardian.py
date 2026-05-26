"""
Guardian — self-healing watchdog for the email automation pipeline.

Runs every 60 seconds via APScheduler. Detects draft velocity anomalies,
auto-pauses individual talents or kills ai_enabled globally, emails an alert
with a one-click HMAC kill link, and schedules self-recovery.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"

# AppState keys used by guardian
_KEY_DISABLED_AT = "guardian_ai_disabled_at"
_KEY_ALERT_SENT_AT = "guardian_alert_last_sent_at"
_KEY_LAST_RUN_AT = "guardian_last_run_at"
_KEY_LAST_TRIGGER = "guardian_last_trigger"


# ── Token helpers ─────────────────────────────────────────────────────────────

def make_kill_token(secret: str) -> str:
    expiry = int(time.time()) + 900  # 15-minute window
    payload = f"kill:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}:{sig}"


def verify_kill_token(token: str, secret: str) -> bool:
    try:
        expiry_str, sig = token.split(":", 1)
        if time.time() > int(expiry_str):
            return False
        payload = f"kill:{expiry_str}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# ── Watchdog ──────────────────────────────────────────────────────────────────

class GuardianWatchdog:

    def __init__(self, scheduler=None):
        self._scheduler = scheduler

    def run(self, db: Session) -> None:
        try:
            from backend.core.config import get_settings
            cfg = get_settings().app_config.get("guardian", {})
            if not cfg.get("enabled", True):
                return

            velocity_by_talent, velocity_total = self._compute_velocity(db, cfg)

            triggers: list[dict] = []
            triggers += self._check_draft_velocity(db, cfg, velocity_by_talent, velocity_total)
            triggers += self._check_per_talent_caps(db, cfg)
            triggers += self._check_draft_email_ratio(db, cfg)
            self._check_stuck_processing(db, cfg)
            self._check_backlog_blaster_safety(velocity_total)
            self.maybe_schedule_recovery(db, cfg)

            for t in triggers:
                self._dispatch(db, t, cfg)

            _set_state(db, _KEY_LAST_RUN_AT, datetime.utcnow().isoformat())
            if triggers:
                _set_state(db, _KEY_LAST_TRIGGER, triggers[0]["type"])
        except Exception as exc:
            logger.error("Guardian.run failed: %s", exc)

    # ── Checks ────────────────────────────────────────────────────────────────

    def _compute_velocity(self, db: Session, cfg: dict) -> tuple[dict[str, int], int]:
        from backend.models.db import Draft
        window = cfg.get("velocity_window_minutes", 10)
        since = datetime.utcnow() - timedelta(minutes=window)
        rows = (
            db.query(Draft.talent_key, func.count(Draft.id))
            .filter(Draft.created_at >= since)
            .group_by(Draft.talent_key)
            .all()
        )
        by_talent = {k: c for k, c in rows}
        return by_talent, sum(by_talent.values())

    def _check_draft_velocity(
        self, db: Session, cfg: dict, by_talent: dict[str, int], total: int
    ) -> list[dict]:
        triggers = []
        hard_global = cfg.get("global_draft_hard_limit", 50)
        hard_talent = cfg.get("per_talent_draft_hard_limit", 30)
        warn_talent = cfg.get("per_talent_draft_warn_limit", 15)
        window = cfg.get("velocity_window_minutes", 10)

        if total >= hard_global:
            triggers.append({
                "type": "global_kill",
                "talent_key": None,
                "reason": f"Global draft velocity {total} in {window}min exceeds hard limit {hard_global}",
                "detail": {"velocity_by_talent": by_talent, "total": total},
            })
            return triggers  # global kill supersedes per-talent

        for talent_key, count in by_talent.items():
            if count >= hard_talent:
                triggers.append({
                    "type": "talent_pause",
                    "talent_key": talent_key,
                    "reason": f"{count} drafts in {window}min for {talent_key} (limit {hard_talent})",
                    "detail": {"count": count, "window_minutes": window},
                })
            elif count >= warn_talent:
                triggers.append({
                    "type": "talent_warn",
                    "talent_key": talent_key,
                    "reason": f"{count} drafts in {window}min for {talent_key} (warn at {warn_talent})",
                    "detail": {"count": count},
                })
        return triggers

    def _check_per_talent_caps(self, db: Session, cfg: dict) -> list[dict]:
        from backend.core.config import get_settings
        from backend.models.db import Draft
        triggers = []
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        talent_map = {t["key"].lower(): t for t in get_settings().app_config.get("talents", [])}
        rows = (
            db.query(Draft.talent_key, func.count(Draft.id))
            .filter(Draft.created_at >= today_start)
            .group_by(Draft.talent_key)
            .all()
        )
        for talent_key, count in rows:
            talent_cfg = talent_map.get(talent_key.lower(), {})
            cap = talent_cfg.get("max_drafts_per_day", cfg.get("default_max_drafts_per_day", 50))
            if count >= cap:
                triggers.append({
                    "type": "talent_pause",
                    "talent_key": talent_key,
                    "reason": f"{count} drafts today for {talent_key} exceeds daily cap {cap}",
                    "detail": {"count_today": count, "cap": cap},
                })
        return triggers

    def _check_draft_email_ratio(self, db: Session, cfg: dict) -> list[dict]:
        from backend.models.db import Draft, ProcessedEmail
        triggers = []
        since = datetime.utcnow() - timedelta(minutes=10)
        draft_count = db.query(Draft).filter(Draft.created_at >= since).count()
        email_count = db.query(ProcessedEmail).filter(ProcessedEmail.processed_at >= since).count()
        ratio = draft_count / max(email_count, 1)
        kill_ratio = cfg.get("draft_email_ratio_kill", 5.0)
        warn_ratio = cfg.get("draft_email_ratio_warn", 3.0)
        detail = {"drafts_10min": draft_count, "emails_10min": email_count, "ratio": round(ratio, 2)}
        if ratio >= kill_ratio and draft_count > 20:
            triggers.append({
                "type": "global_kill",
                "talent_key": None,
                "reason": f"Draft/email ratio {ratio:.1f}x exceeds kill threshold {kill_ratio}x",
                "detail": detail,
            })
        elif ratio >= warn_ratio and draft_count > 10:
            triggers.append({
                "type": "ratio_warn",
                "talent_key": None,
                "reason": f"Draft/email ratio {ratio:.1f}x above warning threshold {warn_ratio}x",
                "detail": detail,
            })
        return triggers

    def _check_stuck_processing(self, db: Session, cfg: dict) -> int:
        from backend.models.db import EmailStatus, ProcessedEmail
        threshold = cfg.get("stuck_processing_threshold_minutes", 5)
        cutoff = datetime.utcnow() - timedelta(minutes=threshold)
        stuck = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.status == EmailStatus.processing,
                ProcessedEmail.processed_at < cutoff,
            )
            .all()
        )
        if not stuck:
            return 0
        for row in stuck:
            row.status = EmailStatus.flagged
        try:
            db.commit()
        except Exception as exc:
            logger.warning("Guardian: failed to clear stuck rows: %s", exc)
            db.rollback()
            return 0
        count = len(stuck)
        _log_audit(db, "clear_stuck_processing", reason=f"Cleared {count} stuck processing rows older than {threshold}min",
                   detail=json.dumps({"count": count, "threshold_minutes": threshold}))
        return count

    def _check_backlog_blaster_safety(self, velocity_total: int) -> None:
        if self._scheduler is None:
            return
        try:
            job = self._scheduler.get_job("backlog_blaster")
            if job is None:
                return
            paused = job.next_run_time is None
            if velocity_total > 20 and not paused:
                self._scheduler.pause_job("backlog_blaster")
                logger.warning("Guardian: paused backlog_blaster (velocity=%d)", velocity_total)
            elif velocity_total <= 10 and paused:
                self._scheduler.resume_job("backlog_blaster")
                logger.info("Guardian: resumed backlog_blaster (velocity=%d)", velocity_total)
        except Exception as exc:
            logger.warning("Guardian: backlog_blaster safety check failed: %s", exc)

    def maybe_schedule_recovery(self, db: Session, cfg: dict) -> None:
        disabled_at_str = _get_state(db, _KEY_DISABLED_AT)
        if not disabled_at_str:
            return
        try:
            disabled_at = datetime.fromisoformat(disabled_at_str)
        except ValueError:
            return
        minutes_elapsed = (datetime.utcnow() - disabled_at).total_seconds() / 60
        wait = cfg.get("recovery_wait_minutes", 30)
        if minutes_elapsed < wait:
            return
        from backend.services.health import compute_health_score
        health = compute_health_score(db)
        threshold = cfg.get("recovery_health_threshold", 0.7)
        if health["score"] >= threshold:
            self._set_ai_enabled(True)
            _log_audit(db, "re_enable_ai",
                       reason=f"Auto-recovery: health={health['score']:.2f} after {minutes_elapsed:.0f}min pause",
                       detail=json.dumps({"health_score": health["score"], "minutes_paused": round(minutes_elapsed)}))
            _set_state(db, _KEY_DISABLED_AT, "")
            self._send_guardian_alert(db, "RECOVERY: AI drafting re-enabled automatically", None,
                                      {"health_score": health["score"], "minutes_paused": round(minutes_elapsed)}, cfg)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, db: Session, trigger: dict, cfg: dict) -> None:
        t = trigger["type"]
        talent_key = trigger.get("talent_key")
        reason = trigger["reason"]
        detail = trigger.get("detail", {})

        if t == "global_kill":
            self._set_ai_enabled(False)
            _set_state(db, _KEY_DISABLED_AT, datetime.utcnow().isoformat())
            _log_audit(db, "disable_ai", reason=reason, talent_key=None,
                       detail=json.dumps(detail))
            _log_marco(db, f"GUARDIAN: {reason}", talent_key=None, severity="critical")
            self._send_guardian_alert(db, f"ALERT: Global AI kill triggered — {reason}", None, detail, cfg)

        elif t == "talent_pause":
            # Idempotency: if talent is already paused, skip entirely.
            # Without this, the guardian fires _log_marco every 60s indefinitely
            # because the velocity window still shows the breach after pause.
            try:
                cfg_data = json.loads(_CONFIG_PATH.read_text())
                already_paused = next(
                    (bool(t_cfg.get("paused")) for t_cfg in cfg_data.get("talents", [])
                     if t_cfg.get("key", "").lower() == (talent_key or "").lower()),
                    False,
                )
                if already_paused:
                    logger.info("Guardian: %s already paused — skipping re-dispatch", talent_key)
                    return
            except Exception as exc:
                logger.warning("Guardian: could not check pause state for %s: %s", talent_key, exc)

            # Per-talent pause cooldown — defense-in-depth for the race window
            # between settings.json write and the next guardian cycle read.
            pause_key = f"guardian_pause_sent_at_{talent_key or 'global'}"
            last_pause_str = _get_state(db, pause_key)
            if last_pause_str:
                try:
                    last_pause = datetime.fromisoformat(last_pause_str)
                    if (datetime.utcnow() - last_pause).total_seconds() < cfg.get("alert_cooldown_minutes", 30) * 60:
                        logger.info("Guardian: pause cooldown active for %s — skipping", talent_key)
                        return
                except ValueError:
                    pass
            # Set cooldown key BEFORE the pause action — if _pause_talent fails
            # (settings write error), the cooldown still prevents 60s re-fire loops.
            _set_state(db, pause_key, datetime.utcnow().isoformat())

            self._pause_talent(talent_key, reason)
            _log_audit(db, "pause_talent", reason=reason, talent_key=talent_key,
                       detail=json.dumps(detail))
            _log_marco(db, f"GUARDIAN: {reason}", talent_key=talent_key, severity="critical")
            self._send_guardian_alert(db, f"ALERT: {talent_key} paused — {reason}", talent_key, detail, cfg)

        elif t in ("talent_warn", "ratio_warn"):
            # Cooldown: suppress repeated warn notifications for the same talent
            warn_cooldown = cfg.get("warn_cooldown_minutes", 30)
            warn_key = f"guardian_warn_sent_at_{talent_key or 'global'}"
            last_warn_str = _get_state(db, warn_key)
            if last_warn_str:
                try:
                    last_warn = datetime.fromisoformat(last_warn_str)
                    if (datetime.utcnow() - last_warn).total_seconds() < warn_cooldown * 60:
                        logger.info("Guardian: warn suppressed for %s (cooldown active)", talent_key)
                        return
                except ValueError:
                    pass
            _log_marco(db, f"GUARDIAN WARNING: {reason}", talent_key=talent_key, severity="warning")
            _set_state(db, warn_key, datetime.utcnow().isoformat())

    # ── Remediation ───────────────────────────────────────────────────────────

    def _set_ai_enabled(self, enabled: bool) -> None:
        try:
            data = json.loads(_CONFIG_PATH.read_text())
            data["ai_enabled"] = enabled
            _CONFIG_PATH.write_text(json.dumps(data, indent=2))
            logger.warning("Guardian: ai_enabled set to %s", enabled)
        except Exception as exc:
            logger.error("Guardian: failed to set ai_enabled=%s: %s", enabled, exc)

    def _pause_talent(self, talent_key: str, reason: str) -> None:
        try:
            data = json.loads(_CONFIG_PATH.read_text())
            for t in data.get("talents", []):
                if t.get("key", "").lower() == talent_key.lower():
                    t["paused"] = True
                    t["_guardian_paused_reason"] = reason
                    t["_guardian_paused_at"] = datetime.utcnow().isoformat()
            _CONFIG_PATH.write_text(json.dumps(data, indent=2))
            logger.warning("Guardian: paused talent %s", talent_key)
        except Exception as exc:
            logger.error("Guardian: failed to pause talent %s: %s", talent_key, exc)

    def _send_guardian_alert(
        self, db: Session, subject: str, talent_key: str | None, detail: dict[str, Any], cfg: dict
    ) -> None:
        try:
            cooldown = cfg.get("alert_cooldown_minutes", 30)
            last_sent_str = _get_state(db, _KEY_ALERT_SENT_AT)
            if last_sent_str:
                try:
                    last_sent = datetime.fromisoformat(last_sent_str)
                    if (datetime.utcnow() - last_sent).total_seconds() < cooldown * 60:
                        logger.info("Guardian: alert suppressed (cooldown active)")
                        return
                except ValueError:
                    pass

            from backend.core.config import get_settings
            settings = get_settings()
            secret = settings.agency_secret_key or settings.api_key or "guardian"
            kill_token = make_kill_token(secret)
            base_url = settings.app_base_url or os.environ.get("APP_BASE_URL", "")
            kill_url = f"{base_url}/api/guardian/kill?token={kill_token}"
            dashboard_url = f"{base_url}/dashboard"
            alert_email = cfg.get("alert_email", "colineyre222@gmail.com")

            body = (
                f"AUTOMATED ALERT — TABOOST Email Guardian\n\n"
                f"Trigger: {subject}\n"
                f"Talent: {talent_key or 'ALL (global)'}\n"
                f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Details:\n{json.dumps(detail, indent=2)}\n\n"
                f"ONE-CLICK KILL (disables all AI drafting — expires in 15 min):\n{kill_url}\n\n"
                f"Dashboard: {dashboard_url}\n\n"
                f"To re-enable: POST {base_url}/api/admin/guardian/enable-ai\n"
                f"with header: x-api-key: <your-api-key>\n"
            )

            from backend.models.db import TalentToken
            token_row = db.query(TalentToken).filter(TalentToken.active == True).first()  # noqa: E712
            if token_row:
                from backend.services.gmail import send_standalone_message
                send_standalone_message(token_row, to=alert_email, subject=subject, body=body, db=db)
                _set_state(db, _KEY_ALERT_SENT_AT, datetime.utcnow().isoformat())
                _log_audit(db, "send_alert", reason=subject, talent_key=talent_key,
                           detail=json.dumps({"to": alert_email}))
            else:
                logger.warning("Guardian: no active token to send alert email")
        except Exception as exc:
            logger.error("Guardian: alert send failed: %s", exc)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_state(db: Session, key: str) -> str | None:
    from backend.models.db import AppState
    try:
        row = db.query(AppState).filter(AppState.key == key).first()
        return row.value_text if row else None
    except Exception:
        return None


def _set_state(db: Session, key: str, value: str) -> None:
    from backend.models.db import AppState
    try:
        row = db.query(AppState).filter(AppState.key == key).first()
        if not row:
            row = AppState(key=key)
        row.value_text = value
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("Guardian._set_state failed for %s: %s", key, exc)


def _log_audit(
    db: Session,
    action: str,
    reason: str,
    talent_key: str | None = None,
    detail: str | None = None,
    triggered_by: str = "guardian",
) -> None:
    from backend.models.db import GuardianAuditLog
    try:
        row = GuardianAuditLog(
            action=action,
            talent_key=talent_key,
            reason=reason,
            detail=detail,
            triggered_by=triggered_by,
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("Guardian._log_audit failed: %s", exc)


def _log_marco(
    db: Session, message: str, talent_key: str | None, severity: str = "critical",
    dedup_minutes: int = 30,
) -> None:
    from backend.models.db import MarcoMessage
    try:
        # Suppress identical guardian messages within dedup window — prevents
        # Marco's Activity Hub from filling with the same alert every 60s.
        since = datetime.utcnow() - timedelta(minutes=dedup_minutes)
        existing = (
            db.query(MarcoMessage)
            .filter(
                MarcoMessage.message == message,
                MarcoMessage.talent_key == talent_key,
                MarcoMessage.category == "guardian",
                MarcoMessage.created_at >= since,
            )
            .first()
        )
        if existing:
            logger.info("Guardian._log_marco: suppressed duplicate '%s' for %s", message[:60], talent_key)
            return
        row = MarcoMessage(
            message=message,
            category="guardian",
            talent_key=talent_key,
            severity=severity,
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("Guardian._log_marco failed: %s", exc)
