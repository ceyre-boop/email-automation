"""Bounded-growth data retention.

The safety property under test: `pending` drafts and `processed_emails` rows
must NEVER be deleted, no matter how old — pending drafts are unsent work,
and processed_emails is the de-dup key that stops the poller re-sending old
replies. Everything else purged here is either a terminal draft record or a
pure cache/log table that is safe to prune.
"""
from datetime import datetime, timedelta

import pytest

from backend.models.db import Draft, DraftStatus, InboxEmail, PollHealth, ProcessedEmail
from backend.services import retention as retention_svc
from backend.services.retention import run_retention


def _draft(db, status, days_ago, talent_key="Allee", suffix="1"):
    row = Draft(
        talent_key=talent_key,
        gmail_message_id=f"m-{status}-{suffix}",
        status=status,
        draft_text="body",
        created_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _inbox_email(db, days_ago, suffix="1"):
    row = InboxEmail(
        talent_key="allee",
        gmail_message_id=f"g-{suffix}",
        last_synced_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _poll_health(db, days_ago, suffix="1"):
    row = PollHealth(
        talent_key="allee",
        polled_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _processed_email(db, days_ago, suffix="1"):
    row = ProcessedEmail(
        talent_key="allee",
        gmail_message_id=f"p-{suffix}",
        processed_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _default_cfg(monkeypatch):
    """Use small, deterministic retention config for every test unless overridden."""
    monkeypatch.setattr(retention_svc, "_cfg", lambda: {
        "drafts_retention_days": 14,
        "inbox_cache_retention_days": 30,
        "poll_health_retention_days": 7,
        "batch_size": 500,
        "max_batches_per_run": 20,
    })


def test_pending_drafts_are_never_deleted_regardless_of_age(db_session):
    old_pending = _draft(db_session, DraftStatus.pending, days_ago=365, suffix="old-pending")

    summary = run_retention(db_session, dry_run=False)

    db_session.refresh(old_pending)
    assert old_pending.status == DraftStatus.pending
    assert summary["drafts"]["deleted"] == 0
    assert db_session.query(Draft).filter(Draft.id == old_pending.id).count() == 1


def test_terminal_drafts_past_cutoff_are_deleted(db_session):
    old_sent_id = _draft(db_session, DraftStatus.sent, days_ago=20, suffix="old-sent").id
    old_discarded_id = _draft(db_session, DraftStatus.discarded, days_ago=30, suffix="old-discarded").id

    summary = run_retention(db_session, dry_run=False)

    assert summary["drafts"]["deleted"] == 2
    assert db_session.query(Draft).filter(Draft.id == old_sent_id).count() == 0
    assert db_session.query(Draft).filter(Draft.id == old_discarded_id).count() == 0


def test_recent_rows_survive(db_session):
    recent_sent = _draft(db_session, DraftStatus.sent, days_ago=1, suffix="recent-sent")
    recent_inbox = _inbox_email(db_session, days_ago=1, suffix="recent-inbox")
    recent_poll = _poll_health(db_session, days_ago=1, suffix="recent-poll")

    summary = run_retention(db_session, dry_run=False)

    assert summary["drafts"]["deleted"] == 0
    assert summary["inbox_emails"]["deleted"] == 0
    assert summary["poll_health"]["deleted"] == 0
    assert db_session.query(Draft).filter(Draft.id == recent_sent.id).count() == 1
    assert db_session.query(InboxEmail).filter(InboxEmail.id == recent_inbox.id).count() == 1
    assert db_session.query(PollHealth).filter(PollHealth.id == recent_poll.id).count() == 1


def test_inbox_emails_purged_on_last_synced_at(db_session):
    old_inbox_id = _inbox_email(db_session, days_ago=45, suffix="old-inbox").id

    summary = run_retention(db_session, dry_run=False)

    assert summary["inbox_emails"]["deleted"] == 1
    assert db_session.query(InboxEmail).filter(InboxEmail.id == old_inbox_id).count() == 0


def test_poll_health_purged_past_cutoff(db_session):
    old_poll_id = _poll_health(db_session, days_ago=10, suffix="old-poll").id

    summary = run_retention(db_session, dry_run=False)

    assert summary["poll_health"]["deleted"] == 1
    assert db_session.query(PollHealth).filter(PollHealth.id == old_poll_id).count() == 0


def test_dry_run_deletes_nothing_but_reports_accurate_counts(db_session):
    _draft(db_session, DraftStatus.sent, days_ago=20, suffix="dry-sent")
    _draft(db_session, DraftStatus.discarded, days_ago=30, suffix="dry-discarded")
    _draft(db_session, DraftStatus.pending, days_ago=365, suffix="dry-pending")
    _inbox_email(db_session, days_ago=45, suffix="dry-inbox")
    _poll_health(db_session, days_ago=10, suffix="dry-poll")

    summary = run_retention(db_session, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["drafts"]["deleted"] == 2  # only the two terminal, past-cutoff rows
    assert summary["inbox_emails"]["deleted"] == 1
    assert summary["poll_health"]["deleted"] == 1

    # Nothing was actually removed.
    assert db_session.query(Draft).count() == 3
    assert db_session.query(InboxEmail).count() == 1
    assert db_session.query(PollHealth).count() == 1


def test_batching_respects_max_batches_per_run(db_session, monkeypatch):
    monkeypatch.setattr(retention_svc, "_cfg", lambda: {
        "drafts_retention_days": 14,
        "inbox_cache_retention_days": 30,
        "poll_health_retention_days": 7,
        "batch_size": 2,
        "max_batches_per_run": 2,
    })
    # 5 eligible terminal drafts; batch_size=2, max_batches=2 → at most 4 deleted.
    for i in range(5):
        _draft(db_session, DraftStatus.sent, days_ago=20, suffix=f"batch-{i}")

    summary = run_retention(db_session, dry_run=False)

    assert summary["drafts"]["deleted"] == 4
    assert summary["drafts"]["batches"] == 2
    assert db_session.query(Draft).filter(Draft.status == DraftStatus.sent).count() == 1


def test_processed_emails_is_never_touched(db_session):
    old_processed = _processed_email(db_session, days_ago=9999, suffix="ancient")

    summary = run_retention(db_session, dry_run=False)

    assert "processed_emails" not in summary
    assert db_session.query(ProcessedEmail).filter(ProcessedEmail.id == old_processed.id).count() == 1


def test_never_raises_on_error_and_returns_partial_summary(db_session, monkeypatch):
    def _boom():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(retention_svc, "_cfg", _boom)

    summary = run_retention(db_session, dry_run=False)

    assert "errors" in summary
    assert summary["errors"]
