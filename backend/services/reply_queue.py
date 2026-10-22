"""Reply queue — brand replies waiting on a human, grouped by manager.

Why this exists
---------------
SOP Rule 5 sends every follow-up (a thread that already has a message) to human
review, and Rule 12 sets Reply-To so the brand's answer to our rate quote lands
in a shared mailbox. The automation then leaves it in the INBOX and nothing
owns it. Those replies are the closest thing to a signed deal the pipeline sees,
and nothing in the system showed how many were waiting or for how long.

This builds the list a manager works from: flagged mail that is a reply, or
that comes from a sender we already sent a draft to, and that is *still in the
Gmail INBOX* — so anything a human already archived or answered drops off.

Where "still in the INBOX" comes from
-------------------------------------
  * Shared-inbox talents (talent-mgmt@): one live Gmail listing of the shared
    mailbox's INBOX, intersected with message ids.
  * Individually polled talents (Partnerships): the ``inbox_emails`` mirror,
    which holds those mailboxes' INBOX labels. Only a mirror row *without* INBOX
    hides a message; a message missing from the mirror is shown, unverified.
If the live listing fails (or the inbox is too large to list completely) the
shared-inbox rows are still returned, marked ``inbox_verified: false`` — better
to over-show than to hide a deal.

Read-only: nothing here sends, labels or archives anything.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import Draft, DraftStatus, EmailStatus, InboxEmail, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc
from backend.services.inbox_routing import SHARED_INBOX_KEYS, get_shared_inbox_token

logger = logging.getLogger(__name__)

# SOP Rule 10 — handled manually by the team, never part of this workflow.
REPEAT_CLIENT_DOMAINS = {"taboost.me", "favored.live", "nextwave-talent.com"}

# Rows that are not brand mail at all.
_NON_BRAND_KEYS = {"system_notification"}

UNASSIGNED = "Unassigned"

_REPLY_SUBJECT_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw|sv|回复|答复|回覆)\s*[:：]\s*)+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+")


def _email_of(sender: str | None) -> str | None:
    match = _EMAIL_RE.search(sender or "")
    return match.group(0).lower() if match else None


def _is_reply_subject(subject: str | None) -> bool:
    return bool(_REPLY_SUBJECT_RE.match(subject or ""))


def _quoted_sender_emails(db: Session) -> set[str]:
    """Lowercased addresses of every brand we have already sent a draft to."""
    rows = db.query(Draft.sender).filter(Draft.status == DraftStatus.sent).distinct().all()
    return {e for (s,) in rows if (e := _email_of(s))}


def _individual_talent_keys(db: Session) -> set[str]:
    """Lowercased keys of talents who own their mailbox (not the shared inbox).

    Includes inactive tokens: a talent whose own token lapsed is still not in the
    shared mailbox, so checking their mail against the shared INBOX would hide it.
    """
    shared = get_shared_inbox_token(db)
    shared_id = shared.id if shared is not None else None
    keys = set()
    for key, token_id in db.query(TalentToken.talent_key, TalentToken.id):
        k = (key or "").lower()
        if k and k not in SHARED_INBOX_KEYS and token_id != shared_id:
            keys.add(k)
    return keys


def _mirror_inbox_state(db: Session, keys: set[str]) -> dict[str, bool]:
    """{gmail_message_id: still_in_INBOX} for individually-polled talents.

    The mirror only holds the newest page of each mailbox and lags the poller, so
    a message *absent* from it is unknown, not handled. Only an entry that exists
    without the INBOX label proves a human got it out of the inbox.
    """
    if not keys:
        return {}
    rows = (
        db.query(InboxEmail.gmail_message_id, InboxEmail.label_ids)
        .filter(InboxEmail.talent_key.in_(sorted(keys)))
        .all()
    )
    return {mid: "INBOX" in (labels or "") for mid, labels in rows}


def _live_shared_inbox_ids(db: Session) -> set[str] | None:
    """Message ids currently in the shared mailbox INBOX, or None if unreachable."""
    token = get_shared_inbox_token(db)
    if token is None:
        return None
    try:
        return gmail_svc.list_inbox_message_ids(token, db=db)
    except Exception:  # noqa: BLE001 — a failed listing must degrade, not 500 the report
        logger.exception("reply_queue: live shared-inbox listing failed")
        return None


def build_reply_queue(
    db: Session,
    days: int = 14,
    manager: str | None = None,
    shared_inbox_ids: set[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Return {"generated_at", "days", "inbox_verified", "total", "managers": [...]}.

    ``shared_inbox_ids`` lets callers (and tests) supply the live INBOX listing;
    when omitted it is fetched from Gmail.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)
    settings = get_settings()
    profiles = {k.lower(): p for k, p in (settings.talent_profiles or {}).items()}

    quoted = _quoted_sender_emails(db)
    individual = _individual_talent_keys(db)
    mirror = _mirror_inbox_state(db, individual)
    if shared_inbox_ids is None:
        shared_inbox_ids = _live_shared_inbox_ids(db)
    verified = shared_inbox_ids is not None

    candidates = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.status == EmailStatus.flagged, ProcessedEmail.processed_at >= cutoff)
        .all()
    )

    by_manager: dict[str, list[dict]] = {}
    for row in candidates:
        key = (row.talent_key or "").lower()
        if key in _NON_BRAND_KEYS:
            continue
        if (row.sender_domain or "").lower() in REPEAT_CLIENT_DOMAINS:
            continue
        sender_email = _email_of(row.sender)
        was_quoted = bool(sender_email and sender_email in quoted)
        if not (was_quoted or _is_reply_subject(row.subject)):
            continue

        row_verified = True
        if key in individual:
            in_mirror = mirror.get(row.gmail_message_id)
            if in_mirror is False:
                continue
            row_verified = in_mirror is True
        elif verified:
            if row.gmail_message_id not in shared_inbox_ids:
                continue
        else:
            row_verified = False

        profile = profiles.get(key)
        mgr = (getattr(profile, "manager", None) or UNASSIGNED) if key != "unrouted" else UNASSIGNED
        if manager and mgr.lower() != manager.lower():
            continue

        received = row.email_date or row.processed_at
        by_manager.setdefault(mgr, []).append({
            "talent": row.talent_key,
            "brand": row.brand_name,
            "sender": row.sender,
            "subject": row.subject,
            "received_at": received.isoformat() if received else None,
            "age_days": round((now - received).total_seconds() / 86400, 1) if received else None,
            "we_quoted_them": was_quoted,
            "score": row.score,
            "gmail_message_id": row.gmail_message_id,
            "inbox_verified": row_verified,
        })

    managers = []
    for name, items in by_manager.items():
        # Brands we already quoted first (a deal in flight), then oldest first.
        items.sort(key=lambda i: (not i["we_quoted_them"], -(i["age_days"] or 0)))
        managers.append({
            "manager": name,
            "count": len(items),
            "quoted_count": sum(1 for i in items if i["we_quoted_them"]),
            "oldest_age_days": max((i["age_days"] or 0) for i in items),
            "items": items,
        })
    managers.sort(key=lambda m: (m["manager"] == UNASSIGNED, -m["quoted_count"], m["manager"]))

    return {
        "generated_at": now.isoformat(),
        "days": days,
        "inbox_verified": verified,
        "total": sum(m["count"] for m in managers),
        "managers": managers,
    }


def render_text(queue: dict, per_manager_limit: int = 25) -> str:
    """Plain-text version a manager can read in under a minute."""
    lines = [
        f"Replies waiting on a human — last {queue['days']} days — {queue['total']} total",
    ]
    if not queue["inbox_verified"]:
        lines.append("(Gmail INBOX state could not be checked; shared-inbox rows may already be handled.)")
    for m in queue["managers"]:
        lines.append("")
        lines.append(
            f"{m['manager']}: {m['count']} waiting, {m['quoted_count']} from brands we quoted, "
            f"oldest {m['oldest_age_days']}d"
        )
        for item in m["items"][:per_manager_limit]:
            tag = "QUOTED" if item["we_quoted_them"] else "reply "
            lines.append(
                f"  [{tag}] {item['age_days']}d  {item['talent']}  —  "
                f"{(item['sender'] or '')[:40]}  —  {(item['subject'] or '')[:70]}"
            )
        hidden = m["count"] - per_manager_limit
        if hidden > 0:
            lines.append(f"  … and {hidden} more (use the JSON view)")
    return "\n".join(lines)
