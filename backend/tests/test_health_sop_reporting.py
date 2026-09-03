"""/health must report the SOP that is actually live.

main.py restores the active SOP version from the DB over sheets/sop.md at
startup, *after* cron.py is imported. Computing the hash at import time made
/health report the git-repo seed file instead — a stale repo copy then looks
identical to the live SOP, which is how a 180-line drift went unnoticed.
"""
import hashlib

from backend.routers import cron


def test_sop_stats_reparse_when_the_file_changes(tmp_path, monkeypatch):
    sop = tmp_path / "sop.md"
    sop.write_text("Talent Email AI Guidelines\nPart 1 — Global Workflow Rules\n")
    monkeypatch.setattr(cron, "_SOP_PATH", sop)
    monkeypatch.setattr(cron, "_sop_stats_cache", None)
    monkeypatch.setattr(cron, "_sop_stats_key", None)

    first_hash = cron._sop_stats()[0]
    assert first_hash == hashlib.sha256(sop.read_bytes()).hexdigest()[:12]

    # Simulate the startup restore writing a different SOP over the same path.
    sop.write_text("Talent Email AI Guidelines\nPart 1 — Global Workflow Rules\nchanged\n")
    second_hash = cron._sop_stats()[0]

    assert second_hash != first_hash
    assert second_hash == hashlib.sha256(sop.read_bytes()).hexdigest()[:12]


def test_missing_sop_file_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(cron, "_SOP_PATH", tmp_path / "nope.md")
    monkeypatch.setattr(cron, "_sop_stats_cache", None)
    monkeypatch.setattr(cron, "_sop_stats_key", None)
    sop_hash, count, warnings, _ = cron._sop_stats()
    assert sop_hash == "MISSING" and count == 0 and warnings


def test_active_sop_version_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("backend.models.db.get_session_factory", boom)
    out = cron._active_sop_version()
    assert out["sop_version_id"] is None
    assert "sop_version_error" in out


def test_active_sop_version_uses_real_model_field_names():
    """Guards against silently returning sop_version_error for a typo'd column.

    The first cut read `row.label`; the model field is `version_label`, so
    /health degraded to nulls in production while still returning 200.
    """
    from backend.models.db import SopVersion
    for field in ("id", "version_label", "uploaded_at", "is_active", "doc_type"):
        assert hasattr(SopVersion, field), f"SopVersion.{field} missing"
