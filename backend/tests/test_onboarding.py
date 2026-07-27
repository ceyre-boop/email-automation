"""
Tests for the onboarding static page — GET /connect
"""
from __future__ import annotations

import pytest


def test_connect_page_loads(client):
    # "Jocelyn" is present in sheets/sop.md — use a real roster entry so the
    # route's talent-key validation passes.
    resp = client.get("/connect?talent=Jocelyn")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_connect_page_has_connect_button(client):
    resp = client.get("/connect?talent=Jocelyn")
    assert "Connect Gmail" in resp.text or "connect" in resp.text.lower()


def test_connect_page_unknown_talent_returns_404(client):
    resp = client.get("/connect?talent=notatalent")
    assert resp.status_code == 404


def test_connect_page_missing_talent_returns_422(client):
    resp = client.get("/connect")
    assert resp.status_code == 422
