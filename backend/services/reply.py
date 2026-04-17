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
    talent_data = sop.get(talent_key, {})
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
) -> list[dict]:
    """Fill reply.md template variables and return chat messages."""
    raw = get_settings().reply_prompt
    system_text, user_template = _parse_prompt_sections(raw)

    sop_rules = _build_sop_rules_text(talent_key)

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
    cfg = settings.app_config.get("openai", {})
    client = OpenAI(api_key=settings.openai_api_key)

    messages = _build_reply_messages(
        talent_key=talent_key,
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        offer_type=offer_type,
        brand_name=brand_name,
        proposed_rate=proposed_rate,
        triage_reason=triage_reason,
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
