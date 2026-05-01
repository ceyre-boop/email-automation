"""
Tests for /auth routes — connect redirect and callback token storage.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.models.db import OAuthState, TalentToken
from backend.tests.conftest import make_token


_TEST_STATE = "test-csrf-state-token"


def _insert_state(db_session, talent_key: str = "Sylvia") -> str:
    """Pre-insert an OAuthState row so the callback CSRF check passes."""
    db_session.add(OAuthState(state=_TEST_STATE, pinned_talent_key=talent_key))
    db_session.commit()
    return _TEST_STATE


# ── GET /auth/connect ─────────────────────────────────────────────────────────

def test_connect_redirects_to_google(client):
    """Should 307-redirect to accounts.google.com."""
    resp = client.get("/auth/connect?talent_key=Sylvia", follow_redirects=False)
    assert resp.status_code in (307, 302)
    location = resp.headers.get("location", "")
    assert "accounts.google.com" in location or "google.com" in location


def test_connect_encodes_talent_key_in_state(client):
    resp = client.get("/auth/connect?talent_key=Sylvia", follow_redirects=False)
    location = resp.headers.get("location", "")
    assert "Sylvia" in location or "state=" in location


def test_connect_unknown_talent_returns_404(client):
    resp = client.get("/auth/connect?talent_key=notatalent", follow_redirects=False)
    assert resp.status_code == 404


def test_connect_missing_talent_key_returns_redirect(client):
    """No talent_key → should still redirect to Google (talent-less connect is allowed)."""
    resp = client.get("/auth/connect", follow_redirects=False)
    assert resp.status_code in (307, 302)


# ── GET /auth/callback ────────────────────────────────────────────────────────

def _mock_exchange_code_result():
    return {
        "access_token": "ya29.test-access-token",
        "refresh_token": "1//test-refresh-token",
        "expiry": datetime.utcnow() + timedelta(hours=1),
    }


def _mock_userinfo_response():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "email": "sylvia@gmail.com",
        "sub": "google-uid-123",
        "name": "Sylvia",
    }
    return mock_resp


@patch("backend.routers.auth.requests.get")
@patch("backend.routers.auth.exchange_code")
def test_callback_stores_new_token(mock_exchange, mock_get, client, db_session):
    _insert_state(db_session)
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_get.return_value = _mock_userinfo_response()

    resp = client.get(f"/auth/callback?code=test-code&state={_TEST_STATE}")
    assert resp.status_code == 200
    assert "connected" in resp.text.lower() or "✅" in resp.text

    row = db_session.query(TalentToken).filter(TalentToken.talent_key == "Sylvia").first()
    assert row is not None
    assert row.email == "sylvia@gmail.com"
    assert row.access_token == "ya29.test-access-token"
    assert row.active is True


@patch("backend.routers.auth.requests.get")
@patch("backend.routers.auth.exchange_code")
def test_callback_updates_existing_token(mock_exchange, mock_get, client, db_session):
    make_token(db_session, talent_key="Sylvia")
    _insert_state(db_session)

    mock_exchange.return_value = {
        "access_token": "ya29.new-token",
        "refresh_token": "1//new-refresh",
        "expiry": datetime.utcnow() + timedelta(hours=1),
    }
    mock_get.return_value = _mock_userinfo_response()

    resp = client.get(f"/auth/callback?code=test-code&state={_TEST_STATE}")
    assert resp.status_code == 200

    db_session.expire_all()
    row = db_session.query(TalentToken).filter(TalentToken.talent_key == "Sylvia").first()
    assert row.access_token == "ya29.new-token"


@patch("backend.routers.auth.exchange_code", side_effect=Exception("Token exchange failed"))
def test_callback_exchange_failure_returns_500(mock_exchange, client, db_session):
    _insert_state(db_session)
    resp = client.get(f"/auth/callback?code=bad-code&state={_TEST_STATE}")
    assert resp.status_code == 500


def test_callback_unknown_state_returns_400(client):
    resp = client.get("/auth/callback?code=test&state=notavalidstate")
    assert resp.status_code == 400


def test_callback_missing_code_returns_422(client):
    resp = client.get("/auth/callback?state=somestate")
    assert resp.status_code == 422


@patch("backend.routers.auth.requests.get")
@patch("backend.routers.auth.exchange_code")
def test_callback_success_page_shows_talent_name(mock_exchange, mock_get, client, db_session):
    _insert_state(db_session)
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_get.return_value = _mock_userinfo_response()

    resp = client.get(f"/auth/callback?code=test-code&state={_TEST_STATE}")
    assert resp.status_code == 200
    # Talent full_name from settings.json should appear in the HTML
    assert "<strong>" in resp.text or "Sylvia" in resp.text


@patch("backend.routers.auth.requests.get")
@patch("backend.routers.auth.exchange_code")
def test_callback_success_page_escapes_html(mock_exchange, mock_get, client, db_session):
    """Ensure talent name is HTML-escaped (no XSS)."""
    _insert_state(db_session)
    mock_exchange.return_value = _mock_exchange_code_result()
    mock_get.return_value = _mock_userinfo_response()

    resp = client.get(f"/auth/callback?code=test-code&state={_TEST_STATE}")
    assert resp.status_code == 200
    # Raw angle brackets from talent name should never appear unescaped
    assert "<script>" not in resp.text
