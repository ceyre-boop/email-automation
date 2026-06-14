"""Pre-send validation gate — runs before every outbound reply (auto-send + manual approve).

All checks are pure local computation — no Gmail API, no OpenAI calls.
Returns (True, None) if all checks pass.
Returns (False, "<error>") on first failure, error goes into Draft.validation_error.
"""
from __future__ import annotations

import re
import logging

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, ProcessedEmail
from backend.services.gmail import parse_cc_recipients

logger = logging.getLogger(__name__)

_SOP_METADATA_MARKERS = [
    "Classification:",
    "Matched Scenario:",
    "Draft Created:",
    "Remove INBOX Label:",
    "Apply Label:",
]


def _key_to_name(talent_key: str) -> str | None:
    for t in get_settings().app_config.get("talents", []):
        if t["key"].lower() == talent_key.lower():
            return t.get("full_name", t["key"])
    return None


def run_pre_send_checks(draft: Draft, db: Session) -> tuple[bool, str | None]:
    """Run all pre-send checks. Returns (True, None) or (False, error_string)."""
    body = draft.draft_text or ""

    # Check 1 — minimum body length
    if len(body.strip()) < 50:
        return False, "Draft body is too short (< 50 chars)"

    # Check 2 — SOP metadata strings not in body
    body_lower = body.lower()
    for marker in _SOP_METADATA_MARKERS:
        if marker.lower() in body_lower:
            return False, f"SOP metadata found in draft body: '{marker}'"

    # Check 4 — CC addresses not embedded in body
    cc_list = parse_cc_recipients(draft.cc_recipients) or []
    for addr in cc_list:
        if addr.lower() in body_lower:
            return False, f"CC address '{addr}' is embedded in draft body — should be in cc_recipients field only"
    if re.search(r'(?i)^cc\s*:', body, re.MULTILINE):
        return False, "Draft body contains a 'CC:' line — CC must be in cc_recipients field, not the body"

    # Check 5 — talent match (source from sop.md profiles, not settings.json talents[])
    known_keys = {k.lower() for k in get_settings().talent_profiles}
    if known_keys and draft.talent_key.lower() not in known_keys:
        return False, f"talent_key '{draft.talent_key}' is not in the talent roster"
    if draft.gmail_message_id:
        pe = db.query(ProcessedEmail).filter(
            ProcessedEmail.gmail_message_id == draft.gmail_message_id
        ).first()
        if pe and pe.talent_key.lower() != draft.talent_key.lower():
            return False, (
                f"Talent mismatch: draft.talent_key='{draft.talent_key}' "
                f"but email was triaged for '{pe.talent_key}'"
            )

    return True, None
