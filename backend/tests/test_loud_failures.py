"""Dead tokens, alert webhook, heartbeat and poll-failure recording."""
from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from backend.models.db import PollHealth, TalentToken
from backend.services import alerts, oauth
from backend.services.oauth import TokenRefreshError, record_token_failure, reset_token_failure
from backend.tests.conftest import make_token


class _NoClose:
    """Wrap the test session so worker code can 'close' it without ending the test."""

    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.fixture
def webhook_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "post_alert_webhook", lambda title, body="": calls.append((title, body)) or True)
    return calls


# ── (a) 3 consecutive failures before deactivation ───────────────────────────

def test_token_survives_two_rejections_and_dies_on_the_third(db_session, webhook_calls):
    tok = make_token(db_session)
    exc = TokenRefreshError("Google rejected token refresh: invalid_grant")
    assert record_token_failure(db_session, tok, exc) is False
    assert record_token_failure(db_session, tok, exc) is False
    assert tok.active is True and tok.consecutive_failures == 2 and tok.deactivated_at is None
    assert record_token_failure(db_session, tok, exc) is True
    db_session.refresh(tok)
    assert tok.active is False
    assert tok.consecutive_failures == 3
    assert "invalid_grant" in tok.last_error
    assert isinstance(tok.deactivated_at, datetime)
    assert len(webhook_calls) == 1 and tok.talent_key in webhook_calls[0][0]


def test_non_auth_failures_never_deactivate(db_session, webhook_calls):
    tok = make_token(db_session)
    for _ in range(6):
        record_token_failure(db_session, tok, RuntimeError("503 from Gmail"), auth_rejected=False)
    assert tok.active is True and tok.consecutive_failures == 6
    assert tok.deactivated_at is None and webhook_calls == []


def test_reconnect_clears_deactivation(db_session, webhook_calls):
    tok = make_token(db_session)
    for _ in range(3):
        record_token_failure(db_session, tok, TokenRefreshError("x"))
    assert tok.active is False
    tok.active = True
    db_session.commit()
    reset_token_failure(db_session, tok.talent_key)
    db_session.refresh(tok)
    assert tok.deactivated_at is None and tok.consecutive_failures == 0 and tok.last_error is None


def test_proactive_refresh_deactivates_only_after_three_rejections(db_session, webhook_calls):
    from google.auth.exceptions import RefreshError
    tok = make_token(db_session)
    tok.token_expiry = datetime.utcnow()
    db_session.commit()
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("invalid_grant")
    with patch.object(oauth, "credentials_from_token_row", return_value=creds):
        r1 = oauth.proactive_refresh_all_tokens(db_session)
        r2 = oauth.proactive_refresh_all_tokens(db_session)
        assert (r1["deactivated"], r2["deactivated"]) == (0, 0) and tok.active is True
        r3 = oauth.proactive_refresh_all_tokens(db_session)
    assert r3["deactivated"] == 1 and tok.active is False and tok.deactivated_at is not None


def test_proactive_refresh_network_errors_do_not_deactivate(db_session, webhook_calls):
    tok = make_token(db_session)
    tok.token_expiry = datetime.utcnow()
    db_session.commit()
    creds = MagicMock()
    creds.refresh.side_effect = ConnectionError("network down")
    with patch.object(oauth, "credentials_from_token_row", return_value=creds):
        for _ in range(5):
            oauth.proactive_refresh_all_tokens(db_session)
    assert tok.active is True and tok.consecutive_failures == 5


def test_poller_needs_three_rejections_before_deactivating(db_session, webhook_calls):
    from backend.services import poller
    tok = make_token(db_session, talent_key="Sylvia")
    profile = MagicMock(key="Sylvia", full_name="Sylvia", minimum_rate_usd=500, manager="")
    with patch.object(poller, "_get_session_factory", return_value=lambda: _NoClose(db_session)), \
         patch.object(poller, "sync_inbox_for_talent"), patch.object(poller, "fetch_pending_bodies"), \
         patch.object(poller.gmail_svc, "list_unread_inbox_messages", side_effect=TokenRefreshError("invalid_grant")):
        for expected_active in (True, True, False):
            poller._poll_one_talent_locked(tok.id, profile, True)
            db_session.refresh(tok)
            assert tok.active is expected_active
    assert tok.deactivated_at is not None and "invalid_grant" in tok.last_error


def test_dead_tokens_populated_on_health_and_dashboard(client, db_session, monkeypatch, webhook_calls):
    from backend.models import db as m
    from backend.routers import cron

    dead = make_token(db_session, talent_key="Deadtalent", active=False)
    dead.deactivated_at = datetime(2026, 11, 1, 12, 0)
    dead.last_error = "TokenRefreshError: invalid_grant"
    dead.consecutive_failures = 3
    quiet = make_token(db_session, talent_key="Aliasonly", active=False)  # deliberately inactive
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(m, "get_session_factory", lambda: factory)
    cron._pipeline_cache = None
    try:
        pipeline = client.get("/health").json()["pipeline"]
    finally:
        cron._pipeline_cache = None
    assert pipeline["dead_tokens"] == ["Deadtalent"]
    detail = pipeline["dead_token_details"][0]
    assert detail["reason"] == "TokenRefreshError: invalid_grant"
    assert detail["deactivated_at"].startswith("2026-11-01T12:00")

    rows = {r["talent_key"]: r for r in client.get("/api/dashboard/health/tokens").json()}
    assert rows["Deadtalent"]["dead"] is True
    assert rows["Deadtalent"]["deactivated_at"].startswith("2026-11-01")
    assert rows["Aliasonly"]["dead"] is False


# ── (b) ALERT_WEBHOOK_URL ────────────────────────────────────────────────────

def test_webhook_unset_logs_warning_once(monkeypatch, caplog):
    monkeypatch.setattr(alerts, "_webhook_url", lambda: "")
    monkeypatch.setattr(alerts, "_webhook_warned", False)
    with caplog.at_level(logging.WARNING, logger="backend.services.alerts"):
        assert alerts.post_alert_webhook("t", "b") is False
        assert alerts.post_alert_webhook("t", "b") is False
    warnings = [r for r in caplog.records if "ALERT_WEBHOOK_URL" in r.getMessage()]
    assert len(warnings) == 1


def test_webhook_posts_slack_compatible_json(monkeypatch):
    monkeypatch.setattr(alerts, "_webhook_url", lambda: "https://hooks.example/abc")
    resp = MagicMock(status_code=200)
    with patch.object(alerts.httpx, "post", return_value=resp) as post:
        assert alerts.post_alert_webhook("Pipeline stalled", "details here") is True
    url = post.call_args.args[0]
    payload = post.call_args.kwargs["json"]
    assert url == "https://hooks.example/abc"
    assert "Pipeline stalled" in payload["text"] and "details here" in payload["text"]


def test_webhook_failures_never_raise(monkeypatch):
    monkeypatch.setattr(alerts, "_webhook_url", lambda: "https://hooks.example/abc")
    with patch.object(alerts.httpx, "post", side_effect=httpx.ConnectError("boom")):
        assert alerts.post_alert_webhook("t") is False
    with patch.object(alerts.httpx, "post", return_value=MagicMock(status_code=500, text="err")):
        assert alerts.post_alert_webhook("t") is False


def test_guardian_alert_goes_to_webhook_and_email(db_session, webhook_calls):
    from backend.services.guardian import GuardianWatchdog
    make_token(db_session)
    with patch("backend.services.gmail.send_standalone_message") as send:
        GuardianWatchdog()._send_guardian_alert(db_session, "ALERT: test", None, {"a": 1}, {})
    assert len(webhook_calls) == 1 and "ALERT: test" in webhook_calls[0][0]
    send.assert_called_once()
    assert "kill?token" not in webhook_calls[0][1], "bearer kill link must stay out of the webhook"


def test_guardian_alert_still_delivered_when_every_gmail_token_is_dead(db_session, webhook_calls):
    from backend.services.guardian import GuardianWatchdog, _KEY_ALERT_SENT_AT, _get_state
    make_token(db_session, active=False)
    with patch("backend.services.gmail.send_standalone_message") as send:
        GuardianWatchdog()._send_guardian_alert(db_session, "ALERT: test", None, {}, {})
    send.assert_not_called()
    assert len(webhook_calls) == 1
    assert _get_state(db_session, _KEY_ALERT_SENT_AT)


def test_stall_alarm_goes_to_webhook_and_email_and_survives_dead_tokens(db_session, webhook_calls):
    from backend.services.stall_alarm import _maybe_send_alert
    result = {"reason": "NOTHING PROCESSED", "window_minutes": 30, "processed_in_window": 0,
              "unread_waiting": 3, "score3_last_hour": 0, "drafts_last_hour": 0, "sends_last_hour": 0}
    make_token(db_session, active=False)
    with patch("backend.services.gmail.send_standalone_message") as send:
        _maybe_send_alert(db_session, result, {})
    send.assert_not_called()
    assert len(webhook_calls) == 1 and "stalled" in webhook_calls[0][0]

    make_token(db_session, talent_key="Live", email="live@example.com")
    from backend.services.guardian import _set_state
    _set_state(db_session, "stall_alarm_last_sent_at", "")
    with patch("backend.services.gmail.send_standalone_message") as send:
        _maybe_send_alert(db_session, result, {})
    send.assert_called_once()


# ── (c) heartbeat + (d) poll failure recording ───────────────────────────────

@pytest.fixture
def poll_env(db_session, monkeypatch):
    from backend.models import db as m
    monkeypatch.setattr(m, "get_session_factory", lambda: (lambda: _NoClose(db_session)))
    return db_session


def test_heartbeat_pinged_only_after_successful_poll(poll_env):
    from backend.routers import cron
    with patch.object(cron, "poll_all_inboxes", return_value={"processed": 1}), \
         patch("backend.services.alerts.ping_heartbeat") as ping:
        cron._run_poll()
    ping.assert_called_once()


def test_failed_poll_is_logged_with_traceback_recorded_and_not_pinged(poll_env, caplog):
    from backend.routers import cron
    with patch.object(cron, "poll_all_inboxes", side_effect=RuntimeError("db exploded")), \
         patch("backend.services.alerts.ping_heartbeat") as ping, \
         caplog.at_level(logging.ERROR, logger="backend.routers.cron"):
        cron._run_poll()
    ping.assert_not_called()
    rec = next(r for r in caplog.records if r.levelno == logging.ERROR and "Poll cycle FAILED" in r.getMessage())
    assert rec.exc_info is not None, "traceback must be attached"
    row = poll_env.query(PollHealth).filter(PollHealth.talent_key == cron.POLL_CYCLE_KEY).one()
    assert "db exploded" in row.error_message and "Traceback" in row.error_message


def test_heartbeat_noop_when_unset_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(alerts, "_heartbeat_url", lambda: "")
    with patch.object(alerts.httpx, "get") as get:
        assert alerts.ping_heartbeat() is False
    get.assert_not_called()
    monkeypatch.setattr(alerts, "_heartbeat_url", lambda: "https://hc-ping.example/uuid")
    with patch.object(alerts.httpx, "get", side_effect=httpx.ConnectError("x")):
        assert alerts.ping_heartbeat() is False
    with patch.object(alerts.httpx, "get", return_value=MagicMock(status_code=200)) as get:
        assert alerts.ping_heartbeat() is True
    assert get.call_args.args[0] == "https://hc-ping.example/uuid"


def test_skipped_poll_does_not_ping(poll_env):
    from backend.routers import cron
    from backend.services.locks import advisory_lock
    with patch.object(cron, "poll_all_inboxes") as poll, patch("backend.services.alerts.ping_heartbeat") as ping:
        with advisory_lock("job:poll"):
            assert cron._run_poll() is None
    poll.assert_not_called()
    ping.assert_not_called()
