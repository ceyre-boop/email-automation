"""
API tests for the External Channel Review dashboard surface.

Covers the list endpoint (undismissed items, original body returned) and the
per-item dismiss endpoint (removes from dashboard only — informational).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.models.db import EmailStatus, ProcessedEmail
from backend.routers.deps import verify_api_key


@pytest.fixture(autouse=True)
def _bypass_api_key(client):
    """The router requires an x-api-key header; override the guard for these tests."""
    client.app.dependency_overrides[verify_api_key] = lambda: None
    yield
    client.app.dependency_overrides.pop(verify_api_key, None)


def _make_ext_email(db_session, mid: str, channel: str, dismissed: bool = False):
    row = ProcessedEmail(
        talent_key="Sylvia",
        gmail_message_id=mid,
        thread_id=f"thread-{mid}",
        sender="brand@nike.com",
        subject="Let's chat",
        score=3,
        offer_type="Sponsored Post",
        body_text="Hi! Please message me on WhatsApp to continue.",
        status=EmailStatus.draft_saved,
        processed_at=datetime.utcnow(),
        external_channel_review=True,
        external_channel_requested=channel,
        external_channel_dismissed=dismissed,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_list_returns_only_undismissed_with_body(client, db_session):
    _make_ext_email(db_session, "ext-1", "WhatsApp")
    _make_ext_email(db_session, "ext-2", "Discord", dismissed=True)

    resp = client.get("/api/dashboard/external-channel-review")
    assert resp.status_code == 200
    data = resp.json()

    ids = {r["gmail_message_id"] for r in data}
    assert ids == {"ext-1"}  # dismissed one excluded
    item = data[0]
    assert item["channel_requested"] == "WhatsApp"
    # Original inbound body is returned (not a generated reply).
    assert "WhatsApp" in item["body_text"]


def test_dismiss_removes_from_list(client, db_session):
    _make_ext_email(db_session, "ext-3", "Both")

    assert len(client.get("/api/dashboard/external-channel-review").json()) == 1

    resp = client.post("/api/dashboard/external-channel-review/ext-3/dismiss")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.get("/api/dashboard/external-channel-review").json() == []

    # Row still exists; only the dismissed flag flipped (informational — nothing else changed).
    row = db_session.query(ProcessedEmail).filter_by(gmail_message_id="ext-3").first()
    assert row.external_channel_dismissed is True
    assert row.status == EmailStatus.draft_saved


def test_dismiss_unknown_message_404(client, db_session):
    resp = client.post("/api/dashboard/external-channel-review/does-not-exist/dismiss")
    assert resp.status_code == 404
