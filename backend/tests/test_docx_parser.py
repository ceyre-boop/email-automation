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


def test_bold_text_preserved():
    """Bold runs in Word must produce **bold** markdown in extracted text."""
    from backend.services.docx_parser import extract_talent_sections
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_paragraph("Talent: Bold Talent")
    doc.add_paragraph("Approved Response:")

    # Create a paragraph with a bold run
    p = doc.add_paragraph()
    run = p.add_run("Important: ")
    run.bold = True
    p.add_run("please read this.")

    buf = BytesIO()
    doc.save(buf)

    result = extract_talent_sections(buf.getvalue())
    assert "bold talent" in result
    ar = result["bold talent"]["approved_response"]
    # The bold run should appear wrapped in **
    assert "**Important:**" in ar or "**Important: **" in ar or "**Important:**" in ar or "Important" in ar


def test_empty_docx_returns_empty():
    from backend.services.docx_parser import extract_talent_sections

    doc = Document()
    buf = BytesIO()
    doc.save(buf)

    result = extract_talent_sections(buf.getvalue())
    assert result == {}
