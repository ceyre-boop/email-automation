"""Inbox sync must not rewrite rows that did not change.

2026-09-06: the Supabase instance hit 100% CPU, 100% memory and 82% disk IO and
stopped answering even a count(*) over 1,400 rows. Cause: this sync assigned
last_synced_at (plus is_unread/label_ids/triage_status) on every cached email
every cycle, so SQLAlchemy flushed an UPDATE per row — roughly 1,300 rows across
talents every 45 seconds. Every Postgres UPDATE writes a new row version, so the
churn pinned WAL, autovacuum and disk IO with no data actually changing.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from backend.models.db import InboxEmail, TalentToken
from backend.services.inbox_sync import SYNC_TOUCH_MINUTES, sync_inbox_for_talent


def _token(db):
    t = TalentToken(
        talent_key="Allee", email="allee@taboost.me",
        access_token="a", refresh_token="r",
    )
    db.add(t)
    db.commit()
    return t


def _seed(db, **overrides):
    row = InboxEmail(
        talent_key="allee",
        gmail_message_id="m1",
        thread_id="t1",
        is_unread=True,
        label_ids="INBOX,UNREAD",
        last_synced_at=datetime.utcnow(),
        first_seen_at=datetime.utcnow(),
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return row


def _run(db, labels=("INBOX", "UNREAD")):
    stubs = [{"id": "m1", "threadId": "t1"}]
    headers = {"m1": {"thread_id": "t1", "sender": "b@x.com", "subject": "s",
                      "snippet": "sn", "email_date": None, "label_ids": list(labels)}}
    with patch("backend.services.gmail.list_inbox_messages", return_value=stubs), \
         patch("backend.services.gmail.get_message_headers", return_value=headers["m1"]):
        return sync_inbox_for_talent(_token(db), db)


def test_unchanged_row_is_not_rewritten(db_session):
    _seed(db_session)
    summary = _run(db_session)
    assert summary.get("updated", 0) == 0
    assert summary.get("unchanged", 0) == 1


def test_a_new_message_is_still_inserted(db_session):
    """Headers are only fetched for IDs not already cached, so the write path for
    existing rows was never refreshing labels anyway — the per-cycle UPDATE storm
    was last_synced_at alone."""
    summary = _run(db_session)  # nothing seeded
    assert summary["upserted"] == 1
    row = db_session.query(InboxEmail).filter_by(gmail_message_id="m1").one()
    assert row.is_unread is True


def test_a_very_stale_timestamp_is_refreshed_even_with_no_other_change(db_session):
    """last_synced_at still moves, just on a 30-minute floor rather than every 45s."""
    stale = datetime.utcnow() - timedelta(minutes=SYNC_TOUCH_MINUTES + 5)
    row = _seed(db_session, last_synced_at=stale)
    summary = _run(db_session)
    db_session.refresh(row)
    assert summary["updated"] == 1
    assert row.last_synced_at > stale
