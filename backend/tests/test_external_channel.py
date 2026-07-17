"""
Tests for External Channel Review detection.

Deterministic scan — no OpenAI calls, no DB. Requires a real contact detail:
phone number for WhatsApp, invite link for Discord.
"""
from __future__ import annotations

import pytest

from backend.services.external_channel import detect_external_channel


# ── WhatsApp: needs an actual phone number ─────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "You can add me on WhatsApp to learn more: +19168424188",
        "Please message me on WhatsApp at +1 555 010 2020 to continue.",
        "Reply on WhatsApp: 447911123456",
    ],
)
def test_whatsapp_with_phone_flags(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) == "WhatsApp"


@pytest.mark.parametrize(
    "body",
    [
        "Reply on WhatsApp when you get a chance.",           # no number
        "Join our WhatsApp group for details.",               # no number
        "We're on WhatsApp — reach out anytime!",             # no number
    ],
)
def test_whatsapp_without_phone_not_flagged(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) is None


# ── Discord: needs a real invite link ──────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "Join our Discord: https://discord.gg/abc123",
        "Continue on Discord — discord.gg/PrOvEnce",
        "Our server: https://discord.com/invite/xyz789",
    ],
)
def test_discord_with_invite_flags(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) == "Discord"


@pytest.mark.parametrize(
    "body",
    [
        "Learn more: join discord Provence Beauty | 17021 Kingsview ave | Carson, CA 90746 US",
        "Contact me on Discord to discuss.",                  # no invite link
        "Join our Discord server for the brief.",             # no invite link
    ],
)
def test_discord_without_invite_not_flagged(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) is None


def test_both_when_phone_and_invite():
    body = "WhatsApp me at +19168424188 or join discord.gg/abc123"
    assert detect_external_channel("Collab", "brand@nike.com", body) == "Both"


# ── Regression: the exact false positives from production ───────────────────────

def test_provence_beauty_footer_not_flagged():
    body = (
        "Hi Creator, I'm Kaz from Provence Beauty ... send us: Your flat-fee rate for "
        "1 video ... Legal name + shipping address + phone number ... Learn more: "
        "join discord Provence Beauty | 17021 Kingsview ave | Carson, CA 90746 US "
        "Unsubscribe | Update Profile"
    )
    assert detect_external_channel("Collaboration Invitation from Provence Beauty",
                                   "kaz@provence.test", body) is None


def test_numeric_noise_body_not_flagged():
    # body_text was literally "96"; subject mentioned neither channel with contact detail
    assert detect_external_channel("Terez & Honor | Viral Eyelash Serum", "x@y.test", "96") is None


# ── Exclusion clause still holds ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "Reach me on Telegram @brandrep.",
        "Book a slot on my Calendly: calendly.com/brandrep.",
        "Check out our TikTok and Instagram profiles.",
        "Call me at +1 555 000 1111 to discuss.",             # phone but no channel keyword
    ],
)
def test_excluded_tokens_not_flagged(body):
    assert detect_external_channel("Collab", "brand@nike.com", body) is None


def test_empty_and_none_inputs():
    assert detect_external_channel("", "", "") is None
    assert detect_external_channel(None, None, None) is None
