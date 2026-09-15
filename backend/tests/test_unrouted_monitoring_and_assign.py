"""Closing the remaining gap in the 2026-09-14 thread-continuity fix.

The fallback self-heals every reply in a thread that ever resolved — but the
FIRST message in a brand-new negotiation has no prior resolution to fall back
to, so a routing failure there stays UNROUTED forever with no automated fix.
That silently costs closed deals, which is why it needs both an alarm (someone
notices fast) and a manual escape hatch (someone can actually fix it).
"""
from datetime import datetime, timedelta

from backend.models.db import ProcessedEmail
from backend.services.stall_alarm import check_pipeline_stall


def _unrouted(db, n, hours_ago=1):
    for i in range(n):
        db.add(ProcessedEmail(
            talent_key="UNROUTED", gmail_message_id=f"u{i}-{hours_ago}", score=0,
            status="flagged", processed_at=datetime.utcnow() - timedelta(hours=hours_ago),
        ))
    db.commit()


def test_unrouted_volume_is_reported_even_when_the_pipeline_looks_healthy(db_session):
    """A routing regression does not stop polling/triage/drafting — every OTHER
    metric here stays green while real brand mail quietly misses its talent."""
    _unrouted(db_session, 5)
    result = check_pipeline_stall(db_session)
    assert result["unrouted_last_24h"] == 5
    assert result["stalled"] is False  # under the default warn threshold


def test_unrouted_spike_trips_the_alarm(db_session):
    _unrouted(db_session, 20)
    result = check_pipeline_stall(db_session)
    assert result["stalled"] is True
    assert "ROUTING REGRESSION" in result["reason"]
    assert "20" in result["reason"]


def test_old_unrouted_mail_outside_the_24h_window_does_not_count(db_session):
    _unrouted(db_session, 50, hours_ago=48)
    result = check_pipeline_stall(db_session)
    assert result["unrouted_last_24h"] == 0
    assert result["stalled"] is False


def test_assign_moves_the_email_off_the_unrouted_bucket(client, db_session):
    db_session.add(ProcessedEmail(
        talent_key="UNROUTED", gmail_message_id="m1", thread_id="t1", score=0,
        status="flagged", triage_reason="No alias match — to_address=None",
        processed_at=datetime.utcnow(),
    ))
    db_session.commit()

    resp = client.post("/api/dashboard/admin/unrouted/m1/assign?talent_key=Allee")
    assert resp.status_code == 200, resp.text

    row = db_session.query(ProcessedEmail).filter_by(gmail_message_id="m1").one()
    assert row.talent_key == "Allee"


def test_assigning_the_root_unlocks_the_whole_thread_for_the_poller_fallback(client, db_session):
    """This is the actual point: fixing message 1 must make the existing
    thread-continuity fallback pick up every later reply automatically."""
    db_session.add(ProcessedEmail(
        talent_key="UNROUTED", gmail_message_id="m1", thread_id="t1", score=0,
        status="flagged", processed_at=datetime.utcnow() - timedelta(hours=1),
    ))
    db_session.commit()

    client.post("/api/dashboard/admin/unrouted/m1/assign?talent_key=Allee")

    prior = (
        db_session.query(ProcessedEmail.talent_key)
        .filter(ProcessedEmail.thread_id == "t1", ProcessedEmail.talent_key != "UNROUTED")
        .first()
    )
    assert prior is not None and prior[0] == "Allee"


def test_assign_rejects_an_inactive_or_unknown_talent(client, db_session):
    db_session.add(ProcessedEmail(
        talent_key="UNROUTED", gmail_message_id="m2", thread_id="t2", score=0,
        status="flagged", processed_at=datetime.utcnow(),
    ))
    db_session.commit()

    resp = client.post("/api/dashboard/admin/unrouted/m2/assign?talent_key=NotARealTalent")
    assert resp.status_code == 400
    assert db_session.query(ProcessedEmail).filter_by(gmail_message_id="m2").one().talent_key == "UNROUTED"


def test_assign_404s_on_an_already_resolved_email(client, db_session):
    db_session.add(ProcessedEmail(
        talent_key="Allee", gmail_message_id="m3", thread_id="t3", score=3,
        status="sent", processed_at=datetime.utcnow(),
    ))
    db_session.commit()

    resp = client.post("/api/dashboard/admin/unrouted/m3/assign?talent_key=Allee")
    assert resp.status_code == 404
