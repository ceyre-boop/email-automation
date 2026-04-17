"""
Tests for core config and static file loading.
"""
from __future__ import annotations

import json
import pytest

from backend.core.config import get_settings


def test_settings_loads():
    s = get_settings()
    assert s.google_client_id == "test-client-id"
    assert s.openai_api_key == "test-openai-key"


def test_app_config_has_talents():
    s = get_settings()
    cfg = s.app_config
    assert "talents" in cfg
    assert len(cfg["talents"]) > 0


def test_talent_keys_are_strings():
    s = get_settings()
    for talent in s.app_config["talents"]:
        assert isinstance(talent["key"], str)
        assert len(talent["key"]) > 0


def test_sop_data_loads():
    s = get_settings()
    sop = s.sop_data
    assert isinstance(sop, dict)
    # At least one talent should have rules
    all_rules = [t for t in sop.values() if t.get("rules")]
    assert len(all_rules) > 0


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
