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
import time

from openai import OpenAI

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Prompt section cache ──────────────────────────────────────────────────────
# Parsing the prompt file (regex over ~3 KB) on every triage call is wasteful.
# Cache the parsed (system_text, user_template) pair — it doesn't change at runtime.
_triage_sections: tuple[str, str] | None = None
_EVENT_INVITE_KEYWORDS = (
    "event invite", "invitation", "you're invited", "rsvp", "launch party",
    "panel", "red carpet", "guest list", "join us at", "attend our event",
    "private dinner", "popup", "premiere", "screening",
)


def _get_triage_sections() -> tuple[str, str]:
    global _triage_sections
    if _triage_sections is None:
        _triage_sections = _parse_prompt_sections(get_settings().triage_prompt)
    return _triage_sections


def clear_triage_cache() -> None:
    """Force reload of triage prompt on next call. Call after prompt file updates."""
    global _triage_sections
    _triage_sections = None


def _looks_like_event_invite(subject: str, body: str, offer_type: str) -> bool:
    """
    Detect obvious event invitations that should not generate outbound replies.
    This is intentionally conservative: only clear invite / RSVP language triggers it.
    """
    haystack = f"{subject}\n{body}".lower()
    offer_lower = (offer_type or "").lower()
    if "event" not in offer_lower and "appearance" not in offer_lower:
        return False
    return any(keyword in haystack for keyword in _EVENT_INVITE_KEYWORDS)


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
) -> list[dict]:
    """Parse the triage.md prompt and fill in template variables."""
    system_text, user_template = _get_triage_sections()

    user_text = (
        user_template
        .replace("{{TALENT_NAME}}", talent_name)
        .replace("{{MINIMUM_RATE}}", str(int(minimum_rate)))
        .replace("{{EMAIL_SUBJECT}}", subject)
        .replace("{{SENDER_EMAIL}}", sender)
        .replace("{{SENDER_DOMAIN}}", sender_domain)
        .replace("{{EMAIL_BODY}}", body[:2000])
    )

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
    brand_name: str = "",
) -> int:
    """All talent-specific routing lives in the SOP (sheets/sop.md), not here."""
    return score


# ── OpenAI retry helper ───────────────────────────────────────────────────────

def _call_openai_with_retry(client: OpenAI, talent_key: str, **kwargs):
    """
    Call client.chat.completions.create with retry for per-minute rate limits.
    Daily cap (RPD) errors are NOT retried — they won't clear until midnight.
    RPM errors get up to 3 attempts with short backoff (5s, 10s, 20s).
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            is_rate_limit = "429" in err_str or "rate_limit_exceeded" in err_str
            is_daily_cap = "requests per day" in err_str.lower() or "RPD" in err_str
            if is_rate_limit and not is_daily_cap:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                logger.warning(
                    "RPM rate limit for %s (attempt %d/3) — retrying in %ds",
                    talent_key, attempt + 1, wait,
                )
                time.sleep(wait)
                last_exc = exc
                continue
            raise  # daily cap or non-rate-limit error — propagate immediately
    raise last_exc  # type: ignore[misc]


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
        "proposed_rate_usd": 0.0,
        "brand_name": str,
    }
    Falls back to score=2 on any error (never silently drops emails).
    """
    settings = get_settings()
    if not settings.app_config.get("ai_enabled", True):
        raise RuntimeError("AI is disabled (ai_enabled=false in settings.json) — triage skipped")
    cfg = settings.app_config.get("openai", {})
    policy = settings.confidence_policy
    triage_cfg = settings.app_config.get("triage", {})

    talent_cfg = next(
        (t for t in settings.app_config.get("talents", []) if t.get("key", "").lower() == talent_key.lower()),
        {},
    )

    # ── Pre-filter: personal email forward (Rule 8) ───────────────────────────
    # If the inbound sender matches any of the talent's personal emails, leave in INBOX.
    # personal_email may be a string (single address), comma-separated string, or a JSON array.
    sender_lower = sender.lower()
    personal_email = talent_cfg.get("personal_email", "")
    if personal_email:
        if isinstance(personal_email, list):
            personal_emails = [e.strip().lower() for e in personal_email if e.strip()]
        else:
            personal_emails = [e.strip().lower() for e in personal_email.split(",") if e.strip()]
        if sender_lower in personal_emails:
            logger.info(
                "Pre-filter: personal email match for %s (%s) → ignore, leave in INBOX",
                talent_key, sender,
            )
            return _ignore_leave_inbox(
                "Email originated from talent personal email — left in INBOX for human review.",
                "Personal Email Forward",
            )
    # ── Pre-filter: explicit never-reply rules → Score 1, no GPT call ───────────
    never_reply = triage_cfg.get("never_reply", {}) if isinstance(triage_cfg, dict) else {}
    blocked_domains = {str(d).strip().lower() for d in never_reply.get("domains", []) if str(d).strip()}
    blocked_senders = {str(s).strip().lower() for s in never_reply.get("senders", []) if str(s).strip()}
    # also support legacy "emails" key from older config versions
    blocked_senders |= {str(s).strip().lower() for s in never_reply.get("emails", []) if str(s).strip()}
    blocked_subject_keywords = [str(k).strip().lower() for k in never_reply.get("subject_keywords", []) if str(k).strip()]
    blocked_body_keywords = [str(k).strip().lower() for k in never_reply.get("body_keywords", []) if str(k).strip()]

    subject_lower = subject.lower()
    body_lower = (body or "").lower()

    if (
        sender_domain.lower() in blocked_domains
        or sender_lower in blocked_senders
        or any(kw in subject_lower for kw in blocked_subject_keywords)
        or any(kw in body_lower for kw in blocked_body_keywords)
    ):
        logger.info(
            "Pre-filter: never-reply rule matched for %s (%s / %s) → Score 1",
            talent_key, sender, subject,
        )
        return {
            "score": 1,
            "reason": "Matched never-reply rule (sender/domain/keyword blocklist).",
            "offer_type": "Blocked",
            "proposed_rate_usd": 0.0,
            "brand_name": "",
            "sentiment_score": 0,
            "urgency_score": 0,
            "risk_score": 8,
            "alternatives_considered": "Pre-filter blocklist match — no GPT call made.",
        }

    # ── Pre-filter: known automated / non-human senders → Score 1, no GPT call ──
    _AUTO_DOMAINS = {
        "shop.tiktok.com", "tiktok.com", "notifications.tiktok.com",
        "mailer.tiktok.com", "noreply.tiktok.com",
    }
    _AUTO_SUBJECT_KEYWORDS = [
        "sample order has arrived", "your order has", "order confirmation",
        "unsubscribe", "tracking number", "shipping update", "delivery update",
        "password reset", "verify your email", "email verification",
        "account alert", "security alert", "invoice #", "receipt for",
    ]
    is_auto_domain = sender_domain in _AUTO_DOMAINS
    is_auto_subject = any(kw in subject_lower for kw in _AUTO_SUBJECT_KEYWORDS)
    is_collab = any(
        kw in subject_lower
        for kw in ("collab", "collaboration", "partner", "partnership", "campaign")
    )
    if (is_auto_domain or is_auto_subject) and not is_collab:
        logger.info(
            "Pre-filter: automated sender/subject for %s (%s / %s) → Score 1",
            talent_key, sender, subject,
        )
        return {
            "score": 1,
            "reason": "Automated system email — not a real partnership offer.",
            "offer_type": "Automated",
            "proposed_rate_usd": 0.0,
            "brand_name": "",
            "sentiment_score": 5,
            "urgency_score": 0,
            "risk_score": 0,
            "alternatives_considered": "Auto-sender pre-filter — no GPT call made.",
        }

    messages = _build_triage_messages(
        talent_name=talent_name,
        minimum_rate=minimum_rate,
        subject=subject,
        sender=sender,
        sender_domain=sender_domain,
        body=body,
    )

    client = OpenAI(api_key=settings.openai_api_key)
    model = cfg.get("triage_model", "gpt-4o-mini")
    max_tok = cfg.get("max_tokens_triage", 400)
    try:
        response = _call_openai_with_retry(
            client,
            talent_key=talent_key,
            model=model,
            messages=messages,
            max_tokens=max_tok,
            temperature=cfg.get("temperature_triage", 0.1),
            response_format={"type": "json_object"},
        )
        raw_json = response.choices[0].message.content or ""
        finish_reason = response.choices[0].finish_reason

        # Hard truncation detection — if GPT stopped because it hit max_tokens mid-JSON
        if finish_reason == "length":
            logger.error(
                "TRIAGE TRUNCATION: %s hit max_tokens (%d) mid-output. "
                "Raise max_tokens_triage in settings.json. Raw: %.80s",
                talent_key, max_tok, raw_json,
            )
            return _fallback(talent_key, f"output truncated at {max_tok} tokens — raise max_tokens_triage")

        result = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.error(
            "TRIAGE JSON ERROR: %s returned non-JSON (likely truncated). "
            "finish_reason unknown. Raw snippet: %.80s — %s",
            talent_key, raw_json[:80] if "raw_json" in dir() else "N/A", exc,
        )
        return _fallback(talent_key, "non-JSON output from triage model")
    except Exception as exc:  # noqa: BLE001
        logger.error("Triage API error for %s: %s", talent_key, exc)
        return _fallback(talent_key, f"API error: {exc}")

    # Schema validation — required fields must all be present
    _REQUIRED = {"score", "reason", "offer_type", "brand_name"}
    missing = _REQUIRED - set(result.keys())
    if missing:
        logger.error(
            "TRIAGE SCHEMA MISMATCH for %s — missing fields: %s. Raw: %.120s",
            talent_key, missing, raw_json[:120],
        )
        return _fallback(talent_key, f"schema mismatch — missing: {missing}")

    # Score must be 1, 2, or 3
    score = result.get("score")
    if score not in (1, 2, 3):
        logger.warning("Invalid score %r for %s — routing to Score 2", score, talent_key)
        return _fallback(talent_key, f"invalid score value: {score}")

    proposed_rate = 0.0
    offer_type = str(result.get("offer_type", "Unknown"))
    brand_name = str(result.get("brand_name", "") or "")

    def _clamp_score(val, default=5):
        try:
            return max(0, min(10, int(val)))
        except (TypeError, ValueError):
            return default

    # Apply special per-talent overrides
    score = _apply_special_routing(talent_key, score, offer_type, proposed_rate, policy, brand_name)

    # Event invite detection (Rule 7) — leave in INBOX, no draft, no label change
    if _looks_like_event_invite(subject, body, offer_type):
        logger.info(
            "Event invite detected for %s (%s) → ignore, leave in INBOX", talent_key, subject,
        )
        return _ignore_leave_inbox(
            f"Event / appearance / speaking invite — left in INBOX for human review. ({result.get('reason', '')})",
            offer_type,
        )

    return {
        "score": score,
        "reason": result.get("reason", ""),
        "offer_type": offer_type,
        "proposed_rate_usd": 0.0,
        "brand_name": brand_name,
        "sentiment_score": _clamp_score(result.get("sentiment_score"), 5),
        "urgency_score": _clamp_score(result.get("urgency_score"), 0),
        "risk_score": _clamp_score(result.get("risk_score"), 0),
        "alternatives_considered": str(result.get("alternatives_considered", "") or ""),
    }


def _ignore_leave_inbox(reason: str, offer_type: str = "Event Invite") -> dict:
    """Return a triage result that tells the poller to leave the email in INBOX untouched."""
    return {
        "score": 2,
        "reason": reason,
        "offer_type": offer_type,
        "proposed_rate_usd": 0.0,
        "brand_name": "",
        "sentiment_score": 5,
        "urgency_score": 0,
        "risk_score": 0,
        "alternatives_considered": "",
        "ignore_leave_inbox": True,
    }


def _fallback(talent_key: str, note: str) -> dict:
    return {
        "score": 2,
        "reason": f"Triage fallback — {note}",
        "offer_type": "Unknown",
        "proposed_rate_usd": 0.0,
        "brand_name": "",
        "sentiment_score": 5,
        "urgency_score": 0,
        "risk_score": 0,
        "alternatives_considered": "",
    }
