"""Required migrations are applied idempotently and schema drift is loud."""
from __future__ import annotations

import logging

from backend.models import db as m


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConn:
    def __init__(self, already_applied: set[str], executed: list[str]):
        self._already = already_applied
        self._executed = executed

    def execution_options(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, clause):
        sql = str(clause)
        self._executed.append(sql)
        if sql.startswith("SELECT"):
            hit = any(label_sql == sql for label_sql in self._already)
            return _FakeResult((1,) if hit else None)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn

    def dispose(self):
        pass


def _patch_pg(monkeypatch, already: set[str], executed: list[str]):
    class _S:
        database_url = "postgresql://u:p@localhost/db"

    monkeypatch.setattr(m, "get_settings", lambda: _S())
    monkeypatch.setattr(m, "create_engine", lambda *a, **k: _FakeEngine(_FakeConn(already, executed)))


def test_required_migrations_cover_retry_logic():
    ddl = " ".join(d for _, _, d in m.REQUIRED_MIGRATIONS)
    assert "ADD VALUE IF NOT EXISTS 'retry_pending'" in ddl
    assert "ADD COLUMN IF NOT EXISTS triage_retry_count" in ddl
    assert "ADD COLUMN IF NOT EXISTS triage_next_retry_at" in ddl
    for _, _, d in m.REQUIRED_MIGRATIONS:
        up = d.upper()
        assert "DROP" not in up and "DELETE" not in up, "required migrations must be additive only"
        assert "IF NOT EXISTS" in up


def test_apply_runs_every_missing_statement(monkeypatch):
    executed: list[str] = []
    _patch_pg(monkeypatch, already=set(), executed=executed)
    report = m.apply_required_migrations()
    assert len(report["applied"]) == len(m.REQUIRED_MIGRATIONS)
    assert report["failed"] == []
    for _, _, ddl in m.REQUIRED_MIGRATIONS:
        assert ddl in executed


def test_apply_is_idempotent_and_runs_no_ddl_when_current(monkeypatch):
    executed: list[str] = []
    _patch_pg(monkeypatch, already={chk for _, chk, _ in m.REQUIRED_MIGRATIONS}, executed=executed)
    report = m.apply_required_migrations()
    assert report["applied"] == []
    assert len(report["already"]) == len(m.REQUIRED_MIGRATIONS)
    assert all(sql.startswith("SELECT") for sql in executed), "no DDL (and no table lock) when already applied"


def test_apply_reports_failures_instead_of_raising(monkeypatch):
    class _Boom(_FakeConn):
        def execute(self, clause):
            raise RuntimeError("lock timeout")

    class _S:
        database_url = "postgresql://u:p@localhost/db"

    monkeypatch.setattr(m, "get_settings", lambda: _S())
    monkeypatch.setattr(m, "create_engine", lambda *a, **k: _FakeEngine(_Boom(set(), [])))
    report = m.apply_required_migrations()
    assert len(report["failed"]) == len(m.REQUIRED_MIGRATIONS)


def test_apply_is_noop_on_sqlite():
    assert m.apply_required_migrations() == {"applied": [], "already": [], "failed": []}


def test_schema_mismatch_logged_at_error_and_exposed_on_health(db_engine, monkeypatch, caplog, client):
    m.Base.metadata.tables["sop_versions"].drop(bind=db_engine, checkfirst=True)
    monkeypatch.setattr(m, "get_engine", lambda: db_engine)
    with caplog.at_level(logging.ERROR, logger="backend.models.db"):
        report = m.verify_schema_matches_models()
    assert report["ok"] is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("sop_versions" in r.getMessage() for r in errors)
    assert any("SCHEMA MISMATCH" in r.getMessage() for r in errors)

    body = client.get("/health").json()
    assert body["schema_ok"] is False
    assert "sop_versions" in body["schema"]["missing_tables"]


def test_schema_ok_true_when_models_match(db_engine, monkeypatch, client):
    monkeypatch.setattr(m, "get_engine", lambda: db_engine)
    m.verify_schema_matches_models()
    assert client.get("/health").json()["schema_ok"] is True
