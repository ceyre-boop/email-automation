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
    """Stray tokens are silently ignored in single-inbox mode — no 'Orphaned' warning.

    In the old per-talent polling loop, every token was inspected and a missing
    sop.md profile triggered an 'Orphaned' warning.  That concept doesn't apply
    to single-inbox mode: the poller never iterates talent tokens at all, so no
    individual token can be 'orphaned' by this path.
    """
    _add_token(db_session, "Sam")
    _run(poller_env, db_session, caplog)

    all_msgs = [r.getMessage() for r in caplog.records]
    assert not any("Orphaned" in m for m in all_msgs), \
        f"No orphan warning expected in single-inbox mode; got: {all_msgs}"


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
