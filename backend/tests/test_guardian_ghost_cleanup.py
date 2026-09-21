"""Guardian's score=0 ghost-row cleanup (cron.py::_run_guardian) must only ever
delete crashed atomic-claim rows (status='processing'), never UNROUTED markers
(also score=0, but status='flagged').

Found 2026-09-21: this DELETE had no status filter, so it deleted every
UNROUTED row within 10 minutes of creation, every 60 seconds, since
2026-06-01. The Gmail message stays unread (SOP Rule 12 Option B never
archives/labels it), so the poller's dedup (_batch_already_processed_ids)
saw no ProcessedEmail row and immediately recreated the exact same UNROUTED
marker on the next cycle — forever. Real unrouted brand mail never got a
chance to be reviewed because its own marker kept disappearing. Silent until
the unrouted_last_24h stall-alarm check (added 2026-09-14) started surfacing
the same recreated rows as a fresh-looking regression roughly daily.
"""
from datetime import datetime, timedelta

from sqlalchemy import text

from backend.models.db import EmailStatus, ProcessedEmail


def _row(db, talent_key, status, minutes_ago, gmail_message_id):
    row = ProcessedEmail(
        talent_key=talent_key,
        gmail_message_id=gmail_message_id,
        score=0,
        status=status,
        processed_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


def _run_ghost_cleanup(db):
    """The exact query from cron.py::_run_guardian, post-fix."""
    db.execute(text(
        "DELETE FROM processed_emails WHERE score = 0 "
        "AND status = 'processing' "
        "AND processed_at < :cutoff"
    ), {"cutoff": datetime.utcnow() - timedelta(minutes=10)})
    db.commit()


def test_stale_claim_row_is_deleted(db_session):
    _row(db_session, "Allee", EmailStatus.processing, minutes_ago=15, gmail_message_id="claim-1")
    _run_ghost_cleanup(db_session)
    assert db_session.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == "claim-1"
    ).first() is None


def test_stale_unrouted_marker_survives(db_session):
    """The bug: an UNROUTED row (status='flagged') must NOT be swept just
    because it also happens to have score=0."""
    _row(db_session, "UNROUTED", EmailStatus.flagged, minutes_ago=15, gmail_message_id="unrouted-1")
    _run_ghost_cleanup(db_session)
    row = db_session.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == "unrouted-1"
    ).first()
    assert row is not None
    assert row.status == EmailStatus.flagged


def test_fresh_claim_row_survives_the_10_minute_grace_period(db_session):
    _row(db_session, "Allee", EmailStatus.processing, minutes_ago=2, gmail_message_id="claim-2")
    _run_ghost_cleanup(db_session)
    assert db_session.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == "claim-2"
    ).first() is not None


def test_mixed_batch_only_removes_stale_processing_rows(db_session):
    _row(db_session, "Allee", EmailStatus.processing, minutes_ago=20, gmail_message_id="claim-3")
    _row(db_session, "UNROUTED", EmailStatus.flagged, minutes_ago=20, gmail_message_id="unrouted-2")
    _row(db_session, "Wesley", EmailStatus.sent, minutes_ago=20, gmail_message_id="unrelated-1")
    _run_ghost_cleanup(db_session)

    remaining = {
        r.gmail_message_id
        for r in db_session.query(ProcessedEmail).all()
    }
    assert remaining == {"unrouted-2", "unrelated-1"}
