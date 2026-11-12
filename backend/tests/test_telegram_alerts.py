"""Telegram alert channel: sender, fan-out, stall recovery, admin test endpoint."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.services import alerts, stall_alarm

TOKEN = "123456:SECRET-TOKEN"


@pytest.fixture
def tg(monkeypatch):
    monkeypatch.setattr(alerts, "_telegram_creds", lambda: (TOKEN, "42"))
    monkeypatch.setattr(alerts, "_webhook_url", lambda: "")
    monkeypatch.setattr(alerts, "_telegram_warned", False)


def test_telegram_posts_expected_request(tg):
    with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=200)) as post:
        assert alerts.post_alert_telegram("Title", "body") is True
    assert post.call_args.args[0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = post.call_args.kwargs["json"]
    assert payload["chat_id"] == "42"
    assert payload["text"] == "Title\nbody"
    assert payload["disable_web_page_preview"] is True
    assert post.call_args.kwargs["timeout"] == 10.0


def test_telegram_unset_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(alerts, "_telegram_creds", lambda: ("", "42"))
    monkeypatch.setattr(alerts, "_telegram_warned", False)
    with patch.object(alerts.httpx, "post") as post, caplog.at_level(logging.WARNING, logger="backend.services.alerts"):
        assert alerts.post_alert_telegram("t") is False
        assert alerts.post_alert_telegram("t") is False
    post.assert_not_called()
    assert len([r for r in caplog.records if "TELEGRAM_BOT_TOKEN" in r.getMessage()]) == 1


def test_telegram_failures_never_raise_or_log_token(tg, caplog):
    err = httpx.ConnectError(f"boom https://api.telegram.org/bot{TOKEN}/sendMessage")
    with caplog.at_level(logging.DEBUG, logger="backend.services.alerts"):
        with patch.object(alerts.httpx, "post", side_effect=err):
            assert alerts.post_alert_telegram("t") is False
        with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=401, text=TOKEN)):
            assert alerts.post_alert_telegram("t") is False
    assert caplog.records
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_fan_out_counts_telegram_as_delivered(tg):
    with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=200)):
        assert alerts.send_out_of_band_alert("t", "b") == ["telegram"]


def test_stall_alert_cooldown_starts_with_telegram_only(db_session, tg):
    from backend.services.guardian import _get_state
    result = {"reason": "stuck", "window_minutes": 30, "processed_in_window": 0, "unread_waiting": 5,
              "score3_last_hour": 0, "drafts_last_hour": 0, "sends_last_hour": 0}
    with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=200)):
        stall_alarm._maybe_send_alert(db_session, result, {})
    assert _get_state(db_session, stall_alarm._KEY_ALARM_SENT_AT)


def test_recovery_message_sent_once_after_stall_clears(db_session, tg):
    from backend.services.guardian import _set_state
    _set_state(db_session, stall_alarm._KEY_ALARM_SENT_AT, "2026-11-01T00:00:00")
    clear = {"stalled": False, "reason": None}
    with patch.object(stall_alarm, "check_pipeline_stall", return_value=clear), \
         patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=200)) as post:
        stall_alarm.run_stall_alarm(db_session)
        stall_alarm.run_stall_alarm(db_session)
    assert post.call_count == 1
    assert "✅ Pipeline recovered" in post.call_args.kwargs["json"]["text"]


def test_no_recovery_message_without_prior_alert(db_session, tg):
    clear = {"stalled": False, "reason": None}
    with patch.object(stall_alarm, "check_pipeline_stall", return_value=clear), \
         patch.object(alerts.httpx, "post") as post:
        stall_alarm.run_stall_alarm(db_session)
    post.assert_not_called()


def test_admin_test_endpoint_reports_channels(client, tg):
    with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=200)):
        r = client.post("/api/admin/alerts/test")
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == {"webhook": False, "telegram": True}
    assert body["succeeded"] == ["telegram"]


def test_admin_test_endpoint_requires_api_key(db_session, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.routers.deps import verify_api_key
    app.dependency_overrides.pop(verify_api_key, None)
    monkeypatch.setattr("backend.routers.deps.get_settings", lambda: MagicMock(api_key="k"))
    with TestClient(app) as c:
        assert c.post("/api/admin/alerts/test").status_code == 403
