"""
Tests for the poller's handling of Gmail tokens with no sop.md profile.

Background: an active TalentToken exists for "Sam" with no matching profile in
sheets/sop.md. The poller must skip it — attempting triage would produce a draft
with no approved response to validate against, which the send-time verbatim gate
would then reject anyway.

The guard already existed but was untested, and it could not distinguish a
*paused* talent from a genuinely *orphaned* token: both fell through the same
branch and logged the same "no profile" warning. These tests lock in both the
skip and the distinction.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from backend.models.db import TalentToken


def _add_token(db, key: str):
    from datetime import datetime, timedelta

    token = TalentToken(
        talent_key=key,
        email=f"{key.lower()}@taboost.me",
        access_token="a",
        refresh_token="r",
        token_expiry=datetime.utcnow() + timedelta(hours=1),
        active=True,
    )
    db.add(token)
    db.commit()
    return token


@pytest.fixture
def poller_env(db_session, monkeypatch):
    """Run poll_all_inboxes against the test DB without touching Gmail."""
    from backend.services import poller

    monkeypatch.setattr(poller, "_get_session_factory", lambda: (lambda: db_session))
    return poller


def _run(poller, db_session, caplog):
    """Invoke the poll loop, capturing logs. Gmail is never reached because the
    only tokens present have no profile, so no job is ever submitted."""
    with caplog.at_level(logging.INFO, logger="backend.services.poller"):
        return poller.poll_all_inboxes(db_session)


def test_orphaned_token_is_skipped_and_never_triaged(poller_env, db_session, caplog):
    """A token with no sop.md profile must be skipped before any triage call."""
    _add_token(db_session, "Sam")

    with patch("backend.services.poller._poll_one_talent") as one_talent:
        summary = _run(poller_env, db_session, caplog)

    one_talent.assert_not_called(), "orphaned token must never reach triage"
    assert summary["errors"] == 0, "an orphaned token is not an error condition"
    assert summary["processed"] == 0


def test_orphaned_token_logs_an_actionable_warning(poller_env, db_session, caplog):
    """The warning must name the talent and say the inbox is not being processed."""
    _add_token(db_session, "Sam")

    with patch("backend.services.poller._poll_one_talent"):
        _run(poller_env, db_session, caplog)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Sam" in m and "Orphaned" in m for m in warnings), warnings
    assert any("NOT being processed" in m for m in warnings), warnings


def test_paused_talent_is_not_reported_as_orphaned(poller_env, db_session, caplog):
    """A paused talent has a profile — it must not produce an 'orphaned' warning.

    Both cases skip the talent, but conflating them makes a routine pause look
    like a broken configuration and buries real orphans in the noise.
    """
    from backend.core.config import get_settings

    profiles = get_settings().talent_profiles
    paused_key = next((k for k, p in profiles.items() if p.paused), None)
    if paused_key is None:
        pytest.skip("no paused talent in sop.md to exercise this path")

    _add_token(db_session, paused_key)

    with patch("backend.services.poller._poll_one_talent") as one_talent:
        _run(poller_env, db_session, caplog)

    one_talent.assert_not_called()
    messages = [r.getMessage() for r in caplog.records]
    assert any("paused in sop.md" in m for m in messages), messages
    assert not any("Orphaned" in m for m in messages), "paused talent flagged as orphaned"


def test_known_active_talent_is_still_polled(poller_env, db_session, caplog):
    """Guard must not over-reach: a real, unpaused talent still gets processed."""
    from backend.core.config import get_settings

    profiles = get_settings().talent_profiles
    active_key = next((k for k, p in profiles.items() if not p.paused), None)
    assert active_key, "sop.md should contain at least one active talent"

    _add_token(db_session, active_key)

    with patch("backend.services.poller._poll_one_talent", return_value={}) as one_talent:
        _run(poller_env, db_session, caplog)

    assert one_talent.called, f"{active_key} should have been polled"


# ── Schema drift detection ────────────────────────────────────────────────────

def test_schema_check_reports_missing_table(db_engine, monkeypatch):
    """A table in the models but not in the DB must be reported, not silently ignored.

    SKIP_MIGRATIONS=true means create_tables() never runs in production, so a
    new table simply never appears — sop_versions was missing for its entire
    life and only surfaced as a 500. This check makes that visible at boot.
    """
    from sqlalchemy import inspect
    from backend.models import db as m

    m.Base.metadata.tables["sop_versions"].drop(bind=db_engine, checkfirst=True)
    monkeypatch.setattr(m, "get_engine", lambda: db_engine)

    report = m.verify_schema_matches_models()
    assert "sop_versions" in report["missing_tables"]
    assert "sop_versions" not in inspect(db_engine).get_table_names(), "check must not create it"


def test_schema_check_is_clean_when_db_matches(db_engine, monkeypatch):
    from backend.models import db as m

    monkeypatch.setattr(m, "get_engine", lambda: db_engine)
    report = m.verify_schema_matches_models()
    assert report == {"missing_tables": [], "missing_columns": []}


def test_schema_check_runs_no_ddl(db_engine, monkeypatch):
    """It must never repair anything — applying DDL is what SKIP_MIGRATIONS forbids."""
    from sqlalchemy import inspect
    from backend.models import db as m

    m.Base.metadata.tables["sop_versions"].drop(bind=db_engine, checkfirst=True)
    monkeypatch.setattr(m, "get_engine", lambda: db_engine)

    before = set(inspect(db_engine).get_table_names())
    m.verify_schema_matches_models()
    m.verify_schema_matches_models()
    assert set(inspect(db_engine).get_table_names()) == before
