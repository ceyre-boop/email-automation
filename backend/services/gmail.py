"""
Gmail API helpers.

All Gmail operations (read, archive, create draft, send reply) go through here.
"""
from __future__ import annotations

import base64
import email as email_lib
import html
import logging
import re
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.services.oauth import TokenRefreshError, credentials_from_token_row, refresh_if_needed

logger = logging.getLogger(__name__)


def _plain_to_html(body: str) -> str:
    escaped = html.escape(body or "")
    escaped = re.sub(
        r"(https?://[^\s<>\"]+[^\s<>\".,;!?)])",
        r'<a href="\1">\1</a>',
        escaped,
    )
    return f"<div>{escaped.replace('\n', '<br>')}</div>"


def parse_cc_recipients(raw: str | None) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _gmail_service(token_row, db=None):
    """Return an authenticated Gmail API service. Persists refreshed token to DB if db is given."""
    creds = credentials_from_token_row(token_row)
    creds = refresh_if_needed(creds)  # raises TokenRefreshError if Google rejects it
    token_row.access_token = creds.token
    if creds.expiry:
        token_row.token_expiry = creds.expiry.replace(tzinfo=None)
    if db is not None:
        db.add(token_row)
        db.commit()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_service(token_row, db=None):
    """Public helper: build and return an authenticated Gmail service.

    Call this once at the start of a message processing chain and pass the
    returned service to subsequent gmail functions via the ``service=`` param
    to avoid rebuilding (and re-authenticating) for every API call.
    """
    return _gmail_service(token_row, db)


# ── Reading ──────────────────────────────────────────────────────────────────


def list_all_messages_since(token_row, days_back: int = 30, db=None) -> list[dict]:
    """
    Return ALL message stubs (id + threadId) from the last N days, paginating
    through the full history. Can return thousands of messages for active inboxes.
    """
    import datetime as dt
    since = (dt.datetime.utcnow() - dt.timedelta(days=days_back)).strftime("%Y/%m/%d")
    service = _gmail_service(token_row, db)
    messages: list[dict] = []
    page_token = None
    try:
        while True:
            kwargs: dict = {"userId": "me", "q": f"in:inbox after:{since}", "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            messages.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        logger.error("Gmail history list error: %s", exc)
    return messages


def list_inbox_messages(token_row, max_results: int = 50, db=None) -> list[dict]:
    """
    Return the talent's actual Gmail inbox (read + unread), newest first.
    Each item: {"id": <gmail_message_id>, "threadId": <thread_id>}
    """
    service = _gmail_service(token_row, db)
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail inbox list error for %s: %s", token_row.talent_key, exc)
        return []
    return result.get("messages", [])


def list_unread_inbox_messages(token_row, db=None, max_results: int = 100) -> list[dict]:
    """
    Return a list of unread INBOX messages for the talent.
    Each item: {"id": <gmail_message_id>, "threadId": <thread_id>}
    """
    service = _gmail_service(token_row, db)
    try:
        result = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                maxResults=max_results,
            )
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail list error for %s: %s", token_row.talent_key, exc)
        return []
    return result.get("messages", [])


def get_message_headers(token_row, message_id: str, db=None) -> dict[str, Any]:
    """
    Fetch only headers (subject, from, date) + labels for a message — no body.
    ~10x faster than get_message_detail; used for inbox list rendering.
    """
    service = _gmail_service(token_row, db)
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
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


def get_message_detail(token_row, message_id: str, db=None, service=None) -> dict[str, Any]:
    """
    Fetch full message detail and parse it into a flat dict:
    {
        id, thread_id, subject, sender, sender_domain,
        body_text, snippet, label_ids
    }
    Retries once on transient failure.
    Pass a pre-built ``service`` to avoid an extra build() call when processing
    multiple API calls in the same chain.
    """
    if service is None:
        service = _gmail_service(token_row, db)
    for attempt in range(2):
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            break
        except HttpError as exc:
            if attempt == 0 and exc.resp.status in (429, 500, 503):
                logger.warning("Gmail get transient error for %s (retrying): %s", message_id, exc)
                service = _gmail_service(token_row, db)  # rebuild fresh service on retry
                continue
            logger.error("Gmail get error for message %s: %s", message_id, exc)
            return {}
    else:
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
        "message_id_header": headers.get("message-id", ""),  # for In-Reply-To threading
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


def _collect_parts(payload: dict, plain: list, html: list) -> None:
    """Walk the MIME tree and collect all text/plain and text/html parts."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        plain.append(base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace"))
    elif mime_type == "text/html" and body_data:
        html.append(base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace"))

    for part in payload.get("parts", []):
        _collect_parts(part, plain, html)


def _html_to_text(html: str) -> str:
    """Strip HTML to readable plain text, removing style/script blocks first."""
    # Remove style and script blocks entirely (content between tags, not just the tags)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines for readable formatting
    html = re.sub(r"<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common HTML entities
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&zwnj;", "").replace("&rsquo;", "'").replace("&lsquo;", "'").replace("&mdash;", "—").replace("&ndash;", "–")
    # Collapse excessive whitespace / blank lines
    html = re.sub(r" {2,}", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _extract_body(payload: dict) -> str:
    """
    Extract readable plain-text body from a Gmail message payload.
    Prefers text/plain parts; falls back to HTML-stripped text/html.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_parts(payload, plain_parts, html_parts)

    if plain_parts:
        return "\n\n".join(plain_parts).strip()
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    return ""


# ── Thread guard ─────────────────────────────────────────────────────────────


def thread_has_prior_sent_reply(service, thread_id: str) -> bool:
    """
    Return True if this Gmail thread already has a SENT message in it.

    Catches threads where a human already replied (no DB record exists because
    the conversation predates this system). Uses format='minimal' to keep the
    API call cheap — we only need labels, not full message content.
    Returns False on any API error so a failure never silently blocks a draft.
    """
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="minimal"
        ).execute()
        for msg in thread.get("messages", []):
            if "SENT" in msg.get("labelIds", []):
                return True
        return False
    except HttpError as exc:
        logger.warning("thread_has_prior_sent_reply failed for %s (non-fatal): %s", thread_id, exc)
        return False


# ── Labelling / archiving ─────────────────────────────────────────────────────


def archive_message(token_row, message_id: str, db=None, service=None) -> bool:
    """Remove INBOX and UNREAD labels (archives the message)."""
    if service is None:
        service = _gmail_service(token_row, db)
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["INBOX", "UNREAD"]},
        ).execute()
        return True
    except HttpError as exc:
        logger.error("Archive failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


def mark_as_read(token_row, message_id: str, db=None, service=None) -> bool:
    """Remove UNREAD label only."""
    if service is None:
        service = _gmail_service(token_row, db)
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return True
    except HttpError as exc:
        logger.error("Mark-read failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


# ── Drafts ────────────────────────────────────────────────────────────────────


def create_gmail_draft(
    token_row,
    thread_id: str,
    reply_to: str,
    subject: str,
    body: str,
    db=None,
    in_reply_to: str | None = None,
    cc: list[str] | None = None,
    service=None,
) -> str | None:
    """
    Save a draft reply in the talent's Gmail account, threaded correctly.
    Returns the Gmail draft ID, or None on failure.
    Pass a pre-built ``service`` to skip an extra build() call.
    """
    if service is None:
        service = _gmail_service(token_row, db)
    mime_msg = MIMEMultipart("alternative")
    mime_msg.attach(MIMEText(body or "", "plain"))
    mime_msg.attach(MIMEText(_plain_to_html(body or ""), "html"))
    mime_msg["To"] = reply_to
    if cc:
        mime_msg["Cc"] = ", ".join(cc)
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    if in_reply_to:
        mime_msg["In-Reply-To"] = in_reply_to
        mime_msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        draft = (
            service.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            )
            .execute()
        )
        return draft.get("id")
    except HttpError as exc:
        logger.error("Draft creation failed for %s: %s", token_row.talent_key, exc)
        return None


def send_reply(
    token_row,
    thread_id: str,
    reply_to: str,
    subject: str,
    body: str,
    db=None,
    in_reply_to: str | None = None,
    cc: list[str] | None = None,
) -> bool:
    """
    Send a reply email as the talent.
    Used when an agency reviewer approves a draft.
    """
    service = _gmail_service(token_row, db)
    mime_msg = MIMEMultipart("alternative")
    mime_msg.attach(MIMEText(body or "", "plain"))
    mime_msg.attach(MIMEText(_plain_to_html(body or ""), "html"))
    mime_msg["To"] = reply_to
    if cc:
        mime_msg["Cc"] = ", ".join(cc)
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    if in_reply_to:
        mime_msg["In-Reply-To"] = in_reply_to
        mime_msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id},
        ).execute()
        return True
    except HttpError as exc:
        logger.error("Send failed for %s: %s", token_row.talent_key, exc)
        return False


def send_standalone_message(token_row, to: str, subject: str, body: str, db=None) -> bool:
    """Send a fresh (non-reply) email from the talent's Gmail account."""
    service = _gmail_service(token_row, db)
    mime_msg = MIMEText(body, "plain")
    mime_msg["To"] = to
    mime_msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except HttpError as exc:
        logger.error("Standalone send failed for %s: %s", token_row.talent_key, exc)
        return False


def thread_has_prior_sent_reply(service, thread_id: str) -> bool:
    """
    Return True if the Gmail thread contains any message with the SENT label,
    meaning the talent (or a manager) has already manually replied.

    This catches ongoing deal threads that were handled before the system was
    set up and therefore have no ProcessedEmail / Draft DB records.
    Uses format="minimal" to fetch only label IDs — fast and cheap.
    """
    try:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
        return any(
            "SENT" in msg.get("labelIds", [])
            for msg in thread.get("messages", [])
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("thread_has_prior_sent_reply failed for %s: %s", thread_id, exc)
        return False  # conservative: don't block on API failure


def delete_gmail_draft(token_row, gmail_draft_id: str, db=None) -> bool:
    """Delete a draft from the talent's Gmail account."""
    service = _gmail_service(token_row, db)
    try:
        service.users().drafts().delete(userId="me", id=gmail_draft_id).execute()
        return True
    except HttpError as exc:
        logger.error("Draft delete failed for %s / %s: %s", token_row.talent_key, gmail_draft_id, exc)


def list_gmail_drafts(token_row, max_results: int = 25, db=None) -> list[dict]:
    """
    Fetch the talent's actual Gmail drafts folder, newest first.
    Returns a list of dicts with draft content parsed out.
    """
    service = _gmail_service(token_row, db)
    try:
        result = service.users().drafts().list(userId="me", maxResults=max_results).execute()
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
            full = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
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


def send_gmail_draft(token_row, gmail_draft_id: str, db=None) -> bool:
    """Send an existing Gmail draft by its draft ID."""
    service = _gmail_service(token_row, db)
    try:
        service.users().drafts().send(userId="me", body={"id": gmail_draft_id}).execute()
        return True
    except HttpError as exc:
        logger.error("Draft send failed for %s / %s: %s", token_row.talent_key, gmail_draft_id, exc)
        return False
