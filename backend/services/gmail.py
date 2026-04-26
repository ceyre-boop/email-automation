"""
Gmail API helpers.

All Gmail operations (read, archive, create draft, send reply) go through here.
"""
from __future__ import annotations

import base64
import email as email_lib
import logging
import re
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.services.oauth import credentials_from_token_row, refresh_if_needed
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})


def _build_service(token_row, force_refresh: bool = False):
    """Build an authenticated Gmail service, optionally forcing a token refresh."""
    creds = credentials_from_token_row(token_row)
    if force_refresh:
        creds.refresh(GoogleAuthRequest())
    else:
        creds = refresh_if_needed(creds)
    token_row.access_token = creds.token
    if creds.expiry:
        token_row.token_expiry = creds.expiry.replace(tzinfo=None)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _gmail_service(token_row):
    """Return an authenticated Gmail API service for the given token row."""
    return _build_service(token_row)


def _call_with_retry(token_row, fn, *, max_attempts: int = 3, backoff: float = 2.0):
    """
    Execute fn(service) with reliability guards:
    - On 401/403: force-refresh the token and retry once.
    - On 429/5xx: exponential backoff, up to max_attempts total.
    """
    service = _build_service(token_row)
    last_exc: HttpError | None = None
    for attempt in range(max_attempts):
        try:
            return fn(service)
        except HttpError as exc:
            status = exc.resp.status
            last_exc = exc
            if status in _AUTH_STATUSES and attempt == 0:
                logger.warning(
                    "Auth error (%s) for %s — force-refreshing token and retrying",
                    status, token_row.talent_key,
                )
                service = _build_service(token_row, force_refresh=True)
                continue
            if status in _TRANSIENT_STATUSES and attempt < max_attempts - 1:
                wait = backoff ** attempt
                logger.warning(
                    "Transient error (%s) for %s — retrying in %.1fs (attempt %d/%d)",
                    status, token_row.talent_key, wait, attempt + 1, max_attempts,
                )
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


# ── Reading ──────────────────────────────────────────────────────────────────


def list_all_messages_since(token_row, days_back: int = 30) -> list[dict]:
    """
    Return ALL message stubs (id + threadId) from the last N days, paginating
    through the full history. Can return thousands of messages for active inboxes.
    """
    import datetime as dt
    since = (dt.datetime.utcnow() - dt.timedelta(days=days_back)).strftime("%Y/%m/%d")
    messages: list[dict] = []
    page_token = None
    try:
        while True:
            kwargs: dict = {"userId": "me", "q": f"in:inbox after:{since}", "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            result = _call_with_retry(
                token_row,
                lambda svc, kw=kwargs: svc.users().messages().list(**kw).execute(),
            )
            messages.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        logger.error("Gmail history list error: %s", exc)
    return messages


def list_inbox_messages(token_row, max_results: int = 50) -> list[dict]:
    """
    Return the talent's actual Gmail inbox (read + unread), newest first.
    Each item: {"id": <gmail_message_id>, "threadId": <thread_id>}
    """
    try:
        return _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().list(
                userId="me", labelIds=["INBOX"], maxResults=max_results
            ).execute().get("messages", []),
        )
    except HttpError as exc:
        logger.error("Gmail inbox list error for %s: %s", token_row.talent_key, exc)
        return []


def list_unread_inbox_messages(token_row, max_results: int = 30) -> list[dict]:
    """
    Return a list of unread INBOX messages for the talent.
    Each item: {"id": <gmail_message_id>, "threadId": <thread_id>}
    """
    try:
        return _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                maxResults=max_results,
            ).execute().get("messages", []),
        )
    except HttpError as exc:
        logger.error("Gmail list error for %s: %s", token_row.talent_key, exc)
        return []


def get_message_headers(token_row, message_id: str) -> dict[str, Any]:
    """
    Fetch only headers (subject, from, date) + labels for a message — no body.
    ~10x faster than get_message_detail; used for inbox list rendering.
    """
    try:
        msg = _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute(),
        )
    except HttpError as exc:
        logger.error("Gmail headers error for message %s: %s", message_id, exc)
        return {}

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("from", "")
    return {
        "id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("subject", ""),
        "sender": sender,
        "sender_domain": _extract_domain(sender),
        "email_date": _parse_email_date(headers.get("date", "")),
        "label_ids": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }


def get_message_detail(token_row, message_id: str) -> dict[str, Any]:
    """
    Fetch full message detail and parse it into a flat dict:
    {
        id, thread_id, subject, sender, sender_domain,
        body_text, snippet, label_ids
    }
    """
    try:
        msg = _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute(),
        )
    except HttpError as exc:
        logger.error("Gmail get error for message %s: %s", message_id, exc)
        return {}

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    sender_domain = _extract_domain(sender)
    body = _extract_body(msg.get("payload", {}))
    email_date = _parse_email_date(headers.get("date", ""))

    return {
        "id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": subject,
        "sender": sender,
        "sender_domain": sender_domain,
        "body_text": body,
        "snippet": msg.get("snippet", ""),
        "label_ids": msg.get("labelIds", []),
        "email_date": email_date,
    }


def _parse_email_date(date_header: str) -> datetime | None:
    """Parse RFC 2822 Date header into a naive UTC datetime, or None on failure."""
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _extract_domain(from_header: str) -> str:
    match = re.search(r"@([\w.\-]+)", from_header)
    return match.group(1).lower() if match else ""


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    if mime_type == "text/html" and body_data:
        # Fall back: strip tags if no plain text found
        html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", html).strip()

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


# ── Labelling / archiving ─────────────────────────────────────────────────────


def archive_message(token_row, message_id: str) -> bool:
    """Remove INBOX and UNREAD labels (archives the message)."""
    try:
        _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX", "UNREAD"]},
            ).execute(),
        )
        return True
    except HttpError as exc:
        logger.error("Archive failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


def mark_as_read(token_row, message_id: str) -> bool:
    """Remove UNREAD label only."""
    try:
        _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute(),
        )
        return True
    except HttpError as exc:
        logger.error("Mark-read failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


# ── Drafts ────────────────────────────────────────────────────────────────────


def create_gmail_draft(token_row, thread_id: str, reply_to: str, subject: str, body: str) -> str | None:
    """
    Save a draft reply in the talent's Gmail account.
    Returns the Gmail draft ID, or None on failure.
    """
    mime_msg = MIMEText(body, "plain")
    mime_msg["To"] = reply_to
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        draft = _call_with_retry(
            token_row,
            lambda svc: svc.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            ).execute(),
        )
        return draft.get("id")
    except HttpError as exc:
        logger.error("Draft creation failed for %s: %s", token_row.talent_key, exc)
        return None


def send_reply(token_row, thread_id: str, reply_to: str, subject: str, body: str) -> bool:
    """
    Send a reply email as the talent.
    Used when an agency reviewer approves a draft.
    """
    mime_msg = MIMEText(body, "plain")
    mime_msg["To"] = reply_to
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        _call_with_retry(
            token_row,
            lambda svc: svc.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": thread_id},
            ).execute(),
        )
        return True
    except HttpError as exc:
        logger.error("Send failed for %s: %s", token_row.talent_key, exc)
        return False


def delete_gmail_draft(token_row, gmail_draft_id: str) -> bool:
    """Delete a draft from the talent's Gmail account."""
    try:
        _call_with_retry(
            token_row,
            lambda svc: svc.users().drafts().delete(userId="me", id=gmail_draft_id).execute(),
        )
        return True
    except HttpError as exc:
        logger.error("Draft delete failed for %s / %s: %s", token_row.talent_key, gmail_draft_id, exc)
        return False


def list_gmail_drafts(token_row, max_results: int = 25) -> list[dict]:
    """
    Fetch the talent's actual Gmail drafts folder, newest first.
    Returns a list of dicts with draft content parsed out.
    """
    try:
        result = _call_with_retry(
            token_row,
            lambda svc: svc.users().drafts().list(userId="me", maxResults=max_results).execute(),
        )
    except HttpError as exc:
        logger.error("Gmail drafts list error for %s: %s", token_row.talent_key, exc)
        return []

    stubs = result.get("drafts", [])
    if not stubs:
        return []

    drafts = []
    for stub in stubs:
        draft_id = stub.get("id")
        try:
            full = _call_with_retry(
                token_row,
                lambda svc, did=draft_id: svc.users().drafts().get(
                    userId="me", id=did, format="full"
                ).execute(),
            )
        except HttpError as exc:
            logger.warning("Could not fetch Gmail draft %s: %s", draft_id, exc)
            continue

        msg = full.get("message", {})
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        body_text = _extract_body(payload)

        drafts.append({
            "gmail_draft_id": draft_id,
            "message_id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "snippet": msg.get("snippet", ""),
            "body_text": body_text,
        })

    return drafts


def send_gmail_draft(token_row, gmail_draft_id: str) -> bool:
    """Send an existing Gmail draft by its draft ID."""
    try:
        _call_with_retry(
            token_row,
            lambda svc: svc.users().drafts().send(userId="me", body={"id": gmail_draft_id}).execute(),
        )
        return True
    except HttpError as exc:
        logger.error("Draft send failed for %s / %s: %s", token_row.talent_key, gmail_draft_id, exc)
        return False
