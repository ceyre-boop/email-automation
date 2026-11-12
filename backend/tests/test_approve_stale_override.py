"""Stale send claims block approve unless a human explicitly overrides (2026-11-12)."""
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.models.db import Draft, DraftStatus


def _stale_draft(db, minutes=60):
    d = Draft(talent_key="Allee", gmail_message_id=f"g{datetime.utcnow().timestamp()}",
              thread_id="t1", sender="brand@x.com", subject="Collab", draft_text="hi",
              status=DraftStatus.pending, send_claimed_at=datetime.utcnow() - timedelta(minutes=minutes))
    db.add(d); db.commit()
    return d


def _client(db_session):
    from backend.main import app
    from backend.routers.deps import get_db, verify_api_key
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[verify_api_key] = lambda: None
    return app, TestClient(app)


def _patches():
    return (patch("backend.routers.drafts._get_token_or_404", return_value=object()),
            patch("backend.services.validation.run_pre_send_checks", return_value=(True, None)),
            patch("backend.routers.drafts.gmail_svc.send_reply", return_value=(True, None)),
            patch("backend.routers.drafts.gmail_svc.mark_initial_response_sent"))


def test_stale_claim_blocks_plain_approve(db_session):
    d = _stale_draft(db_session)
    app, c = _client(db_session)
    p = _patches()
    with p[0], p[1], p[2] as send, p[3]:
        r = c.post(f"/api/drafts/{d.id}/approve", json={})
    app.dependency_overrides.clear()
    assert r.status_code == 409 and send.call_count == 0


def test_override_sends_stale_claimed_draft(db_session):
    d = _stale_draft(db_session)
    app, c = _client(db_session)
    p = _patches()
    with p[0], p[1], p[2] as send, p[3]:
        r = c.post(f"/api/drafts/{d.id}/approve", json={"override_stale_claim": True, "reviewed_by": "colin"})
    app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    assert send.call_count == 1
    db_session.refresh(d)
    assert d.status == DraftStatus.sent


def test_override_does_not_break_a_fresh_claim(db_session):
    d = _stale_draft(db_session, minutes=1)
    app, c = _client(db_session)
    p = _patches()
    with p[0], p[1], p[2] as send, p[3]:
        r = c.post(f"/api/drafts/{d.id}/approve", json={"override_stale_claim": True})
    app.dependency_overrides.clear()
    assert r.status_code == 409 and send.call_count == 0
