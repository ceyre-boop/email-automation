"""
Tests for /health, /cron/poll-inboxes, and /api/status routes.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import Draft, DraftStatus
from backend.tests.conftest import make_draft, make_token


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # deployed_at is also present but we don't assert its exact value


# ── GET /api/status ───────────────────────────────────────────────────────────

def test_status_lists_all_talents(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "talents" in data
    assert len(data["talents"]) > 0


def test_status_shows_not_connected_by_default(client):
    resp = client.get("/api/status")
    data = resp.json()
    # No tokens inserted — all should be not connected
    for t in data["talents"]:
        assert t["connected"] is False


def test_status_shows_connected_talent(client, db_session):
    make_token(db_session, talent_key="Sylvia")
    resp = client.get("/api/status")
    data = resp.json()
    sylvia = next((t for t in data["talents"] if t["key"] == "Sylvia"), None)
    assert sylvia is not None
    assert sylvia["connected"] is True
    assert sylvia["email"] == "sylvia@gmail.com"


def test_status_pending_drafts_count(client, db_session):
    make_token(db_session)
    make_draft(db_session)
    make_draft(db_session)
    resp = client.get("/api/status")
    assert resp.json()["pending_drafts"] == 2


def test_status_pending_drafts_excludes_sent(client, db_session):
    make_token(db_session)
    make_draft(db_session, status=DraftStatus.pending)
    make_draft(db_session, status=DraftStatus.sent)
    resp = client.get("/api/status")
    assert resp.json()["pending_drafts"] == 1


def test_status_talent_has_required_fields(client):
    resp = client.get("/api/status")
    for t in resp.json()["talents"]:
        assert "key" in t
        assert "full_name" in t
        assert "connected" in t


# ── GET /cron/poll-inboxes ────────────────────────────────────────────────────

@patch("backend.routers.cron.poll_all_inboxes")
def test_cron_poll_success(mock_poll, client):
    mock_poll.return_value = {"processed": 3, "skipped": 0, "errors": []}
    resp = client.get("/cron/poll-inboxes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # Poll runs in the background; the response confirms it was queued
    assert data["status"] == "poll started in background"


@patch("backend.routers.cron.poll_all_inboxes", side_effect=Exception("DB down"))
def test_cron_poll_exception_does_not_crash(mock_poll, client):
    """Cron endpoint returns 200 immediately (poll runs in background); never raises 500."""
    resp = client.get("/cron/poll-inboxes")
    assert resp.status_code == 200
    # Background task errors are logged but the HTTP response is always ok=True
    assert resp.json()["ok"] is True


@patch("backend.routers.cron.poll_all_inboxes")
def test_cron_poll_no_tokens_returns_empty_summary(mock_poll, client):
    mock_poll.return_value = {"processed": 0, "skipped": 0, "errors": []}
    resp = client.get("/cron/poll-inboxes")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
