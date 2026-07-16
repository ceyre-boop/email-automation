"""
External Channel Review detection.

Purely informational flag (independent of the 1/2/3 triage score) that surfaces
inbound emails whose sender asks to continue the conversation through an outside
channel — WhatsApp or Discord.

This is a deterministic keyword scan with NO GPT call, so it runs on every email,
including ones that skip triage entirely (personal-email forwards, never-reply
blocklist, automated senders). It never affects routing, drafts, labels, or send
behaviour — the result is metadata only and must never appear in a draft body.

The spec's exclusion clause (do not flag on a bare phone number, website, TikTok,
Instagram, Telegram, Calendly, or generic social link) is satisfied for free: none
of those tokens contain the substrings "whatsapp" or "discord", so a plain
word-presence scan never trips on them.
"""
from __future__ import annotations

# Substring matches (case-insensitive). "whatsapp" also covers "whatsapp group",
# "whatsapp number", etc.; "whats app" covers the spaced variant. "discord"
# covers "discord server", "discord chat", etc.
_WHATSAPP_TOKENS = ("whatsapp", "whats app")
_DISCORD_TOKENS = ("discord",)


def detect_external_channel(subject: str, sender: str, body: str) -> str | None:
    """Detect a request to communicate via WhatsApp or Discord.

    Scans the sender, subject, and body (the signature is part of the body text).

    Returns "WhatsApp", "Discord", "Both", or None (no external-channel request).
    """
    haystack = f"{sender or ''}\n{subject or ''}\n{body or ''}".lower()

    has_whatsapp = any(tok in haystack for tok in _WHATSAPP_TOKENS)
    has_discord = any(tok in haystack for tok in _DISCORD_TOKENS)

    if has_whatsapp and has_discord:
        return "Both"
    if has_whatsapp:
        return "WhatsApp"
    if has_discord:
        return "Discord"
    return None
