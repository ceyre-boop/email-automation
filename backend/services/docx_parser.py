"""
Extract SOP content from .docx files.

IMPORTANT — do not "improve" this by walking runs for bold/hyperlinks.

The SOP doc is authored with the markdown typed as LITERAL TEXT: the author
writes the characters ``**1 TikTok**`` and ``[anchor](url)`` directly into
Word. They are not Word bold runs and not Word hyperlink relationships. So
``paragraph.text`` already returns exactly the string sop.md needs, and it
round-trips 18/18 approved responses character-for-character.

A previous version of this module walked the XML tree to re-emit ``**`` for
Word's own bold formatting. Because the author's literal ``**`` was still
there, that double-wrapped everything (``****fashion****``) and the cleanup
regexes then ate the opening marker, producing ``1 TikTok**`` with an orphan
delimiter that renders literally in the sent email. It corrupted 14 of 18
talents. Verified by round-tripping the authored docx against sop.md.
"""
from __future__ import annotations

import re
from io import BytesIO


# ── Heading matchers ──────────────────────────────────────────────────────────

_TALENT_RE = re.compile(r"^Talent\s*:\s*(?P<name>.+)$", re.IGNORECASE)
_APPROVED_RE = re.compile(r"^Approved\s+Response\s*:?\s*$", re.IGNORECASE)
_SCENARIO_RE = re.compile(r"^Scenario\s+[A-Z]\b", re.IGNORECASE)
_PERSONAL_EMAIL_RE = re.compile(r"^Personal\s+Emails?\s*:?\s*$", re.IGNORECASE)
_BULLET_EMAIL_RE = re.compile(r"^[-•]\s*(?P<email>\S+@\S+\.\S+)\s*$")

# Relationship type fragment for hyperlinks
_HYPERLINK_REL = "hyperlink"


# ── Relationship helpers ──────────────────────────────────────────────────────


def _get_rels(doc) -> dict[str, str]:
    """Return {rel_id: url} for all hyperlink relationships in the document part."""
    rels: dict[str, str] = {}
    for rel_id, rel in doc.part.rels.items():
        if _HYPERLINK_REL in rel.reltype:
            try:
                url = rel.target_ref
            except AttributeError:
                url = getattr(rel, "_target", "")
            rels[rel_id] = url
    return rels


# ── Paragraph → Markdown ──────────────────────────────────────────────────────

_RSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _para_to_md(para, rels: dict[str, str]) -> str:
    """
    Return the paragraph's literal text.

    The markdown in the SOP doc is typed by the author as plain characters, so
    the literal text IS the markdown. Re-emitting Word's own bold/hyperlink
    formatting on top of it double-wraps and corrupts the response — see the
    module docstring. `rels` is retained for signature compatibility.
    """
    return para.text


# ── Normalisation ─────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """
    Strip trailing whitespace per line. Nothing else.

    The asterisk-collapsing regexes that used to live here existed only to mop
    up artefacts created by re-emitting Word's bold formatting. That extraction
    is gone, so there are no artefacts — and the regexes were themselves
    destructive: ``**     **1 TikTok**`` came out as ``1 TikTok**``, losing the
    opening delimiter and shifting the authored indent. Leading whitespace is
    significant (rate lines carry a 5-space indent) and must be preserved.
    """
    return "\n".join(line.rstrip() for line in text.splitlines())


def _normalize_legacy_unused(text: str) -> str:  # pragma: no cover - kept for reference
    # 3+ asterisks → **  (catches *** or **** from adjacent spans)
    text = re.sub(r"\*{3,}", "**", text)
    # Bold runs containing only whitespace: **   ** → spaces
    text = re.sub(r"\*\*(\s+)\*\*", r"\1", text)
    # Completely empty bold markers: **** → nothing
    text = re.sub(r"\*\*\s*\*\*", "", text)
    # Merge adjacent bold spans separated by optional whitespace: **X****Y** → **XY**
    # Run multiple passes so deeply nested artefacts collapse fully.
    for _ in range(4):
        text = re.sub(r"\*\*([^*\n]+?)\*\*[ \t]*\*\*([^*\n]+?)\*\*", r"**\1\2**", text)
    # Strip trailing whitespace from each line
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text


# ── Main extractor ────────────────────────────────────────────────────────────


def extract_talent_sections(docx_bytes: bytes) -> dict[str, dict]:
    """
    Parse a .docx SOP file and return extracted talent data.

    Returns a dict keyed by *lowercase full name*:
    ::

        {
            "brittany kuhl": {
                "full_name": "Brittany Kuhl",
                "approved_response": "...",   # markdown with **bold** + [links](url)
                "personal_emails": ["bk@gmail.com"],
            },
            ...
        }

    Only Scenario A (primary response) and Personal Emails are extracted;
    metadata fields (Key, Gmail, Min Rate, etc.) live in sop.md and are NOT
    present in the CEO's docx — the caller is responsible for merging.
    """
    from docx import Document

    doc = Document(BytesIO(docx_bytes))
    rels = _get_rels(doc)

    talents: dict[str, dict] = {}
    current_name: str | None = None
    current_key: str | None = None  # lowercase full name used as dict key
    mode: str | None = None         # "response" | "emails" | None
    response_lines: list[str] = []
    email_list: list[str] = []

    def _flush() -> None:
        nonlocal current_name, current_key, mode, response_lines, email_list
        if current_key is not None:
            talents[current_key] = {
                "full_name": current_name,
                "approved_response": _normalize("\n".join(response_lines)).strip(),
                "personal_emails": list(email_list),
            }
        current_name = None
        current_key = None
        mode = None
        response_lines = []
        email_list = []

    for para in doc.paragraphs:
        raw = para.text.strip()

        # Skip blank lines outside of response mode
        if not raw and mode != "response":
            continue

        # ── New Talent heading ─────────────────────────────────────────────
        m = _TALENT_RE.match(raw)
        if m:
            _flush()
            current_name = m.group("name").strip()
            current_key = current_name.lower()
            mode = None
            continue

        if current_key is None:
            continue  # Haven't encountered the first Talent: yet

        # ── Scenario heading → stop current mode ──────────────────────────
        if _SCENARIO_RE.match(raw):
            mode = None
            continue

        # ── Approved Response heading ──────────────────────────────────────
        if _APPROVED_RE.match(raw):
            mode = "response"
            continue

        # ── Personal Emails heading ────────────────────────────────────────
        if _PERSONAL_EMAIL_RE.match(raw):
            mode = "emails"
            continue

        # ── Content ───────────────────────────────────────────────────────
        if mode == "response":
            # Use rich-text extraction to preserve bold + hyperlinks
            md_line = _para_to_md(para, rels)
            response_lines.append(md_line)

        elif mode == "emails":
            m_email = _BULLET_EMAIL_RE.match(raw)
            if m_email:
                email_list.append(m_email.group("email").strip())
            elif raw:
                # A non-bullet, non-empty line ends the email list
                mode = None

    _flush()
    return talents
