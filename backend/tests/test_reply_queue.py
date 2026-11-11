"""Reply queue — brand replies still waiting on a human, grouped by manager.

Built after 2026-10-22, when the data showed ~50 brand replies a day (answers to
our own rate quotes) being left for human review with no owner and no list.
These tests pin what counts as "waiting" and, as importantly, what must not.
"""
from datetime import datetime, timedelta

from backend.models.db import Draft, DraftStatus, EmailStatus, InboxEmail, ProcessedEmail
from backend.services.reply_queue import build_reply_queue, render_text
from backend.tests.conftest import make_token

NOW = datetime(2026, 10, 22, 12, 0, 0)


def _flagged(db, mid, key="Allee", sender="Ann <ann@brand.com>", subject="Re: Collab", days_ago=3,
             domain="brand.com", score=2):
    row = ProcessedEmail(
        talent_key=key, gmail_message_id=mid, sender=sender, subject=subject, score=score,
        status=EmailStatus.flagged, sender_domain=domain, brand_name="Brand",
        processed_at=NOW - timedelta(days=days_ago), email_date=NOW - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _sent_draft(db, sender="Ann <ann@brand.com>", key="Allee"):
    db.add(Draft(talent_key=key, gmail_message_id=f"orig-{sender}", sender=sender,
                 status=DraftStatus.sent, draft_text="rates"))
    db.commit()


def _ids(queue):
    return {i["gmail_message_id"] for m in queue["managers"] for i in m["items"]}


def test_reply_still_in_inbox_is_listed_under_its_manager(db_session):
    _flagged(db_session, "m1")
    q = build_reply_queue(db_session, shared_inbox_ids={"m1"}, now=NOW)
    assert q["total"] == 1
    assert q["managers"][0]["manager"] == "Chenni Li"
    assert q["inbox_verified"] is True


def test_reply_no_longer_in_inbox_drops_off(db_session):
    _flagged(db_session, "m1")
    q = build_reply_queue(db_session, shared_inbox_ids=set(), now=NOW)
    assert q["total"] == 0


def test_cold_pitch_is_not_a_reply_and_is_excluded(db_session):
    _flagged(db_session, "m1", subject="Collab idea for you", sender="Zed <zed@cold.com>", domain="cold.com")
    q = build_reply_queue(db_session, shared_inbox_ids={"m1"}, now=NOW)
    assert q["total"] == 0


def test_non_reply_subject_from_a_brand_we_quoted_is_included(db_session):
    _sent_draft(db_session)
    _flagged(db_session, "m1", subject="Quick question about your rates")
    q = build_reply_queue(db_session, shared_inbox_ids={"m1"}, now=NOW)
    assert q["total"] == 1 and q["managers"][0]["items"][0]["we_quoted_them"] is True


def test_quoted_brands_sort_before_other_replies_then_oldest_first(db_session):
    _sent_draft(db_session, sender="Quoted <q@quoted.com>")
    _flagged(db_session, "plain-old", sender="X <x@x.com>", domain="x.com", days_ago=9)
    _flagged(db_session, "quoted-new", sender="Quoted <q@quoted.com>", domain="quoted.com", days_ago=1)
    _flagged(db_session, "plain-new", sender="Y <y@y.com>", domain="y.com", days_ago=2)
    q = build_reply_queue(db_session, shared_inbox_ids={"plain-old", "quoted-new", "plain-new"}, now=NOW)
    order = [i["gmail_message_id"] for i in q["managers"][0]["items"]]
    assert order == ["quoted-new", "plain-old", "plain-new"]


def test_repeat_client_domains_are_excluded(db_session):
    for i, dom in enumerate(["taboost.me", "favored.live", "nextwave-talent.com"]):
        _flagged(db_session, f"rc{i}", sender=f"A <a@{dom}>", domain=dom)
    q = build_reply_queue(db_session, shared_inbox_ids={"rc0", "rc1", "rc2"}, now=NOW)
    assert q["total"] == 0


def test_bounce_notifications_are_excluded(db_session):
    _flagged(db_session, "n1", key="SYSTEM_NOTIFICATION", subject="Re: Delivery Status Notification")
    q = build_reply_queue(db_session, shared_inbox_ids={"n1"}, now=NOW)
    assert q["total"] == 0


def test_unrouted_goes_to_unassigned_and_sorts_last(db_session):
    _flagged(db_session, "u1", key="UNROUTED")
    _flagged(db_session, "a1", key="Allee", sender="B <b@b.com>", domain="b.com")
    q = build_reply_queue(db_session, shared_inbox_ids={"u1", "a1"}, now=NOW)
    assert [m["manager"] for m in q["managers"]] == ["Chenni Li", "Unassigned"]


def test_individual_talent_hides_only_mail_the_mirror_shows_out_of_inbox(db_session):
    make_token(db_session, talent_key="Katrina", email="katrina@taboost.me")
    _flagged(db_session, "k-in", key="Katrina")
    _flagged(db_session, "k-out", key="Katrina", sender="C <c@c.com>", domain="c.com")
    db_session.add(InboxEmail(talent_key="katrina", gmail_message_id="k-in", label_ids="INBOX,UNREAD", is_unread=True))
    db_session.add(InboxEmail(talent_key="katrina", gmail_message_id="k-out", label_ids="CATEGORY_UPDATES", is_unread=False))
    db_session.commit()
    q = build_reply_queue(db_session, shared_inbox_ids=set(), now=NOW)
    assert _ids(q) == {"k-in"}


def test_individual_talent_mail_missing_from_mirror_is_shown_unverified(db_session):
    # The mirror holds only the newest page and lags the poller: absent != handled.
    make_token(db_session, talent_key="Katrina", email="katrina@taboost.me")
    _flagged(db_session, "k-unseen", key="Katrina")
    q = build_reply_queue(db_session, shared_inbox_ids=set(), now=NOW)
    assert _ids(q) == {"k-unseen"}
    assert q["managers"][0]["items"][0]["inbox_verified"] is False


def test_individual_talent_with_inactive_token_is_not_checked_against_shared_inbox(db_session):
    make_token(db_session, talent_key="Katrina", email="katrina@taboost.me", active=False)
    _flagged(db_session, "k1", key="Katrina")
    q = build_reply_queue(db_session, shared_inbox_ids=set(), now=NOW)  # shared listing lacks k1
    assert _ids(q) == {"k1"}


def test_mixed_case_talent_key_still_matches_profile_and_mirror(db_session):
    make_token(db_session, talent_key="KATRINA", email="katrina@taboost.me")
    _flagged(db_session, "k1", key="KATRINA")
    db_session.add(InboxEmail(talent_key="katrina", gmail_message_id="k1", label_ids="INBOX", is_unread=True))
    db_session.commit()
    q = build_reply_queue(db_session, shared_inbox_ids=set(), now=NOW)
    assert _ids(q) == {"k1"} and q["managers"][0]["items"][0]["inbox_verified"] is True


def test_unreachable_gmail_keeps_shared_rows_but_marks_them_unverified(db_session):
    _flagged(db_session, "m1")
    q = build_reply_queue(db_session, shared_inbox_ids=None, now=NOW)  # no shared token -> listing unavailable
    assert q["inbox_verified"] is False
    assert q["total"] == 1 and q["managers"][0]["items"][0]["inbox_verified"] is False


def test_manager_filter_and_window(db_session):
    _flagged(db_session, "m1")
    _flagged(db_session, "old", sender="D <d@d.com>", domain="d.com", days_ago=30)
    q = build_reply_queue(db_session, shared_inbox_ids={"m1", "old"}, now=NOW)
    assert _ids(q) == {"m1"}
    assert build_reply_queue(db_session, shared_inbox_ids={"m1"}, manager="Nobody", now=NOW)["total"] == 0


def test_text_report_names_manager_and_flags_quoted(db_session):
    _sent_draft(db_session)
    _flagged(db_session, "m1")
    text = render_text(build_reply_queue(db_session, shared_inbox_ids={"m1"}, now=NOW))
    assert "Chenni Li: 1 waiting, 1 from brands we quoted" in text and "[QUOTED]" in text


def test_endpoint_returns_json_and_text(client, db_session, monkeypatch):
    _flagged(db_session, "m1")
    monkeypatch.setattr("backend.services.reply_queue._live_shared_inbox_ids", lambda db: {"m1"})
    body = client.get("/api/dashboard/reply-queue?days=90").json()
    assert body["total"] == 1
    assert "Replies waiting on a human" in client.get("/api/dashboard/reply-queue?days=90&format=text").text


class _FakeList:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def list(self, **kwargs):
        page = self.pages[self.calls]
        self.calls += 1
        return type("Req", (), {"execute": lambda _self: page})()


def _svc(pages):
    fake = _FakeList(pages)
    messages = type("M", (), {"list": fake.list})()
    users = type("U", (), {"messages": lambda _self: messages})()
    return type("S", (), {"users": lambda _self: users})(), fake


def test_inbox_listing_follows_every_page():
    from backend.services.gmail import list_inbox_message_ids
    svc, fake = _svc([
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t"},
        {"messages": [{"id": "c"}]},
    ])
    assert list_inbox_message_ids(None, service=svc) == {"a", "b", "c"} and fake.calls == 2


def test_inbox_listing_refuses_a_truncated_set():
    import pytest
    from backend.services.gmail import list_inbox_message_ids
    svc, _ = _svc([{"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t"}])
    with pytest.raises(RuntimeError):
        list_inbox_message_ids(None, service=svc, max_results=2)


def test_failed_live_listing_degrades_to_unverified_not_hidden(db_session, monkeypatch):
    _flagged(db_session, "m1")
    shared = type("Tok", (), {"id": -1})()
    monkeypatch.setattr("backend.services.reply_queue.get_shared_inbox_token", lambda db: shared)
    monkeypatch.setattr(
        "backend.services.gmail.list_inbox_message_ids",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("too big")),
    )
    q = build_reply_queue(db_session, now=NOW)
    assert q["inbox_verified"] is False and _ids(q) == {"m1"}
