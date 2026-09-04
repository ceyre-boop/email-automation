"""Stale send-claim reaper.

The safety property under test: a stuck claim must NEVER be resolved by
retrying blindly. A worker can die after Gmail accepted the send, so the reaper
asks Gmail what happened and acts only on an unambiguous answer. Sending a brand
a duplicate reply is worse than leaving a draft stuck.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import Draft, DraftStatus
from backend.services.claim_reaper import reap_stale_claims


def _claimed_draft(db, minutes_ago=60, gmail_draft_id="gd-1", thread_id="th-1"):
    row = Draft(
        talent_key="Allee",
        gmail_message_id=f"m-{minutes_ago}-{gmail_draft_id}",
        thread_id=thread_id,
        status=DraftStatus.pending,
        draft_text="rates below",
        gmail_draft_id=gmail_draft_id,
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago + 60),
        send_claimed_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def _gmail(monkeypatch):
    token = MagicMock()
    monkeypatch.setattr("backend.services.claim_reaper.resolve_token_for_talent",
                        lambda db, key: token)
    monkeypatch.setattr("backend.services.gmail.build_service", lambda t, db=None: MagicMock())
    return MagicMock()


def test_unsent_draft_has_its_claim_released(db_session, _gmail):
    """Gmail draft still exists → nothing was sent → safe to requeue."""
    row = _claimed_draft(db_session)
    with patch("backend.services.gmail.draft_exists_in_gmail", return_value=True), \
         patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        summary = reap_stale_claims(db_session)

    db_session.refresh(row)
    assert summary["released"] == 1
    assert row.send_claimed_at is None
    assert row.status == DraftStatus.pending


def test_already_sent_draft_is_recorded_not_retried(db_session, _gmail):
    """Gmail draft gone + SENT in thread → it went out → record it, never resend."""
    row = _claimed_draft(db_session, gmail_draft_id="gd-2")
    with patch("backend.services.gmail.draft_exists_in_gmail", return_value=False), \
         patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=True):
        summary = reap_stale_claims(db_session)

    db_session.refresh(row)
    assert summary["marked_sent"] == 1
    assert row.status == DraftStatus.sent
    assert row.reviewed_by == "claim_reaper"
    assert row.send_claimed_at is None


def test_ambiguous_draft_is_left_alone_for_a_human(db_session, _gmail):
    """No Gmail draft and no SENT message — cannot tell. Never guess."""
    row = _claimed_draft(db_session, gmail_draft_id="gd-3")
    with patch("backend.services.gmail.draft_exists_in_gmail", return_value=False), \
         patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        summary = reap_stale_claims(db_session)

    db_session.refresh(row)
    assert summary["ambiguous"] == 1
    assert row.send_claimed_at is not None
    assert row.status == DraftStatus.pending


def test_claims_inside_the_lease_are_untouched(db_session, _gmail):
    """A send in flight for 2 minutes is not stale — touching it races the worker."""
    row = _claimed_draft(db_session, minutes_ago=2, gmail_draft_id="gd-4")
    with patch("backend.services.gmail.draft_exists_in_gmail", return_value=True):
        summary = reap_stale_claims(db_session)

    db_session.refresh(row)
    assert summary["examined"] == 0
    assert row.send_claimed_at is not None


def test_a_gmail_error_never_resolves_the_claim(db_session, _gmail):
    row = _claimed_draft(db_session, gmail_draft_id="gd-5")
    with patch("backend.services.gmail.draft_exists_in_gmail", side_effect=RuntimeError("boom")):
        summary = reap_stale_claims(db_session)

    db_session.refresh(row)
    assert summary["errors"] == 1
    assert row.send_claimed_at is not None
    assert row.status == DraftStatus.pending
