"""
Tests for /api/drafts routes — list, get, approve, edit, discard.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import Draft, DraftStatus
from backend.tests.conftest import make_draft, make_token


# ── GET /api/drafts ────────────────────────────────────────────────────────────

def test_list_drafts_empty(client):
    resp = client.get("/api/drafts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_drafts_returns_pending(client, db_session):
    make_token(db_session)
    make_draft(db_session)
    resp = client.get("/api/drafts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "pending"


def test_list_drafts_filters_by_status(client, db_session):
    make_token(db_session)
    make_draft(db_session, status=DraftStatus.pending)
    make_draft(db_session, status=DraftStatus.sent)

    # Default (pending only)
    resp = client.get("/api/drafts")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # Filter by sent
    resp = client.get("/api/drafts?status=sent")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "sent"


def test_list_drafts_filters_by_talent(client, db_session):
    make_token(db_session, talent_key="Sylvia")
    make_token(db_session, talent_key="Trin")
    make_draft(db_session, talent_key="Sylvia")
    make_draft(db_session, talent_key="Trin")

    resp = client.get("/api/drafts?talent_key=Sylvia")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["talent_key"] == "Sylvia"


def test_list_drafts_multiple(client, db_session):
    make_token(db_session)
    make_draft(db_session)
    make_draft(db_session)
    resp = client.get("/api/drafts")
    assert len(resp.json()) == 2


# ── GET /api/drafts/{id} ───────────────────────────────────────────────────────

def test_get_draft_by_id(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)
    resp = client.get(f"/api/drafts/{draft.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == draft.id
    assert resp.json()["brand_name"] == "Nike"


def test_get_draft_not_found(client):
    resp = client.get("/api/drafts/9999")
    assert resp.status_code == 404


# ── POST /api/drafts/{id}/edit ─────────────────────────────────────────────────

def test_edit_draft_updates_text(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)
    resp = client.post(f"/api/drafts/{draft.id}/edit", json={"draft_text": "Updated reply."})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify in DB
    db_session.refresh(draft)
    assert draft.draft_text == "Updated reply."
    assert draft.status == DraftStatus.pending  # stays pending


def test_edit_draft_empty_text_rejected(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)
    resp = client.post(f"/api/drafts/{draft.id}/edit", json={"draft_text": "   "})
    assert resp.status_code == 422


def test_edit_draft_not_found(client):
    resp = client.post("/api/drafts/9999/edit", json={"draft_text": "test"})
    assert resp.status_code == 404


def test_edit_already_sent_draft_rejected(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session, status=DraftStatus.sent)
    resp = client.post(f"/api/drafts/{draft.id}/edit", json={"draft_text": "New text"})
    assert resp.status_code == 400


def test_edit_already_discarded_draft_rejected(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session, status=DraftStatus.discarded)
    resp = client.post(f"/api/drafts/{draft.id}/edit", json={"draft_text": "New text"})
    assert resp.status_code == 400


# ── POST /api/drafts/{id}/discard ─────────────────────────────────────────────

def test_discard_draft(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)
    resp = client.post(f"/api/drafts/{draft.id}/discard")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    db_session.refresh(draft)
    assert draft.status == DraftStatus.discarded
    assert draft.reviewed_at is not None


def test_discard_draft_with_reviewer(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)
    resp = client.post(f"/api/drafts/{draft.id}/discard", json={"reviewed_by": "cara"})
    assert resp.status_code == 200
    db_session.refresh(draft)
    assert draft.reviewed_by == "cara"


def test_discard_draft_not_found(client):
    resp = client.post("/api/drafts/9999/discard")
    assert resp.status_code == 404


def test_discard_already_sent_rejected(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session, status=DraftStatus.sent)
    resp = client.post(f"/api/drafts/{draft.id}/discard")
    assert resp.status_code == 400


# ── POST /api/drafts/{id}/approve ─────────────────────────────────────────────

@patch("backend.routers.drafts.gmail_svc.send_reply", return_value=True)
def test_approve_draft_success(mock_send, client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)

    resp = client.post(f"/api/drafts/{draft.id}/approve", json={"reviewed_by": "chenni"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    db_session.refresh(draft)
    assert draft.status == DraftStatus.sent
    assert draft.reviewed_by == "chenni"
    mock_send.assert_called_once()


@patch("backend.routers.drafts.gmail_svc.send_reply", return_value=False)
def test_approve_draft_gmail_failure(mock_send, client, db_session):
    make_token(db_session)
    draft = make_draft(db_session)

    resp = client.post(f"/api/drafts/{draft.id}/approve")
    assert resp.status_code == 502
    # Draft should remain pending on failure
    db_session.refresh(draft)
    assert draft.status == DraftStatus.pending


def test_approve_draft_not_found(client):
    resp = client.post("/api/drafts/9999/approve")
    assert resp.status_code == 404


def test_approve_already_sent_rejected(client, db_session):
    make_token(db_session)
    draft = make_draft(db_session, status=DraftStatus.sent)
    resp = client.post(f"/api/drafts/{draft.id}/approve")
    assert resp.status_code == 400


def test_approve_no_active_token(client, db_session):
    """If the talent has no active token, approve should 404."""
    # No token inserted — talent is not connected
    draft = make_draft(db_session)
    resp = client.post(f"/api/drafts/{draft.id}/approve")
    assert resp.status_code == 404


@patch("backend.routers.drafts.gmail_svc.send_reply", return_value=True)
@patch("backend.routers.drafts.gmail_svc.delete_gmail_draft")
def test_approve_deletes_gmail_draft_copy(mock_delete, mock_send, client, db_session):
    make_token(db_session)
    draft = make_draft(db_session, gmail_draft_id="gmail-draft-abc")

    resp = client.post(f"/api/drafts/{draft.id}/approve")
    assert resp.status_code == 200
    mock_delete.assert_called_once()
