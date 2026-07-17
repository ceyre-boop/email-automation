"""
External Channel Review detection.

Purely informational flag (independent of the 1/2/3 triage score) that surfaces
inbound emails whose sender asks to continue the conversation through an outside
channel — WhatsApp or Discord — AND provides a real way to do it.

Deterministic, no GPT. Runs on every email including ones that skip triage.
Never affects routing, drafts, labels, or send — metadata only, never in a draft body.

Selectivity (why a bare keyword is not enough): brand blast emails routinely drop
"join our Discord" or the word "WhatsApp" into marketing footers with no actual
handoff. To avoid those false positives we require a concrete contact detail:
  - WhatsApp → the word "whatsapp"/"whats app" AND a real phone number present.
  - Discord  → an actual invite link (discord.gg/… or discord.com/invite/…).
A generic "join discord <brand>" footer with no invite link is NOT flagged.

The exclusion clause (bare phone/website/TikTok/Instagram/Telegram/Calendly) is
still satisfied for free: those never contain "whatsapp"/"discord" or a discord invite.
"""
from __future__ import annotations

import re

_WHATSAPP_TOKENS = ("whatsapp", "whats app")

# A real Discord invite link — not a bare "join our discord" mention.
_DISCORD_INVITE_RE = re.compile(r"discord\.(?:gg|com/invite)/\S+", re.IGNORECASE)

# Candidate phone run: a digit, then digits/separators, ending in a digit.
# Letters break the run, so prose numbers ("1 video", "August 31") don't match.
_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s().\-]{5,}\d")


def _has_phone_number(text: str) -> bool:
    """True if the text contains a plausible phone number (7-15 digits, E.164 range).

    Rejects short numbers (zips, prices) and over-long runs (order IDs, tracking #s).
    """
    for m in _PHONE_CANDIDATE_RE.finditer(text):
        digit_count = len(re.sub(r"\D", "", m.group()))
        if 7 <= digit_count <= 15:
            return True
    return False


def detect_external_channel(subject: str, sender: str, body: str) -> str | None:
    """Detect an actionable request to continue via WhatsApp or Discord.

    Returns "WhatsApp", "Discord", "Both", or None. Requires a concrete contact
    detail (phone number for WhatsApp, invite link for Discord) — a bare keyword
    mention alone is not flagged.
    """
    haystack = f"{sender or ''}\n{subject or ''}\n{body or ''}"
    low = haystack.lower()

    has_whatsapp = any(tok in low for tok in _WHATSAPP_TOKENS) and _has_phone_number(haystack)
    has_discord = bool(_DISCORD_INVITE_RE.search(haystack))

    if has_whatsapp and has_discord:
        return "Both"
    if has_whatsapp:
        return "WhatsApp"
    if has_discord:
        return "Discord"
    return None
