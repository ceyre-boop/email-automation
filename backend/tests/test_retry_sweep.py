"""Stranded retry_pending rows are swept, counted in the stall alarm and /health."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.core.config import get_settings
from backend.models.db import EmailStatus, ProcessedEmail
from backend.services import retry_sweep
from backend.services import triage as triage_svc
from backend.services.stall_alarm import check_pipeline_stall


@pytest.fixture
def talent_key():
    return next(k for k, p in get_settings().talent_profiles.items() if not p.paused)


def _row(db, mid, key, status=EmailStatus.retry_pending, count=1, next_at=None):
    db.add(ProcessedEmail(
        talent_key=key, gmail_message_id=mid, thread_id="", sender="", subject="",
        score=0, status=status, triage_retry_count=count, triage_next_retry_at=next_at,
        processed_at=datetime.utcnow() - timedelta(hours=1),
    ))
    db.commit()


def _ago(minutes):
    return datetime.utcnow() - timedelta(minutes=minutes)


DETAIL = {"label_ids": ["INBOX"], "thread_id": "t", "subject": "Collab", "sender": "a@b.com",
          "sender_domain": "b.com", "body_text": "hello", "email_date": None,
          "message_id_header": "<x>"}
SPAM = {"score": 1, "reason": "spam", "offer_type": "Spam", "proposed_rate_usd": None,
        "brand_name": "", "sentiment_score": 5, "urgency_score": 0, "risk_score": 0,
        "alternatives_considered": ""}


def _sweep(db, triage_result=None, detail=DETAIL, detail_error=None):
    token = MagicMock()
    with patch("backend.services.inbox_routing.resolve_token_for_talent", return_value=token), \
         patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.poller.gmail_svc.get_message_detail",
               side_effect=detail_error, return_value=detail) as get_detail, \
         patch("backend.services.poller.gmail_svc.thread_has_prior_sent_reply", return_value=False), \
         patch("backend.services.poller.gmail_svc.archive_as_spam"), \
         patch("backend.services.poller.triage_svc.triage_email",
               return_value=triage_result or SPAM), \
         patch("backend.services.poller._record_external_channel"), \
         patch("backend.services.gmail.list_unread_inbox_ids", create=True) as listing:
        out = retry_sweep.sweep_stranded_retries(db)
    return out, get_detail, listing


def _get(db, mid):
    db.expire_all()
    return db.query(ProcessedEmail).filter_by(gmail_message_id=mid).one()


def test_overdue_row_is_reprocessed_by_id_without_the_inbox_listing(db_session, talent_key):
    _row(db_session, "m1", talent_key, next_at=_ago(20))
    out, get_detail, listing = _sweep(db_session)
    assert out["reprocessed"] == 1
    assert get_detail.call_args.args[1] == "m1"
    listing.assert_not_called()
    assert _get(db_session, "m1").status == EmailStatus.archived


def test_not_yet_overdue_row_is_left_alone(db_session, talent_key):
    _row(db_session, "m1", talent_key, next_at=_ago(5))
    _row(db_session, "m2", talent_key, next_at=datetime.utcnow() + timedelta(minutes=10))
    out, get_detail, _ = _sweep(db_session)
    assert out["examined"] == 0
    get_detail.assert_not_called()
    assert _get(db_session, "m1").status == EmailStatus.retry_pending


def test_exhausted_row_is_swept_even_when_not_due(db_session, talent_key):
    _row(db_session, "m1", talent_key, count=25, next_at=datetime.utcnow() + timedelta(minutes=20))
    out, _, _ = _sweep(db_session, triage_result=triage_svc._fallback(talent_key, "API error", retryable=True))
    row = _get(db_session, "m1")
    assert out["examined"] == 1
    assert row.status == EmailStatus.flagged and row.score == 2
    assert "retries exhausted" in row.triage_reason
    assert row.triage_reason.startswith("Triage fallback")  # health metric keys on this prefix


def test_exhausted_row_whose_message_cannot_be_fetched_is_finalized_flagged(db_session, talent_key):
    _row(db_session, "m1", talent_key, count=25, next_at=_ago(60))
    out, _, _ = _sweep(db_session, detail_error=RuntimeError("gmail down"))
    row = _get(db_session, "m1")
    assert row.status == EmailStatus.flagged and row.score == 2
    assert "Triage retries exhausted" in row.triage_reason and "human review" in row.triage_reason
    assert out["finalized_exhausted"] == 1


def test_overdue_row_reparked_with_backoff_when_triage_still_failing(db_session, talent_key):
    _row(db_session, "m1", talent_key, count=2, next_at=_ago(30))
    _sweep(db_session, triage_result=triage_svc._fallback(talent_key, "API error", retryable=True))
    row = _get(db_session, "m1")
    assert row.status == EmailStatus.retry_pending
    assert row.triage_retry_count == 3
    assert row.triage_next_retry_at > datetime.utcnow()


def test_failure_on_overdue_row_counts_an_attempt_and_reparks(db_session, talent_key):
    _row(db_session, "m1", talent_key, count=2, next_at=_ago(30))
    out, _, _ = _sweep(db_session, detail_error=RuntimeError("gmail down"))
    row = _get(db_session, "m1")
    assert out["errors"] == 1
    assert row.status == EmailStatus.retry_pending and row.triage_retry_count == 3


def test_stall_alarm_counts_stranded_retries(db_session, talent_key):
    _row(db_session, "old", talent_key, next_at=_ago(30))
    _row(db_session, "fresh", talent_key, next_at=_ago(2))
    _row(db_session, "done", talent_key, status=EmailStatus.flagged, next_at=_ago(60))
    result = check_pipeline_stall(db_session)
    assert result["retry_pending_overdue"] == 1
    assert result["stalled"] is True and "STRANDED RETRIES" in result["reason"]


def test_stall_alarm_quiet_when_nothing_stranded(db_session, talent_key):
    _row(db_session, "fresh", talent_key, next_at=_ago(2))
    result = check_pipeline_stall(db_session)
    assert result["retry_pending_overdue"] == 0
    assert result["stalled"] is False


def test_health_pipeline_block_exposes_retry_pending_overdue(db_session, talent_key, monkeypatch):
    """Do not point the global get_session_factory at the shared in-memory SQLite engine:
    a scheduler thread leaked by an earlier test would then use that connection from a
    second thread and segfault. Patch the stall check to run on the test session instead."""
    from backend.routers import cron
    from backend.services import stall_alarm

    _row(db_session, "old", talent_key, next_at=_ago(30))
    real = stall_alarm.check_pipeline_stall
    monkeypatch.setattr("backend.models.db.get_session_factory", lambda: (lambda: MagicMock()))
    monkeypatch.setattr(stall_alarm, "check_pipeline_stall", lambda _db: real(db_session))
    monkeypatch.setattr(cron, "_pool_block", lambda: {})
    monkeypatch.setattr(cron, "_pipeline_cache", None)
    block = cron._pipeline_block()
    assert block["retry_pending_overdue"] == 1


def test_claim_reaper_job_runs_the_sweep(monkeypatch):
    from backend.routers import cron
    with patch("backend.services.claim_reaper.reap_stale_claims") as reap, \
         patch("backend.services.retry_sweep.sweep_stranded_retries") as sweep:
        cron._run_claim_reaper()
    reap.assert_called_once()
    sweep.assert_called_once()
