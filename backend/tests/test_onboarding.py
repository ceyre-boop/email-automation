"""
Tests for the onboarding static page — GET /connect
"""
from __future__ import annotations

import pytest


def test_root_returns_html_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "TABOOST" in resp.text


def test_root_contains_genie_image(client):
    import base64
    resp = client.get("/")
    assert "data:image/svg+xml;base64," in resp.text
    # Extract the base64 payload and confirm it decodes to a valid SVG
    marker = 'data:image/svg+xml;base64,'
    start  = resp.text.index(marker) + len(marker)
    end    = resp.text.index('"', start)
    svg_bytes = base64.b64decode(resp.text[start:end])
    assert svg_bytes.strip().startswith(b'<svg')


def test_root_contains_onboarding_script(client):
    resp = client.get("/")
    assert "talent_key" in resp.text or "talentKey" in resp.text


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


def test_connect_page_missing_talent_returns_422(client):
    resp = client.get("/connect")
    assert resp.status_code == 422
