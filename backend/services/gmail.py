"""
Gmail API helpers.

All Gmail operations (read, archive, create draft, send reply) go through here.
"""
from __future__ import annotations

import base64
import email as email_lib
import logging
import re
from email.mime.text import MIMEText
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.services.oauth import credentials_from_token_row, refresh_if_needed

logger = logging.getLogger(__name__)


def _gmail_service(token_row):
    """Return an authenticated Gmail API service for the given token row."""
    creds = credentials_from_token_row(token_row)
    creds = refresh_if_needed(creds)
    # Update the token in the token_row in case it was refreshed
    token_row.access_token = creds.token
    if creds.expiry:
        from datetime import timezone
        token_row.token_expiry = creds.expiry.replace(tzinfo=None)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Reading ──────────────────────────────────────────────────────────────────


def list_unread_inbox_messages(token_row, max_results: int = 30) -> list[dict]:
    """
    Return a list of unread INBOX messages for the talent.
    Each item: {"id": <gmail_message_id>, "threadId": <thread_id>}
    """
    service = _gmail_service(token_row)
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


def get_message_detail(token_row, message_id: str) -> dict[str, Any]:
    """
    Fetch full message detail and parse it into a flat dict:
    {
        id, thread_id, subject, sender, sender_domain,
        body_text, snippet, label_ids
    }
    """
    service = _gmail_service(token_row)
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail get error for message %s: %s", message_id, exc)
        return {}

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    sender_domain = _extract_domain(sender)
    body = _extract_body(msg.get("payload", {}))

    return {
        "id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": subject,
        "sender": sender,
        "sender_domain": sender_domain,
        "body_text": body,
        "snippet": msg.get("snippet", ""),
        "label_ids": msg.get("labelIds", []),
    }


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
    service = _gmail_service(token_row)
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


def mark_as_read(token_row, message_id: str) -> bool:
    """Remove UNREAD label only."""
    service = _gmail_service(token_row)
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


def create_gmail_draft(token_row, thread_id: str, reply_to: str, subject: str, body: str) -> str | None:
    """
    Save a draft reply in the talent's Gmail account.
    Returns the Gmail draft ID, or None on failure.
    """
    service = _gmail_service(token_row)
    mime_msg = MIMEText(body, "plain")
    mime_msg["To"] = reply_to
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
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


def send_reply(token_row, thread_id: str, reply_to: str, subject: str, body: str) -> bool:
    """
    Send a reply email as the talent.
    Used when an agency reviewer approves a draft.
    """
    service = _gmail_service(token_row)
    mime_msg = MIMEText(body, "plain")
    mime_msg["To"] = reply_to
    mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
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


def delete_gmail_draft(token_row, gmail_draft_id: str) -> bool:
    """Delete a draft from the talent's Gmail account."""
    service = _gmail_service(token_row)
    try:
        service.users().drafts().delete(userId="me", id=gmail_draft_id).execute()
        return True
    except HttpError as exc:
        logger.error("Draft delete failed for %s / %s: %s", token_row.talent_key, gmail_draft_id, exc)
        return False
