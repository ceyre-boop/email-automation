"""One failure must not become every failure.

2026-09-06: a single slow query aborted auto_send's transaction, and because the
per-talent handler never rolled back, all twelve remaining talents failed with
`InFailedSqlTransaction` — one real error, eleven fake ones, and no sends for
anybody that cycle.
"""
from unittest.mock import MagicMock, patch

from backend.services.auto_send import run_auto_send


def _settings(talents):
    s = MagicMock()
    s.app_config = {"auto_send_enabled": True, "auto_send_hold_minutes": 30}
    s.talent_profiles = {
        k: MagicMock(auto_send=True, paused=False) for k in talents
    }
    return s


def test_one_talent_failure_does_not_poison_the_rest(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []  # no stale claims
    monkeypatch.setattr("backend.services.auto_send.get_settings",
                        lambda: _settings(["Allee", "Angela", "Hana"]))

    calls = []

    def fake_process(_db, talent_key, _cutoff):
        calls.append(talent_key)
        if talent_key == "Allee":
            raise RuntimeError("current transaction is aborted")

    with patch("backend.services.auto_send._process_talent", side_effect=fake_process):
        run_auto_send(db)

    # Every talent was still attempted, and the session was cleaned up in between.
    assert calls == ["Allee", "Angela", "Hana"]
    assert db.rollback.call_count == 1


def test_an_unrecoverable_session_ends_the_cycle_instead_of_looping(monkeypatch):
    """If rollback itself fails the session is unusable — stop rather than emit
    one bogus error per remaining talent. The next scheduled run gets a fresh one."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.rollback.side_effect = RuntimeError("connection already closed")
    monkeypatch.setattr("backend.services.auto_send.get_settings",
                        lambda: _settings(["Allee", "Angela", "Hana"]))

    calls = []

    def fake_process(_db, talent_key, _cutoff):
        calls.append(talent_key)
        raise RuntimeError("current transaction is aborted")

    with patch("backend.services.auto_send._process_talent", side_effect=fake_process):
        run_auto_send(db)

    assert calls == ["Allee"]  # stopped after the unusable session
