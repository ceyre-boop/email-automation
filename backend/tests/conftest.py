from __future__ import annotations

import itertools
import os
import json
import pathlib
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── Set required env vars BEFORE any backend module is imported ──────────────
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost/auth/callback")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault(
    "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON",
    json.dumps({"type": "service_account", "project_id": "test"}),
)
os.environ.setdefault("AGENCY_SECRET_KEY", "test-secret")

from backend.core.config import get_settings
from backend.models.db import Base, Draft, DraftStatus, EmailStatus, ProcessedEmail, TalentToken
from backend.routers.deps import get_db, verify_api_key

# ── Test-only SOP fixture ─────────────────────────────────────────────────────
# The real sheets/sop.md contains only live talent profiles.  Test-only
# "talents" (Sylvia, KatrinaD) live in tests/fixtures/test_talents_sop.md.
# We redirect _SOP_MD_PATH for every test so the merged file (real + fixtures)
# is used.  We can't just seed the module-level cache because code under test
# calls clear_sop_cache(), which silently falls back to the real file.

_FIXTURE_SOPmd = pathlib.Path(__file__).parent / "fixtures" / "test_talents_sop.md"


@pytest.fixture(autouse=True)
def _patch_sop_md_path(tmp_path):
    """Redirect _SOP_MD_PATH to a merged file (real sop.md + test fixtures).

    Cannot seed _sop_md_cache because clear_sop_cache() is called by code
    under test, silently falling back to the real file.  Instead we write a
    merged temp file and point the module-level path at it; any
    clear_sop_cache() call will re-read from the merged file.
    """
    from backend.services import reply

    real_content = reply._SOP_MD_PATH.read_text(encoding="utf-8") if reply._SOP_MD_PATH.exists() else ""
    fixture_content = _FIXTURE_SOPmd.read_text(encoding="utf-8")
    merged = tmp_path / "sop_merged.md"
    merged.write_text(real_content + "\n\n" + fixture_content, encoding="utf-8")

    original_path = reply._SOP_MD_PATH
    reply._SOP_MD_PATH = merged
    reply.clear_sop_cache()

    yield

    reply._SOP_MD_PATH = original_path
    reply.clear_sop_cache()


# ── SQLite in-memory DB fixture ───────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="function")
def client(db_session):
    """FastAPI TestClient with DB overridden to use in-memory SQLite.

    Also bypasses verify_api_key — tests focus on business logic, not auth.
    Individual tests that need to test auth can override this via autouse fixtures
    (see test_external_channel_api.py for an example).
    """
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[verify_api_key] = lambda: None
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helper factories ───────────────────────────────────────────────────────────

def make_token(db_session, talent_key: str = "Sylvia", active: bool = True, email: str | None = None) -> TalentToken:
    token = TalentToken(
        talent_key=talent_key,
        email=email or f"{talent_key.lower()}@gmail.com",
        access_token="fake-access-token",
        refresh_token="fake-refresh-token",
        token_expiry=datetime.utcnow() + timedelta(hours=1),
        active=active,
    )
    db_session.add(token)
    db_session.commit()
    return token


# Counter ensures every make_draft() call gets a unique gmail_message_id within a test session.
_draft_counter = itertools.count(1)


def make_draft(
    db_session,
    talent_key: str = "Sylvia",
    status: DraftStatus = DraftStatus.pending,
    gmail_draft_id: str | None = None,
    gmail_message_id: str | None = None,
) -> Draft:
    if gmail_message_id is None:
        gmail_message_id = f"msg-{next(_draft_counter):06d}"
    draft = Draft(
        talent_key=talent_key,
        gmail_message_id=gmail_message_id,
        thread_id="thread-001",
        sender="brand@nike.com",
        subject="Partnership opportunity",
        brand_name="Nike",
        proposed_rate=3500.0,
        offer_type="Sponsored Post",
        draft_text="Hi Nike, thanks for reaching out! Here are Sylvia's rates...",
        gmail_draft_id=gmail_draft_id,
        status=status,
        is_escalate=False,
    )
    db_session.add(draft)
    db_session.commit()
    return draft
