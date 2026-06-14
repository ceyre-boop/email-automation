"""Parse talent metadata from SOP markdown text."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass
class TalentProfile:
    key: str
    full_name: str
    manager: str
    manager_email: str | None
    gmail_connection_name: str | None
    minimum_rate_usd: int
    rate_unit: str
    auto_send: bool
    paused: bool
    personal_emails: list[str]
    has_approved_response: bool


_TALENT_HEADING_RE = re.compile(
    r"^[ \t]*(?:#+[ \t]*)?Talent:[ \t]*(?P<name>[^\r\n]*)[ \t]*$",
    re.MULTILINE,
)
_METADATA_RE_TEMPLATE = r"^[ \t]*{field}[ \t]*:[ \t]*(?P<value>[^\r\n]*)[ \t]*$"
_MANAGER_WITH_EMAIL_RE = re.compile(r"^(?P<manager>.*?)\s*<(?P<email>[^<>]+)>\s*$")
_RATE_RE = re.compile(r"\$?\s*(?P<amount>\d[\d,]*)\s*(?P<unit>.*)$")
_RATE_UNIT_RE = re.compile(r"\bper\b.*", re.IGNORECASE)
_PERSONAL_EMAIL_HEADING_RE = re.compile(r"^[ \t]*Personal Emails?[ \t]*:[ \t]*$", re.IGNORECASE)
_SECTION_STOP_RE = re.compile(r"^[ \t]*(?:#+[ \t]*)?(?:Scenario\b.*:|Talent:)", re.IGNORECASE)


def parse_sop_md(text: str) -> dict[str, TalentProfile]:
    """Return talent profiles parsed from SOP markdown text."""
    if not text.strip():
        return {}

    profiles: dict[str, TalentProfile] = {}
    matches = list(_TALENT_HEADING_RE.finditer(text))

    for index, match in enumerate(matches):
        full_name = match.group("name").strip()
        if not _is_real_talent_name(full_name):
            continue

        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start():next_start]
        key = _metadata_value(section, "Key") or _fallback_key(full_name)

        if not key:
            continue

        manager, manager_email = _parse_manager(_metadata_value(section, "Manager"))
        minimum_rate_usd, rate_unit = _parse_rate(_metadata_value(section, "Min Rate"))

        profiles[key] = TalentProfile(
            key=key,
            full_name=full_name,
            manager=manager,
            manager_email=manager_email,
            gmail_connection_name=_empty_to_none(_metadata_value(section, "Gmail")),
            minimum_rate_usd=minimum_rate_usd,
            rate_unit=rate_unit,
            auto_send=_parse_yes(_metadata_value(section, "Auto Send")),
            paused=_parse_yes(_metadata_value(section, "Paused")),
            personal_emails=_parse_personal_emails(section),
            has_approved_response="Approved Response:" in section,
        )

    return profiles


def validate_profiles(profiles: dict[str, TalentProfile]) -> list[str]:
    """Return warnings for incomplete profile configuration."""
    warnings: list[str] = []

    for key, profile in profiles.items():
        if not profile.has_approved_response:
            warnings.append(f"{key}: missing approved response")
        if not profile.personal_emails:
            warnings.append(f"{key}: missing personal emails")
        if profile.gmail_connection_name is None:
            warnings.append(f"{key}: missing Gmail connection name")
        if profile.minimum_rate_usd == 0:
            warnings.append(f"{key}: missing minimum rate")
        if profile.manager_email is None:
            warnings.append(f"{key}: missing manager email")

    return warnings


def get_active_profiles(profiles: dict[str, TalentProfile]) -> dict[str, TalentProfile]:
    """Return profiles that are not paused."""
    return {key: profile for key, profile in profiles.items() if not profile.paused}


def _is_real_talent_name(full_name: str) -> bool:
    return bool(full_name and not full_name.startswith("["))


def _fallback_key(full_name: str) -> str:
    return full_name.split()[0] if full_name.split() else ""


def _metadata_value(section: str, field: str) -> str:
    pattern = re.compile(_METADATA_RE_TEMPLATE.format(field=re.escape(field)), re.MULTILINE)
    match = pattern.search(section)
    return match.group("value").strip() if match else ""


def _empty_to_none(value: str) -> str | None:
    return value or None


def _parse_manager(value: str) -> tuple[str, str | None]:
    manager = value.strip()
    if not manager:
        return "", None

    match = _MANAGER_WITH_EMAIL_RE.match(manager)
    if not match:
        return manager, None

    return match.group("manager").strip(), match.group("email").strip()


def _parse_rate(value: str) -> tuple[int, str]:
    rate_text = value.strip()
    if not rate_text:
        return 0, ""

    match = _RATE_RE.search(rate_text)
    if not match:
        unit_match = _RATE_UNIT_RE.search(rate_text)
        return 0, unit_match.group(0).strip() if unit_match else ""

    amount = int(match.group("amount").replace(",", ""))
    return amount, match.group("unit").strip()


def _parse_yes(value: str) -> bool:
    return value.strip().lower() == "yes"


def _parse_personal_emails(section: str) -> list[str]:
    lines = section.splitlines()
    emails: list[str] = []

    for index, line in enumerate(lines):
        if not _PERSONAL_EMAIL_HEADING_RE.match(line):
            continue

        for email_line in lines[index + 1:]:
            stripped = email_line.strip()
            if not stripped:
                continue
            if _SECTION_STOP_RE.match(stripped):
                break
            if not stripped.startswith("- "):
                break

            email = stripped[2:].strip()
            if email:
                emails.append(email)

    return emails
