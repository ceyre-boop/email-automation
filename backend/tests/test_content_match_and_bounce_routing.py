"""Third-tier routing fallback (content-match) and bounce/notification filtering.

Found 2026-09-22 investigating the persistent 'ROUTING REGRESSION' alerts
(1-2/day after the ghost-row cleanup fix): real senders increasingly email
talent-mgmt@ directly — an account manager, a repeat PR/creator-platform
contact — naming the talent only in the subject/body ("AliExpress US x Lizz",
"OGHom x Jenn Lyles") rather than through a per-talent alias. Header-based
routing (alias match, thread continuity) structurally cannot see this; the
name is the only signal that exists. Separately, ~50% of the UNROUTED pile
turned out to be Gmail bounce/delivery-failure notifications that can never
be routed to a talent by any means, since the "sender" is a mail system, not
a brand — these must not consume the UNROUTED review queue or trip the
routing-regression alarm at all.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import ProcessedEmail, TalentToken
from backend.services import poller
from backend.services.poller import _is_auto_generated_notification, _resolve_talent_from_content
from backend.services.sop_parser import TalentProfile


def _profile(key, full_name):
    return TalentProfile(
        key=key, full_name=full_name, minimum_rate_usd=500, manager="", paused=False,
        manager_email="", gmail_connection_name="", rate_unit="per video",
        auto_send=True, personal_emails=[], has_approved_response=True,
    )


TALENT_MAP = {
    "lizz": _profile("Lizz", "Lizz Freixas"),
    "jenn": _profile("Jenn", "Jenn Lyles"),
    "sam": _profile("Sam", "Sam Jones"),
    "hana": _profile("Hana", "Hana Tanaka"),
}


# ── _resolve_talent_from_content ─────────────────────────────────────────────

def test_full_name_match():
    assert _resolve_talent_from_content("Re: OGHom x Jenn Lyles", "", TALENT_MAP) == "Jenn"


def test_first_name_only_match_when_distinctive():
    assert _resolve_talent_from_content("AliExpress US x Lizz", "", TALENT_MAP) == "Lizz"


def test_body_is_also_searched():
    body = "We'd love to collaborate with Hana on a UGC video."
    assert _resolve_talent_from_content("Partnership inquiry", body, TALENT_MAP) == "Hana"


def test_denylisted_short_first_name_never_matches_alone():
    """'Sam' alone is too common/collision-prone to trust — must require the
    full name for this specific talent."""
    assert _resolve_talent_from_content("Thanks Sam, talk soon", "", TALENT_MAP) is None


def test_full_name_still_works_for_denylisted_first_name():
    assert _resolve_talent_from_content("Re: collab with Sam Jones", "", TALENT_MAP) == "Sam"


def test_ambiguous_two_matches_returns_none():
    """Never guess between two plausible talents — must fall through to UNROUTED."""
    subject = "Group collab: Jenn Lyles and Lizz both invited"
    assert _resolve_talent_from_content(subject, "", TALENT_MAP) is None


def test_no_match_returns_none():
    assert _resolve_talent_from_content("Generic partnership inquiry", "no names here", TALENT_MAP) is None


def test_substring_of_a_longer_word_does_not_match():
    """'Hana' must not match inside an unrelated longer word like 'Hanabusa'."""
    assert _resolve_talent_from_content("Hanabusa Ventures LLC", "", TALENT_MAP) is None


# ── _is_auto_generated_notification ─────────────────────────────────────────

def test_mailer_daemon_sender_detected():
    detail = {"sender": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "headers": {}}
    assert _is_auto_generated_notification(detail) is True


def test_auto_submitted_header_detected():
    detail = {"sender": "someone@example.com", "headers": {"auto-submitted": "auto-replied"}}
    assert _is_auto_generated_notification(detail) is True


def test_auto_submitted_no_is_not_flagged():
    detail = {"sender": "brand@nike.com", "headers": {"auto-submitted": "no"}}
    assert _is_auto_generated_notification(detail) is False


def test_x_failed_recipients_header_detected():
    detail = {"sender": "mailer-daemon@googlemail.com", "headers": {"x-failed-recipients": "brand@x.com"}}
    assert _is_auto_generated_notification(detail) is True


def test_real_brand_email_not_flagged():
    detail = {"sender": "Iris <partnerships@brand.com>", "headers": {"to": "talent-mgmt@taboost.me"}}
    assert _is_auto_generated_notification(detail) is False


# ── Wired into _process_shared_inbox_message ────────────────────────────────

def _token(db):
    t = TalentToken(
        talent_key="shared-inbox", email="talent-mgmt@taboost.me",
        access_token="a", refresh_token="r", active=True,
    )
    db.add(t)
    db.commit()
    return t


@pytest.fixture
def env(db_session, monkeypatch):
    monkeypatch.setattr(poller, "_get_session_factory", lambda: (lambda: db_session))
    return db_session


def test_content_match_routes_instead_of_unrouted(env):
    detail = {
        "thread_id": "t1", "sender": "arryn@oghom.com", "subject": "Re: OGHom x Jenn Lyles",
        "body_text": "Would love to collaborate!", "headers": {"to": "talent-mgmt@taboost.me"},
        "email_date": None,
    }
    token = _token(env)
    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=detail), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.poller._process_one_message") as mock_process:
        poller._process_shared_inbox_message(
            token.id, "m1", alias_map={}, talent_map={"jenn": _profile("Jenn", "Jenn Lyles")},
            draft_mode=True,
        )

    assert mock_process.called
    assert mock_process.call_args.kwargs["talent_key"] == "Jenn"
    assert env.query(ProcessedEmail).filter(ProcessedEmail.talent_key == "UNROUTED").count() == 0


def test_bounce_never_reaches_unrouted_or_content_match(env):
    detail = {
        "thread_id": "t2", "sender": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "subject": "Delivery Status Notification (Failure)", "body_text": "",
        "headers": {"to": "talent-mgmt@taboost.me", "x-failed-recipients": "someone@x.com"},
        "email_date": None,
    }
    token = _token(env)
    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=detail), \
         patch("backend.services.poller._process_one_message") as mock_process:
        poller._process_shared_inbox_message(
            token.id, "m2", alias_map={}, talent_map={}, draft_mode=True,
        )

    assert not mock_process.called
    row = env.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == "m2").first()
    assert row is not None
    assert row.talent_key == "SYSTEM_NOTIFICATION"
    assert row.talent_key != "UNROUTED"
    assert row.score == 1


def test_still_genuinely_unroutable_falls_through_to_unrouted(env):
    detail = {
        "thread_id": "t3", "sender": "random@example.com", "subject": "Generic inquiry",
        "body_text": "Hello, interested in working together.",
        "headers": {"to": "talent-mgmt@taboost.me"},
        "email_date": None,
    }
    token = _token(env)
    with patch("backend.services.gmail.build_service", return_value=MagicMock()), \
         patch("backend.services.gmail.get_message_detail", return_value=detail), \
         patch("backend.services.gmail.get_to_address", return_value=None), \
         patch("backend.services.poller._process_one_message") as mock_process:
        poller._process_shared_inbox_message(
            token.id, "m3", alias_map={}, talent_map={"jenn": _profile("Jenn", "Jenn Lyles")},
            draft_mode=True,
        )

    assert not mock_process.called
    row = env.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == "m3").first()
    assert row.talent_key == "UNROUTED"
