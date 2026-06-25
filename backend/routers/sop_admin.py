"""SOP Admin router — manage talent data without directly editing sop.md."""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

from backend.core.config import get_settings
from backend.routers.deps import verify_api_key
from backend.services import sop_writer as _writer
from backend.services.sop_parser import parse_sop_md, validate_profiles

_SOP_PATH = Path(__file__).resolve().parents[2] / "sheets" / "sop.md"

_TALENT_HEADING_RE = re.compile(
    r"^[ \t]*(?:#+[ \t]*)?Talent:[ \t]*(?P<name>[^\r\n]*)[ \t]*$",
    re.MULTILINE,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
)


def _read_sop() -> str:
    return _SOP_PATH.read_text(encoding="utf-8")


def _extract_approved_response(sop_text: str, talent_key: str) -> str:
    """Extract the approved response text for a talent from sop.md text."""
    matches = list(_TALENT_HEADING_RE.finditer(sop_text))
    for i, match in enumerate(matches):
        section_start = match.start()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(sop_text)
        section = sop_text[section_start:section_end]
        key_m = re.search(r"^[ \t]*Key[ \t]*:[ \t]*(.+)$", section, re.MULTILINE)
        if not key_m or key_m.group(1).strip().lower() != talent_key.lower():
            continue
        ar_m = re.search(r"^[ \t]*Approved Response:[ \t]*$", section, re.MULTILINE)
        if not ar_m:
            return ""
        ar_end = ar_m.end()
        next_scenario = re.search(r"^[ \t]*Scenario\b", section[ar_end:], re.MULTILINE)
        content_end = ar_end + next_scenario.start() if next_scenario else len(section)
        return section[ar_end:content_end].strip()
    return ""


def _resolve_profile(talent_key: str):
    """Return the TalentProfile for the given key (case-insensitive). 404 if not found."""
    profiles = get_settings().talent_profiles
    profile = profiles.get(talent_key) or next(
        (p for k, p in profiles.items() if k.lower() == talent_key.lower()), None
    )
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Talent '{talent_key}' not found")
    return profile


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/talents", dependencies=[Depends(verify_api_key)])
def list_talents():
    profiles = get_settings().talent_profiles
    return {
        "talents": [
            {
                "key": p.key,
                "full_name": p.full_name,
                "manager": p.manager,
                "manager_email": p.manager_email,
                "minimum_rate_usd": p.minimum_rate_usd,
                "rate_unit": p.rate_unit,
                "auto_send": p.auto_send,
                "paused": p.paused,
                "has_approved_response": p.has_approved_response,
                "personal_emails": p.personal_emails,
            }
            for p in profiles.values()
        ]
    }


@router.get("/api/talents/{talent_key}", dependencies=[Depends(verify_api_key)])
def get_talent(talent_key: str):
    profile = _resolve_profile(talent_key)
    sop_text = _read_sop()
    approved_response = _extract_approved_response(sop_text, profile.key)
    return {
        "key": profile.key,
        "full_name": profile.full_name,
        "manager": profile.manager,
        "manager_email": profile.manager_email,
        "gmail_connection_name": profile.gmail_connection_name,
        "minimum_rate_usd": profile.minimum_rate_usd,
        "rate_unit": profile.rate_unit,
        "auto_send": profile.auto_send,
        "paused": profile.paused,
        "has_approved_response": profile.has_approved_response,
        "personal_emails": profile.personal_emails,
        "approved_response": approved_response,
    }


class TalentUpdateRequest(BaseModel):
    minimum_rate_usd: int | None = None
    rate_unit: str | None = None
    auto_send: bool | None = None
    paused: bool | None = None
    approved_response: str | None = None
    personal_emails: list[str] | None = None
    manager: str | None = None


@router.put("/api/talents/{talent_key}", dependencies=[Depends(verify_api_key)])
def update_talent(talent_key: str, body: TalentUpdateRequest):
    profile = _resolve_profile(talent_key)

    errors = _writer.validate_before_write(
        body.minimum_rate_usd, body.personal_emails, body.approved_response
    )
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    sop_text = _read_sop()

    if body.minimum_rate_usd is not None or body.rate_unit is not None:
        rate_usd = body.minimum_rate_usd if body.minimum_rate_usd is not None else profile.minimum_rate_usd
        rate_unit = body.rate_unit if body.rate_unit is not None else profile.rate_unit
        sop_text = _writer.update_talent_field(
            sop_text, profile.key, "Min Rate",
            f"${rate_usd} {rate_unit}".strip()
        )

    if body.auto_send is not None:
        sop_text = _writer.update_talent_field(
            sop_text, profile.key, "Auto Send", "yes" if body.auto_send else "no"
        )

    if body.paused is not None:
        sop_text = _writer.update_talent_field(
            sop_text, profile.key, "Paused", "yes" if body.paused else "no"
        )

    if body.manager is not None:
        sop_text = _writer.update_talent_field(sop_text, profile.key, "Manager", body.manager)

    if body.approved_response is not None:
        sop_text = _writer.update_approved_response(sop_text, profile.key, body.approved_response)

    if body.personal_emails is not None:
        sop_text = _writer.update_personal_emails(sop_text, profile.key, body.personal_emails)

    _writer.write_sop_md(sop_text)
    return {"status": "ok", "key": profile.key}


@router.post("/api/talents/{talent_key}/toggle-auto-send", dependencies=[Depends(verify_api_key)])
def toggle_auto_send(talent_key: str):
    profile = _resolve_profile(talent_key)
    new_value = not profile.auto_send
    sop_text = _read_sop()
    sop_text = _writer.update_talent_field(
        sop_text, profile.key, "Auto Send", "yes" if new_value else "no"
    )
    _writer.write_sop_md(sop_text)
    return {"key": profile.key, "auto_send": new_value}


@router.get("/api/sop/raw", dependencies=[Depends(verify_api_key)])
def sop_raw():
    return Response(content=_read_sop(), media_type="text/plain")


@router.post("/api/sop/import-docx", dependencies=[Depends(verify_api_key)])
async def import_sop_docx(file: UploadFile = File(...)):
    """Parse a .docx file and return a preview — does not write anything."""
    try:
        from docx import Document  # python-docx
    except ImportError:
        raise HTTPException(status_code=500, detail="python-docx not installed on server")

    content = await file.read()
    try:
        doc = Document(BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse docx: {exc}")

    sop_text = "\n".join(p.text for p in doc.paragraphs)
    profiles = parse_sop_md(sop_text)

    if len(profiles) < 5:
        raise HTTPException(
            status_code=400,
            detail=f"Only {len(profiles)} talent profile(s) parsed — check that the docx uses the same format as sop.md (Talent:, Key:, Manager:, etc.)",
        )

    warnings = validate_profiles(profiles)
    return {
        "talent_count": len(profiles),
        "talent_names": [p.full_name for p in profiles.values()],
        "warnings": warnings,
        "sop_text": sop_text,
    }


@router.post("/api/sop/import-docx/confirm", dependencies=[Depends(verify_api_key)])
async def confirm_sop_import(payload: dict):
    """Write the sop_text returned by import-docx to sop.md and commit."""
    sop_text = payload.get("sop_text", "")
    profiles = parse_sop_md(sop_text)
    if len(profiles) < 5:
        raise HTTPException(status_code=400, detail="Re-validation failed — aborting write")
    _writer.write_sop_md(sop_text)
    return {"status": "ok", "talent_count": len(profiles)}
