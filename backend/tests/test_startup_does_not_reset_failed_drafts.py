"""Boot must never rewrite draft state: a validation_failed draft stays failed."""
from __future__ import annotations

from unittest.mock import MagicMock

from backend.models import db as m
from backend.models.db import Draft
from backend.tests.conftest import make_draft


def test_startup_leaves_validation_failed_drafts_failed(db_engine, db_session, monkeypatch):
    draft = make_draft(db_session)
    draft.validation_failed = True
    draft.validation_error = "placeholder text left in body"
    db_session.commit()

    monkeypatch.setattr(m, "get_engine", lambda: db_engine)
    # Don't start a real scheduler from inside the test.
    monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler", MagicMock())

    from backend import main
    main._startup_work()

    db_session.expire_all()
    row = db_session.get(Draft, draft.id)
    assert row.validation_failed is True
    assert row.validation_error == "placeholder text left in body"
