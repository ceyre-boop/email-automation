"""ai_enabled and talent pause live in the DB (app_state), never in files."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from backend.core.config import get_settings
from backend.models.db import AppState
from backend.services import runtime_state
from backend.services.guardian import GuardianWatchdog
from backend.services.sop_parser import get_active_profiles

_ROOT = Path(__file__).resolve().parents[2]
_FILES = [_ROOT / "config" / "settings.json", _ROOT / "sheets" / "sop.md"]


def _digest():
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in _FILES}


@pytest.fixture
def rt(db_engine, db_session, monkeypatch):
    """Point the runtime-state reader at the test database."""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    monkeypatch.setattr(runtime_state, "_open_session", lambda: factory())
    runtime_state.invalidate_cache()
    return db_session


def _a_talent_key():
    return next(iter(get_settings().talent_profiles))


# ── ai_enabled ───────────────────────────────────────────────────────────────

def test_ai_enabled_defaults_to_file_value_when_no_db_row(rt, monkeypatch):
    assert runtime_state.is_ai_enabled() is True
    monkeypatch.setattr(runtime_state, "_file_ai_default", lambda: False)
    runtime_state.invalidate_cache()
    assert runtime_state.is_ai_enabled() is False


def test_db_value_beats_file_default_and_flips_immediately(rt):
    runtime_state.set_ai_enabled(rt, False)
    assert runtime_state.is_ai_enabled() is False
    assert rt.get(AppState, runtime_state.KEY_AI_ENABLED).value_text == "false"
    runtime_state.set_ai_enabled(rt, True)
    assert runtime_state.is_ai_enabled() is True


def test_unreadable_db_keeps_last_known_value(rt, monkeypatch):
    runtime_state.set_ai_enabled(rt, False)
    runtime_state.invalidate_cache()

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime_state, "_open_session", boom)
    assert runtime_state.is_ai_enabled() is False  # a kill must survive a DB blip


def test_guardian_kill_persists_to_db_and_writes_no_files(rt):
    before = _digest()
    assert GuardianWatchdog()._set_ai_enabled(False, db=rt) is True
    assert runtime_state.is_ai_enabled() is False
    assert _digest() == before, "guardian must not rewrite settings.json / sop.md"
    assert GuardianWatchdog()._set_ai_enabled(True, db=rt) is True
    assert runtime_state.is_ai_enabled() is True


def test_triage_refuses_when_db_says_disabled(rt):
    from backend.services.triage import triage_email
    runtime_state.set_ai_enabled(rt, False)
    with pytest.raises(RuntimeError, match="AI is disabled"):
        triage_email(
            talent_key="x", talent_name="T", minimum_rate=1, subject="s",
            sender="a@b.com", sender_domain="b.com", body="b",
        )


def test_draft_reply_does_not_draft_when_db_says_disabled(rt):
    from backend.services import reply
    runtime_state.set_ai_enabled(rt, False)
    try:
        result = reply.draft_reply(
            talent_key="x", talent_name="Sylvia", minimum_rate=500, subject="s",
            sender="a@b.com", offer_type="post", brand_name="B", proposed_rate=100.0,
            triage_reason="r", db=None, body_text="hi",
        )
    except RuntimeError as exc:
        assert "AI is disabled" in str(exc)
        return
    assert result["is_escalate"] is True


# ── talent pause ─────────────────────────────────────────────────────────────

def test_pause_override_flows_into_every_profile_reader(rt):
    key = _a_talent_key()
    assert get_settings().talent_profiles[key].paused in (True, False)
    runtime_state.set_talent_paused(rt, key, True)
    assert get_settings().talent_profiles[key].paused is True
    assert key not in get_active_profiles(get_settings().talent_profiles)
    row = next(t for t in get_settings().talent_list if t["key"] == key)
    assert row["paused"] is True


def test_explicit_unpause_beats_paused_yes_in_sop_md(rt):
    key = _a_talent_key()
    runtime_state.set_talent_paused(rt, key, True)
    runtime_state.set_talent_paused(rt, key, False)
    assert get_settings().talent_profiles[key].paused is False
    assert key in get_active_profiles(get_settings().talent_profiles)


def test_pause_and_unpause_routes_round_trip_via_db(client, rt):
    key = _a_talent_key()
    before = _digest()
    r = client.post(f"/api/admin/guardian/pause-talent/{key.upper()}", json={"reason": "t"})
    assert r.status_code == 200, r.text
    assert get_settings().talent_profiles[key].paused is True
    r = client.post(f"/api/admin/guardian/unpause-talent/{key}", json={"reason": "t"})
    assert r.status_code == 200, r.text
    assert get_settings().talent_profiles[key].paused is False, "unpause used to be a no-op"
    assert _digest() == before


def test_pause_unknown_talent_is_404(client, rt):
    assert client.post("/api/admin/guardian/pause-talent/nobody-here", json={"reason": "t"}).status_code == 404
    assert client.post("/api/admin/guardian/unpause-talent/nobody-here", json={"reason": "t"}).status_code == 404


def test_guardian_pause_dispatch_uses_db(rt):
    key = _a_talent_key()
    GuardianWatchdog()._dispatch(
        rt, {"type": "talent_pause", "talent_key": key, "reason": "velocity", "detail": {}}, {}
    )
    assert get_settings().talent_profiles[key].paused is True


def test_disable_and_enable_ai_routes(client, rt):
    assert client.post("/api/admin/guardian/disable-ai", json={"reason": "t"}).status_code == 200
    assert runtime_state.is_ai_enabled() is False
    assert client.get("/api/admin/guardian/status").json()["ai_enabled"] is False
    assert client.post("/api/admin/guardian/enable-ai", json={"reason": "t"}).status_code == 200
    assert runtime_state.is_ai_enabled() is True
