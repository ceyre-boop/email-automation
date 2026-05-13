"""
Reply drafting engine — generates a GPT-4o reply using the talent's SOP rules.

Logic follows prompts/reply.md and config/confidence_policy.json.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re

from openai import OpenAI

from backend.core.config import get_settings

# ── SOP markdown loader ───────────────────────────────────────────────────────
_SOP_MD_PATH = pathlib.Path(__file__).resolve().parents[2] / "sheets" / "sop.md"
_sop_md_cache: str | None = None

def _load_sop_md() -> str:
    """Load sop.md — the AI's source of truth. Cached after first read."""
    global _sop_md_cache
    if _sop_md_cache is None:
        if _SOP_MD_PATH.exists():
            _sop_md_cache = _SOP_MD_PATH.read_text(encoding="utf-8")
        else:
            _sop_md_cache = "# SOP\nNo SOP document found."
    return _sop_md_cache

def clear_sop_cache() -> None:
    """Force reload of sop.md on next draft call. Call after updating the SOP."""
    global _sop_md_cache
    _sop_md_cache = None

logger = logging.getLogger(__name__)

_ESCALATE_PREFIX = "ESCALATE:"

# Keywords in the triage reason that indicate the brand is *asking* for rates,
# not making a concrete offer. When these appear, GPT must use the initial-rates
# template rather than the counter-offer template — even if it hallucinated a rate.
_INQUIRY_SIGNALS = (
    "asking for rates", "requesting rates", "rate inquiry", "asking for a quote",
    "no rate", "no offer", "no rate mentioned", "rate not mentioned",
    "no specific offer", "no dollar", "no amount", "no proposed rate",
    "asking about", "not mentioned", "what are your", "rate request",
    "inquiring about rates", "seeking collaboration", "interested in working",
    "would love to work", "open to collab", "open to collaboration",
    "exploring partnership",
)
_INQUIRY_EMAIL_SIGNALS = (
    "what are your rates", "rate card", "media kit", "pricing", "share rates",
    "send rates", "quote", "budget range", "can you send your rates",
)

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
    """Return a formatted string of the talent's SOP rules for the GPT prompt."""
    sop = get_settings().sop_data
    sop_key = next((k for k in sop if k.lower() == talent_key.lower()), None)
    talent_data = sop.get(sop_key, {}) if sop_key else {}
    rules = talent_data.get("rules", [])
    if not rules:
        return "No SOP rules found for this talent."
    lines = []
    for rule in rules:
        # New format: scenario-based with explicit fields
        if "scenario" in rule:
            scenario = rule.get("scenario", "")
            label = rule.get("label", "")
            trigger = rule.get("trigger", "").strip()
            response = rule.get("response", "").strip()
            cc = rule.get("cc")
            is_default = rule.get("is_default", False)
            if not response:
                continue
            response = _redact_pii(response)
            cc_note = f"\nCC: {cc}" if cc else ""
            default_note = " [DEFAULT — use when no other scenario matches]" if is_default else ""
            lines.append(f"SCENARIO {scenario}{default_note}: {label}\nTRIGGER: {trigger}\nRESPONSE: {response}{cc_note}")
        else:
            # Legacy format: plain trigger/response pairs
            trigger = rule.get("trigger", "").replace("\r\n", "\n").strip()
            response = rule.get("response", "").replace("\r\n", "\n").strip()
            _ACTION_KEYWORDS = ("move to", "cc ", "delete", "ask for consult", "tagged email", "marked a initial")
            if any(response.lower().startswith(kw) for kw in _ACTION_KEYWORDS):
                continue
            if any(kw in response.lower() for kw in ("move to ", "cc cara", "cc chenni", "cc nicole", "move it to")):
                continue
            response = _redact_pii(response)
            lines.append(f"TRIGGER: {trigger}\nRESPONSE: {response}")
    return "\n\n".join(lines) if lines else "No email-reply rules found — ESCALATE all."


def _get_cc_for_reply(talent_key: str, proposed_rate: float) -> str | None:
    """Return a CC email address if the matched SOP scenario requires one."""
    sop = get_settings().sop_data
    sop_key = next((k for k in sop if k.lower() == talent_key.lower()), None)
    if not sop_key:
        return None
    rules = sop.get(sop_key, {}).get("rules", [])
    for rule in rules:
        if "scenario" not in rule or not rule.get("cc"):
            continue
        # Scenario C fires when proposed_rate > 1500 for Sylvia
        trigger = rule.get("trigger", "").lower()
        if "over" in trigger and proposed_rate > 0:
            try:
                threshold = float(''.join(c for c in trigger.split("over")[1][:10] if c.isdigit()))
                if proposed_rate > threshold:
                    return rule["cc"]
            except (ValueError, IndexError):
                pass
    return None


def _deterministic_initial_or_counter_reply(*args, **kwargs) -> None:
    # Disabled — sop.md is the single source of truth now.
    # GPT reads the full SOP document and returns the correct response verbatim.
    return None


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
    """Build GPT messages using sop.md as the system context — the single source of truth."""
    sop_md = _load_sop_md()
    body_snippet = (body_text or "").strip()[:_MAX_EMAIL_BODY_CHARS]

    system_text = (
        f"{sop_md}\n\n"
        "---\n\n"
        "You are the email drafting agent for TABOOST talent management.\n"
        "Read the full SOP document above before responding.\n"
        f"You are drafting a reply for: **{talent_name}**\n\n"
        "Rules:\n"
        "- Find the correct talent section above. Use ONLY that talent's scenarios.\n"
        "- Match the inbound email to the best scenario.\n"
        "- Return the Approved Response VERBATIM — no rewrites, no changes, no additions.\n"
        "- If no scenario matches, return: ESCALATE: No matching approved response — human review required.\n"
        "- If the talent's SOP status is PENDING, return: ESCALATE: SOP pending for this talent — human admin required.\n"
    )

    if manager_context_text.strip():
        system_text += f"\n\nMANAGER OVERRIDE INSTRUCTIONS (highest priority):\n{manager_context_text}"

    user_text = (
        f"Talent: {talent_name}\n"
        f"Email subject: {subject}\n"
        f"Email sender: {sender}\n"
        f"Offer type: {offer_type}\n"
        f"Brand name: {brand_name}\n"
        f"Proposed rate (USD): {int(proposed_rate) if proposed_rate else 0}\n"
        f"Triage notes: {triage_reason}\n\n"
        f"Original email body:\n---\n{body_snippet or '(not available)'}\n---\n\n"
        f"Find the matching scenario for {talent_name} and return the Approved Response exactly as written."
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

    # ── SOP status gate — never draft for a talent without an approved SOP ──────
    # This is a hard block. Mixing up talent responses is a business-critical error.
    sop = settings.sop_data
    sop_key = next((k for k in sop if k.lower() == talent_key.lower()), None)
    sop_status = sop.get(sop_key, {}).get("sop_status", "pending") if sop_key else "pending"
    if sop_status != "approved":
        logger.warning("SOP not approved for %s — routing to Human Admin Required", talent_key)
        return {
            "draft_text": "ESCALATE: SOP not yet approved for this talent — human admin required.",
            "is_escalate": True,
            "escalate_reason": f"SOP pending for {talent_key}. No approved responses loaded yet.",
        }

    voice_profile, manager_context_text = _load_talent_context(db, talent_key)

    deterministic = _deterministic_initial_or_counter_reply(
        talent_key=talent_key,
        minimum_rate=minimum_rate,
        proposed_rate=proposed_rate,
        triage_reason=triage_reason,
        subject=subject,
        body_text=body_text,
    )
    if deterministic:
        return {
            "draft_text": deterministic,
            "is_escalate": False,
            "escalate_reason": None,
        }

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
