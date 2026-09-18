"""force-draft must refuse to create a second response for an already-answered
thread — the same "only one response per email, ever" guardrail the automated
poller already enforced in `_process_one_message`, extracted to
`poller.thread_already_answered()` so every draft-creating path shares it.

Found 2026-09-18: force-draft (dashboard.py) had zero thread-level check — a
manager invoking it on a message in an already-answered thread could create an
uncontrolled second real response with no guardrail at all.
"""
from unittest.mock import MagicMock, patch

from backend.models.db import Draft, DraftStatus, EmailStatus, ProcessedEmail
from backend.services import poller
from backend.tests.conftest import make_draft, make_token


def _processed(db, thread_id, talent_key="Sylvia", status=EmailStatus.sent, gmail_message_id=None):
    row = ProcessedEmail(
        talent_key=talent_key,
        gmail_message_id=gmail_message_id or f"prior-{thread_id}",
        thread_id=thread_id,
        score=3,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def test_thread_already_answered_true_when_a_draft_exists_on_another_message(db_session):
    make_draft(db_session, talent_key="Sylvia", status=DraftStatus.sent, gmail_message_id="other-msg")
    with patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        assert poller.thread_already_answered(
            db_session, MagicMock(), "thread-001", message_id="new-msg"
        ) is True


def test_thread_already_answered_true_when_processed_email_shows_sent(db_session):
    _processed(db_session, "t1", status=EmailStatus.sent)
    with patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        assert poller.thread_already_answered(db_session, MagicMock(), "t1", message_id="new-msg") is True


def test_thread_already_answered_true_when_gmail_shows_a_manual_reply(db_session):
    """No DB record at all — the talent or a manager replied by hand in Gmail."""
    with patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=True):
        assert poller.thread_already_answered(db_session, MagicMock(), "t1", message_id="new-msg") is True


def test_thread_already_answered_false_for_a_genuinely_fresh_thread(db_session):
    with patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        assert poller.thread_already_answered(db_session, MagicMock(), "t1", message_id="new-msg") is False


def test_thread_already_answered_ignores_the_same_message_id(db_session):
    """A row for the message being processed itself must not count as 'prior'."""
    _processed(db_session, "t1", status=EmailStatus.sent, gmail_message_id="m1")
    with patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=False):
        assert poller.thread_already_answered(db_session, MagicMock(), "t1", message_id="m1") is False


def test_force_draft_refuses_a_second_response_on_an_answered_thread(client, db_session):
    make_token(db_session, talent_key="Sylvia")
    make_draft(db_session, talent_key="Sylvia", status=DraftStatus.sent, gmail_message_id="prior-msg")

    with patch("backend.services.gmail._gmail_service", return_value=MagicMock()), \
         patch("backend.services.poller.thread_already_answered", return_value=True) as mock_guard:
        resp = client.post("/api/dashboard/talents/Sylvia/force-draft/new-msg")

    assert mock_guard.called
    assert resp.status_code == 409
    assert "already has draft" in resp.json()["detail"]
    # No draft row was created for the new message.
    assert db_session.query(Draft).filter(Draft.gmail_message_id == "new-msg").first() is None


def test_force_draft_proceeds_on_a_genuinely_fresh_thread(client, db_session):
    make_token(db_session, talent_key="Sylvia")

    with patch("backend.services.gmail._gmail_service", return_value=MagicMock()), \
         patch("backend.services.poller.thread_already_answered", return_value=False) as mock_guard, \
         patch("backend.services.gmail.create_gmail_draft", return_value="gdraft-1"), \
         patch("backend.services.gmail.remove_from_inbox"):
        resp = client.post("/api/dashboard/talents/Sylvia/force-draft/new-msg")

    assert mock_guard.called
    assert resp.status_code == 200
    assert db_session.query(Draft).filter(Draft.gmail_message_id == "new-msg").first() is not None
