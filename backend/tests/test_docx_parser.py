"""
Tests for backend/services/docx_parser.py — the rich-text DOCX → markdown extractor.

These tests create minimal .docx files in memory using python-docx so they don't
depend on any external fixture files.  python-docx must be installed on the test
runner; the test is skipped with a clear message if it's not.
"""
from __future__ import annotations

from io import BytesIO

import pytest

try:
    from docx import Document
    from docx.shared import Pt
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

pytestmark = pytest.mark.skipif(not HAS_DOCX, reason="python-docx not installed")


def _make_docx(**sections) -> bytes:
    """
    Build a minimal SOP-style .docx in memory.

    Keyword args control what paragraphs are written:
      talent_name  – str, written as "Talent: <name>"
      approved     – str, written after "Approved Response:"
      emails       – list[str], written after "Personal Emails:"
    """
    doc = Document()
    if sections.get("talent_name"):
        doc.add_paragraph(f"Talent: {sections['talent_name']}")
    if sections.get("approved") is not None:
        doc.add_paragraph("Scenario A: Initial Inbound")
        doc.add_paragraph("Approved Response:")
        for line in sections["approved"].splitlines():
            doc.add_paragraph(line)
    if sections.get("emails") is not None:
        doc.add_paragraph("Personal Emails:")
        for email in sections["emails"]:
            doc.add_paragraph(f"- {email}")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── Basic extraction tests ────────────────────────────────────────────────────

def test_extract_single_talent_approved_response():
    from backend.services.docx_parser import extract_talent_sections

    docx = _make_docx(
        talent_name="Test Talent",
        approved="Hi there! Thanks for reaching out.",
    )
    result = extract_talent_sections(docx)
    assert "test talent" in result
    entry = result["test talent"]
    assert entry["full_name"] == "Test Talent"
    assert "Hi there" in entry["approved_response"]


def test_extract_personal_emails():
    from backend.services.docx_parser import extract_talent_sections

    docx = _make_docx(
        talent_name="Email Talent",
        approved="Thanks for the opportunity!",
        emails=["personal@gmail.com", "alt@yahoo.com"],
    )
    result = extract_talent_sections(docx)
    assert "email talent" in result
    assert set(result["email talent"]["personal_emails"]) == {"personal@gmail.com", "alt@yahoo.com"}


def test_extract_no_talents_returns_empty():
    from backend.services.docx_parser import extract_talent_sections

    doc = Document()
    doc.add_paragraph("This is some preamble text with no talent sections.")
    buf = BytesIO()
    doc.save(buf)

    result = extract_talent_sections(buf.getvalue())
    assert result == {}


def test_extract_multiple_talents():
    from backend.services.docx_parser import extract_talent_sections

    doc = Document()
    doc.add_paragraph("Talent: Alice")
    doc.add_paragraph("Approved Response:")
    doc.add_paragraph("Hello from Alice!")
    doc.add_paragraph("Talent: Bob")
    doc.add_paragraph("Approved Response:")
    doc.add_paragraph("Hello from Bob!")
    buf = BytesIO()
    doc.save(buf)

    result = extract_talent_sections(buf.getvalue())
    assert "alice" in result
    assert "bob" in result
    assert "Hello from Alice" in result["alice"]["approved_response"]
    assert "Hello from Bob" in result["bob"]["approved_response"]


def test_literal_markdown_is_preserved_verbatim():
    """The author types markdown as literal text — it must survive byte-for-byte.

    Regression guard. A previous parser re-emitted "**" for Word's own bold
    runs. Because the author's literal "**" was already in the text, that
    double-wrapped ("****fashion****") and the cleanup regexes then ate the
    opening delimiter, yielding "1 TikTok**" — an orphan marker that renders
    literally in the sent email. It corrupted 14 of 18 talents.

    The rate line below is the real authored shape, including the 5-space
    indent, and it is also formatted bold in Word — exactly the combination
    that broke before.
    """
    from backend.services.docx_parser import extract_talent_sections

    rate_line = "     **1 TikTok** [grayson.finks](https://www.tiktok.com/@grayson.finks) - $750"

    doc = Document()
    doc.add_paragraph("Talent: Bold Talent")
    doc.add_paragraph("Approved Response:")
    # A greeting always precedes the rate lines in the real doc; the block is
    # strip()ed as a whole, so the indent only survives on non-leading lines.
    doc.add_paragraph("Happy to share her rates below:")
    p = doc.add_paragraph()
    run = p.add_run(rate_line)
    run.bold = True  # Word bold ON TOP of the literal ** the author typed

    buf = BytesIO()
    doc.save(buf)

    ar = extract_talent_sections(buf.getvalue())["bold talent"]["approved_response"]

    assert ar.endswith(rate_line), f"expected literal text, got {ar!r}"
    assert "****" not in ar, "Word bold was re-emitted on top of literal markdown"
    assert ar.count("**") % 2 == 0, "unbalanced bold delimiters would render literally"


def test_leading_indent_on_rate_lines_is_preserved():
    """Leading whitespace is significant — rate lines carry a 5-space indent."""
    from backend.services.docx_parser import extract_talent_sections

    doc = Document()
    doc.add_paragraph("Talent: Indent Talent")
    doc.add_paragraph("Approved Response:")
    doc.add_paragraph("Rates below:")
    doc.add_paragraph("     **1 UGC Video** - $400")

    buf = BytesIO()
    doc.save(buf)

    ar = extract_talent_sections(buf.getvalue())["indent talent"]["approved_response"]
    assert "\n     **1 UGC Video** - $400" in ar, f"indent lost: {ar!r}"


def test_empty_docx_returns_empty():
    from backend.services.docx_parser import extract_talent_sections

    doc = Document()
    buf = BytesIO()
    doc.save(buf)

    result = extract_talent_sections(buf.getvalue())
    assert result == {}
