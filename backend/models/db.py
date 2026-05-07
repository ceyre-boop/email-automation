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
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

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
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    body_text: Mapped[str | None] = mapped_column(Text)
    email_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum(EmailStatus), default=EmailStatus.flagged, nullable=False
    )


class OAuthState(Base):
    """Short-lived CSRF state tokens for the OAuth flow. DB-backed so restarts don't break reconnects."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    pinned_talent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Draft(Base):
    """AI-generated reply drafts awaiting human approval."""

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("gmail_message_id", name="uq_drafts_gmail_message_id"),
    )

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
    cc_recipients: Mapped[str | None] = mapped_column(Text)
    # ID of the draft saved inside the talent's Gmail account (if saved)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(128))
    message_id_header: Mapped[str | None] = mapped_column(String(512))  # for In-Reply-To threading on approve
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


class InboxEmail(Base):
    """Server-side cache of each talent's Gmail inbox. Upserted every sync cycle."""

    __tablename__ = "inbox_emails"
    __table_args__ = (
        UniqueConstraint("talent_key", "gmail_message_id", name="uq_inbox_talent_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    sender: Mapped[str | None] = mapped_column(String(256))
    subject: Mapped[str | None] = mapped_column(String(512))
    snippet: Mapped[str | None] = mapped_column(String(512))
    email_date: Mapped[datetime | None] = mapped_column(DateTime)
    is_unread: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    label_ids: Mapped[str | None] = mapped_column(String(512))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    score: Mapped[int | None] = mapped_column(Integer)
    brand_name: Mapped[str | None] = mapped_column(String(256))
    proposed_rate: Mapped[float | None] = mapped_column(Float)
    offer_type: Mapped[str | None] = mapped_column(String(128))
    triage_reason: Mapped[str | None] = mapped_column(Text)
    triage_status: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    body_fetch_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    body_fetch_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")


class ManagerContext(Base):
    """Manager instructions injected into every GPT-4o reply system prompt."""

    __tablename__ = "manager_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    added_by: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    talent_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    voice_profile: Mapped[str | None] = mapped_column(Text, nullable=True)


class PollHealth(Base):
    """Rolling log of every poll cycle — powers the observability dashboard."""

    __tablename__ = "poll_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    emails_found: Mapped[int] = mapped_column(Integer, default=0)
    emails_processed: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class TriageAudit(Base):
    """Full audit trail of every triage call — for prompt debugging and accuracy tracking."""

    __tablename__ = "triage_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    talent_key: Mapped[str | None] = mapped_column(String(64), index=True)
    parsed_score: Mapped[int | None] = mapped_column(Integer)
    brand_detected: Mapped[str | None] = mapped_column(String(256))
    rate_detected: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[str | None] = mapped_column(String(16))
    reasoning: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── Engine / session factory ─────────────────────────────────────────────────
# These are created lazily so tests can override DATABASE_URL before import.


def _make_engine():
    settings = get_settings()
    db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
    # NullPool: open/close a connection per request — prevents exhausting
    # Supabase Session Pooler's limited free-tier connection slots.
    return create_engine(db_url, poolclass=NullPool)


def get_engine():
    return _make_engine()


def get_session_factory():
    engine = _make_engine()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    """Create all tables and run additive column migrations (idempotent)."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    # Add body_text column to existing tables that predate it
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS body_text TEXT"
            ))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text(
                "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS email_date TIMESTAMP"
            ))
            conn.commit()
        except Exception:
            pass
        # Token health tracking columns
        for stmt in [
            "ALTER TABLE talents ADD COLUMN IF NOT EXISTS last_poll_at TIMESTAMP",
            "ALTER TABLE talents ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0",
            "ALTER TABLE talents ADD COLUMN IF NOT EXISTS last_error TEXT",
            # Inbox body fetch resilience
            "ALTER TABLE inbox_emails ADD COLUMN IF NOT EXISTS body_fetch_attempts INTEGER DEFAULT 0",
            "ALTER TABLE inbox_emails ADD COLUMN IF NOT EXISTS body_fetch_failed BOOLEAN DEFAULT FALSE",
            # Manager context scoping + voice profiles
            "ALTER TABLE manager_context ADD COLUMN IF NOT EXISTS talent_key TEXT",
            "ALTER TABLE manager_context ADD COLUMN IF NOT EXISTS voice_profile TEXT",
             # Email threading: store original Message-ID header so approved replies thread correctly
             "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS message_id_header VARCHAR(512)",
             "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS cc_recipients TEXT",
         ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        # Unique index on drafts.gmail_message_id — prevents duplicate drafts across poll cycles
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_gmail_message_id "
                "ON drafts (gmail_message_id)"
            ))
            conn.commit()
        except Exception:
            pass
