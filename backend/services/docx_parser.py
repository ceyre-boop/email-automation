"""
Extract rich-text content from SOP .docx files.

python-docx's paragraph.text strips bold formatting and hyperlinks, which
corrupts the approved responses every time someone uploads the CEO's SOP docx.
This module walks the XML directly to preserve **bold** and [anchor](url) links.
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
    Convert a paragraph to Markdown text, preserving bold and hyperlinks.

    Walk para._p XML children instead of using para.runs so that inline
    <w:hyperlink> elements (which python-docx exposes only via the XML tree)
    are captured with their target URL.
    """
    from docx.oxml.ns import qn

    parts: list[str] = []

    for child in para._p:
        local = child.tag.rsplit("}", 1)[-1]

        if local == "hyperlink":
            r_id = child.get(f"{{{_RSHIP_NS}}}id", "")
            url = rels.get(r_id, "")
            # Collect text from all <w:r> children inside the hyperlink
            anchor_parts: list[str] = []
            for r in child:
                if r.tag.endswith("}r"):
                    t_el = r.find(qn("w:t"))
                    if t_el is not None and t_el.text:
                        anchor_parts.append(t_el.text)
            anchor = "".join(anchor_parts)
            if anchor.strip() and url:
                parts.append(f"[{anchor}]({url})")
            elif anchor.strip():
                parts.append(anchor)

        elif local == "r":
            # Regular text run
            t_el = child.find(qn("w:t"))
            if t_el is None or not t_el.text:
                continue
            text_val = t_el.text
            rpr = child.find(qn("w:rPr"))
            # A run is bold if <w:b/> exists inside <w:rPr>; ignore runs that
            # are entirely whitespace so we don't produce **   ** artefacts.
            is_bold = (
                rpr is not None
                and rpr.find(qn("w:b")) is not None
                and text_val.strip()
            )
            if is_bold:
                parts.append(f"**{text_val}**")
            else:
                parts.append(text_val)

    return "".join(parts)


# ── Normalisation ─────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """
    Clean Word formatting artefacts from extracted markdown.

    Word often emits adjacent bold runs as separate spans, producing patterns
    like ``**   ****1 TikTok** **[link](url)`` that look wrong in the final SOP.
    These regexes collapse them into clean markdown.
    """
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
