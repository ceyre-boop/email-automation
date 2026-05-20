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
    """Load sop.md — cached after first read."""
    global _sop_md_cache
    if _sop_md_cache is None:
        if _SOP_MD_PATH.exists():
            _sop_md_cache = _SOP_MD_PATH.read_text(encoding="utf-8")
        else:
            _sop_md_cache = ""
    return _sop_md_cache

def clear_sop_cache() -> None:
    global _sop_md_cache
    _sop_md_cache = None

def _get_talent_sop_section(talent_name: str) -> str:
    """
    Extract just the relevant talent section from sop.md.
    Sends only global rules + the one talent block to GPT — keeps tokens minimal.
    """
    md = _load_sop_md()
    if not md:
        return ""

    # Extract global rules (everything before the first ## Talent: heading)
    talent_heading_match = re.search(r"\n## Talent: ", md)
    global_rules = md[:talent_heading_match.start()].strip() if talent_heading_match else md

    # Find this talent's section
    pattern = rf"\n## Talent: {re.escape(talent_name)}\n"
    start = re.search(pattern, md)
    if not start:
        # Try partial match (first name only)
        first_name = talent_name.split()[0]
        pattern = rf"\n## Talent: {re.escape(first_name)}"
        start = re.search(pattern, md)

    if not start:
        return global_rules  # talent section not found — GPT will escalate

    # Find the next talent section to know where this one ends
    next_talent = re.search(r"\n## Talent: ", md[start.end():])
    if next_talent:
        talent_section = md[start.start():start.end() + next_talent.start()]
    else:
        talent_section = md[start.start():]

    return f"{global_rules}\n\n{talent_section.strip()}"

logger = logging.getLogger(__name__)

_ESCALATE_PREFIX = "ESCALATE:"


# ── Deterministic SOP extractor ───────────────────────────────────────────────

def _get_talent_section_raw(talent_name: str) -> str | None:
    """
    Return just the '## Talent: X' block from sop.md — no global rules prepended.
    Tries full name first, then first name only.
    """
    md = _load_sop_md()
    if not md:
        return None
    for query in (talent_name, talent_name.split()[0]):
        match = re.search(rf"\n## Talent: {re.escape(query)}", md, re.IGNORECASE)
        if match:
            next_section = re.search(r"\n## ", md[match.end():])
            if next_section:
                return md[match.start() : match.end() + next_section.start()]
            return md[match.start():]
    return None


def _extract_approved_response(
    talent_section: str,
    heading_fragment: str,
    strip_cc: bool = True,
) -> str | None:
    """
    Find the ### scenario heading containing heading_fragment (case-insensitive)
    and return its **Approved Response:** text.
    strip_cc=True (default): removes CC routing lines from the body.
    strip_cc=False: preserves the CC line so callers can extract it.
    Returns None if the scenario or response block is not found.
    """
    heading_match = re.search(
        rf"### [^\n]*{re.escape(heading_fragment)}[^\n]*\n",
        talent_section,
        re.IGNORECASE,
    )
    if not heading_match:
        return None

    rest = talent_section[heading_match.end():]
    resp_match = re.search(r"\*\*Approved Response:\*\*\s*\n", rest)
    if not resp_match:
        return None

    response_text = rest[resp_match.end():]
    end_match = re.search(r"\n###\s", response_text)
    if end_match:
        response_text = response_text[: end_match.start()]

    if strip_cc:
        lines = [
            line for line in response_text.strip().splitlines()
            if not line.strip().upper().startswith("CC:")
        ]
        result = "\n".join(lines).strip()
    else:
        result = response_text.strip()

    return result or None


def _extract_adequate_threshold(talent_section: str) -> float | None:
    """Parse the adequate-offer dollar threshold from Scenario C's 'Use when' text.
    Looks for patterns like 'OVER $600' or 'over $900'."""
    match = re.search(r"OVER\s+\$(\d+)", talent_section, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _extract_cc_from_draft(draft_text: str) -> tuple[str | None, str]:
    """
    If draft_text starts with 'CC: ...' strip it and return (cc_string, clean_body).
    Otherwise returns (None, draft_text unchanged).
    Handles GPT sometimes including CC despite instructions.
    """
    lines = draft_text.strip().splitlines()
    if lines and lines[0].strip().upper().startswith("CC:"):
        cc = lines[0][3:].strip()
        body = "\n".join(lines[1:]).lstrip("\n").strip()
        return cc or None, body
    return None, draft_text


def _deterministic_initial_or_counter_reply(
    talent_key: str,
    talent_name: str,
    minimum_rate: int | float,
    proposed_rate: float,
    triage_reason: str,
    subject: str,
    body_text: str,
) -> str | None:
    """
    Return the verbatim SOP approved response for common scenarios — no GPT involved.
    Covers: rates inquiry (default), bundle request, below-minimum counter.
    Returns None to fall through to GPT for anything else.
    """
    talent_section = _get_talent_section_raw(talent_name)
    if not talent_section:
        return None

    triage_lower = (triage_reason or "").lower()
    body_lower = (body_text or "")[:500].lower()

    is_bundle = "bundle" in triage_lower or "bundle" in body_lower
    is_inquiry = (
        not proposed_rate
        or any(sig in triage_lower for sig in _INQUIRY_SIGNALS)
        or any(sig in body_lower for sig in _INQUIRY_EMAIL_SIGNALS)
    )

    # Bundle overrides default
    if is_bundle:
        response = _extract_approved_response(talent_section, "Bundle")
        if response:
            return response

    # Rates inquiry → default response
    if is_inquiry and not is_bundle:
        response = _extract_approved_response(talent_section, "⭐ DEFAULT")
        if response:
            return response
        # Fallback for talents without the ⭐ DEFAULT marker
        response = _extract_approved_response(talent_section, "Scenario A")
        if response:
            return response

    # Adequate offer → Scenario C (CC manager). Return WITH CC line so caller can wire it.
    adequate_threshold = _extract_adequate_threshold(talent_section)
    if adequate_threshold and proposed_rate and float(proposed_rate) > adequate_threshold and not is_bundle:
        response = _extract_approved_response(talent_section, "Adequate", strip_cc=False)
        if response:
            return response

    # Below-minimum counter offer
    if proposed_rate and 0 < proposed_rate < minimum_rate and not is_bundle:
        for marker in ("Counter", "Below", "below minimum", "lower"):
            response = _extract_approved_response(talent_section, marker)
            if response:
                return response

    return None

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
    """Build GPT messages — global rules + this talent's SOP section only."""
    sop_context = _get_talent_sop_section(talent_name)
    body_snippet = (body_text or "").strip()[:_MAX_EMAIL_BODY_CHARS]

    system_text = (
        f"{sop_context}\n\n"
        "---\n"
        f"You are drafting a reply for: {talent_name}\n"
        "Match the inbound email to the correct scenario above.\n"
        "CRITICAL: Copy the Approved Response text CHARACTER-FOR-CHARACTER. Do not paraphrase, shorten, expand, or change a single word. Output it in full — do not stop early.\n"
        "Do NOT include any meta-instruction lines such as 'Email Draft:', 'CC:', or similar headers. Start directly with the greeting.\n"
        "\n"
        "FORMATTING RULES — follow exactly:\n"
        "- Preserve all **bold** markers exactly as written (two asterisks each side).\n"
        "- Preserve all ***bold+italic*** markers exactly as written (three asterisks each side).\n"
        "- Preserve all <u>underline</u> tags exactly as written.\n"
        "- Preserve all [Anchor Text](URL) hyperlinks exactly as written — do not expand URLs or change anchor text.\n"
        "- Preserve → arrow bullets exactly as written.\n"
        "- Do NOT strip, rewrite, or simplify any formatting. Output the markup characters literally.\n"
        "- CC lines are routing instructions — do NOT include them in the email body.\n"
        "\n"
        "If no scenario matches: ESCALATE: No matching approved response — human review required."
    )

    if voice_profile.strip():
        system_text += f"\n\nTALENT VOICE PROFILE:\n{voice_profile}"

    if manager_context_text.strip():
        system_text += f"\n\nMANAGER OVERRIDE (highest priority):\n{manager_context_text}"

    user_text = (
        f"Talent: {talent_name}\n"
        f"Subject: {subject}\n"
        f"Sender: {sender}\n"
        f"Brand: {brand_name}\n"
        f"Proposed rate: ${int(proposed_rate) if proposed_rate else 0}\n"
        f"Triage notes: {triage_reason}\n\n"
        f"Email:\n---\n{body_snippet or '(not available)'}\n---\n\n"
        f"Return the matching Approved Response for {talent_name} exactly as written."
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
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        proposed_rate=proposed_rate,
        triage_reason=triage_reason,
        subject=subject,
        body_text=body_text,
    )
    if deterministic:
        cc, clean_text = _extract_cc_from_draft(deterministic)
        return {
            "draft_text": clean_text,
            "is_escalate": False,
            "escalate_reason": None,
            "cc_recipients": cc,
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
            temperature=cfg.get("temperature_reply", 0.0),
        )
        text = response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Reply API error for %s: %s", talent_key, exc)
        return _escalate_result(f"OpenAI API error: {exc}")

    # Strip any residual meta-instruction prefix GPT may have included despite instructions.
    # e.g. "Email Draft: CC cara@taboost.me\nHi," → "Hi,"
    _META_PREFIXES = ("email draft:", "draft:", "cc:")
    lines = text.splitlines()
    while lines and any(lines[0].strip().lower().startswith(p) for p in _META_PREFIXES):
        lines.pop(0)
    text = "\n".join(lines).strip()

    # Check if GPT decided to escalate
    if text.upper().startswith(_ESCALATE_PREFIX.upper()):
        reason = text[len(_ESCALATE_PREFIX):].strip()
        logger.info("GPT escalated for %s: %s", talent_key, reason)
        return _escalate_result(reason)

    cc, text = _extract_cc_from_draft(text)
    return {
        "draft_text": text,
        "is_escalate": False,
        "escalate_reason": None,
        "cc_recipients": cc,
    }


def _escalate_result(reason: str) -> dict:
    return {
        "draft_text": f"ESCALATE: {reason}",
        "is_escalate": True,
        "escalate_reason": reason,
        "cc_recipients": None,
    }
