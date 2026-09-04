"""Guardian's global draft-velocity kill.

On 2026-09-04 a missing index stalled drafting for hours. The moment the index
landed, the backlog flushed 200 drafts in 10 minutes — one draft per queued
email, entirely legitimate — and this guard killed AI four times in a row,
taking the system offline right after it recovered.

Volume alone cannot distinguish the two cases. A runaway loop drafts repeatedly
for the same email (ratio climbs); catch-up drafts once per queued email (ratio
stays ~1). These tests pin that distinction.
"""
from datetime import datetime, timedelta

from backend.models.db import Draft, DraftStatus, ProcessedEmail
from backend.services.guardian import GuardianWatchdog

CFG = {
    "velocity_window_minutes": 10,
    "global_draft_hard_limit": 500,
    "per_talent_draft_hard_limit": 160,
    "per_talent_draft_warn_limit": 120,
    "draft_email_ratio_warn": 3.0,
}


def _seed(db, drafts: int, emails: int):
    now = datetime.utcnow()
    for i in range(drafts):
        db.add(Draft(
            talent_key="Allee", gmail_message_id=f"d{i}", status=DraftStatus.pending,
            draft_text="rates", created_at=now - timedelta(minutes=1),
        ))
    for i in range(emails):
        db.add(ProcessedEmail(
            talent_key="Allee", gmail_message_id=f"e{i}", score=3,
            processed_at=now - timedelta(minutes=1),
        ))
    db.commit()


def test_backlog_flush_does_not_kill_ai(db_session):
    """520 drafts backed by 520 emails is catch-up — warn, never kill."""
    _seed(db_session, drafts=520, emails=520)
    w = GuardianWatchdog()
    by_talent, total = w._compute_velocity(db_session, CFG)
    triggers = w._check_draft_velocity(db_session, CFG, by_talent, total)

    assert [t["type"] for t in triggers] == ["ratio_warn"]
    assert "backlog catch-up" in triggers[0]["reason"]
    assert triggers[0]["detail"]["drafts_per_email"] < 3.0


def test_runaway_loop_still_kills_ai(db_session):
    """520 drafts from 20 emails is 26x — a loop drafting the same mail repeatedly."""
    _seed(db_session, drafts=520, emails=20)
    w = GuardianWatchdog()
    by_talent, total = w._compute_velocity(db_session, CFG)
    triggers = w._check_draft_velocity(db_session, CFG, by_talent, total)

    assert [t["type"] for t in triggers] == ["global_kill"]
    assert "not backed by inbound mail" in triggers[0]["reason"]


def test_volume_under_the_limit_is_ignored_entirely(db_session):
    _seed(db_session, drafts=100, emails=100)
    w = GuardianWatchdog()
    by_talent, total = w._compute_velocity(db_session, CFG)
    triggers = w._check_draft_velocity(db_session, CFG, by_talent, total)

    assert not any(t["type"] == "global_kill" for t in triggers)


def test_the_real_2026_09_04_burst_would_not_have_killed_ai(db_session):
    """The exact production numbers from the incident: 200 drafts, 200 emails."""
    _seed(db_session, drafts=200, emails=200)
    w = GuardianWatchdog()
    by_talent, total = w._compute_velocity(db_session, CFG)
    triggers = w._check_draft_velocity(db_session, CFG, by_talent, total)

    assert not any(t["type"] == "global_kill" for t in triggers)
