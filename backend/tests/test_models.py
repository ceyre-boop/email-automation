"""
Tests for database models — creation, enum values, defaults.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.models.db import Draft, DraftStatus, EmailStatus, ProcessedEmail, TalentToken
from backend.tests.conftest import make_draft, make_token


def test_create_talent_token(db_session):
    token = TalentToken(
        talent_key="Sylvia",
        email="sylvia@gmail.com",
        access_token="access",
        refresh_token="refresh",
        active=True,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    assert token.id is not None
    assert token.talent_key == "Sylvia"
    assert token.active is True


def test_talent_token_default_active_true(db_session):
    token = TalentToken(
        talent_key="Colleen",
        email="colleen@gmail.com",
        access_token="a",
        refresh_token="r",
    )
    db_session.add(token)
    db_session.commit()
    assert token.active is True


def test_talent_token_unique_key(db_session):
    from sqlalchemy.exc import IntegrityError
    db_session.add(TalentToken(talent_key="Britt", email="a@b.com", access_token="a", refresh_token="r"))
    db_session.commit()
    db_session.add(TalentToken(talent_key="Britt", email="c@d.com", access_token="b", refresh_token="s"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_create_processed_email(db_session):
    email = ProcessedEmail(
        talent_key="Sylvia",
        gmail_message_id="msg-unique-001",
        thread_id="thread-001",
        sender="brand@nike.com",
        subject="Partnership",
        score=3,
        brand_name="Nike",
        proposed_rate=3500.0,
        offer_type="Sponsored Post",
        status=EmailStatus.draft_saved,
    )
    db_session.add(email)
    db_session.commit()
    db_session.refresh(email)

    assert email.id is not None
    assert email.score == 3
    assert email.status == EmailStatus.draft_saved


def test_processed_email_unique_message_id(db_session):
    from sqlalchemy.exc import IntegrityError
    db_session.add(ProcessedEmail(
        talent_key="Sylvia", gmail_message_id="dup-msg-001",
        access_token="a", status=EmailStatus.archived,
    ))
    db_session.commit()
    db_session.add(ProcessedEmail(
        talent_key="Trin", gmail_message_id="dup-msg-001",
        status=EmailStatus.archived,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_create_draft(db_session):
    draft = Draft(
        talent_key="Sylvia",
        gmail_message_id="msg-001",
        thread_id="thread-001",
        draft_text="Here are Sylvia's rates...",
        status=DraftStatus.pending,
        is_escalate=False,
    )
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)

    assert draft.id is not None
    assert draft.status == DraftStatus.pending
    assert draft.is_escalate is False


def test_draft_status_enum_values():
    assert DraftStatus.pending == "pending"
    assert DraftStatus.approved == "approved"
    assert DraftStatus.sent == "sent"
    assert DraftStatus.discarded == "discarded"


def test_email_status_enum_values():
    assert EmailStatus.archived == "archived"
    assert EmailStatus.flagged == "flagged"
    assert EmailStatus.draft_saved == "draft_saved"
    assert EmailStatus.sent == "sent"
    assert EmailStatus.error == "error"


def test_make_token_helper(db_session):
    token = make_token(db_session, talent_key="TestTalent")
    assert token.talent_key == "TestTalent"
    assert token.active is True


def test_make_draft_helper(db_session):
    make_token(db_session)
    draft = make_draft(db_session, talent_key="Sylvia")
    assert draft.talent_key == "Sylvia"
    assert draft.status == DraftStatus.pending
