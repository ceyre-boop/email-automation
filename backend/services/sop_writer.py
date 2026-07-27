"""Write talent data back to sheets/sop.md."""
from __future__ import annotations

import re
from pathlib import Path

_SOP_PATH = Path(__file__).resolve().parents[2] / "sheets" / "sop.md"

# Stray Markdown backslash-escapes (\_ \* \[ …) must never survive into sop.md — a DOCX
# import/merge reintroducing `\_alanacalvs` is what caused the leaked-backslash regression.
# Mirror of gmail._MD_ESCAPE_RE; normalized at every write (all writes flow through here).
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!~>-])")

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
    """Write updated text to sheets/sop.md, invalidate all in-memory caches, and commit+push."""
    import logging
    import subprocess
    # Normalize stray Markdown escapes so imports/merges can't reintroduce artifacts
    # like `\_alanacalvs` that leak into sent emails (see module note above).
    new_text = _MD_ESCAPE_RE.sub(r"\1", new_text)
    _SOP_PATH.write_text(new_text, encoding="utf-8")
    from backend.core.config import get_settings
    get_settings.cache_clear()
    try:
        from backend.services.reply import clear_sop_cache
        clear_sop_cache()
    except Exception:
        pass
    # Commit and push so changes survive redeploys
    _logger = logging.getLogger(__name__)
    try:
        repo_root = str(_SOP_PATH.parents[1])
        subprocess.run(["git", "add", str(_SOP_PATH)], cwd=repo_root, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", "admin: update sop.md via SOP Admin UI"],
            cwd=repo_root, capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=repo_root, check=False, capture_output=True)
            _logger.info("sop_writer: committed and pushed sop.md changes")
        else:
            _logger.debug("sop_writer: git commit skipped (nothing to commit or no changes)")
    except Exception as exc:
        _logger.warning("sop_writer: git auto-commit failed (changes saved in-memory only): %s", exc)


_WORKFLOW_PATH = _SOP_PATH.parent / "Automated Send Workflow.md"

# Anchors every valid Automated Send Workflow doc must contain. Guards against
# writing an empty or wrong document (e.g. someone drops the SOP in this box).
_WORKFLOW_ANCHORS = ("Send Gate", "Post-Send", "Locate Existing Draft")


def validate_workflow_text(new_text: str) -> list[str]:
    """Return blocking reasons why `new_text` is not a valid workflow doc ([] = OK)."""
    problems: list[str] = []
    body = (new_text or "").strip()
    if len(body) < 200:
        problems.append(f"Document is only {len(body)} characters — expected a full workflow doc")
    missing = [a for a in _WORKFLOW_ANCHORS if a.lower() not in body.lower()]
    if missing:
        problems.append("Missing expected sections: " + ", ".join(missing))
    if "Talent: " in body and "Approved Response" in body:
        problems.append("This looks like the SOP document, not the Automated Send Workflow")
    return problems


def write_workflow_md(new_text: str) -> None:
    """Write sheets/Automated Send Workflow.md and commit+push.

    Unlike sop.md this file carries no machine-read metadata (no Key:, Gmail:,
    Min Rate:), so a whole-document replace is safe — but callers must run
    validate_workflow_text() first.
    """
    import logging
    import subprocess

    new_text = _MD_ESCAPE_RE.sub(r"\1", new_text)
    _WORKFLOW_PATH.write_text(new_text, encoding="utf-8")
    _logger = logging.getLogger(__name__)
    try:
        repo_root = str(_WORKFLOW_PATH.parents[1])
        subprocess.run(["git", "add", str(_WORKFLOW_PATH)], cwd=repo_root, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", "admin: update Automated Send Workflow via SOP Manager"],
            cwd=repo_root, capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=repo_root, check=False, capture_output=True)
            _logger.info("sop_writer: committed and pushed workflow doc changes")
    except Exception as exc:
        _logger.warning("sop_writer: workflow git auto-commit failed: %s", exc)


def read_workflow_md() -> str:
    return _WORKFLOW_PATH.read_text(encoding="utf-8") if _WORKFLOW_PATH.exists() else ""


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
