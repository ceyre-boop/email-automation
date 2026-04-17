"""
Pytest configuration and shared fixtures.

All tests use an in-memory SQLite database so no real Postgres/Supabase is needed.
All external calls (OpenAI, Gmail, Google Sheets, OAuth) are mocked.
"""
from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
from backend.routers.deps import get_db


# ── SQLite in-memory DB fixture ───────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
    """FastAPI TestClient with DB overridden to use in-memory SQLite."""
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helper factories ───────────────────────────────────────────────────────────

def make_token(db_session, talent_key: str = "Sylvia", active: bool = True) -> TalentToken:
    token = TalentToken(
        talent_key=talent_key,
        email=f"{talent_key.lower()}@gmail.com",
        access_token="fake-access-token",
        refresh_token="fake-refresh-token",
        token_expiry=datetime.utcnow() + timedelta(hours=1),
        active=active,
    )
    db_session.add(token)
    db_session.commit()
    return token


def make_draft(
    db_session,
    talent_key: str = "Sylvia",
    status: DraftStatus = DraftStatus.pending,
    gmail_draft_id: str | None = None,
) -> Draft:
    draft = Draft(
        talent_key=talent_key,
        gmail_message_id="msg-001",
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
