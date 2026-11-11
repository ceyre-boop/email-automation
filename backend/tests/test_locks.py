"""Advisory-lock based mutual exclusion for jobs and polls."""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, patch

from backend.services import locks
from backend.services.locks import advisory_lock, lock_key, locked_job


def test_lock_keys_are_stable_distinct_signed_64bit():
    assert lock_key("job:poll") == lock_key("job:poll")
    assert lock_key("job:poll") != lock_key("job:draft_queue")
    for name in ("job:poll", "job:draft_queue", "poll:talent:sylvia", "poll:shared-inbox"):
        assert -(2**63) <= lock_key(name) < 2**63


def test_second_acquirer_is_refused_until_release():
    with advisory_lock("t1") as first:
        assert first is True
        with advisory_lock("t1") as second:
            assert second is False
        with advisory_lock("t2") as other:
            assert other is True, "different key must not be blocked"
    with advisory_lock("t1") as again:
        assert again is True


def test_lock_released_when_body_raises():
    try:
        with advisory_lock("t3"):
            raise ValueError("boom")
    except ValueError:
        pass
    with advisory_lock("t3") as got:
        assert got is True


def test_locked_job_skips_when_held_and_logs_info(caplog):
    calls = []

    @locked_job("unit")
    def job():
        calls.append(1)
        return "ran"

    with advisory_lock("job:unit"):
        with caplog.at_level(logging.INFO, logger="backend.services.locks"):
            assert job() is None
    assert calls == []
    assert any(r.levelno == logging.INFO and "skipped" in r.getMessage() for r in caplog.records)
    assert job() == "ran"
    assert calls == [1]


def test_locked_job_excludes_concurrent_threads():
    started, release = threading.Event(), threading.Event()
    results = []

    @locked_job("slow")
    def slow():
        started.set()
        release.wait(5)
        return "done"

    t = threading.Thread(target=lambda: results.append(slow()))
    t.start()
    assert started.wait(5)
    assert slow() is None  # second run while the first holds the lock
    release.set()
    t.join(5)
    assert results == ["done"]


# ── Postgres path (fake DBAPI so no server needed) ───────────────────────────

class _Conn:
    def __init__(self, grant: bool, log: list):
        self._grant, self._log, self.closed = grant, log, False

    def execution_options(self, **kw):
        self._log.append(("opts", kw))
        return self

    def execute(self, clause, params=None):
        sql = str(clause)
        self._log.append((sql, params))
        r = MagicMock()
        r.scalar.return_value = self._grant
        return r

    def close(self):
        self.closed = True


def _pg(monkeypatch, conn=None, raises=None):
    class _S:
        database_url = "postgresql://u:p@aws-0.pooler.supabase.com:6543/postgres"

    monkeypatch.setattr(locks, "get_settings", lambda: _S())
    eng = MagicMock()
    if raises:
        eng.connect.side_effect = raises
    else:
        eng.connect.return_value = conn
    monkeypatch.setattr(locks, "_get_lock_engine", lambda: eng)


def test_postgres_acquire_uses_try_lock_and_unlocks_and_closes(monkeypatch):
    log: list = []
    conn = _Conn(True, log)
    _pg(monkeypatch, conn)
    with advisory_lock("job:poll") as got:
        assert got is True
    sqls = [e[0] for e in log if e[0] != "opts"]
    assert "pg_try_advisory_lock" in sqls[0]
    assert "pg_advisory_unlock" in sqls[1]
    assert ("opts", {"isolation_level": "AUTOCOMMIT"}) in log
    assert conn.closed
    assert log[1][1] == {"k": lock_key("job:poll")} or log[2][1] == {"k": lock_key("job:poll")}


def test_postgres_lock_held_elsewhere_yields_false_and_closes(monkeypatch):
    log: list = []
    conn = _Conn(False, log)
    _pg(monkeypatch, conn)
    with advisory_lock("job:poll") as got:
        assert got is False
    assert conn.closed
    assert not any("pg_advisory_unlock" in str(e[0]) for e in log)


def test_postgres_unreachable_falls_back_to_local_lock_with_error(monkeypatch, caplog):
    _pg(monkeypatch, raises=RuntimeError("pooler down"))
    with caplog.at_level(logging.ERROR, logger="backend.services.locks"):
        with advisory_lock("job:fallback") as got:
            assert got is True
            with advisory_lock("job:fallback") as second:
                assert second is False  # still excluded within this process
    assert any(r.levelno == logging.ERROR and "unavailable" in r.getMessage() for r in caplog.records)


def test_session_mode_url_rewrites_only_the_transaction_pooler_port():
    pooled = "postgresql://u:p@aws-0-us.pooler.supabase.com:6543/postgres"
    assert locks._session_mode_url(pooled) == "postgresql://u:p@aws-0-us.pooler.supabase.com:5432/postgres"
    direct = "postgresql://u:p@db.abc.supabase.co:5432/postgres"
    assert locks._session_mode_url(direct) == direct
    other = "postgresql://u:p@db.example.com:6543/postgres"
    assert locks._session_mode_url(other) == other


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_every_scheduled_job_is_lock_wrapped():
    from backend.routers import cron
    for fn in (
        cron._run_poll, cron._run_draft_queue, cron._run_guardian, cron._run_auto_send,
        cron._run_reconcile, cron._run_inbox_reconcile, cron._run_proactive_refresh,
        cron._run_poll_health_retention, cron._run_stall_alarm, cron._run_claim_reaper,
        cron._run_retention,
    ):
        assert getattr(fn, "lock_name", "").startswith("job:"), fn.__name__
    assert not hasattr(cron, "_run_backlog_blaster")


def test_scheduler_registers_one_drafting_job_and_no_backlog_blaster(db_engine, monkeypatch):
    from backend import main
    from backend.models import db as m

    monkeypatch.setattr(m, "get_engine", lambda: db_engine)
    fake_scheduler = MagicMock()
    monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler", lambda *a, **k: fake_scheduler)
    main._startup_work()
    ids = [c.kwargs["id"] for c in fake_scheduler.add_job.call_args_list]
    assert "draft_queue" in ids
    assert "backlog_blaster" not in ids
    assert len(ids) == len(set(ids))


def test_poll_all_inboxes_takes_no_nested_advisory_locks():
    """job:poll serializes the cycle; nested per-inbox locks exhausted the pooler."""
    from backend.services import poller

    shared = MagicMock(email="talent-mgmt@taboost.me")
    with patch.object(poller, "get_shared_inbox_token", return_value=shared), \
         patch.object(poller, "_poll_single_inbox") as single, \
         patch.object(poller, "_poll_individual_tokens") as individual, \
         patch.object(locks, "advisory_lock") as adv:
        poller.poll_all_inboxes(MagicMock())
    single.assert_called_once()
    individual.assert_called_once()
    adv.assert_not_called()
    assert not hasattr(poller, "advisory_lock")


def test_per_talent_poll_skipped_when_in_process_lock_held():
    from backend.services import poller

    profile = MagicMock(key="Sylvia")
    with patch.object(poller, "_poll_one_talent_locked") as inner:
        lock = poller._talent_thread_lock("sylvia")
        assert lock.acquire(blocking=False)
        try:
            assert poller._poll_one_talent(1, profile, True) == {}
            inner.assert_not_called()
        finally:
            lock.release()
        poller._poll_one_talent(1, profile, True)
        inner.assert_called_once()
        assert lock.acquire(blocking=False), "lock must be released after the poll"
        lock.release()


def test_fail_closed_skips_when_lock_db_unreachable(monkeypatch, caplog):
    _pg(monkeypatch, raises=RuntimeError("pooler down"))
    with caplog.at_level(logging.ERROR, logger="backend.services.locks"):
        with advisory_lock("job:auto_send", fail_closed=True) as got:
            assert got is False
    assert any(r.levelno == logging.ERROR and "CLOSED" in r.getMessage() for r in caplog.records)


def test_fail_closed_jobs_do_not_run_when_lock_db_unreachable(monkeypatch):
    from backend.routers import cron

    _pg(monkeypatch, raises=RuntimeError("pooler down"))
    for fn in (cron._run_auto_send, cron._run_draft_queue):
        with patch("backend.services.auto_send.run_auto_send") as send, \
             patch.object(cron, "_run_draft_queue_inner") as inner:
            assert fn() is None
            send.assert_not_called()
            inner.assert_not_called()


def test_other_jobs_keep_in_process_fallback(monkeypatch):
    _pg(monkeypatch, raises=RuntimeError("pooler down"))
    calls = []

    @locked_job("tolerant")
    def job():
        calls.append(1)

    job()
    assert calls == [1]
