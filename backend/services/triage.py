"""
Triage engine — scores each inbound email 1/2/3 using GPT-4o-mini.

Implements all routing rules from:
  config/confidence_policy.json  (global_score_routing, special_talent_routing)
  config/settings.json           (per-talent minimums, rate_unit)
"""
from __future__ import annotations

import json
import logging
import re

from openai import OpenAI

from backend.core.config import get_settings

logger = logging.getLogger(__name__)


# ── Prompt parsing ────────────────────────────────────────────────────────────

def _parse_prompt_sections(raw: str) -> tuple[str, str]:
    """
    Parse a prompt markdown file using '## SYSTEM PROMPT' and
    '## USER PROMPT TEMPLATE' headings as section delimiters.
    Everything from SYSTEM PROMPT up to (but not including) USER PROMPT TEMPLATE
    is treated as the system message (supports intermediate sections like SCORING RULES).
    Returns (system_text, user_template).
    """
    # Find the USER PROMPT TEMPLATE heading and split there
    user_match = re.search(r"\n## USER PROMPT TEMPLATE\s*\n", raw)
    system_match = re.search(r"\n## SYSTEM PROMPT\s*\n", raw)
    if not system_match or not user_match:
        # Fallback: return everything as system, empty user
        return raw.strip(), ""
    system_text = raw[system_match.end(): user_match.start()].strip()
    user_template = raw[user_match.end():].strip()
    return system_text, user_template


def _build_triage_messages(
    talent_name: str,
    minimum_rate: int | float,
    subject: str,
    sender: str,
    sender_domain: str,
    body: str,
    rate_note: str = "",
) -> list[dict]:
    """Parse the triage.md prompt and fill in template variables.

    rate_note: Optional extra instruction appended to the user message for
    talents whose rate unit differs from the default (e.g. per-hour vs per-video).
    """
    raw = get_settings().triage_prompt
    system_text, user_template = _parse_prompt_sections(raw)

    user_text = (
        user_template
        .replace("{{TALENT_NAME}}", talent_name)
        .replace("{{MINIMUM_RATE}}", str(int(minimum_rate)))
        .replace("{{EMAIL_SUBJECT}}", subject)
        .replace("{{SENDER_EMAIL}}", sender)
        .replace("{{SENDER_DOMAIN}}", sender_domain)
        .replace("{{EMAIL_BODY}}", body[:4000])  # Guard against massive emails
    )

    if rate_note:
        user_text += f"\n\nSPECIAL RATE NOTE: {rate_note}"

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


# ── Special per-talent overrides ──────────────────────────────────────────────

def _apply_special_routing(
    talent_key: str,
    score: int,
    offer_type: str,
    proposed_rate: float,
    policy: dict,
) -> int:
    """Apply per-talent overrides from confidence_policy.json special_talent_routing."""

    if talent_key == "Trin":
        if "affiliate" in offer_type.lower() and proposed_rate == 0:
            logger.info("Trin commission-only override → Score 1")
            return 1

    if talent_key == "Katrina":
        # Dual-manager escalation: all Score-3 offers are escalated at the reply stage.
        # Rate > $650 → CC Cara; rate ≤ $650 (or unknown) → CC Chenni.
        # No score change here — score-3 proceeds to the reply engine which applies
        # the SOP escalation. Logged so the routing intent is visible in server logs.
        if score == 3:
            if proposed_rate > 650:
                logger.info(
                    "Katrina dual-manager rule: rate $%s > $650 threshold → "
                    "reply engine will escalate to Cara",
                    proposed_rate,
                )
            else:
                logger.info(
                    "Katrina dual-manager rule: rate $%s ≤ $650 (or unknown) → "
                    "reply engine will escalate to Chenni",
                    proposed_rate,
                )

    if talent_key == "KatrinaD":
        # Hourly-rate interpretation: all KatrinaD offers are priced per hour.
        # The minimum_rate in settings is already set to the hourly floor ($150/hr).
        # GPT receives a SPECIAL RATE NOTE (injected by triage_email) instructing it
        # to interpret rates as per-hour. If GPT cannot determine the hourly rate
        # from the email (flat fee with no hours mentioned), it should return Score 2.
        # No hard score override here — we trust the enriched prompt; log for visibility.
        logger.debug(
            "KatrinaD hourly-rate triage: proposed_rate=%s (per hour)", proposed_rate
        )

    if talent_key == "Michaela":
        if proposed_rate > 0 and proposed_rate < 1000:
            logger.info("Michaela floor override ($%s < $1000) → Score 1", proposed_rate)
            return 1

    return score


# ── Main triage call ──────────────────────────────────────────────────────────

def triage_email(
    talent_key: str,
    talent_name: str,
    minimum_rate: int | float,
    subject: str,
    sender: str,
    sender_domain: str,
    body: str,
) -> dict:
    """
    Score an email using GPT-4o-mini.

    Returns a dict:
    {
        "score": 1|2|3,
        "reason": str,
        "offer_type": str,
        "proposed_rate_usd": float,
        "brand_name": str,
    }
    Falls back to score=2 on any error (never silently drops emails).
    """
    settings = get_settings()
    cfg = settings.app_config.get("openai", {})
    policy = settings.confidence_policy

    # Build a per-hour rate note for talents whose rate unit is not "per video".
    # This is critical for KatrinaD (per hour) so GPT interprets offered amounts correctly.
    talent_cfg = next(
        (t for t in settings.app_config.get("talents", []) if t.get("key") == talent_key),
        {},
    )
    rate_unit = talent_cfg.get("rate_unit", "per video")
    rate_note = (
        f"This talent's rate is {rate_unit}. The minimum rate listed above is "
        f"{rate_unit}. Interpret all offered amounts accordingly. "
        "If an offer quotes a flat fee without specifying hours, respond with Score 2 "
        "because the effective hourly rate cannot be determined."
        if rate_unit != "per video"
        else ""
    )

    messages = _build_triage_messages(
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        sender_domain=sender_domain,
        body=body,
        rate_note=rate_note,
    )

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.chat.completions.create(
            model=cfg.get("triage_model", "gpt-4o-mini"),
            messages=messages,
            max_tokens=cfg.get("max_tokens_triage", 200),
            temperature=cfg.get("temperature_triage", 0.1),
            response_format={"type": "json_object"},
        )
        raw_json = response.choices[0].message.content
        result = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("Triage JSON parse error for %s: %s", talent_key, exc)
        return _fallback(talent_key, "non-JSON output from triage model")
    except Exception as exc:  # noqa: BLE001
        logger.error("Triage API error for %s: %s", talent_key, exc)
        return _fallback(talent_key, f"API error: {exc}")

    # Validate score
    score = result.get("score")
    if score not in (1, 2, 3):
        logger.warning("Invalid score %r for %s — routing to Score 2", score, talent_key)
        return _fallback(talent_key, f"invalid score value: {score}")

    proposed_rate = float(result.get("proposed_rate_usd", 0) or 0)
    offer_type = str(result.get("offer_type", "Unknown"))

    # Apply special per-talent overrides
    score = _apply_special_routing(talent_key, score, offer_type, proposed_rate, policy)

    return {
        "score": score,
        "reason": result.get("reason", ""),
        "offer_type": offer_type,
        "proposed_rate_usd": proposed_rate,
        "brand_name": str(result.get("brand_name", "") or ""),
    }


def _fallback(talent_key: str, note: str) -> dict:
    return {
        "score": 2,
        "reason": f"Triage fallback — {note}",
        "offer_type": "Unknown",
        "proposed_rate_usd": 0.0,
        "brand_name": "",
    }
