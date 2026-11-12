"""2026-11-12: DB statement timeouts made every stall-metric query fail, and the
alarm reported stalled=False — /health said "ok" and the banner stayed green
while drafting and sending crawled. A failed check must read as "unknown",
never as "fine", and must alert once it persists.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.services.stall_alarm as sa


def test_metric_failure_reports_unknown_not_healthy(db_session):
    with patch.object(db_session, "query", side_effect=RuntimeError("statement timeout")), \
         patch.object(sa, "_dead_tokens", return_value=([], [])):
        result = sa.check_pipeline_stall(db_session)
    assert result["stalled"] is None
    assert result["reason"] == sa.METRICS_UNAVAILABLE_REASON
    assert "statement timeout" in result["error"]


def test_unknown_never_sends_recovery_and_alerts_after_streak(db_session):
    unknown = {"stalled": None, "reason": sa.METRICS_UNAVAILABLE_REASON, "error": "statement timeout"}
    sa._unknown_streak = 0
    with patch.object(sa, "check_pipeline_stall", return_value=unknown), \
         patch.object(sa, "_maybe_send_recovery") as recovery, \
         patch.object(sa, "_maybe_send_alert") as alert:
        for _ in range(sa.UNKNOWN_ALERT_AFTER - 1):
            sa.run_stall_alarm(db_session)
        assert alert.call_count == 0
        sa.run_stall_alarm(db_session)
        assert alert.call_count == 1
        sent = alert.call_args[0][1]
        assert sent["stalled"] is True and "DATABASE TOO SLOW" in sent["reason"]
        recovery.assert_not_called()
    sa._unknown_streak = 0


def test_health_status_degraded_when_pipeline_unknown():
    from backend.main import app
    import backend.routers.cron as cron
    payload = {"stalled": None, "reason": sa.METRICS_UNAVAILABLE_REASON, "error": "statement timeout"}
    with patch.object(cron, "_pipeline_block", return_value=payload):
        data = TestClient(app).get("/health").json()
    assert data["status"] == "degraded"
    assert data["pipeline"]["stalled"] is None


def test_health_status_stalled_when_pipeline_stalled():
    from backend.main import app
    import backend.routers.cron as cron
    with patch.object(cron, "_pipeline_block", return_value={"stalled": True, "reason": "NOTHING PROCESSED"}):
        data = TestClient(app).get("/health").json()
    assert data["status"] == "stalled"


def test_db_slow_alert_really_delivers_with_partial_metrics(db_session):
    """Reviewer catch: the alert body read metric keys the unknown result lacks,
    raised KeyError, and nothing was ever delivered."""
    alert = {"stalled": True, "reason": "DATABASE TOO SLOW — x", "error": "statement timeout"}
    with patch("backend.services.alerts.send_out_of_band_alert", return_value=["telegram"]) as oob:
        sa._maybe_send_alert(db_session, alert, {}, cooldown_key=sa._KEY_DBSLOW_SENT_AT)
    oob.assert_called_once()
    from backend.services.guardian import _get_state
    assert _get_state(db_session, sa._KEY_DBSLOW_SENT_AT)
    assert not _get_state(db_session, sa._KEY_ALARM_SENT_AT)
