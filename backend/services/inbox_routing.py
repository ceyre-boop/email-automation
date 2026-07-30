"""
Inbox routing — which mailbox a talent's mail lives in, and where replies go.

SOP v16 runs two modes side by side:

  Shared inbox   talent-mgmt@taboost.me receives mail for most talents. The
                 talent is identified from the original recipient alias header
                 (see gmail.get_to_address), NOT from the receiving account.
                 These drafts get no Reply-To.

  Per-token      The Partnerships talents keep their own Gmail accounts and are
                 identified by which account received the mail (the pre-v16
                 behaviour). Their drafts carry Reply-To: partnerships@taboost.me.

Both the poller and the send path need to agree on this, so the lookups live
here rather than being duplicated. Source of truth for the groups is
sheets/sop.md Part 3, mirrored into config/settings.json.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.models.db import TalentToken

logger = logging.getLogger(__name__)

DEFAULT_SHARED_INBOX = "talent-mgmt@taboost.me"

# talent_key values that identify the shared mailbox rather than a real talent.
SHARED_INBOX_KEYS = {"shared-inbox", "talent-mgmt", "talentmgmt"}


def _norm(value: str | None) -> str:
    return (value or "").lower().strip()


def shared_inbox_email(settings=None) -> str:
    """Return the consolidated inbox address from settings.json."""
    if settings is None:
        from backend.core.config import get_settings
        settings = get_settings()
    configured = settings.app_config.get("single_inbox", {}).get("inbox_email")
    return _norm(configured) or DEFAULT_SHARED_INBOX


def reply_to_groups(settings=None) -> dict[str, set[str]]:
    """Return {reply_to_address: {member inbox emails}} from reply_to_routing.

    Under SOP v16 there is exactly one group — partnerships@taboost.me — but the
    shape is kept general so sop.md Part 3 maps onto it 1:1 if more return.
    """
    if settings is None:
        from backend.core.config import get_settings
        settings = get_settings()
    raw = (settings.app_config.get("reply_to_routing") or {}).get("groups") or {}
    return {
        _norm(address): {_norm(m) for m in (members or [])}
        for address, members in raw.items()
    }


def reply_to_for_inbox(inbox_email: str | None, settings=None) -> str | None:
    """Return the Reply-To address for a talent inbox, or None if it has none.

    Matching is on the TALENT INBOX, never the sender — SOP v2b Rule 2 is
    explicit about that. Header-only routing: the result is never added to or
    referenced in the email body. An inbox in no group gets None, which is the
    case for the shared inbox and every talent polled through it.
    """
    inbox = _norm(inbox_email)
    if not inbox:
        return None
    try:
        for address, members in reply_to_groups(settings).items():
            if inbox in members:
                return address or None
    except Exception as exc:  # noqa: BLE001 — routing must never break drafting
        logger.warning("Reply-To routing lookup failed for %s: %s", inbox, exc)
    return None


def reply_to_for_token(token_row, settings=None) -> str | None:
    """Return the Reply-To address for the mailbox this token belongs to."""
    return reply_to_for_inbox(getattr(token_row, "email", None), settings=settings)


def is_shared_inbox_token(token_row, settings=None) -> bool:
    """True if this token is the consolidated mailbox rather than a talent's own."""
    return (
        _norm(getattr(token_row, "email", None)) == shared_inbox_email(settings)
        or _norm(getattr(token_row, "talent_key", None)) in SHARED_INBOX_KEYS
    )


def get_shared_inbox_token(db: Session, settings=None) -> TalentToken | None:
    """Return the active token for the consolidated inbox, or None."""
    inbox = shared_inbox_email(settings)
    token = (
        db.query(TalentToken)
        .filter(TalentToken.active == True, TalentToken.email.ilike(inbox))  # noqa: E712
        .first()
    )
    if token is not None:
        return token
    # Fallback: connected before the email was known, keyed by convention.
    return (
        db.query(TalentToken)
        .filter(TalentToken.active == True, TalentToken.talent_key.in_(sorted(SHARED_INBOX_KEYS)))  # noqa: E712
        .first()
    )


def resolve_token_for_talent(db: Session, talent_key: str | None, settings=None) -> TalentToken | None:
    """Return the Gmail token that holds this talent's mail.

    Own token first, shared inbox as fallback. That ordering means a talent who
    keeps an individual Gmail account (the Partnerships four) is unaffected,
    while a talent polled from the consolidated inbox resolves to it — their
    drafts live there, so that is the account that must send them.

    Callers do not need to know which mode a talent is in.
    """
    key = _norm(talent_key)
    if key and key not in SHARED_INBOX_KEYS:
        own = (
            db.query(TalentToken)
            .filter(TalentToken.talent_key.ilike(key), TalentToken.active == True)  # noqa: E712
            .first()
        )
        if own is not None:
            return own
    return get_shared_inbox_token(db, settings=settings)
