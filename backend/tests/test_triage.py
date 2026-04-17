"""
Tests for triage service — prompt building, special routing, fallback logic.
No live OpenAI calls; the API client is mocked.
"""
from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from backend.services.triage import (
    _apply_special_routing,
    _build_triage_messages,
    _fallback,
    triage_email,
)


# ── Prompt building ────────────────────────────────────────────────────────────

def test_triage_messages_no_unreplaced_placeholders():
    msgs = _build_triage_messages(
        talent_name="Trinity Blair",
        minimum_rate=2000,
        subject="Partnership opportunity",
        sender="brand@nike.com",
        sender_domain="nike.com",
        body="Hi, we'd love to collaborate.",
    )
    combined = msgs[0]["content"] + msgs[1]["content"]
    placeholders = re.findall(r"\{\{[A-Z_]+\}\}", combined)
    assert placeholders == [], f"Unreplaced placeholders found: {placeholders}"


def test_triage_messages_contains_talent_name():
    msgs = _build_triage_messages("Trinity Blair", 2000, "Subj", "a@b.com", "b.com", "body")
    assert "Trinity Blair" in msgs[1]["content"]


def test_triage_messages_contains_minimum_rate():
    msgs = _build_triage_messages("Sam", 700, "Subj", "a@b.com", "b.com", "body")
    assert "700" in msgs[1]["content"]


def test_triage_messages_body_truncated_at_4000():
    long_body = "x" * 5000
    msgs = _build_triage_messages("Sam", 700, "Subj", "a@b.com", "b.com", long_body)
    assert "x" * 4001 not in msgs[1]["content"]


def test_triage_system_prompt_is_non_empty():
    msgs = _build_triage_messages("Sam", 700, "Subj", "a@b.com", "b.com", "body")
    assert len(msgs[0]["content"]) > 100


def test_triage_returns_two_messages():
    msgs = _build_triage_messages("Sam", 700, "Subj", "a@b.com", "b.com", "body")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


# ── Special routing ────────────────────────────────────────────────────────────

def test_trin_commission_only_overrides_to_score1():
    policy = {"special_talent_routing": {"Trin": {}}}
    result = _apply_special_routing("Trin", 3, "affiliate commission", 0.0, policy)
    assert result == 1


def test_trin_paid_offer_not_overridden():
    policy = {"special_talent_routing": {"Trin": {}}}
    result = _apply_special_routing("Trin", 3, "Sponsored Post", 2000.0, policy)
    assert result == 3


def test_michaela_below_floor_overrides_to_score1():
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Michaela", 3, "Sponsored Post", 500.0, policy)
    assert result == 1


def test_michaela_at_floor_not_overridden():
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Michaela", 3, "Sponsored Post", 1000.0, policy)
    assert result == 3


def test_michaela_zero_rate_not_overridden_by_floor():
    """Rate=0 means we don't know the rate — don't override."""
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Michaela", 3, "Sponsored Post", 0.0, policy)
    assert result == 3


def test_other_talent_not_affected():
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Colleen", 3, "Sponsored Post", 100.0, policy)
    assert result == 3


# ── Fallback ───────────────────────────────────────────────────────────────────

def test_fallback_returns_score2():
    r = _fallback("Sylvia", "test error")
    assert r["score"] == 2
    assert "test error" in r["reason"]
    assert r["offer_type"] == "Unknown"
    assert r["proposed_rate_usd"] == 0.0


# ── triage_email with mocked OpenAI ───────────────────────────────────────────

def _mock_openai_response(score: int, offer_type: str = "Sponsored Post",
                          proposed_rate: float = 3500.0, brand_name: str = "Nike",
                          reason: str = "Test reason"):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "score": score,
        "offer_type": offer_type,
        "proposed_rate_usd": proposed_rate,
        "brand_name": brand_name,
        "reason": reason,
    })
    return mock_response


@patch("backend.services.triage.OpenAI")
def test_triage_email_score3(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(3)

    result = triage_email("Sylvia", "Sylvia", 1000, "Partnership", "brand@nike.com", "nike.com", "body")
    assert result["score"] == 3
    assert result["brand_name"] == "Nike"
    assert result["proposed_rate_usd"] == 3500.0
    assert result["offer_type"] == "Sponsored Post"


@patch("backend.services.triage.OpenAI")
def test_triage_email_score1(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(1, proposed_rate=0.0)

    result = triage_email("Sylvia", "Sylvia", 1000, "You won!", "spam@promo.net", "promo.net", "prize")
    assert result["score"] == 1


@patch("backend.services.triage.OpenAI")
def test_triage_email_invalid_score_falls_back(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    bad_response = MagicMock()
    bad_response.choices[0].message.content = json.dumps({"score": 99, "reason": "weird"})
    mock_client.chat.completions.create.return_value = bad_response

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["score"] == 2  # fallback


@patch("backend.services.triage.OpenAI")
def test_triage_email_api_error_falls_back(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = Exception("API timeout")

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["score"] == 2


@patch("backend.services.triage.OpenAI")
def test_triage_email_non_json_falls_back(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    bad_response = MagicMock()
    bad_response.choices[0].message.content = "This is not JSON"
    mock_client.chat.completions.create.return_value = bad_response

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["score"] == 2


@patch("backend.services.triage.OpenAI")
def test_triage_trin_commission_override(mock_openai_cls):
    """GPT says Score 3 but Trin affiliate+$0 should downgrade to 1."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(
        3, offer_type="affiliate commission", proposed_rate=0.0
    )
    result = triage_email("Trin", "Trinity", 2000, "Subj", "a@b.com", "b.com", "body")
    assert result["score"] == 1
