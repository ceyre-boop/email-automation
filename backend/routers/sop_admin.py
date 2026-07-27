"""SOP Admin router — manage talent data without directly editing sop.md."""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
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
    """Write the sop_text returned by import-docx to sop.md and commit.

    Merge rules applied before writing (single commit):
    - Existing talent: preserve their current Paused + Auto Send values so a docx
      upload can't accidentally reset a green/yellow talent.
    - New talent: automatically set Paused=no, Auto Send=no (yellow mode) so managers
      don't need a manual unpause step after adding a talent.
    """
    sop_text = payload.get("sop_text", "")
    incoming_profiles = parse_sop_md(sop_text)
    if len(incoming_profiles) < 5:
        raise HTTPException(status_code=400, detail="Re-validation failed — aborting write")

    try:
        existing_profiles = parse_sop_md(_read_sop())
    except Exception:
        existing_profiles = {}

    merged_text = sop_text
    new_talent_keys: list[str] = []

    for key, incoming in incoming_profiles.items():
        existing = existing_profiles.get(key) or next(
            (p for k, p in existing_profiles.items() if k.lower() == key.lower()), None
        )
        try:
            if existing:
                if incoming.paused != existing.paused:
                    merged_text = _writer.update_talent_field(
                        merged_text, key, "Paused", "yes" if existing.paused else "no"
                    )
                if incoming.auto_send != existing.auto_send:
                    merged_text = _writer.update_talent_field(
                        merged_text, key, "Auto Send", "yes" if existing.auto_send else "no"
                    )
            else:
                new_talent_keys.append(key)
                if incoming.paused:
                    merged_text = _writer.update_talent_field(merged_text, key, "Paused", "no")
                if incoming.auto_send:
                    merged_text = _writer.update_talent_field(merged_text, key, "Auto Send", "no")
        except ValueError:
            pass

    _writer.write_sop_md(merged_text)
    return {
        "status": "ok",
        "talent_count": len(incoming_profiles),
        "new_talents": new_talent_keys,
    }


# ── V2 SOP upload (rich-text aware) ──────────────────────────────────────────


@router.post("/api/sop/upload-v2", dependencies=[Depends(verify_api_key)])
async def upload_sop_v2(
    file: UploadFile = File(...),
    label: str = Form(""),
):
    """
    Accept a .docx SOP file, extract rich text (preserving **bold** and
    [hyperlinks](url)), merge the approved responses + personal emails into
    the existing sop.md (keeping all metadata: Key, Gmail, Min Rate, etc.),
    write the result to disk, persist it in the sop_versions table, and
    clear all in-memory caches so the new SOP is live immediately.

    Does NOT trigger a Render redeploy — that's a separate button.
    The startup handler restores the active DB version on the next deploy.
    """
    try:
        from backend.services.docx_parser import extract_talent_sections
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"docx_parser not available: {exc}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        docx_talents = extract_talent_sections(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse docx: {exc}")

    if not docx_talents:
        raise HTTPException(
            status_code=400,
            detail="No 'Talent: Name' sections found in the docx — check the file format",
        )

    # Read current sop.md and parse existing profiles
    sop_text = _read_sop()
    existing_profiles = parse_sop_md(sop_text)

    matched: list[str] = []
    unmatched_sop: list[str] = []
    unmatched_docx: list[str] = []
    warnings: list[str] = []

    for key, profile in existing_profiles.items():
        name_lower = profile.full_name.lower()
        docx_entry = docx_talents.get(name_lower)

        # Fallback: match by first word of name (catches "Brittany" matching "Brittany Kuhl")
        if docx_entry is None:
            first_word = name_lower.split()[0] if name_lower.split() else ""
            docx_entry = next(
                (v for k, v in docx_talents.items() if k.startswith(first_word + " ") or k == first_word),
                None,
            )

        if docx_entry is None:
            unmatched_sop.append(profile.key)
            continue

        matched.append(profile.key)

        if docx_entry.get("approved_response"):
            try:
                sop_text = _writer.update_approved_response(
                    sop_text, profile.key, docx_entry["approved_response"]
                )
            except ValueError as exc:
                warnings.append(f"{profile.key}: approved response not updated — {exc}")

        if docx_entry.get("personal_emails"):
            try:
                sop_text = _writer.update_personal_emails(
                    sop_text, profile.key, docx_entry["personal_emails"]
                )
            except ValueError as exc:
                warnings.append(f"{profile.key}: personal emails not updated — {exc}")

    # Find docx talents that had no match in sop.md
    matched_name_lowers = {existing_profiles[k].full_name.lower() for k in matched}
    for docx_name, docx_entry in docx_talents.items():
        if docx_name not in matched_name_lowers:
            unmatched_docx.append(docx_entry["full_name"])

    # Write updated sop.md to disk + clear caches
    _writer.write_sop_md(sop_text)

    # Persist merged sop.md to sop_versions table as new active version
    version_id: int | None = None
    try:
        from backend.models.db import SopVersion, get_session_factory
        db = get_session_factory()()
        try:
            db.query(SopVersion).filter(SopVersion.is_active == True).update(  # noqa: E712
                {"is_active": False}
            )
            version_label = label.strip() or f"Upload {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            new_ver = SopVersion(
                version_label=version_label,
                raw_content=sop_text,
                talent_count=len(existing_profiles),
                is_active=True,
            )
            db.add(new_ver)
            db.commit()
            db.refresh(new_ver)
            version_id = new_ver.id
        finally:
            db.close()
    except Exception as exc:
        warnings.append(f"DB version save failed (non-fatal — SOP is still live on disk): {exc}")

    return {
        "status": "ok",
        "matched": matched,
        "unmatched_sop": unmatched_sop,   # sop.md talents with no docx match (response unchanged)
        "unmatched_docx": unmatched_docx,  # docx talents with no sop.md match (ignored)
        "warnings": warnings,
        "version_id": version_id,
        "talent_count": len(existing_profiles),
    }


# ── Version history ───────────────────────────────────────────────────────────


@router.get("/api/sop/versions", dependencies=[Depends(verify_api_key)])
def list_sop_versions():
    """Return the 20 most recent SOP versions from the DB."""
    try:
        from backend.models.db import SopVersion, get_session_factory
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    db = get_session_factory()()
    try:
        versions = (
            db.query(SopVersion)
            .order_by(SopVersion.uploaded_at.desc())
            .limit(20)
            .all()
        )
        return {
            "versions": [
                {
                    "id": v.id,
                    "label": v.version_label,
                    "talent_count": v.talent_count,
                    "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
                    "is_active": v.is_active,
                }
                for v in versions
            ]
        }
    finally:
        db.close()


@router.post("/api/sop/versions/{version_id}/restore", dependencies=[Depends(verify_api_key)])
def restore_sop_version(version_id: int):
    """Restore a past SOP version: write it to sop.md, mark it active in DB."""
    try:
        from backend.models.db import SopVersion, get_session_factory
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    db = get_session_factory()()
    try:
        version = db.query(SopVersion).filter(SopVersion.id == version_id).first()
        if version is None:
            raise HTTPException(status_code=404, detail=f"Version {version_id} not found")

        # Write to disk + clear caches
        _writer.write_sop_md(version.raw_content)

        # Mark this version active
        db.query(SopVersion).filter(SopVersion.is_active == True).update(  # noqa: E712
            {"is_active": False}
        )
        version.is_active = True
        db.commit()

        return {"status": "ok", "version_id": version_id, "label": version.version_label}
    finally:
        db.close()


# ── Render deploy hook ────────────────────────────────────────────────────────


@router.post("/api/sop/deploy-render", dependencies=[Depends(verify_api_key)])
def deploy_to_render():
    """
    Trigger a Render redeploy via the deploy hook URL stored in
    RENDER_DEPLOY_HOOK_URL env var.  The SOP itself is already live on disk
    (write_sop_md is instant); this deploy is only needed so the next startup
    also restores from DB correctly after any code changes.
    """
    hook = get_settings().render_deploy_hook_url
    if not hook:
        raise HTTPException(
            status_code=400,
            detail="RENDER_DEPLOY_HOOK_URL not set — add it in Render → Environment Variables",
        )
    try:
        req = urllib.request.Request(hook, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": "ok", "http_status": resp.status}
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Render hook returned {exc.code}: {exc.reason}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Render deploy hook failed: {exc}")
