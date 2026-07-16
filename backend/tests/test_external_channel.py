"""
Tests for External Channel Review detection.

Deterministic keyword scan — no OpenAI calls, no DB.
"""
from __future__ import annotations

import pytest

from backend.services.external_channel import detect_external_channel


# ── Positive cases ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "Please message me on WhatsApp to continue.",
        "Reply on WhatsApp when you get a chance.",
        "Send me a WhatsApp message about the campaign.",
        "Join our WhatsApp group for details.",
        "Here is my WhatsApp number: +1 555 000 1111",
        "Let's coordinate through Whats App.",
    ],
)
def test_detects_whatsapp(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) == "WhatsApp"


@pytest.mark.parametrize(
    "body",
    [
        "Contact me on Discord to discuss.",
        "Join our Discord server for the brief.",
        "Let's continue on our Discord chat.",
    ],
)
def test_detects_discord(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) == "Discord"


def test_detects_both():
    body = "You can reach us on WhatsApp or join our Discord server."
    assert detect_external_channel("Collab", "brand@nike.com", body) == "Both"


def test_matches_in_subject():
    assert detect_external_channel("Message me on WhatsApp", "b@x.com", "") == "WhatsApp"


def test_matches_in_sender_name():
    assert detect_external_channel("Collab", "Discord Team <b@x.com>", "hi") == "Discord"


# ── Negative cases (exclusion clause) ──────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "Call me at +1 555 000 1111 to discuss.",
        "Reach me on Telegram @brandrep.",
        "Book a slot on my Calendly: calendly.com/brandrep.",
        "Check out our TikTok and Instagram profiles.",
        "Visit our website at example.com for the deck.",
        "Looking forward to working together — reply here anytime.",
    ],
)
def test_does_not_flag_excluded_tokens(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) is None


def test_empty_inputs():
    assert detect_external_channel("", "", "") is None


def test_none_inputs():
    assert detect_external_channel(None, None, None) is None
