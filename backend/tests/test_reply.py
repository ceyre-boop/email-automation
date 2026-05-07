"""
Tests for the reply drafting service.
No live OpenAI calls; the API client is mocked.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from backend.services.reply import _build_reply_messages, _escalate_result, _redact_pii, draft_reply


# ── PII redaction ──────────────────────────────────────────────────────────────

def test_redact_pii_phone_number():
    text = "Call us at (555) 867-5309 for details."
    result = _redact_pii(text)
    assert "(555) 867-5309" not in result
    assert "[REDACTED]" in result


def test_redact_pii_ssn():
    text = "SSN: 123-45-6789"
    result = _redact_pii(text)
    assert "123-45-6789" not in result
    assert "[REDACTED]" in result


def test_redact_pii_street_address():
    text = "Mail checks to 123 Main Street Los Angeles CA."
    result = _redact_pii(text)
    assert "123 Main Street" not in result


def test_redact_pii_clean_text_unchanged():
    text = "Thank you for reaching out about a partnership!"
    assert _redact_pii(text) == text


# ── Prompt building ────────────────────────────────────────────────────────────

def test_reply_messages_no_unreplaced_placeholders():
    msgs = _build_reply_messages(
        talent_key="Sylvia",
        talent_name="Sylvia",
        minimum_rate=1000,
        subject="Partnership",
        sender="brand@nike.com",
        offer_type="Sponsored Post",
        brand_name="Nike",
        proposed_rate=3500.0,
        triage_reason="Score 3 — strong brand, rate above minimum",
    )
    combined = msgs[0]["content"] + msgs[1]["content"]
    placeholders = re.findall(r"\{\{[A-Z_]+\}\}", combined)
    assert placeholders == [], f"Unreplaced placeholders: {placeholders}"


def test_reply_messages_contain_talent_name():
    msgs = _build_reply_messages(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Subj", sender="a@b.com", offer_type="Sponsored Post",
        brand_name="Nike", proposed_rate=1000.0, triage_reason="reason",
    )
    assert "Sylvia" in msgs[1]["content"]


def test_reply_messages_contain_brand_name():
    msgs = _build_reply_messages(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Subj", sender="a@b.com", offer_type="Sponsored Post",
        brand_name="Sephora", proposed_rate=1000.0, triage_reason="reason",
    )
    assert "Sephora" in msgs[1]["content"]


def test_reply_messages_returns_two_chat_messages():
    msgs = _build_reply_messages(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Subj", sender="a@b.com", offer_type="Sponsored Post",
        brand_name="Nike", proposed_rate=1000.0, triage_reason="reason",
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


# ── Escalate helper ────────────────────────────────────────────────────────────

def test_escalate_result_structure():
    r = _escalate_result("Rate too low")
    assert r["is_escalate"] is True
    assert "Rate too low" in r["escalate_reason"]
    assert "ESCALATE:" in r["draft_text"]


# ── draft_reply with mocked OpenAI ────────────────────────────────────────────

@patch("backend.services.reply.OpenAI")
def test_draft_reply_normal(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value.choices[0].message.content = (
        "Hi Nike, thank you for reaching out about a partnership with Sylvia!"
    )

    result = draft_reply(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Partnership", sender="brand@nike.com",
        offer_type="Sponsored Post", brand_name="Nike",
        proposed_rate=3500.0, triage_reason="Score 3",
    )
    assert result["is_escalate"] is False
    assert result["escalate_reason"] is None
    assert "Nike" in result["draft_text"]


@patch("backend.services.reply.OpenAI")
def test_draft_reply_no_offer_uses_initial_rule_without_openai(mock_openai_cls):
    result = draft_reply(
        talent_key="Sylvia",
        talent_name="Sylvia",
        minimum_rate=1000,
        subject="Partnership inquiry",
        sender="brand@nike.com",
        offer_type="Unknown",
        brand_name="Nike",
        proposed_rate=0.0,
        triage_reason="No firm offer shared.",
    )
    assert result["is_escalate"] is False
    assert "However her rates are higher than your offer" not in result["draft_text"]
    assert "potential partnership" in result["draft_text"]
    mock_openai_cls.assert_not_called()


@patch("backend.services.reply.OpenAI")
def test_draft_reply_below_minimum_uses_counter_rule_without_openai(mock_openai_cls):
    result = draft_reply(
        talent_key="Sylvia",
        talent_name="Sylvia",
        minimum_rate=1000,
        subject="Offer",
        sender="brand@nike.com",
        offer_type="Sponsored Post",
        brand_name="Nike",
        proposed_rate=250.0,
        triage_reason="Low offer.",
    )
    assert result["is_escalate"] is False
    assert "However her rates are higher than your offer" in result["draft_text"]
    mock_openai_cls.assert_not_called()


@patch("backend.services.reply.OpenAI")
def test_draft_reply_gpt_escalates(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value.choices[0].message.content = (
        "ESCALATE: Rate is $50, well below the $1000 minimum."
    )

    result = draft_reply(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Partnership", sender="brand@promo.com",
        offer_type="Sponsored Post", brand_name="PromoBrand",
        proposed_rate=1500.0, triage_reason="Score 3 forced",
    )
    assert result["is_escalate"] is True
    assert result["escalate_reason"] is not None


@patch("backend.services.reply.OpenAI")
def test_draft_reply_api_error_escalates(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = Exception("Network timeout")

    result = draft_reply(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Subj", sender="a@b.com",
        offer_type="Sponsored Post", brand_name="Nike",
        proposed_rate=1000.0, triage_reason="reason",
    )
    assert result["is_escalate"] is True
    assert "OpenAI API error" in result["escalate_reason"]


@patch("backend.services.reply.OpenAI")
def test_draft_reply_escalate_case_insensitive(mock_openai_cls):
    """ESCALATE: check should be case-insensitive."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value.choices[0].message.content = (
        "escalate: unusual request"
    )

    result = draft_reply(
        talent_key="Sylvia", talent_name="Sylvia", minimum_rate=1000,
        subject="Subj", sender="a@b.com",
        offer_type="Sponsored Post", brand_name="Nike",
        proposed_rate=1500.0, triage_reason="reason",
    )
    assert result["is_escalate"] is True
