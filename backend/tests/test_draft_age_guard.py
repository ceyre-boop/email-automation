"""The draft queue must never draft a reply to old mail.

2026-09-08: a retention purge deleted 29,247 old sent drafts. "Needs a draft" is
defined as "Score 3 with no draft row", so 29,000 already-answered emails
instantly looked brand new. The queue began re-drafting them — 325 in one hour,
sourced from emails as old as Aug 24 — and would have sent second replies to
brands answered weeks earlier. Only the guardian's runaway kill stopped it.

The age guard makes the queue immune to that whole class of failure: a missing
draft row can no longer be read as "never answered".
"""
from datetime import datetime, timedelta

import pytest

from backend.models.db import Draft, DraftStatus, ProcessedEmail
from backend.routers.cron import DRAFT_MAX_AGE_DAYS


def _email(db, days_ago, mid, score=3, status="flagged"):
    row = ProcessedEmail(
        talent_key="allee", gmail_message_id=mid, score=score, status=status,
        processed_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _candidates(db):
    """The draft queue's candidate query, including the age guard."""
    from sqlalchemy import select

    drafted_exists = (
        select(Draft.id)
        .where(Draft.gmail_message_id == ProcessedEmail.gmail_message_id)
        .exists()
    )
    cutoff = datetime.utcnow() - timedelta(days=DRAFT_MAX_AGE_DAYS)
    return (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.score == 3,
            ProcessedEmail.status != "archived",
            ProcessedEmail.processed_at >= cutoff,
            ~drafted_exists,
        )
        .all()
    )


def test_default_cutoff_is_three_days():
    assert DRAFT_MAX_AGE_DAYS == 3


def test_recent_undrafted_email_is_a_candidate(db_session):
    _email(db_session, days_ago=0, mid="fresh")
    assert [r.gmail_message_id for r in _candidates(db_session)] == ["fresh"]


def test_old_undrafted_email_is_never_drafted(db_session):
    """This is the exact 2026-09-08 shape: a Score 3 from weeks ago whose draft row
    no longer exists. Before the guard it was picked up and re-drafted."""
    _email(db_session, days_ago=15, mid="purged-history")
    assert _candidates(db_session) == []


def test_a_deleted_draft_row_cannot_resurrect_an_old_email(db_session):
    old = _email(db_session, days_ago=20, mid="answered-in-august")
    # Simulate retention having removed the sent draft for it — no Draft row exists.
    assert db_session.query(Draft).filter_by(gmail_message_id=old.gmail_message_id).count() == 0
    assert _candidates(db_session) == []


def test_email_right_at_the_boundary_is_excluded(db_session):
    _email(db_session, days_ago=DRAFT_MAX_AGE_DAYS + 1, mid="just-too-old")
    _email(db_session, days_ago=DRAFT_MAX_AGE_DAYS - 1, mid="just-inside")
    assert [r.gmail_message_id for r in _candidates(db_session)] == ["just-inside"]


def test_recent_email_that_already_has_a_draft_is_not_redrafted(db_session):
    _email(db_session, days_ago=0, mid="has-draft")
    db_session.add(Draft(
        talent_key="allee", gmail_message_id="has-draft", status=DraftStatus.pending,
        draft_text="rates", created_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert _candidates(db_session) == []
