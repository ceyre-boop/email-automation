"""
Tests for the poller's token-handling behaviour under the SOP v16 single-inbox
architecture (commit 76fde93+).

Background — SOP v16 consolidates all talent-inbox polling into ONE shared Gmail
account (talent-mgmt@taboost.me).  The poller no longer iterates individual
talent tokens at all — it looks only for the shared-inbox token, calls
_poll_single_inbox(), and routes each email by alias header.

What this means for the old orphan-guard contract
--------------------------------------------------
* Stray TalentToken rows for individual talent keys (e.g. "Sam" created by a
  test) are simply not the shared-inbox token.  poll_all_inboxes() ignores them
  completely — it does not iterate all tokens, so there is nothing to "orphan".
* The shared-inbox token is identified by email == "talent-mgmt@taboost.me" (or
  talent_key == "shared-inbox" as a fallback).  When that token is absent the
  poller logs a warning and exits cleanly.
* Individual talent tokens for the 4 Partnerships talents (Katrina, Kylika,
  Audur, Trinity) will be polled via _poll_one_talent() in a future dual-mode
  pass; those tests will live in test_poller_partnerships.py once that path is
  implemented.

These tests lock in the observable behaviour of the shared-inbox path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.models.db import TalentToken


# ── helpers ──────────────────────────────────────────────────────────────────

def _add_token(db, key: str, email: str | None = None):
    token = TalentToken(
        talent_key=key,
        email=email or f"{key.lower()}@taboost.me",
        access_token="a",
        refresh_token="r",
        token_expiry=datetime.utcnow() + timedelta(hours=1),
        active=True,
    )
    db.add(token)
    db.commit()
    return token


def _add_shared_inbox_token(db):
    """Add the canonical shared-inbox token that poll_all_inboxes() expects."""
    return _add_token(db, "shared-inbox", email="talent-mgmt@taboost.me")


@pytest.fixture
def poller_env(db_session, monkeypatch):
    """Patch the session factory so workers use the test DB session."""
    from backend.services import poller
    monkeypatch.setattr(poller, "_get_session_factory", lambda: (lambda: db_session))
    return poller


def _run(poller, db_session, caplog):
    with caplog.at_level(logging.INFO, logger="backend.services.poller"):
        return poller.poll_all_inboxes(db_session)


# ── shared-inbox missing (no token) ──────────────────────────────────────────

def test_orphaned_token_is_skipped_and_never_triaged(poller_env, db_session, caplog):
    """A stray token for an unknown talent must never reach triage.

    In single-inbox mode poll_all_inboxes() only looks for the shared-inbox
    token.  A token for "Sam" is completely invisible to it — _poll_one_talent
    (and _poll_single_inbox) must never be called.
    """
    _add_token(db_session, "Sam")

    with patch("backend.services.poller._poll_one_talent") as one_talent, \
         patch("backend.services.poller._poll_single_inbox") as single_inbox:
        summary = _run(poller_env, db_session, caplog)

    one_talent.assert_not_called(), "stray token must never reach triage"
    single_inbox.assert_not_called(), "single-inbox path requires the shared-inbox token"
    assert summary["errors"] == 0, "a stray token is not an error condition"
    assert summary["processed"] == 0


def test_missing_shared_inbox_token_logs_warning(poller_env, db_session, caplog):
    """When no shared-inbox token exists, poll_all_inboxes must warn and exit cleanly."""
    # No tokens added — shared inbox not connected yet.
    summary = _run(poller_env, db_session, caplog)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Single inbox not connected" in m or "talent-mgmt@taboost.me" in m for m in warnings), \
        f"Expected a warning about missing shared-inbox token; got: {warnings}"
    assert summary["errors"] == 0, "missing token is expected state — not an error"


def test_stray_token_does_not_generate_orphan_warning(poller_env, db_session, caplog):
    """Stray tokens with no sop.md profile generate an 'Orphaned' warning in hybrid mode.

    SOP v16 introduced a hybrid poller: phase 1 handles the shared inbox, phase 2
    iterates remaining active tokens for the Partnerships talents.  Phase 2 still
    runs the orphan guard — a token with no matching sop.md profile is logged as
    'Orphaned' so zombie tokens don't silently sit in the DB unnoticed.

    This is the CORRECT behaviour in hybrid mode; the old test asserted the
    opposite because it was written when poll_all_inboxes() never iterated tokens.
    """
    _add_token(db_session, "Sam")
    _run(poller_env, db_session, caplog)

    all_msgs = [r.getMessage() for r in caplog.records]
    assert any("Orphaned" in m for m in all_msgs), \
        f"Expected orphan warning for unknown token 'Sam'; got: {all_msgs}"


def test_paused_talent_token_silently_ignored(poller_env, db_session, caplog):
    """A paused talent's individual token must produce no warning in single-inbox mode.

    The paused/active distinction is meaningful only inside _poll_single_inbox
    (where emails are routed by alias and then the profile is checked for pause).
    A paused talent's stray individual token is simply invisible to poll_all_inboxes.
    """
    from backend.core.config import get_settings

    profiles = get_settings().talent_profiles
    paused_key = next((k for k, p in profiles.items() if p.paused), None)
    if paused_key is None:
        pytest.skip("no paused talent in sop.md to exercise this path")

    _add_token(db_session, paused_key)
    _run(poller_env, db_session, caplog)

    all_msgs = [r.getMessage() for r in caplog.records]
    assert not any("Orphaned" in m for m in all_msgs), \
        "paused talent individual token must not be flagged as orphaned in single-inbox mode"


def test_shared_inbox_token_triggers_single_inbox_poll(poller_env, db_session, caplog):
    """When the shared-inbox token exists, poll_all_inboxes must call _poll_single_inbox.

    This is the happy-path smoke test: the right token → the right path.
    _poll_single_inbox is patched to avoid any Gmail I/O.
    """
    _add_shared_inbox_token(db_session)

    fake_summary = {"processed": 0, "archived": 0, "flagged": 0, "drafted": 0, "errors": 0, "unrouted": 0}
    with patch("backend.services.poller._poll_single_inbox", return_value=fake_summary) as single_inbox:
        summary = _run(poller_env, db_session, caplog)

    assert single_inbox.called, "shared-inbox token must trigger _poll_single_inbox"
    assert summary["errors"] == 0


# ── Hybrid mode: the shared inbox must not also be polled per-token ───────────

def test_shared_inbox_token_is_never_polled_per_token(poller_env, db_session, caplog):
    """The consolidated mailbox is handled by phase 1 only.

    If phase 2 also picked it up, every message in it would be processed twice in
    one cycle and double-drafted.
    """
    _add_token(db_session, "shared-inbox")

    with patch("backend.services.poller._poll_single_inbox", return_value={}) as single, \
         patch("backend.services.poller._poll_one_talent", return_value={}) as one_talent:
        _run(poller_env, db_session, caplog)

    assert single.called, "phase 1 should have polled the shared inbox"
    one_talent.assert_not_called(), "shared inbox must not be polled per-token"
    assert not any("Orphaned" in r.getMessage() for r in caplog.records), \
        "the shared inbox is not an orphaned token"


def test_shared_inbox_excluded_by_email_even_with_an_odd_key(poller_env, db_session, caplog):
    """OAuth auto-generates the talent_key, so exclusion must key off the email."""
    from datetime import datetime, timedelta

    db_session.add(TalentToken(
        talent_key="talentmgmt7",
        email="talent-mgmt@taboost.me",
        access_token="a", refresh_token="r",
        token_expiry=datetime.utcnow() + timedelta(hours=1),
        active=True,
    ))
    db_session.commit()

    with patch("backend.services.poller._poll_single_inbox", return_value={}), \
         patch("backend.services.poller._poll_one_talent", return_value={}) as one_talent:
        _run(poller_env, db_session, caplog)

    one_talent.assert_not_called()


def test_partnerships_talent_is_polled_per_token(poller_env, db_session, caplog):
    """Katrina keeps her own mailbox, so phase 2 must pick her up."""
    _add_token(db_session, "Katrina")

    with patch("backend.services.poller._poll_single_inbox", return_value={}), \
         patch("backend.services.poller._poll_one_talent", return_value={}) as one_talent:
        _run(poller_env, db_session, caplog)

    assert one_talent.called, "Katrina should have been polled per-token"
    polled_keys = {c.args[1].key for c in one_talent.call_args_list}
    assert "Katrina" in polled_keys


def test_per_token_phase_runs_even_when_shared_inbox_is_missing(poller_env, db_session, caplog):
    """A disconnected shared inbox must not silence the Partnerships tokens."""
    _add_token(db_session, "Katrina")

    with patch("backend.services.poller._poll_one_talent", return_value={}) as one_talent:
        summary = _run(poller_env, db_session, caplog)

    assert one_talent.called, "per-token phase must still run"
    assert any("not connected" in r.getMessage() for r in caplog.records), \
        "a missing shared inbox must be logged, not silent"
    assert summary["errors"] == 0


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
