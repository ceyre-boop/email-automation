"""
Unit tests for backend/services/gmail.py.

All Google API calls are mocked at the `build()` level so no real OAuth
credentials are needed.  These tests verify that:
  - create_gmail_draft builds the MIME message correctly and stores the draft
    in Gmail with the right thread + raw payload
  - list_gmail_drafts fetches each stub and returns parsed dicts
  - delete_gmail_draft calls the correct API endpoint
  - send_gmail_draft sends the draft by ID
  - send_reply builds the correct MIME reply and sends it
  - archive_message removes the correct labels
"""
from __future__ import annotations

import base64
import email as email_lib
from unittest.mock import MagicMock, call, patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_token():
    token = MagicMock()
    token.talent_key = "katrina"
    token.access_token = "fake-access"
    token.refresh_token = "fake-refresh"
    token.token_expiry = None
    return token


def _mock_service():
    """Build a fake Gmail API service with chainable method mocks."""
    svc = MagicMock()
    return svc


# ── helpers ───────────────────────────────────────────────────────────────────


def _decode_raw(raw_b64: str) -> email_lib.message.Message:
    """Decode a base64url-encoded raw MIME message."""
    raw_bytes = base64.urlsafe_b64decode(raw_b64)
    return email_lib.message_from_bytes(raw_bytes)


# ── create_gmail_draft ────────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_returns_draft_id(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().create().execute.return_value = {"id": "draft-xyz"}

    result = create_gmail_draft(
        token_row=_make_token(),
        thread_id="thread-001",
        reply_to="brand@nike.com",
        subject="Partnership",
        body="Hi Nike!",
    )

    assert result == "draft-xyz"


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_adds_re_prefix(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().create().execute.return_value = {"id": "draft-abc"}

    create_gmail_draft(
        token_row=_make_token(),
        thread_id="t1",
        reply_to="x@brand.com",
        subject="Partnership",  # no "Re:" prefix
        body="Thanks!",
    )

    # Capture the body passed to .create()
    create_call = fake_svc.users().drafts().create
    body_arg = create_call.call_args[1]["body"]
    raw = _decode_raw(body_arg["message"]["raw"])
    assert raw["Subject"].startswith("Re:")


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_preserves_existing_re_prefix(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().create().execute.return_value = {"id": "d"}

    create_gmail_draft(
        token_row=_make_token(),
        thread_id="t1",
        reply_to="x@brand.com",
        subject="Re: Partnership",
        body="Body",
    )

    create_call = fake_svc.users().drafts().create
    body_arg = create_call.call_args[1]["body"]
    raw = _decode_raw(body_arg["message"]["raw"])
    # Should not double-prefix
    assert raw["Subject"] == "Re: Partnership"


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_sets_in_reply_to(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().create().execute.return_value = {"id": "d"}

    create_gmail_draft(
        token_row=_make_token(),
        thread_id="t1",
        reply_to="x@brand.com",
        subject="Re: Hello",
        body="Body",
        in_reply_to="<msg-id-123@mail.gmail.com>",
    )

    create_call = fake_svc.users().drafts().create
    body_arg = create_call.call_args[1]["body"]
    raw = _decode_raw(body_arg["message"]["raw"])
    assert raw["In-Reply-To"] == "<msg-id-123@mail.gmail.com>"
    assert raw["References"] == "<msg-id-123@mail.gmail.com>"


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_threads_correctly(mock_build, mock_creds, mock_refresh):
    """The threadId in the API payload must match the supplied thread_id."""
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().create().execute.return_value = {"id": "d"}

    create_gmail_draft(
        token_row=_make_token(),
        thread_id="thread-999",
        reply_to="x@brand.com",
        subject="Test",
        body="Body",
    )

    create_call = fake_svc.users().drafts().create
    body_arg = create_call.call_args[1]["body"]
    assert body_arg["message"]["threadId"] == "thread-999"


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_create_draft_returns_none_on_http_error(mock_build, mock_creds, mock_refresh):
    from googleapiclient.errors import HttpError
    from backend.services.gmail import create_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    resp_mock = MagicMock()
    resp_mock.status = 403
    fake_svc.users().drafts().create().execute.side_effect = HttpError(resp_mock, b"Forbidden")

    result = create_gmail_draft(
        token_row=_make_token(),
        thread_id="t1",
        reply_to="x@brand.com",
        subject="Test",
        body="Body",
    )

    assert result is None


# ── list_gmail_drafts ─────────────────────────────────────────────────────────


def _make_full_draft_response(draft_id: str, to: str, subject: str, body: str) -> dict:
    """Build the dict that drafts().get().execute() returns."""
    from email.mime.text import MIMEText
    mime = MIMEText(body, "plain")
    mime["To"] = to
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    return {
        "id": draft_id,
        "message": {
            "id": f"msg-{draft_id}",
            "threadId": f"thread-{draft_id}",
            "snippet": body[:50],
            "payload": {
                "headers": [
                    {"name": "To", "value": to},
                    {"name": "Subject", "value": subject},
                ],
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                "parts": [],
            },
        },
    }


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_list_gmail_drafts_returns_parsed_list(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import list_gmail_drafts

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    # list() returns two stubs
    fake_svc.users().drafts().list().execute.return_value = {
        "drafts": [{"id": "d1"}, {"id": "d2"}]
    }

    # get() returns full draft for each
    draft1 = _make_full_draft_response("d1", "brand1@co.com", "Deal 1", "Hello Brand1")
    draft2 = _make_full_draft_response("d2", "brand2@co.com", "Deal 2", "Hello Brand2")

    fake_svc.users().drafts().get.side_effect = lambda **kwargs: (
        MagicMock(execute=MagicMock(return_value=draft1))
        if kwargs.get("id") == "d1"
        else MagicMock(execute=MagicMock(return_value=draft2))
    )

    result = list_gmail_drafts(token_row=_make_token())

    assert len(result) == 2
    ids = {d["gmail_draft_id"] for d in result}
    assert ids == {"d1", "d2"}
    subjects = {d["subject"] for d in result}
    assert subjects == {"Deal 1", "Deal 2"}


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_list_gmail_drafts_empty_inbox(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import list_gmail_drafts

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().list().execute.return_value = {"drafts": []}

    result = list_gmail_drafts(token_row=_make_token())
    assert result == []


# ── delete_gmail_draft ────────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_delete_gmail_draft_calls_api(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import delete_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().delete().execute.return_value = {}

    result = delete_gmail_draft(token_row=_make_token(), gmail_draft_id="draft-abc")

    assert result is True
    fake_svc.users().drafts().delete.assert_called_with(userId="me", id="draft-abc")


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_delete_gmail_draft_returns_false_on_error(mock_build, mock_creds, mock_refresh):
    from googleapiclient.errors import HttpError
    from backend.services.gmail import delete_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    resp_mock = MagicMock()
    resp_mock.status = 404
    fake_svc.users().drafts().delete().execute.side_effect = HttpError(resp_mock, b"Not Found")

    result = delete_gmail_draft(token_row=_make_token(), gmail_draft_id="bad-id")
    assert result is None  # function returns None (falsy) on error


# ── send_gmail_draft ──────────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_send_gmail_draft_calls_api(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import send_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().drafts().send().execute.return_value = {"id": "msg-sent"}

    result = send_gmail_draft(token_row=_make_token(), gmail_draft_id="draft-xyz")

    assert result is True
    fake_svc.users().drafts().send.assert_called_with(userId="me", body={"id": "draft-xyz"})


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_send_gmail_draft_returns_false_on_error(mock_build, mock_creds, mock_refresh):
    from googleapiclient.errors import HttpError
    from backend.services.gmail import send_gmail_draft

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    resp_mock = MagicMock()
    resp_mock.status = 500
    fake_svc.users().drafts().send().execute.side_effect = HttpError(resp_mock, b"Server Error")

    result = send_gmail_draft(token_row=_make_token(), gmail_draft_id="draft-xyz")
    assert result is False


# ── send_reply ────────────────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_send_reply_builds_correct_mime(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import send_reply

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().messages().send().execute.return_value = {"id": "sent-msg"}

    result = send_reply(
        token_row=_make_token(),
        thread_id="thread-999",
        reply_to="brand@co.com",
        subject="Partnership",
        body="Thanks for reaching out!",
    )

    assert result is True
    send_call = fake_svc.users().messages().send
    body_arg = send_call.call_args[1]["body"]
    assert body_arg["threadId"] == "thread-999"
    raw = _decode_raw(body_arg["raw"])
    assert raw["To"] == "brand@co.com"
    assert raw["Subject"] == "Re: Partnership"


# ── archive_message ───────────────────────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_archive_message_removes_inbox_label(mock_build, mock_creds, mock_refresh):
    from backend.services.gmail import archive_message

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    mock_creds.return_value = MagicMock(token="t", expiry=None)
    mock_refresh.return_value = MagicMock(token="t", expiry=None)

    fake_svc.users().messages().modify().execute.return_value = {}

    result = archive_message(token_row=_make_token(), message_id="msg-001")

    assert result is True
    modify_call = fake_svc.users().messages().modify
    body_arg = modify_call.call_args[1]["body"]
    assert "INBOX" in body_arg["removeLabelIds"]
    assert "UNREAD" in body_arg["removeLabelIds"]


# ── token refresh is persisted to DB ─────────────────────────────────────────


@patch("backend.services.gmail.refresh_if_needed")
@patch("backend.services.gmail.credentials_from_token_row")
@patch("backend.services.gmail.build")
def test_token_refresh_persisted_when_db_provided(mock_build, mock_creds, mock_refresh):
    """If db is passed, refreshed access_token must be committed."""
    from backend.services.gmail import create_gmail_draft

    new_creds = MagicMock()
    new_creds.token = "new-access-token"
    new_creds.expiry = None
    mock_creds.return_value = MagicMock(token="old", expiry=None)
    mock_refresh.return_value = new_creds

    fake_svc = _mock_service()
    mock_build.return_value = fake_svc
    fake_svc.users().drafts().create().execute.return_value = {"id": "d"}

    token = _make_token()
    db = MagicMock()

    create_gmail_draft(
        token_row=token, thread_id="t", reply_to="x@y.com", subject="S", body="B", db=db
    )

    assert token.access_token == "new-access-token"
    db.add.assert_called_with(token)
    db.commit.assert_called()
