"""
Reply drafting engine — generates a GPT-4o reply using the talent's SOP rules.

Logic follows prompts/reply.md and config/confidence_policy.json.
"""
from __future__ import annotations

import json
import logging
import re

from openai import OpenAI

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

_ESCALATE_PREFIX = "ESCALATE:"

# Maximum characters of the original email body included in the reply prompt.
# Keeps token usage reasonable while giving GPT enough context for a targeted reply.
_MAX_EMAIL_BODY_CHARS = 3000

# ── Prompt section cache ──────────────────────────────────────────────────────
# Parsing the reply.md file (regex over ~3 KB) on every draft_reply call is wasteful.
# Cache the parsed (system_text, user_template) pair — it doesn't change at runtime.
_reply_sections: tuple[str, str] | None = None


def _get_reply_sections() -> tuple[str, str]:
    global _reply_sections
    if _reply_sections is None:
        _reply_sections = _parse_prompt_sections(get_settings().reply_prompt)
    return _reply_sections


def _load_talent_context(db, talent_key: str) -> tuple[str, str]:
    """
    Return (voice_profile, manager_instructions) for a talent.

    Loads the most recent active ManagerContext row for this talent.
    Also loads global (no talent_key) context entries as extra instructions.
    """
    if db is None:
        return "", ""
    try:
        from backend.models.db import ManagerContext  # local import avoids circular

        # Per-talent voice profile + instructions
        talent_row = (
            db.query(ManagerContext)
            .filter(
                ManagerContext.talent_key == talent_key.lower(),
                ManagerContext.active == True,  # noqa: E712
            )
            .order_by(ManagerContext.added_at.desc())
            .first()
        )
        voice_profile = (talent_row.voice_profile or "").strip() if talent_row else ""
        per_talent_instructions = (talent_row.text or "").strip() if talent_row else ""

        # Global instructions (rows with no talent_key)
        global_rows = (
            db.query(ManagerContext)
            .filter(
                ManagerContext.talent_key == None,  # noqa: E711
                ManagerContext.active == True,  # noqa: E712
            )
            .order_by(ManagerContext.added_at.asc())
            .all()
        )
        global_text = "\n".join(f"- {r.text}" for r in global_rows) if global_rows else ""

        combined_instructions = "\n".join(filter(None, [per_talent_instructions, global_text]))
        return voice_profile, combined_instructions
    except Exception as exc:
        logger.warning("Could not load talent context for %s: %s", talent_key, exc)
        return "", ""

# PII patterns that must never appear in AI-generated replies
_PII_PATTERNS = [
    # Street addresses
    re.compile(r"\d{3,5}\s+\w[\w\s,\.]+(?:Ave|Avenue|St|Street|Rd|Road|Blvd|Blvd\.|Dr|Drive|Ct|Court|Ln|Lane|Way|Pl|Place)[\w\s,\.]*", re.IGNORECASE),
    # US phone numbers
    re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}"),
    # SSN / EIN patterns
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
]


def _redact_pii(text: str) -> str:
    """Remove known PII patterns from SOP templates before sending to GPT."""
    for pattern in _PII_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _build_sop_rules_text(talent_key: str) -> str:
    """Return a formatted string of the talent's SOP rules from sop_data.json."""
    sop = get_settings().sop_data
    # SOP data is keyed by the config key (title-case, e.g. "Katrina") but talent_key
    # from the DB is often stored lowercase — do a case-insensitive lookup.
    sop_key = next((k for k in sop if k.lower() == talent_key.lower()), None)
    talent_data = sop.get(sop_key, {}) if sop_key else {}
    rules = talent_data.get("rules", [])
    if not rules:
        return "No SOP rules found for this talent."
    lines = []
    for rule in rules:
        trigger = rule.get("trigger", "").replace("\n", " ").strip()
        response = rule.get("response", "").replace("\n", " ").strip()
        response = _redact_pii(response)
        lines.append(f"TRIGGER: {trigger}\nRESPONSE: {response}")
    return "\n\n".join(lines)


def _parse_prompt_sections(raw: str) -> tuple[str, str]:
    """
    Parse a prompt markdown file using '## SYSTEM PROMPT' and
    '## USER PROMPT TEMPLATE' headings as section delimiters.
    Everything from SYSTEM PROMPT up to (but not including) USER PROMPT TEMPLATE
    is treated as the system message.
    Returns (system_text, user_template).
    """
    user_match = re.search(r"\n## USER PROMPT TEMPLATE\s*\n", raw)
    system_match = re.search(r"\n## SYSTEM PROMPT\s*\n", raw)
    if not system_match or not user_match:
        return raw.strip(), ""
    system_text = raw[system_match.end(): user_match.start()].strip()
    user_template = raw[user_match.end():].strip()
    return system_text, user_template


def _build_reply_messages(
    talent_key: str,
    talent_name: str,
    minimum_rate: int | float,
    subject: str,
    sender: str,
    offer_type: str,
    brand_name: str,
    proposed_rate: float,
    triage_reason: str,
    voice_profile: str = "",
    manager_context_text: str = "",
    body_text: str = "",
) -> list[dict]:
    """Fill reply.md template variables and return chat messages."""
    system_text, user_template = _get_reply_sections()

    # Replace {{TALENT_NAME}} in the system prompt — new reply.md writes the persona
    # directly into the system message so GPT adopts the talent's voice from the start.
    system_text = system_text.replace("{{TALENT_NAME}}", talent_name)

    if voice_profile.strip():
        system_text += (
            "\n\n## TALENT VOICE & TONE\n"
            "Write all replies in this voice/style:\n"
            + voice_profile
        )

    if manager_context_text.strip():
        system_text += (
            "\n\n## MANAGER INSTRUCTIONS\n"
            "Apply these with highest priority — they override SOP defaults:\n"
            + manager_context_text
        )

    sop_rules = _build_sop_rules_text(talent_key)

    # Truncate the email body to avoid excessive token usage while still giving
    # GPT enough context to write a well-targeted reply.
    body_snippet = (body_text or "").strip()[:_MAX_EMAIL_BODY_CHARS]

    user_text = (
        user_template
        .replace("{{TALENT_NAME}}", talent_name)
        .replace("{{MINIMUM_RATE}}", str(int(minimum_rate)))
        .replace("{{EMAIL_SUBJECT}}", subject)
        .replace("{{SENDER_EMAIL}}", sender)
        .replace("{{OFFER_TYPE}}", offer_type)
        .replace("{{BRAND_NAME}}", brand_name)
        .replace("{{PROPOSED_RATE}}", str(int(proposed_rate)))
        .replace("{{TRIAGE_NOTES}}", triage_reason)
        .replace("{{EMAIL_BODY}}", body_snippet or "(not available)")
        .replace("{{SOP_RULES}}", sop_rules)
    )

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def draft_reply(
    talent_key: str,
    talent_name: str,
    minimum_rate: int | float,
    subject: str,
    sender: str,
    offer_type: str,
    brand_name: str,
    proposed_rate: float,
    triage_reason: str,
    db=None,
    body_text: str = "",
) -> dict:
    """
    Generate a reply draft using GPT-4o.

    Returns:
    {
        "draft_text": str,
        "is_escalate": bool,
        "escalate_reason": str | None,
    }
    Always returns something — falls back to ESCALATE on error.
    """
    settings = get_settings()
    if not settings.app_config.get("ai_enabled", True):
        raise RuntimeError("AI is disabled (ai_enabled=false in settings.json) — reply drafting skipped")
    cfg = settings.app_config.get("openai", {})
    client = OpenAI(api_key=settings.openai_api_key)

    voice_profile, manager_context_text = _load_talent_context(db, talent_key)

    messages = _build_reply_messages(
        talent_key=talent_key,
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        offer_type=offer_type,
        brand_name=brand_name,
        proposed_rate=proposed_rate,
        voice_profile=voice_profile,
        triage_reason=triage_reason,
        manager_context_text=manager_context_text,
        body_text=body_text,
    )

    try:
        response = client.chat.completions.create(
            model=cfg.get("reply_model", "gpt-4o"),
            messages=messages,
            max_tokens=cfg.get("max_tokens_reply", 800),
            temperature=cfg.get("temperature_reply", 0.4),
        )
        text = response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Reply API error for %s: %s", talent_key, exc)
        return _escalate_result(f"OpenAI API error: {exc}")

    # Check if GPT decided to escalate
    if text.upper().startswith(_ESCALATE_PREFIX.upper()):
        reason = text[len(_ESCALATE_PREFIX):].strip()
        logger.info("GPT escalated for %s: %s", talent_key, reason)
        return _escalate_result(reason)

    return {
        "draft_text": text,
        "is_escalate": False,
        "escalate_reason": None,
    }


def _escalate_result(reason: str) -> dict:
    return {
        "draft_text": f"ESCALATE: {reason}",
        "is_escalate": True,
        "escalate_reason": reason,
    }
