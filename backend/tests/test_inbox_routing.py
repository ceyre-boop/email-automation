"""
Tests for the SOP v16 hybrid routing model.

Two modes run in one poll cycle:
  - the consolidated inbox (talent-mgmt@taboost.me), talent resolved from the
    original recipient alias header, no Reply-To on outbound mail
  - the Partnerships tokens, talent resolved from the receiving account, with
    Reply-To: partnerships@taboost.me on every draft and reply

These tests pin the parts that are easy to break silently: which mailbox sends a
given talent's mail, and which drafts carry the header.
"""
from __future__ import annotations

import base64
import email as email_lib
from unittest.mock import MagicMock, patch

import pytest

from backend.tests.conftest import make_token

PARTNERSHIPS = "partnerships@taboost.me"
SHARED = "talent-mgmt@taboost.me"


def _token(email: str, talent_key: str = "someone"):
    token = MagicMock()
    token.email = email
    token.talent_key = talent_key
    token.access_token = "fake-access"
    token.refresh_token = "fake-refresh"
    token.token_expiry = None
    return token


def _decode_raw(raw_b64: str) -> email_lib.message.Message:
    return email_lib.message_from_bytes(base64.urlsafe_b64decode(raw_b64))


# ── reply_to_for_inbox ────────────────────────────────────────────────────────


@pytest.mark.parametrize("inbox", [
    "katrina@taboost.me",
    "kylika@taboost.me",
    "audur@taboost.me",
    "trinity@taboost.me",
])
def test_partnerships_inboxes_get_reply_to(inbox):
    from backend.services.inbox_routing import reply_to_for_inbox

    assert reply_to_for_inbox(inbox) == PARTNERSHIPS


@pytest.mark.parametrize("inbox", [
    SHARED,                    # a group ADDRESS, never a member of a group
    "skyler@taboost.me",       # genuinely absent from sop.md Part 3
    "brittanie@taboost.me",
    "someone@example.com",
    "",
    None,
])
def test_inboxes_absent_from_sop_part3_get_no_reply_to(inbox):
    """Part 3: "If a talent or inbox is not listed here, leave Reply-To blank/default."

    allee@ and hana@ used to be asserted here as getting no Reply-To. That was
    wrong — both are listed under the talent-mgmt@ group in sop.md Part 3, and
    only two of Part 3's three groups had ever been mirrored into settings.json.
    See test_reply_to_routing.py, which checks config against sop.md directly.
    """
    from backend.services.inbox_routing import reply_to_for_inbox

    assert reply_to_for_inbox(inbox) is None


def test_reply_to_matching_is_case_and_whitespace_insensitive():
    from backend.services.inbox_routing import reply_to_for_inbox

    assert reply_to_for_inbox("  Katrina@TaBoost.Me  ") == PARTNERSHIPS


def test_reply_to_keys_off_token_email_not_talent_key():
    """SOP v2b Rule 2 — match the inbox, never the sender or the key."""
    from backend.services.inbox_routing import reply_to_for_token

    # A token whose key looks like a partnerships talent but whose mailbox is
    # the shared inbox must NOT get the header.
    assert reply_to_for_token(_token(SHARED, talent_key="Katrina")) is None
    # And the reverse: the key is irrelevant when the mailbox is a member.
    assert reply_to_for_token(_token("audur@taboost.me", talent_key="whatever")) == PARTNERSHIPS


def test_reply_to_never_raises_on_broken_config():
    """Routing must never break drafting."""
    from backend.services import inbox_routing

    with patch.object(inbox_routing, "reply_to_groups", side_effect=RuntimeError("boom")):
        assert inbox_routing.reply_to_for_inbox("katrina@taboost.me") is None


# ── MIME header presence ──────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_sets_reply_to_for_partnerships_token(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = MagicMock()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)
    fake_svc.users().drafts().create().execute.return_value = {"id": "d1"}

    create_gmail_draft(
        token_row=_token("katrina@taboost.me", "Katrina"),
        thread_id="t1",
        reply_to="brand@nike.com",
        subject="Partnership",
        body="Hi",
    )

    body_arg = fake_svc.users().drafts().create.call_args.kwargs["body"]
    raw = _decode_raw(body_arg["message"]["raw"])
    assert raw["Reply-To"] == PARTNERSHIPS


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_omits_reply_to_for_shared_inbox(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = MagicMock()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)
    fake_svc.users().drafts().create().execute.return_value = {"id": "d2"}

    create_gmail_draft(
        token_row=_token(SHARED, "shared-inbox"),
        thread_id="t1",
        reply_to="brand@nike.com",
        subject="Partnership",
        body="Hi",
    )

    body_arg = fake_svc.users().drafts().create.call_args.kwargs["body"]
    raw = _decode_raw(body_arg["message"]["raw"])
    assert raw["Reply-To"] is None


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_send_reply_sets_reply_to_for_partnerships_token(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import send_reply

    fake_svc = MagicMock()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    ok, _ = send_reply(
        token_row=_token("trinity@taboost.me", "Trin"),
        thread_id="t1",
        reply_to="brand@nike.com",
        subject="Re: Deal",
        body="Sounds good",
    )

    assert ok is True
    body_arg = fake_svc.users().messages().send.call_args.kwargs["body"]
    raw = _decode_raw(body_arg["raw"])
    assert raw["Reply-To"] == PARTNERSHIPS


# ── resolve_token_for_talent ──────────────────────────────────────────────────


def test_resolver_prefers_the_talents_own_token(db_session):
    from backend.services.inbox_routing import resolve_token_for_talent

    make_token(db_session, talent_key="shared-inbox", email=SHARED)
    own = make_token(db_session, talent_key="Katrina", email="katrina@taboost.me")

    assert resolve_token_for_talent(db_session, "Katrina").id == own.id
    assert resolve_token_for_talent(db_session, "katrina").id == own.id  # case-insensitive


def test_resolver_falls_back_to_shared_inbox(db_session):
    """A talent polled from the consolidated inbox has no token of their own —
    their drafts live in the shared mailbox, so that is what must send them."""
    from backend.services.inbox_routing import resolve_token_for_talent

    shared = make_token(db_session, talent_key="shared-inbox", email=SHARED)

    assert resolve_token_for_talent(db_session, "Allee").id == shared.id
    assert resolve_token_for_talent(db_session, "Hana").id == shared.id


def test_resolver_returns_none_when_nothing_is_connected(db_session):
    from backend.services.inbox_routing import resolve_token_for_talent

    assert resolve_token_for_talent(db_session, "Allee") is None


def test_resolver_ignores_inactive_own_token_and_falls_back(db_session):
    from backend.services.inbox_routing import resolve_token_for_talent

    shared = make_token(db_session, talent_key="shared-inbox", email=SHARED)
    make_token(db_session, talent_key="Katrina", email="katrina@taboost.me", active=False)

    assert resolve_token_for_talent(db_session, "Katrina").id == shared.id


def test_shared_inbox_token_found_by_email_regardless_of_key(db_session):
    """OAuth auto-generates an unpredictable talent_key, so the email is what
    identifies the consolidated mailbox."""
    from backend.services.inbox_routing import get_shared_inbox_token

    tok = make_token(db_session, talent_key="talentmgmt2", email=SHARED)
    assert get_shared_inbox_token(db_session).id == tok.id


def test_is_shared_inbox_token_recognises_both_conventions():
    from backend.services.inbox_routing import is_shared_inbox_token

    assert is_shared_inbox_token(_token(SHARED, "anything")) is True
    assert is_shared_inbox_token(_token("Talent-Mgmt@TABOOST.me", "x")) is True
    assert is_shared_inbox_token(_token("unknown@x.com", "shared-inbox")) is True
    assert is_shared_inbox_token(_token("katrina@taboost.me", "Katrina")) is False


# ── config integrity ──────────────────────────────────────────────────────────


def test_alias_map_values_all_match_a_sop_key():
    """A value with no sop.md profile is dropped silently by the poller."""
    from backend.core.config import get_settings

    settings = get_settings()
    alias_map = settings.app_config["single_inbox"]["alias_map"]
    profile_keys = {k.lower() for k in settings.talent_profiles}

    unknown = {v for v in alias_map.values() if v.lower() not in profile_keys}
    assert not unknown, f"alias_map values with no sop.md profile: {sorted(unknown)}"


def test_partnerships_talents_are_not_in_the_alias_map():
    """They poll their own mailboxes — an alias entry would double-route them."""
    from backend.core.config import get_settings

    alias_map = get_settings().app_config["single_inbox"]["alias_map"]
    for alias in ("katrina@taboost.me", "kylika@taboost.me",
                  "audur@taboost.me", "trinity@taboost.me"):
        assert alias not in alias_map
