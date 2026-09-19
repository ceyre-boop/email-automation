"""Regression test for the proposed_rate key-mismatch bug in _spam_sweep_for_talent.

Found 2026-09-18: this function read triage_result.get("proposed_rate") — a key
triage_email() never returns (it only ever returns "proposed_rate_usd") — so
every Draft/ProcessedEmail row created via the spam-sweep path had a dead rate
regardless of what triage actually found. See backend/services/poller.py.
"""
from unittest.mock import MagicMock, patch

from backend.models.db import Draft, ProcessedEmail, TalentToken
from backend.services import poller
from backend.services.sop_parser import TalentProfile


def _token(db):
    t = TalentToken(
        talent_key="Sylvia", email="sylvia@taboost.me",
        access_token="a", refresh_token="r", active=True,
    )
    db.add(t)
    db.commit()
    return t


SYLVIA = TalentProfile(
    key="Sylvia", full_name="Sylvia", minimum_rate_usd=1000, manager="",
    paused=False, manager_email="", gmail_connection_name="", rate_unit="per video",
    auto_send=True, personal_emails=[], has_approved_response=True,
)


def test_spam_sweep_draft_gets_the_real_extracted_rate(db_session):
    token = _token(db_session)

    with patch("backend.services.gmail.list_spam_messages", return_value=[{"id": "m1", "threadId": "t1"}]), \
         patch("backend.services.poller._batch_already_processed_ids", return_value=set()), \
         patch("backend.services.gmail._gmail_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value={
             "subject": "Partnership", "sender": "brand@nike.com", "sender_domain": "nike.com",
             "body_text": "We can offer $900 for a TikTok video.", "thread_id": "t1",
             "message_id_header": "<m1@nike.com>", "email_date": None,
         }), \
         patch("backend.services.poller.triage_svc.triage_email", return_value={
             "score": 3, "reason": "Real brand offering $900", "offer_type": "Sponsored Post",
             "proposed_rate_usd": 900.0, "brand_name": "Nike",
         }), \
         patch("backend.services.poller.reply_svc.draft_reply", return_value={
             "draft_text": "Thanks for reaching out!", "is_escalate": False, "cc_recipients": None,
         }), \
         patch("backend.services.gmail.create_gmail_draft", return_value="gdraft-1"), \
         patch("backend.services.poller._record_external_channel"):
        drafted = poller._spam_sweep_for_talent(token, SYLVIA, db_session)

    assert drafted == 1
    draft = db_session.query(Draft).filter(Draft.gmail_message_id == "m1").first()
    processed = db_session.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == "m1").first()
    assert draft.proposed_rate == 900.0
    assert processed.proposed_rate == 900.0


def test_spam_sweep_no_rate_stored_as_none_not_zero(db_session):
    token = _token(db_session)

    with patch("backend.services.gmail.list_spam_messages", return_value=[{"id": "m2", "threadId": "t2"}]), \
         patch("backend.services.poller._batch_already_processed_ids", return_value=set()), \
         patch("backend.services.gmail._gmail_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value={
             "subject": "Collab?", "sender": "brand@acme.com", "sender_domain": "acme.com",
             "body_text": "Would love to work together, no rate mentioned yet.", "thread_id": "t2",
             "message_id_header": "<m2@acme.com>", "email_date": None,
         }), \
         patch("backend.services.poller.triage_svc.triage_email", return_value={
             "score": 3, "reason": "Real brand, no rate stated", "offer_type": "Unknown",
             "proposed_rate_usd": None, "brand_name": "Acme",
         }), \
         patch("backend.services.poller.reply_svc.draft_reply", return_value={
             "draft_text": "Thanks for reaching out!", "is_escalate": False, "cc_recipients": None,
         }), \
         patch("backend.services.gmail.create_gmail_draft", return_value="gdraft-2"), \
         patch("backend.services.poller._record_external_channel"):
        poller._spam_sweep_for_talent(token, SYLVIA, db_session)

    draft = db_session.query(Draft).filter(Draft.gmail_message_id == "m2").first()
    processed = db_session.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == "m2").first()
    assert draft.proposed_rate is None
    assert processed.proposed_rate is None
