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
    _parse_proposed_rate,
    triage_email,
)


# ── _parse_proposed_rate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("$500", 500.0),
    ("500", 500.0),
    (500, 500.0),
    (500.0, 500.0),
    (None, None),
    ("$500-$700", 500.0),
    ("500 - 700", 500.0),
    ("N/A", None),
    ("n/a", None),
    ("", None),
    ("   ", None),
    ("Unknown", None),
    ("TBD", None),
    ("$1,000", 1000.0),
    ("$1,000.50", 1000.5),
    (-50, None),
    ("-50", None),
    (True, None),  # bool is technically an int subclass — must not become 1.0
    ("free", None),
])
def test_parse_proposed_rate(raw, expected):
    assert _parse_proposed_rate(raw) == expected


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
    msgs = _build_triage_messages("Wesley", 700, "Subj", "a@b.com", "b.com", "body")
    assert "700" in msgs[1]["content"]


def test_triage_messages_body_truncated_at_4000():
    long_body = "x" * 5000
    msgs = _build_triage_messages("Wesley", 700, "Subj", "a@b.com", "b.com", long_body)
    assert "x" * 4001 not in msgs[1]["content"]


def test_triage_system_prompt_is_non_empty():
    msgs = _build_triage_messages("Wesley", 700, "Subj", "a@b.com", "b.com", "body")
    assert len(msgs[0]["content"]) > 100


def test_triage_returns_two_messages():
    msgs = _build_triage_messages("Wesley", 700, "Subj", "a@b.com", "b.com", "body")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_triage_rate_note_injected_when_provided():
    """A non-empty rate_note should appear in the user message."""
    msgs = _build_triage_messages(
        "Katrina D", 150, "Subj", "a@b.com", "b.com", "body",
        rate_note="This talent's rate is per hour.",
    )
    assert "per hour" in msgs[1]["content"]


def test_triage_no_rate_note_by_default():
    """No SPECIAL RATE NOTE section when rate_note is omitted."""
    msgs = _build_triage_messages("Wesley", 700, "Subj", "a@b.com", "b.com", "body")
    assert "SPECIAL RATE NOTE" not in msgs[1]["content"]


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


def test_michaela_none_rate_not_overridden_by_floor():
    """Rate=None (no rate stated) must behave identically to 0.0 — unknown, don't override."""
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Michaela", 3, "Sponsored Post", None, policy)
    assert result == 3


def test_trin_commission_only_overrides_to_score1_with_none_rate():
    """None (no rate stated) must trigger the same downgrade as 0.0."""
    policy = {"special_talent_routing": {"Trin": {}}}
    result = _apply_special_routing("Trin", 3, "affiliate commission", None, policy)
    assert result == 1


def test_other_talent_not_affected():
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Colleen", 3, "Sponsored Post", 100.0, policy)
    assert result == 3


def test_katrina_score3_high_rate_unchanged():
    """Katrina: rate > $650 — score stays 3 (reply engine will escalate to Cara)."""
    policy = {"special_talent_routing": {"Katrina": {"condition_a": "If proposed_rate_usd > 650"}}}
    result = _apply_special_routing("Katrina", 3, "Sponsored Post", 700.0, policy)
    assert result == 3


def test_katrina_score3_high_rate_well_above_threshold():
    """Katrina: rate well above $650 still stays 3."""
    policy = {"special_talent_routing": {"Katrina": {"condition_a": "If proposed_rate_usd > 650"}}}
    result = _apply_special_routing("Katrina", 3, "Sponsored Post", 5000.0, policy)
    assert result == 3


def test_katrina_score3_low_rate_unchanged():
    """Katrina: rate ≤ $650 — score stays 3 (reply engine will escalate to Chenni)."""
    policy = {"special_talent_routing": {"Katrina": {"condition_a": "If proposed_rate_usd > 650"}}}
    result = _apply_special_routing("Katrina", 3, "Sponsored Post", 400.0, policy)
    assert result == 3


def test_katrina_score3_unknown_rate_unchanged():
    """Katrina: unknown rate (0) — score stays 3."""
    policy = {"special_talent_routing": {"Katrina": {"condition_a": "If proposed_rate_usd > 650"}}}
    result = _apply_special_routing("Katrina", 3, "Sponsored Post", 0.0, policy)
    assert result == 3


def test_katrina_score1_not_affected():
    """Katrina: score-1 offers are not upgraded."""
    policy = {"special_talent_routing": {"Katrina": {"condition_a": "If proposed_rate_usd > 650"}}}
    result = _apply_special_routing("Katrina", 1, "Sponsored Post", 800.0, policy)
    assert result == 1


def test_katrina_threshold_fallback_when_policy_missing():
    """Katrina: gracefully uses $650 fallback when policy entry is absent."""
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("Katrina", 3, "Sponsored Post", 700.0, policy)
    assert result == 3


def test_katrinad_score_unchanged():
    """KatrinaD: no hard score override — enriched prompt handles hourly logic."""
    policy = {"special_talent_routing": {}}
    result = _apply_special_routing("KatrinaD", 3, "Sponsored Post", 500.0, policy)
    assert result == 3


# ── Fallback ───────────────────────────────────────────────────────────────────

def test_fallback_returns_score2():
    r = _fallback("Sylvia", "test error")
    assert r["score"] == 2
    assert "test error" in r["reason"]
    assert r["offer_type"] == "Unknown"
    # None, not 0.0 — a fallback never attempted extraction, so it must not
    # look like a confirmed $0 rate to any downstream deal-value aggregation.
    assert r["proposed_rate_usd"] is None


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

    # A timeout is transient, so it is retried before the fallback. sleep is
    # patched so the suite does not actually wait out the 5s + 10s backoff.
    with patch("backend.services.openai_client.time.sleep"):
        result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")

    assert result["score"] == 2
    assert mock_client.chat.completions.create.call_count == 3


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


@patch("backend.services.triage.OpenAI")
def test_triage_katrina_score3_preserved(mock_openai_cls):
    """Katrina Score-3 offers are NOT overridden — reply engine handles escalation."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(
        3, proposed_rate=800.0
    )
    result = triage_email("Katrina", "Katrina", 300, "Collab", "brand@nike.com", "nike.com", "body")
    assert result["score"] == 3


@patch("backend.services.triage.OpenAI")
def test_triage_katrinad_rate_note_in_prompt(mock_openai_cls):
    """KatrinaD triage call includes a SPECIAL RATE NOTE about per-hour pricing."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(3, proposed_rate=200.0)

    triage_email("KatrinaD", "Katrina D", 150, "Livestream collab", "brand@sephora.com", "sephora.com", "body")

    call_args = mock_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args.args[0] if call_args.args else call_args.kwargs["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")
    assert "per hour" in user_msg.lower()
    assert "SPECIAL RATE NOTE" in user_msg


@patch("backend.services.triage.OpenAI")
def test_triage_standard_talent_no_rate_note(mock_openai_cls):
    """Standard per-video talent prompt does not contain a SPECIAL RATE NOTE."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(3)

    triage_email("Sylvia", "Sylvia", 1000, "Partnership", "brand@nike.com", "nike.com", "body")

    call_args = mock_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args.args[0] if call_args.args else call_args.kwargs["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")
    assert "SPECIAL RATE NOTE" not in user_msg


@patch("backend.services.triage.OpenAI")
@patch("backend.services.triage.get_settings")
def test_triage_taboost_domain_blocked(mock_settings, mock_openai_cls):
    """Emails from taboost.me (internal staff) must be blocked without a GPT call."""
    s = MagicMock()
    s.app_config = {
        "ai_enabled": True,
        "triage": {"never_reply": {"domains": ["taboost.me"]}},
        "openai": {},
    }
    s.confidence_policy = {}
    s.openai_api_key = "test-key"
    mock_settings.return_value = s

    result = triage_email(
        "Wesley",
        "Wesley Barker",
        600,
        "Re: collab with brand",
        "cara@taboost.me",
        "taboost.me",
        "Hey, checking in on this deal",
    )
    assert result["score"] == 1
    assert result["offer_type"] == "Blocked"
    mock_openai_cls.assert_not_called()


@patch("backend.services.triage.OpenAI")
@patch("backend.services.triage.get_settings")
def test_triage_never_reply_domain_blocklist_short_circuits(mock_settings, mock_openai_cls):
    s = MagicMock()
    s.app_config = {
        "ai_enabled": True,
        "triage": {"never_reply": {"domains": ["blocked.example"]}},
        "openai": {},
    }
    s.confidence_policy = {}
    s.openai_api_key = "test-key"
    mock_settings.return_value = s

    result = triage_email(
        "Sylvia",
        "Sylvia",
        1000,
        "Quick question",
        "sender@blocked.example",
        "blocked.example",
        "body",
    )
    assert result["score"] == 1
    assert result["offer_type"] == "Blocked"
    mock_openai_cls.assert_not_called()


@patch("backend.services.triage.OpenAI")
def test_triage_email_missing_rate_field_falls_back(mock_openai_cls):
    """proposed_rate_usd is in _REQUIRED — if a future prompt regression drops
    the field from GPT's output, this must fail loudly (Score 2) instead of
    silently reverting to the old always-0.0 behavior."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    bad_response = MagicMock()
    bad_response.choices[0].message.content = json.dumps({
        "score": 3, "reason": "looks real", "offer_type": "Sponsored Post", "brand_name": "Nike",
    })
    mock_client.chat.completions.create.return_value = bad_response

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["score"] == 2
    assert "proposed_rate_usd" in result["reason"]


@patch("backend.services.triage.OpenAI")
def test_triage_email_null_rate_means_none_not_zero(mock_openai_cls):
    """GPT explicitly returning proposed_rate_usd: null must produce None, not 0.0."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    response = MagicMock()
    response.choices[0].message.content = json.dumps({
        "score": 3, "reason": "no rate mentioned", "offer_type": "Unknown",
        "brand_name": "Acme", "proposed_rate_usd": None,
    })
    mock_client.chat.completions.create.return_value = response

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["proposed_rate_usd"] is None


@patch("backend.services.triage.OpenAI")
def test_triage_email_string_rate_parsed(mock_openai_cls):
    """GPT returning the rate as a currency string still parses to a clean float."""
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    response = MagicMock()
    response.choices[0].message.content = json.dumps({
        "score": 3, "reason": "offered $900", "offer_type": "UGC",
        "brand_name": "Olenis", "proposed_rate_usd": "$900",
    })
    mock_client.chat.completions.create.return_value = response

    result = triage_email("Sylvia", "Sylvia", 1000, "Subj", "a@b.com", "b.com", "body")
    assert result["proposed_rate_usd"] == 900.0
