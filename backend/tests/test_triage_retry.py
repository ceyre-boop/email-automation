"""Triage retry logic: transient OpenAI failures are non-terminal.

Before: any failure became a terminal score-2 ProcessedEmail row, which the dedupe
then excluded forever (281 real brand emails buried in the 2026-10 credit outage).
Now: retryable failures park as retry_pending with backoff and are re-claimed when due.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import EmailStatus, ProcessedEmail
from backend.services import poller
from backend.services import triage as triage_svc


# ── (a) retryable classification ────────────────────────────────────────────

def _run_triage(*, side_effect=None, content=None, finish_reason="stop"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].finish_reason = finish_reason
    with patch("backend.services.triage.OpenAI"), \
         patch("backend.services.triage._call_openai_with_retry",
               side_effect=side_effect, return_value=resp):
        return triage_svc.triage_email(
            talent_key="Sylvia", talent_name="Sylvia", minimum_rate=500.0,
            subject="Paid collab opportunity", sender="Jo <jo@brand.com>",
            sender_domain="brand.com", body="We'd love to work with you.",
        )


def test_fallback_default_not_retryable():
    assert triage_svc._fallback("Sylvia", "x")["retryable"] is False
    assert triage_svc._fallback("Sylvia", "x", retryable=True)["retryable"] is True


def test_api_error_is_retryable():
    r = _run_triage(side_effect=Exception("Error code: 429 - insufficient_quota"))
    assert r["score"] == 2 and r["retryable"] is True


def test_non_json_is_retryable():
    r = _run_triage(content="{not json")
    assert r["score"] == 2 and r["retryable"] is True


def test_truncation_is_retryable():
    r = _run_triage(content='{"score": 3', finish_reason="length")
    assert r["score"] == 2 and r["retryable"] is True


def test_schema_mismatch_not_retryable():
    r = _run_triage(content=json.dumps({"score": 3}))
    assert r["score"] == 2 and r["retryable"] is False


def test_invalid_score_not_retryable():
    r = _run_triage(content=json.dumps({
        "score": 9, "reason": "x", "offer_type": "Paid", "brand_name": "B",
        "proposed_rate_usd": 100,
    }))
    assert r["score"] == 2 and r["retryable"] is False


# ── (b) backoff math + give-up ──────────────────────────────────────────────

@pytest.mark.parametrize("count,expected", [(1, 2), (2, 4), (3, 8), (4, 16), (5, 30), (20, 30)])
def test_backoff_minutes(count, expected):
    assert poller._triage_backoff_minutes(count, 30) == expected


def _row(db, mid="m1", status=EmailStatus.processing, count=0, next_at=None):
    row = ProcessedEmail(
        talent_key="Sylvia", gmail_message_id=mid, thread_id="", sender="", subject="",
        score=0, status=status, triage_retry_count=count, triage_next_retry_at=next_at,
    )
    db.add(row)
    db.commit()
    return row


def test_park_sets_retry_pending_and_backoff(db_session):
    row = _row(db_session)
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert poller._park_for_triage_retry(db_session, "m1", now=now) is True
    db_session.refresh(row)
    assert row.status == EmailStatus.retry_pending
    assert row.triage_retry_count == 1
    assert row.triage_next_retry_at == now + timedelta(minutes=2)


def test_park_gives_up_at_max_retries(db_session):
    row = _row(db_session, count=25)
    assert poller._park_for_triage_retry(db_session, "m1") is False
    db_session.refresh(row)
    assert row.status == EmailStatus.processing  # untouched; caller finalizes
    assert row.triage_retry_count == 25


def test_park_honours_settings_max_retries(db_session):
    row = _row(db_session, count=2)
    with patch("backend.services.poller._triage_retry_config", return_value=(2, 30)):
        assert poller._park_for_triage_retry(db_session, "m1") is False


def test_default_retry_config():
    assert poller._triage_retry_config() == (25, 30)


# ── (c) dedupe ──────────────────────────────────────────────────────────────

def test_dedupe_includes_due_retry_excludes_not_due(db_session):
    now = datetime.utcnow()
    _row(db_session, "due", EmailStatus.retry_pending, 1, now - timedelta(minutes=1))
    _row(db_session, "later", EmailStatus.retry_pending, 1, now + timedelta(minutes=10))
    _row(db_session, "done", EmailStatus.flagged)
    _row(db_session, "proc", EmailStatus.processing)
    done = poller._batch_already_processed_ids(db_session, ["due", "later", "done", "proc", "new"])
    assert done == {"later", "done", "proc"}


# ── (d) claim re-entry ──────────────────────────────────────────────────────

def _run_process(db, triage_result):
    summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0, "unrouted": 0}
    detail = {"label_ids": ["INBOX"], "thread_id": "t", "subject": "Collab", "sender": "a@b.com",
              "sender_domain": "b.com", "body_text": "hello", "email_date": None,
              "message_id_header": "<x>"}
    with patch("backend.services.poller.gmail_svc.get_message_detail", return_value=detail), \
         patch("backend.services.poller.gmail_svc.thread_has_prior_sent_reply", return_value=False), \
         patch("backend.services.poller.triage_svc.triage_email", return_value=triage_result), \
         patch("backend.services.poller._record_external_channel"):
        poller._process_one_message(
            db, MagicMock(), "m1", "Sylvia", "Sylvia", 500.0, True, summary,
        )
    return summary


def test_due_retry_row_is_reclaimed_and_reparked(db_session):
    row = _row(db_session, "m1", EmailStatus.retry_pending, 1,
               datetime.utcnow() - timedelta(minutes=1))
    _run_process(db_session, triage_svc._fallback("Sylvia", "API error", retryable=True))
    db_session.expire_all()
    rows = db_session.query(ProcessedEmail).filter_by(gmail_message_id="m1").all()
    assert len(rows) == 1
    assert rows[0].status == EmailStatus.retry_pending
    assert rows[0].triage_retry_count == 2
    assert rows[0].triage_next_retry_at > datetime.utcnow()


def test_not_due_retry_row_is_not_reclaimed(db_session):
    _row(db_session, "m1", EmailStatus.retry_pending, 1,
         datetime.utcnow() + timedelta(minutes=10))
    assert poller._reclaim_due_retry(db_session, "m1") is False


def test_retry_success_finalizes_on_normal_path(db_session):
    _row(db_session, "m1", EmailStatus.retry_pending, 3, datetime.utcnow() - timedelta(minutes=1))
    ok = {"score": 1, "reason": "spam", "offer_type": "Spam", "proposed_rate_usd": None,
          "brand_name": "", "sentiment_score": 5, "urgency_score": 0, "risk_score": 0,
          "alternatives_considered": ""}
    with patch("backend.services.poller.gmail_svc.archive_as_spam"):
        s = _run_process(db_session, ok)
    db_session.expire_all()
    row = db_session.query(ProcessedEmail).filter_by(gmail_message_id="m1").one()
    assert row.status == EmailStatus.archived
    assert s["archived"] == 1


def test_exhausted_retries_finalize_as_flagged(db_session):
    _row(db_session, "m1", EmailStatus.retry_pending, 25, datetime.utcnow() - timedelta(minutes=1))
    s = _run_process(db_session, triage_svc._fallback("Sylvia", "API error", retryable=True))
    db_session.expire_all()
    row = db_session.query(ProcessedEmail).filter_by(gmail_message_id="m1").one()
    assert row.status == EmailStatus.flagged and row.score == 2
    assert s["flagged"] == 1


def test_fresh_message_still_inserts_claim(db_session):
    _run_process(db_session, triage_svc._fallback("Sylvia", "API error", retryable=True))
    row = db_session.query(ProcessedEmail).filter_by(gmail_message_id="m1").one()
    assert row.status == EmailStatus.retry_pending and row.triage_retry_count == 1
