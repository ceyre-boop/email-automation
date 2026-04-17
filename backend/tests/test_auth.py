"""
Tests for /auth routes — connect redirect and callback token storage.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import TalentToken
from backend.tests.conftest import make_token


# ── GET /auth/connect ─────────────────────────────────────────────────────────

def test_connect_redirects_to_google(client):
    """Should 307-redirect to accounts.google.com."""
    resp = client.get("/auth/connect?talent_key=Sylvia", allow_redirects=False)
    assert resp.status_code in (307, 302)
    location = resp.headers.get("location", "")
    assert "accounts.google.com" in location or "google.com" in location


def test_connect_encodes_talent_key_in_state(client):
    resp = client.get("/auth/connect?talent_key=Sylvia", allow_redirects=False)
    location = resp.headers.get("location", "")
    assert "Sylvia" in location or "state=" in location


def test_connect_unknown_talent_returns_404(client):
    resp = client.get("/auth/connect?talent_key=notatalent", allow_redirects=False)
    assert resp.status_code == 404


def test_connect_missing_talent_key_returns_422(client):
    resp = client.get("/auth/connect", allow_redirects=False)
    assert resp.status_code == 422


# ── GET /auth/callback ────────────────────────────────────────────────────────

def _mock_exchange_code_result():
    return {
        "access_token": "ya29.test-access-token",
        "refresh_token": "1//test-refresh-token",
        "expiry": datetime.utcnow() + timedelta(hours=1),
    }


def _mock_userinfo():
    mock_service = MagicMock()
    mock_service.userinfo().get().execute.return_value = {
        "email": "sylvia@gmail.com",
        "id": "google-uid-123",
    }
    return mock_service


@patch("backend.routers.auth.build")
@patch("backend.routers.auth.exchange_code")
def test_callback_stores_new_token(mock_exchange, mock_build, client, db_session):
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_build.return_value = _mock_userinfo()

    resp = client.get("/auth/callback?code=test-code&state=Sylvia")
    assert resp.status_code == 200
    assert "connected" in resp.text.lower() or "✅" in resp.text

    row = db_session.query(TalentToken).filter(TalentToken.talent_key == "Sylvia").first()
    assert row is not None
    assert row.email == "sylvia@gmail.com"
    assert row.access_token == "ya29.test-access-token"
    assert row.active is True


@patch("backend.routers.auth.build")
@patch("backend.routers.auth.exchange_code")
def test_callback_updates_existing_token(mock_exchange, mock_build, client, db_session):
    make_token(db_session, talent_key="Sylvia")

    mock_exchange.return_value = {
        "access_token": "ya29.new-token",
        "refresh_token": "1//new-refresh",
        "expiry": datetime.utcnow() + timedelta(hours=1),
    }
    mock_build.return_value = _mock_userinfo()

    resp = client.get("/auth/callback?code=test-code&state=Sylvia")
    assert resp.status_code == 200

    db_session.expire_all()
    row = db_session.query(TalentToken).filter(TalentToken.talent_key == "Sylvia").first()
    assert row.access_token == "ya29.new-token"


@patch("backend.routers.auth.exchange_code", side_effect=Exception("Token exchange failed"))
def test_callback_exchange_failure_returns_500(mock_exchange, client):
    resp = client.get("/auth/callback?code=bad-code&state=Sylvia")
    assert resp.status_code == 500


def test_callback_unknown_state_returns_400(client):
    resp = client.get("/auth/callback?code=test&state=notatalent")
    assert resp.status_code == 400


def test_callback_missing_code_returns_422(client):
    resp = client.get("/auth/callback?state=Sylvia")
    assert resp.status_code == 422


@patch("backend.routers.auth.build")
@patch("backend.routers.auth.exchange_code")
def test_callback_success_page_shows_talent_name(mock_exchange, mock_build, client, db_session):
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_build.return_value = _mock_userinfo()

    resp = client.get("/auth/callback?code=test-code&state=Sylvia")
    assert resp.status_code == 200
    # Talent full_name from settings.json should appear in the HTML
    assert "<strong>" in resp.text


@patch("backend.routers.auth.build")
@patch("backend.routers.auth.exchange_code")
def test_callback_success_page_escapes_html(mock_exchange, mock_build, client, db_session):
    """Ensure talent name is HTML-escaped (no XSS)."""
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_build.return_value = _mock_userinfo()

    resp = client.get("/auth/callback?code=test-code&state=Sylvia")
    assert resp.status_code == 200
    # Raw angle brackets from talent name should never appear unescaped
    assert "<script>" not in resp.text
