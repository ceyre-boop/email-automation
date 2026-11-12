"""
Guardian admin endpoints — manual overrides, status, audit log, and HMAC kill switch.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.db import Draft, GuardianAuditLog, ProcessedEmail, TalentToken
from backend.routers.deps import get_db, verify_api_key
from backend.services.guardian import (
    GuardianWatchdog,
    _get_state,
    _log_audit,
    verify_kill_token,
)

router = APIRouter(tags=["guardian"])
logger = logging.getLogger(__name__)


class ReasonBody(BaseModel):
    reason: str = "Manual action"


# ── Alert channel test ────────────────────────────────────────────────────────

@router.post("/api/admin/alerts/test")
def test_alerts(_: str = Depends(verify_api_key)):
    """Send a test message to every configured out-of-band channel (webhook, Telegram)."""
    from backend.services import alerts
    results = {
        "webhook": alerts.post_alert_webhook("Test alert", "Alert channel test from the admin endpoint."),
        "telegram": alerts.post_alert_telegram("Test alert", "Alert channel test from the admin endpoint."),
    }
    return {
        "results": results,
        "succeeded": [k for k, v in results.items() if v],
        "failed_or_unconfigured": [k for k, v in results.items() if not v],
    }


# ── Manual kill switches ──────────────────────────────────────────────────────

@router.post("/api/admin/guardian/disable-ai")
def disable_ai(body: ReasonBody, db: Session = Depends(get_db), _: str = Depends(verify_api_key)):
    from backend.services.runtime_state import is_ai_enabled
    if not is_ai_enabled():
        return {"ok": True, "message": "ai_enabled was already false"}
    if not GuardianWatchdog()._set_ai_enabled(False, db=db):
        raise HTTPException(status_code=500, detail="Could not persist ai_enabled=false — see logs")
    _log_audit(db, "disable_ai", reason=body.reason, triggered_by="manual")
    return {"ok": True, "message": "ai_enabled set to false"}


@router.post("/api/admin/guardian/enable-ai")
def enable_ai(body: ReasonBody, db: Session = Depends(get_db), _: str = Depends(verify_api_key)):
    if not GuardianWatchdog()._set_ai_enabled(True, db=db):
        raise HTTPException(status_code=500, detail="Could not persist ai_enabled=true — see logs")
    # Clear the guardian disabled-at marker so recovery loop doesn't re-disable
    from backend.services.guardian import _set_state, _KEY_DISABLED_AT
    _set_state(db, _KEY_DISABLED_AT, "")
    _log_audit(db, "re_enable_ai", reason=body.reason, triggered_by="manual")
    return {"ok": True, "message": "ai_enabled set to true"}


def _resolve_talent_key(talent_key: str) -> str:
    """Canonical key from sop.md (case-insensitive) or 404."""
    from backend.core.config import get_settings
    for key in get_settings().talent_profiles:
        if key.lower() == talent_key.strip().lower():
            return key
    raise HTTPException(status_code=404, detail=f"Unknown talent: {talent_key}")


@router.post("/api/admin/guardian/pause-talent/{talent_key}")
def pause_talent(
    talent_key: str,
    body: ReasonBody,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    key = _resolve_talent_key(talent_key)
    if not GuardianWatchdog()._pause_talent(key, body.reason, db=db):
        raise HTTPException(status_code=500, detail=f"Could not persist pause for {key} — see logs")
    _log_audit(db, "pause_talent", reason=body.reason, talent_key=key, triggered_by="manual")
    return {"ok": True, "message": f"{key} paused"}


@router.post("/api/admin/guardian/unpause-talent/{talent_key}")
def unpause_talent(
    talent_key: str,
    body: ReasonBody,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """Clear a pause. Stored as an explicit paused=false override in app_state, so it
    also beats a ``Paused: yes`` left in sop.md. (This route used to edit a
    ``talents[]`` array in settings.json that no longer exists — it did nothing.)"""
    key = _resolve_talent_key(talent_key)
    if not GuardianWatchdog()._unpause_talent(key, body.reason, db=db):
        raise HTTPException(status_code=500, detail=f"Could not persist unpause for {key} — see logs")
    _log_audit(db, "unpause_talent", reason=body.reason, talent_key=key, triggered_by="manual")
    return {"ok": True, "message": f"{key} unpaused"}


# ── Status & audit ────────────────────────────────────────────────────────────

@router.get("/api/admin/guardian/status")
def guardian_status(db: Session = Depends(get_db), _: str = Depends(verify_api_key)):
    from backend.core.config import get_settings
    settings = get_settings()
    cfg = settings.app_config.get("guardian", {})
    from backend.services.runtime_state import is_ai_enabled
    ai_enabled = is_ai_enabled()
    now = datetime.utcnow()
    window_minutes = cfg.get("velocity_window_minutes", 10)
    since_window = now - timedelta(minutes=window_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    velocity_rows = (
        db.query(Draft.talent_key, func.count(Draft.id))
        .filter(Draft.created_at >= since_window)
        .group_by(Draft.talent_key)
        .all()
    )
    today_rows = (
        db.query(Draft.talent_key, func.count(Draft.id))
        .filter(Draft.created_at >= today_start)
        .group_by(Draft.talent_key)
        .all()
    )
    stuck_count = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.status == "processing",
            ProcessedEmail.processed_at < now - timedelta(minutes=cfg.get("stuck_processing_threshold_minutes", 5)),
        )
        .count()
    )
    recent_audit = (
        db.query(GuardianAuditLog)
        .order_by(GuardianAuditLog.created_at.desc())
        .limit(5)
        .all()
    )

    return {
        "ai_enabled": ai_enabled,
        "guardian_enabled": cfg.get("enabled", True),
        "last_run_at": _get_state(db, "guardian_last_run_at"),
        "last_trigger": _get_state(db, "guardian_last_trigger"),
        "ai_disabled_at": _get_state(db, "guardian_ai_disabled_at"),
        "stuck_processing_count": stuck_count,
        "draft_velocity_last_10min": {k: c for k, c in velocity_rows},
        "drafts_today": {k: c for k, c in today_rows},
        "recent_audit": [
            {"action": r.action, "talent_key": r.talent_key, "reason": r.reason, "created_at": r.created_at.isoformat()}
            for r in recent_audit
        ],
    }


@router.get("/api/admin/guardian/audit-log")
def guardian_audit_log(
    limit: int = 100,
    talent_key: str | None = None,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    q = db.query(GuardianAuditLog).order_by(GuardianAuditLog.created_at.desc())
    if talent_key:
        q = q.filter(GuardianAuditLog.talent_key == talent_key)
    rows = q.limit(limit).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "talent_key": r.talent_key,
            "reason": r.reason,
            "detail": r.detail,
            "triggered_by": r.triggered_by,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ── One-click kill link (no auth — used from alert email) ─────────────────────

@router.get("/api/guardian/kill", response_class=HTMLResponse)
def kill_switch(token: str = Query(...), db: Session = Depends(get_db)):
    from backend.core.config import get_settings
    settings = get_settings()
    secret = settings.agency_secret_key or settings.api_key or "guardian"
    if not verify_kill_token(token, secret):
        return HTMLResponse(
            "<h1>Link expired or invalid</h1><p>This kill link has expired (15-minute window). "
            "Use the dashboard or API to disable AI manually.</p>",
            status_code=403,
        )
    watcher = GuardianWatchdog()
    if not watcher._set_ai_enabled(False, db=db):
        return HTMLResponse(
            "<h1>Kill FAILED</h1><p>The switch could not be saved to the database, so AI is "
            "still ENABLED. Use <code>POST /api/admin/guardian/disable-ai</code> or check the logs.</p>",
            status_code=500,
        )
    from backend.services.guardian import _set_state, _KEY_DISABLED_AT
    _set_state(db, _KEY_DISABLED_AT, datetime.utcnow().isoformat())
    _log_audit(db, "disable_ai", reason="One-click kill link from alert email", triggered_by="kill_link")
    base_url = settings.app_base_url
    return HTMLResponse(
        f"<h1>AI drafting disabled ✓</h1>"
        f"<p>The system has been paused. No new drafts will be created.</p>"
        f"<p><a href='{base_url}/dashboard'>Return to dashboard</a></p>"
        f"<p>To re-enable, call: <code>POST {base_url}/api/admin/guardian/enable-ai</code></p>"
    )
