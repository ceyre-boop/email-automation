"""
Tests for the onboarding static page — GET /connect
"""
from __future__ import annotations

import pytest


def test_connect_page_loads(client):
    resp = client.get("/connect?talent=Sylvia")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_connect_page_has_connect_button(client):
    resp = client.get("/connect?talent=Sylvia")
    assert "Connect Gmail" in resp.text or "connect" in resp.text.lower()


def test_connect_page_unknown_talent_returns_404(client):
    resp = client.get("/connect?talent=notatalent")
    assert resp.status_code == 404


def test_connect_page_missing_talent_returns_404_or_422(client):
    resp = client.get("/connect")
    assert resp.status_code in (404, 422)
