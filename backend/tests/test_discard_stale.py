"""Clearing stale drafts — the button Marco needed and didn't have.

On 2026-09-08 the review queue held 280 pending drafts: 4 actionable, 276 dating
back to June that the automation could never send (the draft queue refuses mail
older than DRAFT_MAX_AGE_DAYS). Clearing them meant 276 individual clicks, so
they just sat there burying the real work.

Two properties matter here: recent drafts must survive, and drafts mid-send must
be skipped — that guard is what stops a brand receiving two replies.
"""
from datetime import datetime, timedelta

from backend.models.db import Draft, DraftStatus, ProcessedEmail


def _pair(db, days_ago, mid, claimed=False, status=DraftStatus.pending):
    db.add(ProcessedEmail(
        talent_key="allee", gmail_message_id=mid, score=3, status="flagged",
        processed_at=datetime.utcnow() - timedelta(days=days_ago),
    ))
    db.add(Draft(
        talent_key="allee", gmail_message_id=mid, status=status, draft_text="rates",
        created_at=datetime.utcnow() - timedelta(days=days_ago),
        send_claimed_at=datetime.utcnow() if claimed else None,
    ))
    db.commit()


def test_dry_run_reports_without_discarding(client, db_session):
    _pair(db_session, 30, "old-1")
    r = client.post("/api/drafts/discard-stale?older_than_days=3&apply=false")
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["matched"] == 1 and body["discarded"] == 0
    assert db_session.query(Draft).filter_by(gmail_message_id="old-1").one().status == DraftStatus.pending


def test_apply_discards_only_the_old_ones(client, db_session):
    _pair(db_session, 30, "old-2")
    _pair(db_session, 0, "fresh-2")

    r = client.post("/api/drafts/discard-stale?older_than_days=3&apply=true")
    assert r.json()["discarded"] == 1

    assert db_session.query(Draft).filter_by(gmail_message_id="old-2").one().status == DraftStatus.discarded
    assert db_session.query(Draft).filter_by(gmail_message_id="fresh-2").one().status == DraftStatus.pending


def test_a_draft_mid_send_is_never_touched(client, db_session):
    """Discarding a claimed draft races the send worker — that is how a brand ends
    up with two replies."""
    _pair(db_session, 30, "claimed-3", claimed=True)

    body = client.post("/api/drafts/discard-stale?older_than_days=3&apply=true").json()

    assert body["discarded"] == 0
    assert body["skipped_send_in_progress"] == 1
    assert db_session.query(Draft).filter_by(gmail_message_id="claimed-3").one().status == DraftStatus.pending


def test_rows_are_discarded_not_deleted(client, db_session):
    """Deleting draft rows is what caused the 2026-09-08 incident: the queue reads a
    missing row as 'never answered' and re-drafts the email."""
    _pair(db_session, 30, "keep-row")
    client.post("/api/drafts/discard-stale?older_than_days=3&apply=true")
    assert db_session.query(Draft).filter_by(gmail_message_id="keep-row").count() == 1
