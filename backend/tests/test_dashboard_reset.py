from __future__ import annotations

from datetime import datetime, timedelta

from backend.models.db import AppState, Draft, DraftStatus, EmailStatus, InboxEmail, ProcessedEmail
from backend.tests.conftest import make_draft, make_token


def test_reset_badges_clears_dashboard_counts(client, db_session):
    make_token(db_session, talent_key="Sylvia")
    draft = make_draft(db_session, talent_key="Sylvia")
    processed = ProcessedEmail(
        talent_key="Sylvia",
        gmail_message_id="processed-msg-001",
        sender="brand@nike.com",
        subject="Partnership",
        score=3,
        brand_name="Nike",
        proposed_rate=3500.0,
        offer_type="Sponsored Post",
        status=EmailStatus.draft_saved,
        processed_at=datetime.utcnow() - timedelta(hours=1),
    )
    inbox = InboxEmail(
        talent_key="sylvia",
        gmail_message_id="inbox-msg-001",
        sender="brand@nike.com",
        subject="Partnership",
        score=3,
        brand_name="Nike",
        proposed_rate=3500.0,
        offer_type="Sponsored Post",
        triage_reason="Looks good",
        triage_status="draft_saved",
    )
    db_session.add_all([processed, inbox])
    db_session.commit()

    resp = client.post("/api/dashboard/reset-badges")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["discarded_drafts"] == 1
    assert data["cleared_inbox_badges"] == 1

    db_session.refresh(draft)
    db_session.refresh(inbox)
    assert draft.status == DraftStatus.discarded
    assert draft.reviewed_by == "dashboard-reset"
    assert draft.reviewed_at is not None
    assert inbox.score is None
    assert inbox.brand_name is None
    assert inbox.proposed_rate is None
    assert inbox.offer_type is None
    assert inbox.triage_reason is None
    assert inbox.triage_status is None

    reset_row = db_session.query(AppState).filter(AppState.key == "dashboard_reset_started_at").first()
    assert reset_row is not None
    assert reset_row.value_text

    report = client.get("/api/dashboard/report")
    assert report.status_code == 200
    sylvia = next(t for t in report.json()["talents"] if t["talent_key"] == "Sylvia")
    assert report.json()["total_emails"] == 0
    assert sylvia["count_good"] == 0
    assert sylvia["pending_drafts"] == 0
    assert sylvia["pending_real_drafts"] == 0

    drafts = client.get("/api/dashboard/talents/Sylvia/drafts")
    assert drafts.status_code == 200
    assert drafts.json() == []


def test_report_counts_new_activity_after_reset(client, db_session):
    make_token(db_session, talent_key="Sylvia")

    reset_resp = client.post("/api/dashboard/reset-badges")
    assert reset_resp.status_code == 200
    reset_at = datetime.fromisoformat(reset_resp.json()["reset_at"])

    processed = ProcessedEmail(
        talent_key="Sylvia",
        gmail_message_id="processed-msg-after-reset",
        sender="brand@adidas.com",
        subject="Fresh deal",
        score=3,
        brand_name="Adidas",
        proposed_rate=4200.0,
        offer_type="Sponsored Post",
        status=EmailStatus.draft_saved,
        processed_at=reset_at + timedelta(seconds=1),
    )
    draft = Draft(
        talent_key="Sylvia",
        gmail_message_id="draft-msg-after-reset",
        sender="brand@adidas.com",
        subject="Fresh deal",
        brand_name="Adidas",
        proposed_rate=4200.0,
        offer_type="Sponsored Post",
        draft_text="Draft reply",
        status=DraftStatus.pending,
        is_escalate=False,
        created_at=reset_at + timedelta(seconds=1),
    )
    db_session.add_all([processed, draft])
    db_session.commit()

    report = client.get("/api/dashboard/report")
    assert report.status_code == 200
    data = report.json()
    sylvia = next(t for t in data["talents"] if t["talent_key"] == "Sylvia")
    assert data["total_good"] == 1
    assert data["total_emails"] == 1
    assert sylvia["count_good"] == 1
    assert sylvia["pending_drafts"] == 1
    assert sylvia["pending_real_drafts"] == 1
