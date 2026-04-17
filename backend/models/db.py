"""SQLAlchemy models and Alembic-compatible Base."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from backend.core.config import get_settings


class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────────────────────────


class DraftStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    sent = "sent"
    discarded = "discarded"


class EmailStatus(str, enum.Enum):
    archived = "archived"      # Score 1
    flagged = "flagged"        # Score 2
    draft_saved = "draft_saved"  # Score 3 draft created
    sent = "sent"              # Reply sent
    error = "error"


# ── Tables ───────────────────────────────────────────────────────────────────


class TalentToken(Base):
    """Stores OAuth tokens for each talent's Gmail account."""

    __tablename__ = "talents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    talent_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    google_user_id: Mapped[str | None] = mapped_column(String(128))
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProcessedEmail(Base):
    """Log of every inbound email the system has processed."""

    __tablename__ = "processed_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    sender: Mapped[str | None] = mapped_column(String(256))
    subject: Mapped[str | None] = mapped_column(String(512))
    score: Mapped[int | None] = mapped_column(Integer)
    brand_name: Mapped[str | None] = mapped_column(String(256))
    proposed_rate: Mapped[float | None] = mapped_column(Float)
    offer_type: Mapped[str | None] = mapped_column(String(128))
    triage_reason: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum(EmailStatus), default=EmailStatus.flagged, nullable=False
    )


class Draft(Base):
    """AI-generated reply drafts awaiting human approval."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    sender: Mapped[str | None] = mapped_column(String(256))
    subject: Mapped[str | None] = mapped_column(String(512))
    brand_name: Mapped[str | None] = mapped_column(String(256))
    proposed_rate: Mapped[float | None] = mapped_column(Float)
    offer_type: Mapped[str | None] = mapped_column(String(128))
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    # ID of the draft saved inside the talent's Gmail account (if saved)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        Enum(DraftStatus), default=DraftStatus.pending, nullable=False
    )
    is_escalate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalate_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))


# ── Engine / session factory ─────────────────────────────────────────────────
# These are created lazily so tests can override DATABASE_URL before import.


def _make_engine():
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


def get_engine():
    return _make_engine()


def get_session_factory():
    engine = _make_engine()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    """Create all tables (idempotent — safe to call on startup)."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
