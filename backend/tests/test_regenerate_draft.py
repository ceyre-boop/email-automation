"""Regenerating a draft updates the existing row rather than inserting a new one.

drafts.gmail_message_id is UNIQUE — one draft row per email is the invariant that
stops a brand being replied to twice. The regenerate endpoint discarded the old
row and INSERTed a fresh one, which violated that constraint every time, so it
had never worked for an email that already had a draft — which is every case a
human actually reaches for it.

Found on 2026-09-14: the V-16d SOP change invalidated two in-flight Anastasiya
drafts (written against the previous approved response), and regenerating them
failed with UniqueViolation.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import Draft, DraftStatus, ProcessedEmail, TalentToken


@pytest.fixture
def _seeded(db_session):
    db_session.add(TalentToken(
        talent_key="Allee", email="allee@taboost.me",
        access_token="a", refresh_token="r", active=True,
    ))
    db_session.add(ProcessedEmail(
        talent_key="Allee", gmail_message_id="m1", score=3, status="flagged",
        subject="Collab", sender="brand@x.com", processed_at=datetime.utcnow(),
    ))
    db_session.add(Draft(
        talent_key="allee", gmail_message_id="m1", status=DraftStatus.pending,
        draft_text="STALE text from the previous SOP",
        gmail_draft_id="old-gd", validation_failed=True,
        validation_error="Verbatim SOP check failed at send time",
        created_at=datetime.utcnow() - timedelta(hours=2),
    ))
    db_session.commit()
    return db_session


def _regen(client):
    with patch("backend.services.gmail.get_message_headers", return_value={"thread_id": "t1"}), \
         patch("backend.services.gmail.create_gmail_draft", return_value="new-gd"), \
         patch("backend.services.gmail.remove_from_inbox", return_value=True), \
         patch("backend.services.reply.draft_reply", return_value={
             "draft_text": "FRESH text matching the current SOP",
             "is_escalate": False, "escalate_reason": None, "cc_recipients": None,
         }):
        return client.post("/api/drafts/orphaned/m1/regenerate")


def test_regenerate_updates_in_place_without_violating_the_unique_constraint(client, _seeded):
    resp = _regen(client)
    assert resp.status_code == 200, resp.text

    rows = _seeded.query(Draft).filter_by(gmail_message_id="m1").all()
    assert len(rows) == 1                      # still exactly one row for this email
    assert rows[0].draft_text == "FRESH text matching the current SOP"
    assert rows[0].status == DraftStatus.pending


def test_regenerate_clears_the_invalid_flags(client, _seeded):
    """The stale text is gone, so the verbatim failure that flagged it is gone too.
    Leaving these set would keep the draft INVALID forever."""
    _regen(client)
    row = _seeded.query(Draft).filter_by(gmail_message_id="m1").one()
    assert row.validation_failed is False
    assert row.validation_error is None


def test_regenerate_restarts_the_auto_send_hold(client, _seeded):
    """A regenerated draft deserves a fresh review window, not an instant send."""
    before = datetime.utcnow() - timedelta(minutes=1)
    _regen(client)
    row = _seeded.query(Draft).filter_by(gmail_message_id="m1").one()
    assert row.created_at >= before


def test_regenerate_points_at_the_new_gmail_draft(client, _seeded):
    _regen(client)
    row = _seeded.query(Draft).filter_by(gmail_message_id="m1").one()
    assert row.gmail_draft_id == "new-gd"


def test_regenerate_refuses_while_a_send_is_in_progress(client, _seeded):
    """Rewriting a draft mid-send races the send worker — that is how a brand gets
    two different replies."""
    row = _seeded.query(Draft).filter_by(gmail_message_id="m1").one()
    row.send_claimed_at = datetime.utcnow()
    _seeded.commit()

    resp = _regen(client)
    assert resp.status_code >= 400
    assert _seeded.query(Draft).filter_by(gmail_message_id="m1").one().draft_text.startswith("STALE")
