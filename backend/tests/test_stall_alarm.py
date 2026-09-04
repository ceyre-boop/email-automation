"""Pipeline stall alarm.

Built after 2026-09-03, when drafting died for ~4 hours and nothing surfaced it:
/health said "ok" and APScheduler logged every job as executed successfully
because the wrapper swallows exceptions. These tests pin the two conditions that
would have caught it, and the two quiet-system cases that must NOT alarm.
"""
from datetime import datetime, timedelta

from backend.models.db import Draft, DraftStatus, InboxEmail, ProcessedEmail
from backend.services.stall_alarm import check_pipeline_stall


def _processed(db, minutes_ago, score=3, key="Allee"):
    row = ProcessedEmail(
        talent_key=key,
        gmail_message_id=f"m{datetime.utcnow().timestamp()}{minutes_ago}{score}",
        score=score,
        processed_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


def _unread(db, key="Allee"):
    row = InboxEmail(
        talent_key=key,
        gmail_message_id=f"u{datetime.utcnow().timestamp()}",
        is_unread=True,
    )
    db.add(row)
    db.commit()
    return row


def _draft(db, minutes_ago, key="Allee"):
    row = Draft(
        talent_key=key,
        gmail_message_id=f"d{datetime.utcnow().timestamp()}{minutes_ago}",
        status=DraftStatus.pending,
        draft_text="rates below",
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


def test_alarms_when_unread_mail_sits_and_nothing_is_processed(db_session):
    _unread(db_session)
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is True
    assert "NOTHING PROCESSED" in result["reason"]


def test_alarms_when_score3_produces_no_drafts(db_session):
    _processed(db_session, minutes_ago=5, score=3)
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is True
    assert "NOTHING DRAFTED" in result["reason"]


def test_quiet_inbox_does_not_alarm(db_session):
    """3 a.m. with no inbound is not a stall — an alarm that cries wolf gets ignored."""
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is False
    assert result["reason"] is None


def test_healthy_pipeline_does_not_alarm(db_session):
    _unread(db_session)
    _processed(db_session, minutes_ago=5, score=3)
    _draft(db_session, minutes_ago=4)
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is False


def test_score3_older_than_the_window_is_not_counted(db_session):
    """A Score 3 from two hours ago with no draft is history, not a live stall."""
    _unread(db_session)
    _processed(db_session, minutes_ago=120, score=3)
    _processed(db_session, minutes_ago=2, score=1)  # keeps condition A quiet
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is False


def test_metrics_are_reported_even_when_not_stalled(db_session):
    _processed(db_session, minutes_ago=2, score=1)
    result = check_pipeline_stall(db_session)
    for field in ("processed_in_window", "unread_waiting", "score3_last_hour",
                  "drafts_last_hour", "sends_last_hour", "window_minutes"):
        assert field in result
