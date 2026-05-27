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
    processing = "processing"   # Claimed by a worker — prevents concurrent re-processing
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
    # ── Extended log schema ───────────────────────────────────────────────────
    sender_domain: Mapped[str | None] = mapped_column(String(256))
    email_length: Mapped[int | None] = mapped_column(Integer)
    sentiment_score: Mapped[int | None] = mapped_column(Integer)   # 0-10
    urgency_score: Mapped[int | None] = mapped_column(Integer)     # 0-10
    risk_score: Mapped[int | None] = mapped_column(Integer)        # 0-10
    is_thread: Mapped[bool | None] = mapped_column(Boolean)
    has_attachments: Mapped[bool | None] = mapped_column(Boolean)
    has_links: Mapped[bool | None] = mapped_column(Boolean)
    alternatives_considered: Mapped[str | None] = mapped_column(Text)
    time_to_classify_ms: Mapped[int | None] = mapped_column(Integer)
    time_to_draft_ms: Mapped[int | None] = mapped_column(Integer)
    human_override_occurred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    scenario_needs_improvement: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")


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
    # Human-touch audit
    human_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    human_edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    human_edited_by: Mapped[str | None] = mapped_column(String(128))
    original_draft_text: Mapped[str | None] = mapped_column(Text)  # AI original before any edits
    triggered_by_job: Mapped[str | None] = mapped_column(String(32))
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")


class DraftEditLog(Base):
    """Every human edit to a draft — full audit trail."""

    __tablename__ = "draft_edit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    edited_by: Mapped[str | None] = mapped_column(String(128))
    edit_note: Mapped[str | None] = mapped_column(Text)
    text_before: Mapped[str] = mapped_column(Text, nullable=False)
    text_after: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


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


class AppState(Base):
    """Small key/value store for persistent dashboard state."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        onupdate=datetime.utcnow,
    )


class MarcoMessage(Base):
    """AI-generated system narrative messages surfaced to the manager (Marco)."""

    __tablename__ = "marco_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)  # volume|quality|spam|escalation|health
    talent_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)  # info|warning|critical
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GuardianAuditLog(Base):
    """Persistent audit trail for Guardian circuit-breaker actions."""

    __tablename__ = "guardian_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    talent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="guardian", nullable=False, server_default="guardian")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


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
        try:
            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS guardian_audit_log (
                    id SERIAL PRIMARY KEY,
                    action VARCHAR(64) NOT NULL,
                    talent_key VARCHAR(64),
                    reason TEXT NOT NULL,
                    detail TEXT,
                    triggered_by VARCHAR(64) NOT NULL DEFAULT 'guardian',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
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
            # Human-touch audit columns
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited BOOLEAN DEFAULT FALSE",
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited_at TIMESTAMP",
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited_by VARCHAR(128)",
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS original_draft_text TEXT",
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS triggered_by_job VARCHAR(32)",
            "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS dismissed BOOLEAN NOT NULL DEFAULT FALSE",
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
        # Extended log schema columns on processed_emails
        for stmt in [
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS sender_domain VARCHAR(256)",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS email_length INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS sentiment_score INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS urgency_score INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS risk_score INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS is_thread BOOLEAN",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS has_attachments BOOLEAN",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS has_links BOOLEAN",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS alternatives_considered TEXT",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS time_to_classify_ms INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS time_to_draft_ms INTEGER",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS human_override_occurred BOOLEAN DEFAULT FALSE",
            "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS scenario_needs_improvement BOOLEAN DEFAULT FALSE",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        # Add 'processing' value to the emailstatus enum type (idempotent — fails silently if already exists)
        try:
            conn.execute(text("ALTER TYPE emailstatus ADD VALUE IF NOT EXISTS 'processing'"))
            conn.commit()
        except Exception:
            pass
        # Clean up ghost claim rows stuck at score=0 from crashed poll cycles
        try:
            conn.execute(text(
                "DELETE FROM processed_emails WHERE score = 0 "
                "AND processed_at < NOW() - INTERVAL '10 minutes'"
            ))
            conn.commit()
        except Exception:
            pass
