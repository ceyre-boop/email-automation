"""Write talent data back to sheets/sop.md."""
from __future__ import annotations

import re
from pathlib import Path

_SOP_PATH = Path(__file__).resolve().parents[2] / "sheets" / "sop.md"

_TALENT_HEADING_RE = re.compile(
    r"^[ \t]*(?:#+[ \t]*)?Talent:[ \t]*(?P<name>[^\r\n]*)[ \t]*$",
    re.MULTILINE,
)


def _find_talent_section(sop_text: str, talent_key: str) -> tuple[int, int]:
    """Return (start, end) char offsets of the talent's section. Raises ValueError if not found."""
    matches = list(_TALENT_HEADING_RE.finditer(sop_text))
    for i, match in enumerate(matches):
        section_start = match.start()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(sop_text)
        section = sop_text[section_start:section_end]
        key_match = re.search(r"^[ \t]*Key[ \t]*:[ \t]*(.+)$", section, re.MULTILINE)
        if key_match and key_match.group(1).strip().lower() == talent_key.lower():
            return section_start, section_end
    raise ValueError(f"Talent '{talent_key}' not found in sop.md")


def update_talent_field(sop_text: str, talent_key: str, field: str, new_value: str) -> str:
    """Replace a single metadata field line for a talent without touching other sections."""
    start, end = _find_talent_section(sop_text, talent_key)
    section = sop_text[start:end]
    pattern = re.compile(
        r"^([ \t]*" + re.escape(field) + r"[ \t]*:[ \t]*)([^\r\n]*)$",
        re.MULTILINE,
    )
    new_section, count = pattern.subn(rf"\g<1>{new_value}", section, count=1)
    if count == 0:
        raise ValueError(f"Field '{field}' not found in section for talent '{talent_key}'")
    return sop_text[:start] + new_section + sop_text[end:]


def update_approved_response(sop_text: str, talent_key: str, new_response: str) -> str:
    """Replace the approved response text for a talent."""
    start, end = _find_talent_section(sop_text, talent_key)
    section = sop_text[start:end]

    ar_match = re.search(r"^[ \t]*Approved Response:[ \t]*$", section, re.MULTILINE)
    if ar_match is None:
        raise ValueError(f"'Approved Response:' not found for talent '{talent_key}'")

    ar_end = ar_match.end()
    next_scenario = re.search(r"^[ \t]*Scenario\b", section[ar_end:], re.MULTILINE)
    content_end = ar_end + next_scenario.start() if next_scenario else len(section)

    new_section = (
        section[:ar_end]
        + "\n"
        + new_response.rstrip("\n")
        + "\n"
        + section[content_end:]
    )
    return sop_text[:start] + new_section + sop_text[end:]


def update_personal_emails(sop_text: str, talent_key: str, emails: list[str]) -> str:
    """Replace the personal email bullet list for a talent."""
    start, end = _find_talent_section(sop_text, talent_key)
    section = sop_text[start:end]

    pe_match = re.search(
        r"^([ \t]*Personal Emails?[ \t]*:[ \t]*)$",
        section,
        re.MULTILINE | re.IGNORECASE,
    )
    if pe_match is None:
        raise ValueError(f"'Personal Email(s):' not found for talent '{talent_key}'")

    pe_end = pe_match.end()
    # Scan forward: consume blank lines and bullet lines to find where the list ends
    list_end = pe_end
    for line in section[pe_end:].splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("- ") or stripped == "":
            list_end += len(line)
        else:
            break

    email_block = "\n" + "".join(f"\n- {email}" for email in emails) + "\n"
    new_section = section[:pe_end] + email_block + section[list_end:]
    return sop_text[:start] + new_section + sop_text[end:]


def write_sop_md(new_text: str) -> None:
    """Write updated text to sheets/sop.md and invalidate all in-memory caches."""
    _SOP_PATH.write_text(new_text, encoding="utf-8")
    from backend.core.config import get_settings
    get_settings.cache_clear()
    try:
        from backend.services.reply import clear_sop_cache
        clear_sop_cache()
    except Exception:
        pass


def validate_before_write(
    minimum_rate_usd: int | None,
    personal_emails: list[str] | None,
    approved_response: str | None,
) -> list[str]:
    """Return a list of validation error strings. Empty list = valid."""
    errors: list[str] = []
    if minimum_rate_usd is not None and minimum_rate_usd <= 0:
        errors.append("minimum_rate_usd must be greater than 0")
    if personal_emails is not None and not personal_emails:
        errors.append("at least one personal email is required")
    if approved_response is not None and not approved_response.strip():
        errors.append("approved_response cannot be empty")
    return errors
