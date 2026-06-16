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
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr, parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.services.oauth import TokenRefreshError, credentials_from_token_row, refresh_if_needed


class GmailDraftError(Exception):
    """Raised when Gmail's drafts.create API returns an HttpError."""
    def __init__(self, status: str, reason: str, body_snippet: str = ""):
        self.status = status
        self.reason = reason
        self.body_snippet = body_snippet
        super().__init__(f"Gmail draft API error {status}: {reason}")

logger = logging.getLogger(__name__)


def _safe_address(addr_str: str) -> str:
    """RFC 2047-encode the display name if it contains non-ASCII characters.

    Gmail's API rejects MIME headers with raw non-ASCII bytes. Senders from
    Chinese/Korean/accented-name domains must be encoded before insertion.
    """
    name, addr = parseaddr(addr_str)
    if name and not name.isascii():
        name = Header(name, "utf-8").encode()
    return formataddr((name, addr)) if name else addr


_RAW_URL_RE = re.compile(r"(https?://[^\s<>\"]+[^\s<>\".,;!?)])")


def _escape_and_autolink(segment: str) -> str:
    escaped = html.escape(segment or "")
    return _RAW_URL_RE.sub(r'<a href="\1">\1</a>', escaped)


def _iter_internal_link_spans(text: str):
    """
    Yield (start, end, anchor, url) spans for SOP Markdown link format:
      [Anchor Text] (https://url)

    start/end cover the full `[Anchor] (URL)` span including any space
    between ] and ( so the renderer can replace it cleanly.
    """
    i = 0
    n = len(text)
    while i < n:
        lb = text.find("[", i)
        if lb == -1:
            return
        rb = text.find("]", lb + 1)
        if rb == -1:
            return

        anchor = text[lb + 1:rb].strip()
        if not anchor:
            i = lb + 1
            continue

        # Expect `(URL)` immediately after `]`, optionally separated by one space
        after_rb = rb + 1
        if after_rb < n and text[after_rb] == " ":
            after_rb += 1
        if after_rb >= n or text[after_rb] != "(":
            i = rb + 1
            continue

        rp = text.find(")", after_rb + 1)
        if rp == -1:
            i = rb + 1
            continue

        raw_url = text[after_rb + 1:rp].strip()
        if not raw_url.startswith(("http://", "https://")):
            i = rb + 1
            continue

        yield lb, rp + 1, anchor, raw_url
        i = rp + 1


def _apply_inline_formatting(text: str) -> str:
    """
    Convert inline formatting markers to HTML. Runs AFTER html.escape.

      ***bold+italic***     → <strong><em>text</em></strong>   (must come before **)
      **bold**              → <strong>text</strong>
      __underline__         → <u>text</u>
      &lt;u&gt;text&lt;/u&gt; → <u>text</u>  (html.escape artifact)
      [b]bold[/b]           → <strong>text</strong>
      [ul]text[/ul]         → <u>text</u>
    """
    import re
    # ***bold+italic*** — must be matched BEFORE ** to avoid partial match
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', text, flags=re.DOTALL)
    # **bold**
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text, flags=re.DOTALL)
    # __underline__
    text = re.sub(r'__(.+?)__', r'<u>\1</u>', text, flags=re.DOTALL)
    # <u>text</u> — html.escape turns < > into &lt; &gt;, restore them
    text = re.sub(r'&lt;u&gt;(.+?)&lt;/u&gt;', r'<u>\1</u>', text, flags=re.IGNORECASE | re.DOTALL)
    # Hard-coded bracket tags
    text = re.sub(r'\[b\](.+?)\[/b\]', r'<strong>\1</strong>', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[ul\](.+?)\[/ul\]', r'<u>\1</u>', text, flags=re.IGNORECASE | re.DOTALL)
    return text


def _strip_inline_formatting(text: str) -> str:
    """Strip all formatting markers for the plain-text version of the email."""
    import re
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<u>(.+?)</u>', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[b\](.+?)\[/b\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[ul\](.+?)\[/ul\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    return text


def _render_email_body(body: str) -> tuple[str, str]:
    """
    Render body into (plain_text, html_text).

    Supports:
      [Anchor Text] (https://example.com)  → clickable link
      **bold text**                          → <strong>
      __underlined text__                    → <u>
      [b]bold[/b]  /  [ul]underline[/ul]    → same (SOP hard-coded tags)
    """
    source = body or ""
    spans = list(_iter_internal_link_spans(source))

    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    cursor = 0

    for start, end, anchor, url in spans:
        segment = source[cursor:start]

        # Detect **[Anchor](URL)** — bold wrapping around a link.
        # The ** before [ stays in `segment`; the ** after ) is the next char.
        bold_link = segment.endswith('**') and end < len(source) and source[end:end+2] == '**'
        if bold_link:
            segment = segment[:-2]          # strip trailing ** from preceding text
            end_adj = end + 2               # skip trailing ** after the link
        else:
            end_adj = end

        plain_chunks.append(_strip_inline_formatting(segment))
        html_chunks.append(_apply_inline_formatting(_escape_and_autolink(segment)))
        plain_chunks.append(anchor)
        anchor_text = html.escape(anchor)
        url_escaped = html.escape(url, quote=True)
        link_html = f'<a href="{url_escaped}">{anchor_text}</a>'
        html_chunks.append(f'<strong>{link_html}</strong>' if bold_link else link_html)
        cursor = end_adj

    tail = source[cursor:]
    plain_chunks.append(_strip_inline_formatting(tail))
    html_chunks.append(_apply_inline_formatting(_escape_and_autolink(tail)))

    plain = "".join(plain_chunks)
    html_body = "".join(html_chunks)
    return plain, f"<div>{html_body.replace('\n', '<br>')}</div>"


def _plain_to_html(body: str) -> str:
    _, html_text = _render_email_body(body)
    return html_text


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


def list_spam_messages(token_row, db=None, max_results: int = 50) -> list[dict]:
    """Return messages currently in the SPAM folder. No UNREAD filter — spam items may already be read."""
    service = _gmail_service(token_row, db)
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["SPAM"], maxResults=max_results)
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail spam list error for %s: %s", token_row.talent_key, exc)
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


def _get_or_create_custom_label(service, label_name: str, *, background_color: str, text_color: str) -> str | None:
    """Return a Gmail user-label ID, creating it if needed. Only whitelisted labels are permitted."""
    _assert_label_not_blocked(label_name)
    if label_name not in _ALLOWED_LABELS:
        logger.warning("LABEL GUARD: rejected unauthorized label '%s' — only %s are permitted", label_name, sorted(_ALLOWED_LABELS))
        return None
    try:
        existing = service.users().labels().list(userId="me").execute()
        for lbl in existing.get("labels", []):
            if lbl.get("name", "").lower() == label_name.lower():
                return lbl["id"]
        created = service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {
                    "backgroundColor": background_color,
                    "textColor": text_color,
                },
            },
        ).execute()
        return created.get("id")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not ensure Gmail label %s: %s", label_name, exc)
        return None


def archive_as_spam(token_row, message_id: str, db=None, service=None) -> bool:
    """
    Atomic Option C: remove INBOX/UNREAD and apply Misc label in a single API call.
    If the Misc label cannot be created, archives anyway without it — the archive action
    (removing from INBOX) is never blocked by a label failure.
    """
    if service is None:
        service = _gmail_service(token_row, db)
    label_id = _get_or_create_label(service, "Misc", "#e8eaed", "#202124")
    if not label_id:
        logger.warning(
            "Misc label unavailable for %s/%s — archiving without it",
            token_row.talent_key, message_id,
        )
    try:
        body: dict = {
            "removeLabelIds": [
                "INBOX", "UNREAD",
                "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
                "CATEGORY_UPDATES", "CATEGORY_FORUMS",
            ],
        }
        if label_id:
            body["addLabelIds"] = [label_id]
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return True
    except HttpError as exc:
        logger.error("archive_as_spam failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


def remove_from_inbox(token_row, message_id: str, db=None, service=None) -> bool:
    """Remove INBOX/UNREAD/category labels at draft creation time. No label applied."""
    if service is None:
        service = _gmail_service(token_row, db)
    try:
        service.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": [
                "INBOX", "UNREAD",
                "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
                "CATEGORY_UPDATES", "CATEGORY_FORUMS",
            ]},
        ).execute()
        return True
    except HttpError as exc:
        logger.error("remove_from_inbox failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


def mark_initial_response_sent(token_row, message_id: str, db=None, service=None) -> bool:
    """Apply A Initial Response label after a reply has been sent. INBOX already removed at draft creation."""
    if service is None:
        service = _gmail_service(token_row, db)
    label_id = _get_or_create_custom_label(
        service,
        "A Initial Response",
        background_color="#16a765",
        text_color="#ffffff",
    )
    try:
        if label_id:
            service.users().messages().modify(
                userId="me", id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        return True
    except HttpError as exc:
        logger.error("Initial-response label update failed for %s / %s: %s", token_row.talent_key, message_id, exc)
        return False


def move_to_inbox(token_row, message_id: str, db=None, service=None) -> bool:
    """Inverse of mark_initial_response_sent. Removes 'A Initial Response', adds INBOX back."""
    if service is None:
        service = _gmail_service(token_row, db)
    label_id = _get_or_create_custom_label(
        service, "A Initial Response", background_color="#16a765", text_color="#ffffff"
    )
    body: dict = {"addLabelIds": ["INBOX"]}
    if label_id:
        body["removeLabelIds"] = [label_id]
    try:
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return True
    except HttpError as exc:
        logger.error("move_to_inbox failed %s/%s: %s", token_row.talent_key, message_id, exc)
        return False


def restore_inbox_label(token_row, message_id: str, db=None, service=None) -> bool:
    """Adds INBOX, removes all non-system custom labels. Inbox Feed B-button action."""
    _SYSTEM_PREFIXES = (
        "INBOX", "SENT", "DRAFT", "SPAM", "TRASH",
        "UNREAD", "STARRED", "IMPORTANT", "CATEGORY_",
    )
    if service is None:
        service = _gmail_service(token_row, db)
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="minimal").execute()
        current_labels = msg.get("labelIds", [])
        to_remove = [l for l in current_labels if not any(l.startswith(p) for p in _SYSTEM_PREFIXES)]
        body: dict = {"addLabelIds": ["INBOX"]}
        if to_remove:
            body["removeLabelIds"] = to_remove
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return True
    except HttpError as exc:
        logger.error("restore_inbox_label failed %s/%s: %s", token_row.talent_key, message_id, exc)
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
    plain_body, html_body = _render_email_body(body or "")
    mime_msg = MIMEMultipart("alternative")
    mime_msg.attach(MIMEText(plain_body, "plain"))
    mime_msg.attach(MIMEText(html_body, "html"))
    mime_msg["To"] = _safe_address(reply_to)
    if cc:
        mime_msg["Cc"] = ", ".join(_safe_address(a) for a in cc)
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
        status = getattr(getattr(exc, "resp", None), "status", "unknown")
        reason = getattr(exc, "reason", None) or "unknown"
        body_snippet = ""
        raw = getattr(exc, "content", b"") or b""
        if raw:
            try:
                body_snippet = raw[:240].decode("utf-8", "ignore")
            except Exception:
                body_snippet = "<unreadable>"
        logger.error(
            "Draft creation failed for %s — status=%s reason=%s body=%s",
            token_row.talent_key, status, reason, body_snippet,
        )
        raise GmailDraftError(str(status), str(reason), body_snippet)


def send_reply(
    token_row,
    thread_id: str,
    reply_to: str,
    subject: str,
    body: str,
    db=None,
    in_reply_to: str | None = None,
    cc: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Send a reply email as the talent.
    Returns (True, "") on success or (False, error_detail) on failure.
    """
    service = _gmail_service(token_row, db)
    plain_body, html_body = _render_email_body(body or "")
    mime_msg = MIMEMultipart("alternative")
    mime_msg.attach(MIMEText(plain_body, "plain"))
    mime_msg.attach(MIMEText(html_body, "html"))
    mime_msg["To"] = _safe_address(reply_to)
    if cc:
        mime_msg["Cc"] = ", ".join(_safe_address(a) for a in cc)
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
        return True, ""
    except HttpError as exc:
        detail = str(exc)
        logger.error("Send failed for %s: %s", token_row.talent_key, detail)
        return False, detail


def send_standalone_message(token_row, to: str, subject: str, body: str, db=None) -> bool:
    """Send a fresh (non-reply) email from the talent's Gmail account."""
    service = _gmail_service(token_row, db)
    mime_msg = MIMEText(body, "plain")
    mime_msg["From"] = "Colin <colineyre222@gmail.com>"
    mime_msg["To"] = _safe_address(to)
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


# ── Triage labels ─────────────────────────────────────────────────────────────

# Only these labels may ever be applied. All other label operations are rejected.
_ALLOWED_LABELS: frozenset[str] = frozenset({"A Initial Response", "Spam", "Misc"})

# These labels are permanently forbidden — applying any of them raises immediately.
# Belt + suspenders: even if a label somehow exists in Gmail already, it can never
# be applied through any code path in this file.
_BLOCKED_LABELS: frozenset[str] = frozenset({
    "Revisit", "Known Brand", "Nicole Review", "Draft Sent"
})


def _assert_label_not_blocked(label_name: str) -> None:
    """Raise loudly if label_name is on the hard blocklist. Call before any label operation."""
    if label_name in _BLOCKED_LABELS:
        raise ValueError(
            f"LABEL BLOCKLIST: refusing to create/apply blocked label '{label_name}' — "
            f"blocked set: {sorted(_BLOCKED_LABELS)}"
        )

_TRIAGE_LABEL_CFG = {
    1: {"name": "Spam", "backgroundColor": "#e8eaed", "textColor": "#202124"},
}

_EXTRA_LABEL_CFG: dict = {}

# Manager-review label colours — one per manager name
_MANAGER_LABEL_COLORS = {
    "Cara":    {"backgroundColor": "#a479e2", "textColor": "#ffffff"},
    "Chenni":  {"backgroundColor": "#f691b3", "textColor": "#202124"},
    "Nicole":  {"backgroundColor": "#4986e7", "textColor": "#ffffff"},
    "Colin":   {"backgroundColor": "#e8eaed", "textColor": "#202124"},
}
_MANAGER_LABEL_DEFAULT_COLOR = {"backgroundColor": "#e8eaed", "textColor": "#202124"}


def get_label_id_by_name(service, label_name: str) -> str | None:
    """Return the Gmail label ID for an exact label name, or None if absent or on API error."""
    try:
        existing = service.users().labels().list(userId="me").execute()
        for lbl in existing.get("labels", []):
            if lbl.get("name") == label_name:
                return lbl["id"]
    except Exception:
        return None
    return None


def _get_or_create_label(service, name: str, bg: str, fg: str) -> str | None:
    """Return (creating if needed) a Gmail label ID by exact name. Only whitelisted labels are permitted."""
    _assert_label_not_blocked(name)
    if name not in _ALLOWED_LABELS:
        logger.warning("LABEL GUARD: rejected unauthorized label '%s' — only %s are permitted", name, sorted(_ALLOWED_LABELS))
        return None
    try:
        existing = service.users().labels().list(userId="me").execute()
        for lbl in existing.get("labels", []):
            if lbl.get("name") == name:
                return lbl["id"]
        created = service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {"backgroundColor": bg, "textColor": fg},
            },
        ).execute()
        return created.get("id")
    except Exception:
        return None


def _apply_label_ids(service, message_id: str, label_ids: list[str]) -> None:
    if not label_ids:
        return
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": label_ids},
    ).execute()


def _get_or_create_triage_label(service, score: int) -> str | None:
    cfg = _TRIAGE_LABEL_CFG.get(score)
    if not cfg:
        return None
    return _get_or_create_label(service, cfg["name"], cfg["backgroundColor"], cfg["textColor"])


def apply_triage_label(token_row, message_id: str, score: int, db=None, service=None) -> None:
    """Apply the AI triage score label. Non-fatal."""
    try:
        svc = service or _gmail_service(token_row, db)
        label_id = _get_or_create_triage_label(svc, score)
        if label_id:
            _apply_label_ids(svc, message_id, [label_id])
    except Exception as exc:  # noqa: BLE001
        logger.error("apply_triage_label blocked or failed for %s / %s: %s", token_row.talent_key, message_id, exc)


def apply_extra_label(token_row, message_id: str, label_key: str, db=None, service=None) -> None:
    """Apply a named lifecycle label. Non-fatal. No-op if label_key not in _EXTRA_LABEL_CFG or not in _ALLOWED_LABELS."""
    try:
        cfg = _EXTRA_LABEL_CFG.get(label_key)
        if not cfg:
            return
        svc = service or _gmail_service(token_row, db)
        label_id = _get_or_create_label(svc, cfg["name"], cfg["backgroundColor"], cfg["textColor"])
        if label_id:
            _apply_label_ids(svc, message_id, [label_id])
    except Exception as exc:  # noqa: BLE001
        logger.error("apply_extra_label blocked or failed for %s / %s (key=%s): %s", token_row.talent_key, message_id, label_key, exc)


def apply_manager_review_label(token_row, message_id: str, manager_name: str, db=None, service=None) -> None:
    """Apply a per-manager review label (e.g. 'Chenni Review'). Non-fatal."""
    try:
        if not manager_name:
            return
        name = f"{manager_name} Review"
        _assert_label_not_blocked(name)
        color = _MANAGER_LABEL_COLORS.get(manager_name, _MANAGER_LABEL_DEFAULT_COLOR)
        svc = service or _gmail_service(token_row, db)
        label_id = _get_or_create_label(svc, name, color["backgroundColor"], color["textColor"])
        if label_id:
            _apply_label_ids(svc, message_id, [label_id])
    except Exception as exc:  # noqa: BLE001
        logger.error("apply_manager_review_label blocked or failed for %s / %s (manager=%s): %s", token_row.talent_key, message_id, manager_name, exc)


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


def draft_exists_in_gmail(token_row, gmail_draft_id: str, db=None) -> bool:
    """Returns True if the draft still exists in Gmail Drafts (not yet sent/deleted)."""
    service = _gmail_service(token_row, db)
    try:
        service.users().drafts().get(userId="me", id=gmail_draft_id).execute()
        return True
    except HttpError as exc:
        if exc.resp.status == 404:
            return False
        raise


def thread_has_sent_reply(token_row, thread_id: str, original_message_id: str, db=None) -> bool:
    """Returns True if the thread contains a SENT message other than the original."""
    service = _gmail_service(token_row, db)
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Date"],
        ).execute()
        for msg in thread.get("messages", []):
            if msg["id"] == original_message_id:
                continue
            if "SENT" in msg.get("labelIds", []):
                return True
        return False
    except HttpError:
        return False
