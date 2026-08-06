"""
Tests for auto_send's manual-reply guard.

Regression context — under the SOP v16 consolidated inbox, one brand emailing
several talent aliases lands every copy in talent-mgmt@taboost.me, and Gmail
groups them into a single conversation. The guard used to count every non-DRAFT
message in that thread as a reply we had already sent, so a thread holding 3
inbound brand emails dismissed all 3 drafts unsent. Roughly half of multi-alias
brand outreach was being dropped silently.

The intended behaviour is one reply per draft: each talent answers with their
own rates, exactly as they did when every talent owned their mailbox. Only a
SENT message we cannot account for — a human replying by hand — stops the send.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import Draft, DraftStatus
from backend.tests.conftest import make_draft, make_token

THREAD = "thread-shared-001"


def _thread_response(*label_sets):
    """Build a Gmail threads().get() payload from per-message label lists."""
    return {"messages": [{"labelIds": list(labels)} for labels in label_sets]}


def _service_returning(thread_payload):
    svc = MagicMock()
    svc.users().threads().get().execute.return_value = thread_payload
    return svc


def _pending_draft(db, talent_key, thread_id=THREAD, status=DraftStatus.pending):
    d = make_draft(db, talent_key=talent_key, status=status)
    d.thread_id = thread_id
    db.add(d)
    db.commit()
    return d


# ── the regression ───────────────────────────────────────────────────────────


def test_inbound_brand_emails_do_not_block_the_send(db_session):
    """Three inbound messages, no SENT — every draft must still be sendable.

    This is the exact shape of the reported bug: one sender, several aliases,
    all grouped into one Gmail conversation.
    """
    from backend.services import auto_send

    _pending_draft(db_session, "Allee")
    _pending_draft(db_session, "Wesley")
    _pending_draft(db_session, "Anastasiya")

    # 3 inbound brand emails; none carry SENT.
    thread = _thread_response(["INBOX"], ["INBOX"], ["INBOX", "IMPORTANT"])
    sent_in_thread = [m for m in thread["messages"] if "SENT" in m["labelIds"]]
    our_sends = (
        db_session.query(Draft)
        .filter(Draft.thread_id == THREAD, Draft.status == DraftStatus.sent)
        .count()
    )

    assert len(sent_in_thread) == 0
    assert our_sends == 0
    assert not (len(sent_in_thread) > our_sends), (
        "inbound brand emails must never be counted as replies we sent"
    )


def test_sibling_draft_still_sends_after_first_one_goes(db_session):
    """After talent #1 replies, talent #2 on the same thread must still send."""
    from backend.services import auto_send

    _pending_draft(db_session, "Allee", status=DraftStatus.sent)
    _pending_draft(db_session, "Wesley")

    # Thread now holds 2 inbound + our 1 reply.
    thread = _thread_response(["INBOX"], ["INBOX"], ["SENT"])
    sent_in_thread = [m for m in thread["messages"] if "SENT" in m["labelIds"]]
    our_sends = (
        db_session.query(Draft)
        .filter(Draft.thread_id == THREAD, Draft.status == DraftStatus.sent)
        .count()
    )

    assert len(sent_in_thread) == 1 and our_sends == 1
    assert not (len(sent_in_thread) > our_sends), (
        "our own reply must not block the next talent's draft"
    )


def test_manual_human_reply_does_block(db_session):
    """A SENT message we cannot account for means a human replied — stop."""
    from backend.services import auto_send

    _pending_draft(db_session, "Allee")

    # One inbound + one SENT, but we have sent nothing on this thread.
    thread = _thread_response(["INBOX"], ["SENT"])
    sent_in_thread = [m for m in thread["messages"] if "SENT" in m["labelIds"]]
    our_sends = (
        db_session.query(Draft)
        .filter(Draft.thread_id == THREAD, Draft.status == DraftStatus.sent)
        .count()
    )

    assert len(sent_in_thread) == 1 and our_sends == 0
    assert len(sent_in_thread) > our_sends, (
        "an unaccounted-for SENT message must stop the auto-send"
    )


def test_our_drafts_are_not_counted_as_sent(db_session):
    """Gmail drafts sitting on the thread carry DRAFT and must be ignored."""
    thread = _thread_response(["INBOX"], ["DRAFT"], ["DRAFT"])
    sent_in_thread = [m for m in thread["messages"] if "SENT" in m["labelIds"]]
    assert sent_in_thread == [], "DRAFT-labelled messages are not sent replies"


@pytest.mark.parametrize("inbound_count", [2, 5, 12])
def test_guard_is_independent_of_inbound_volume(db_session, inbound_count):
    """Spam volume on a thread must never influence the decision."""
    _pending_draft(db_session, "Allee")
    thread = _thread_response(*([["INBOX"]] * inbound_count))
    sent_in_thread = [m for m in thread["messages"] if "SENT" in m["labelIds"]]
    our_sends = (
        db_session.query(Draft)
        .filter(Draft.thread_id == THREAD, Draft.status == DraftStatus.sent)
        .count()
    )
    assert not (len(sent_in_thread) > our_sends)


# ── end-to-end through the real _process_talent ──────────────────────────────
#
# The assertions above mirror the guard's logic, so they would still pass if the
# source regressed. These drive the actual function and assert on whether the
# send happened.


def _drive_process_talent(db, talent_key, thread_payload):
    """Run the real _process_talent with Gmail and the send stubbed out."""
    from backend.services import auto_send

    token = make_token(db, talent_key=talent_key)
    service = _service_returning(thread_payload)

    # The verbatim-SOP gate runs before the thread guard and would reject the
    # fixture text; stub it so these tests isolate the guard under test.
    with patch.object(auto_send, "resolve_token_for_talent", return_value=token), \
         patch.object(auto_send.gmail_svc, "build_service", return_value=service), \
         patch("backend.services.validation.run_pre_send_checks", return_value=(True, None)), \
         patch.object(auto_send, "_send_draft") as send:
        cutoff = auto_send.datetime.utcnow() + auto_send.timedelta(minutes=1)
        auto_send._process_talent(db, talent_key, cutoff)
    return send


def test_e2e_spammed_thread_still_sends(db_session):
    """The reported bug, end to end: 3 inbound messages must not stop the send."""
    d = _pending_draft(db_session, "Allee")
    d.gmail_draft_id = "gdraft-1"
    db_session.add(d)
    db_session.commit()

    send = _drive_process_talent(
        db_session, "Allee", _thread_response(["INBOX"], ["INBOX"], ["INBOX"])
    )

    assert send.called, "draft was dismissed by inbound volume — the bug is back"
    db_session.refresh(d)
    assert not d.dismissed


def test_e2e_manual_reply_blocks_and_dismisses(db_session):
    """An unaccounted SENT message must still stop the send and dismiss."""
    d = _pending_draft(db_session, "Allee")
    d.gmail_draft_id = "gdraft-2"
    db_session.add(d)
    db_session.commit()

    send = _drive_process_talent(
        db_session, "Allee", _thread_response(["INBOX"], ["SENT"])
    )

    send.assert_not_called()
    db_session.refresh(d)
    assert d.dismissed, "a human reply should dismiss the draft"
