from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest


# Keep imports stable when pytest is run from the backend/ directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.sop_parser import (  # noqa: E402
    TalentProfile,
    get_active_profiles,
    parse_sop_md,
    validate_profiles,
)


@pytest.fixture
def sop_text() -> str:
    return """
Talent Email AI Guidelines

Talent matching is mandatory.

Talent: [talent name, if applicable]
Matched Scenario: Template Example

## Talent: Jocelyn Chardon
Key: Jocelyn
Manager: Cara Best <cara@taboost.me>
Gmail: Gmail - Jocelyn
Min Rate: $850 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out.

Scenario C: Personal Email Forward
Personal Emails:

- jocelynsagec@gmail.com

Talent: Marcus Reed
Manager: Internal Manager
Gmail: Gmail - Marcus
Min Rate: 1,250 per hour
Auto Send: YeS
Paused: YES

Scenario A: Initial Inbound
Please send more details.
"""


def test_parse_sop_md_empty_returns_empty_dict() -> None:
    assert parse_sop_md("") == {}
    assert parse_sop_md(" \n\t ") == {}
    assert parse_sop_md("Talent Email AI Guidelines\nTalent matching is mandatory.") == {}


def test_parse_sop_md_returns_two_profiles_and_skips_template_trap(sop_text: str) -> None:
    profiles = parse_sop_md(sop_text)

    assert len(profiles) == 2
    assert set(profiles) == {"Jocelyn", "Marcus"}
    assert not any(key.startswith("[") for key in profiles)
    assert not any(profile.full_name.startswith("[") for profile in profiles.values())


def test_parse_sop_md_parses_jocelyn_profile(sop_text: str) -> None:
    profile = parse_sop_md(sop_text)["Jocelyn"]

    assert profile == TalentProfile(
        key="Jocelyn",
        full_name="Jocelyn Chardon",
        manager="Cara Best",
        manager_email="cara@taboost.me",
        gmail_connection_name="Gmail - Jocelyn",
        minimum_rate_usd=850,
        rate_unit="per video",
        auto_send=False,
        paused=False,
        personal_emails=["jocelynsagec@gmail.com"],
        has_approved_response=True,
    )


def test_parse_sop_md_handles_key_fallback_no_email_and_no_dollar_rate(
    sop_text: str,
) -> None:
    profile = parse_sop_md(sop_text)["Marcus"]

    assert profile.key == "Marcus"
    assert profile.full_name == "Marcus Reed"
    assert profile.manager == "Internal Manager"
    assert profile.manager_email is None
    assert profile.gmail_connection_name == "Gmail - Marcus"
    assert profile.minimum_rate_usd == 1250
    assert profile.rate_unit == "per hour"
    assert profile.auto_send is True
    assert profile.paused is True
    assert profile.personal_emails == []
    assert profile.has_approved_response is False


def test_parse_sop_md_handles_singular_personal_email_missing_gmail_and_bad_rate() -> None:
    profiles = parse_sop_md(
        """
Talent: Riley Stone
Key: Riley
Manager: Operations
Min Rate: negotiable per video
Auto Send: maybe
Paused: no

Scenario C: Personal Email Forward
Personal Email:
- riley@example.com
"""
    )

    profile = profiles["Riley"]

    assert profile.gmail_connection_name is None
    assert profile.minimum_rate_usd == 0
    assert profile.rate_unit == "per video"
    assert profile.auto_send is False
    assert profile.paused is False
    assert profile.personal_emails == ["riley@example.com"]


def test_validate_profiles_returns_empty_list_for_fully_valid_profile(
    sop_text: str,
) -> None:
    profile = parse_sop_md(sop_text)["Jocelyn"]

    assert validate_profiles({"Jocelyn": profile}) == []


def test_validate_profiles_returns_one_warning_for_missing_approved_response(
    sop_text: str,
) -> None:
    profile = replace(parse_sop_md(sop_text)["Jocelyn"], has_approved_response=False)

    warnings = validate_profiles({"Jocelyn": profile})

    assert len(warnings) == 1
    assert "Jocelyn" in warnings[0]
    assert "approved response" in warnings[0]


def test_validate_profiles_returns_one_warning_for_empty_personal_emails(
    sop_text: str,
) -> None:
    profile = replace(parse_sop_md(sop_text)["Jocelyn"], personal_emails=[])

    warnings = validate_profiles({"Jocelyn": profile})

    assert len(warnings) == 1
    assert "Jocelyn" in warnings[0]
    assert "personal emails" in warnings[0]


def test_validate_profiles_warns_for_each_missing_configuration(sop_text: str) -> None:
    profile = replace(
        parse_sop_md(sop_text)["Jocelyn"],
        gmail_connection_name=None,
        minimum_rate_usd=0,
        manager_email=None,
    )

    warnings = validate_profiles({"Jocelyn": profile})

    assert len(warnings) == 3
    assert all("Jocelyn" in warning for warning in warnings)
    assert any("Gmail" in warning for warning in warnings)
    assert any("minimum rate" in warning for warning in warnings)
    assert any("manager email" in warning for warning in warnings)


def test_get_active_profiles_filters_out_paused_profiles(sop_text: str) -> None:
    profiles = parse_sop_md(sop_text)

    active_profiles = get_active_profiles(profiles)

    assert set(active_profiles) == {"Jocelyn"}
    assert active_profiles["Jocelyn"].paused is False
