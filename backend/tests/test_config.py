"""
Tests for core config and static file loading.
"""
from __future__ import annotations

import json
import pytest

from backend.core.config import get_settings


def test_settings_loads():
    s = get_settings()
    # Env vars are loaded (non-empty) — exact values depend on CI vs local env.
    assert isinstance(s.google_client_id, str) and len(s.google_client_id) > 0
    assert isinstance(s.openai_api_key, str) and len(s.openai_api_key) > 0


def test_talent_profiles_loads():
    """Talent roster is sourced from sop.md (not settings.json since SOP Manager v2)."""
    s = get_settings()
    profiles = s.talent_profiles
    assert isinstance(profiles, dict)
    assert len(profiles) >= 5, f"Expected at least 5 talent profiles, got {len(profiles)}"
    # All keys should be non-empty strings
    for key, profile in profiles.items():
        assert isinstance(key, str) and len(key) > 0, f"Bad profile key: {key!r}"
        assert profile.full_name, f"Profile {key!r} has no full_name"


def test_talent_list_has_correct_shape():
    """talent_list bridges sop.md profiles → legacy dict shape for dashboard code."""
    s = get_settings()
    for talent in s.talent_list:
        assert isinstance(talent["key"], str)
        assert len(talent["key"]) > 0


def test_sop_data_loads():
    s = get_settings()
    sop = s.sop_data
    assert isinstance(sop, dict)
    # sop_data.json entries are dicts; _generated is a metadata string — skip it.
    talent_entries = {k: v for k, v in sop.items() if isinstance(v, dict)}
    assert len(talent_entries) > 0, "sop_data.json should have at least one talent entry"


def test_confidence_policy_loads():
    s = get_settings()
    policy = s.confidence_policy
    assert isinstance(policy, dict)


def test_triage_prompt_loads():
    s = get_settings()
    prompt = s.triage_prompt
    assert "## SYSTEM PROMPT" in prompt
    assert "## USER PROMPT TEMPLATE" in prompt
    assert len(prompt) > 200


def test_reply_prompt_loads():
    s = get_settings()
    prompt = s.reply_prompt
    assert "## SYSTEM PROMPT" in prompt
    assert "## USER PROMPT TEMPLATE" in prompt
    assert len(prompt) > 200


def test_allowed_origins_list():
    s = get_settings()
    origins = s.allowed_origins_list
    assert isinstance(origins, list)
    assert len(origins) >= 1
