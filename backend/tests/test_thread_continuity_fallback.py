"""Thread-continuity fallback for shared-inbox alias routing.

Once talent-mgmt@'s own automated reply goes out on a thread, the brand's next
reply naturally addresses talent-mgmt@ itself — a mail client replies to whoever
sent the last message, not to the alias that started the conversation. The alias
then appears in NO header of the reply, so alias matching fails on every second
message of every negotiation for every shared-inbox talent.

Found 2026-09-14 investigating a routing test: 27 of 37 currently-unrouted emails
were exactly this — real ongoing deals silently misfiled as UNROUTED instead of
showing on the correct talent's dashboard for human review.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import ProcessedEmail, TalentToken
from backend.services import poller
from backend.services.sop_parser import TalentProfile


def _token(db):
    t = TalentToken(
        talent_key="shared-inbox", email="talent-mgmt@taboost.me",
        access_token="a", refresh_token="r", active=True,
    )
    db.add(t)
    db.commit()
    return t


def _prior_resolved(db, thread_id, talent_key, minutes_ago=60):
    db.add(ProcessedEmail(
        talent_key=talent_key, gmail_message_id=f"prior-{thread_id}", thread_id=thread_id,
        score=3, status="sent",
        processed_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    ))
    db.commit()


def _detail(thread_id, sender="brand@x.com"):
    return {
        "thread_id": thread_id, "sender": sender, "subject": "Re: Partnership",
        "body_text": "Following up on your offer", "headers": {"to": "talent-mgmt@taboost.me"},
        "email_date": None,
    }


ALLEE = TalentProfile(key="Allee", full_name="Allee", minimum_rate_usd=500, manager="", paused=False, manager_email="", gmail_connection_name="", rate_unit="per video", auto_send=True, personal_emails=[], has_approved_response=True)


@pytest.fixture
def env(db_session, monkeypatch):
    monkeypatch.setattr(poller, "_get_session_factory", lambda: (lambda: db_session))
    return db_session


def test_reply_with_no_alias_routes_to_the_threads_prior_talent(env):
    """The core case: a brand's counter-offer reply has no alias anywhere, but the
    thread already resolved to Allee — it must go to Allee, not UNROUTED."""
    token = _token(env)
    _prior_resolved(env, "t1", "Allee")

    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=_detail("t1")), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.gmail.thread_has_prior_sent_reply", return_value=True), \
         patch("backend.services.poller._process_one_message") as mock_process:
        poller._process_shared_inbox_message(
            token.id, "m1", alias_map={}, talent_map={"allee": ALLEE}, draft_mode=True,
        )

    assert mock_process.called
    assert mock_process.call_args.kwargs["talent_key"] == "Allee"


def test_brand_new_thread_with_no_prior_resolution_stays_unrouted(env):
    """No prior activity on this thread at all — correctly stays unrouted rather
    than guessing. This is the legitimate Rule 12 Option B case."""
    token = _token(env)

    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=_detail("t2")), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.poller._process_one_message") as mock_process:
        result = poller._process_shared_inbox_message(
            token.id, "m2", alias_map={}, talent_map={"allee": ALLEE}, draft_mode=True,
        )

    assert not mock_process.called
    assert result["summary"]["unrouted"] == 1
    row = env.query(ProcessedEmail).filter_by(gmail_message_id="m2").one()
    assert row.talent_key == "UNROUTED"


def test_a_direct_alias_match_is_never_overridden_by_the_fallback(env):
    """The fallback only fires when alias resolution fails — an email correctly
    addressed to a different talent's alias must never be redirected by thread
    history from an unrelated prior email."""
    token = _token(env)
    _prior_resolved(env, "t3", "Hana")

    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=_detail("t3")), \
         patch("backend.services.gmail.get_to_address", return_value="allee@taboost.me"), \
         patch("backend.services.poller._resolve_talent_from_to", return_value="Allee"), \
         patch("backend.services.poller._process_one_message") as mock_process:
        poller._process_shared_inbox_message(
            token.id, "m3", alias_map={"allee@taboost.me": "Allee"},
            talent_map={"allee": ALLEE, "hana": ALLEE}, draft_mode=True,
        )

    assert mock_process.call_args.kwargs["talent_key"] == "Allee"


def test_fallback_never_targets_a_paused_or_removed_talent(env):
    """A prior resolution to a talent no longer in the active roster must not be
    trusted — same guard applied to a direct alias match a few lines below."""
    token = _token(env)
    _prior_resolved(env, "t4", "Retired")

    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=_detail("t4")), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.poller._process_one_message") as mock_process:
        result = poller._process_shared_inbox_message(
            token.id, "m4", alias_map={}, talent_map={"allee": ALLEE}, draft_mode=True,
        )

    assert not mock_process.called
    assert result["summary"]["unrouted"] == 1


def test_an_unrouted_marker_row_is_never_treated_as_a_prior_resolution(env):
    """UNROUTED is not a talent. A thread with only prior UNROUTED rows must not
    fall-back to itself and must not resolve at all."""
    token = _token(env)
    env.add(ProcessedEmail(
        talent_key="UNROUTED", gmail_message_id="prior-t5", thread_id="t5",
        score=0, status="flagged", processed_at=datetime.utcnow() - timedelta(minutes=30),
    ))
    env.commit()

    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=_detail("t5")), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.poller._process_one_message") as mock_process:
        result = poller._process_shared_inbox_message(
            token.id, "m5", alias_map={}, talent_map={"allee": ALLEE}, draft_mode=True,
        )

    assert not mock_process.called
    assert result["summary"]["unrouted"] == 1
