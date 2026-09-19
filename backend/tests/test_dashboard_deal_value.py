"""Est. Daily Deal Value ("total_deal_value_today") accuracy.

Found 2026-09-18: this stat had been ~$0 essentially forever because
triage never populated ProcessedEmail.proposed_rate (see triage.py /
prompts/triage.md). The read-side query (GET /api/dashboard/report) was
already correct — it filters proposed_rate.isnot(None) and sums since the
12PM-Pacific rollover. These tests pin that read-side behavior with a
realistic mix of real rates, unknown (None) rates, and non-score-3 rows,
and document that the sum does NOT dedupe across talents sharing the same
brand/rate (see poller.py's shared-inbox vs partnerships-token routing —
each talent's copy of a brand email is a real, independent opportunity).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from backend.models.db import EmailStatus, ProcessedEmail
from backend.tests.conftest import make_token

_id_counter = itertools.count(1)


def _processed(db, talent_key, score, proposed_rate, hours_ago=1, gmail_message_id=None,
               brand_name="Nike"):
    row = ProcessedEmail(
        talent_key=talent_key,
        gmail_message_id=gmail_message_id or f"{talent_key}-{next(_id_counter):04d}",
        sender="brand@example.com",
        subject="Partnership",
        score=score,
        brand_name=brand_name,
        proposed_rate=proposed_rate,
        offer_type="Sponsored Post",
        status=EmailStatus.draft_saved if score == 3 else EmailStatus.flagged,
        processed_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )
    db.add(row)
    db.commit()
    return row


def test_deal_value_sums_only_score3_rows_with_a_real_rate(client, db_session):
    make_token(db_session, talent_key="Jocelyn")
    _processed(db_session, "Jocelyn", score=3, proposed_rate=850.0)
    _processed(db_session, "Jocelyn", score=3, proposed_rate=150.0)
    # None = no rate stated — must not count as $0 and must not error the sum.
    _processed(db_session, "Jocelyn", score=3, proposed_rate=None)
    # Score 1/2 rows must never contribute even if a rate is somehow present.
    _processed(db_session, "Jocelyn", score=2, proposed_rate=9999.0)
    _processed(db_session, "Jocelyn", score=1, proposed_rate=9999.0)

    resp = client.get("/api/dashboard/report")
    assert resp.status_code == 200
    assert resp.json()["total_deal_value_today"] == 1000.0


def test_deal_value_excludes_rows_before_todays_rollover(client, db_session):
    make_token(db_session, talent_key="Jocelyn")
    _processed(db_session, "Jocelyn", score=3, proposed_rate=500.0, hours_ago=1)
    _processed(db_session, "Jocelyn", score=3, proposed_rate=99999.0, hours_ago=48)

    resp = client.get("/api/dashboard/report")
    assert resp.json()["total_deal_value_today"] == 500.0


def test_deal_value_does_not_dedupe_the_same_brand_across_talents(client, db_session):
    """Documents the chosen semantics: the same brand/rate landing in two
    different talents' inboxes counts twice — each is a real, independent
    opportunity for that talent, not a duplicate of one shared budget."""
    make_token(db_session, talent_key="Allee")
    make_token(db_session, talent_key="Lizz")
    _processed(db_session, "Allee", score=3, proposed_rate=1000.0, brand_name="PiFi",
               gmail_message_id="allee-pifi")
    _processed(db_session, "Lizz", score=3, proposed_rate=1000.0, brand_name="PiFi",
               gmail_message_id="lizz-pifi")

    resp = client.get("/api/dashboard/report")
    assert resp.json()["total_deal_value_today"] == 2000.0


def test_deal_value_zero_when_nothing_scored_today(client, db_session):
    make_token(db_session, talent_key="Jocelyn")
    resp = client.get("/api/dashboard/report")
    assert resp.json()["total_deal_value_today"] == 0.0
