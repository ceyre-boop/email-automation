"""SQLAlchemy models and Alembic-compatible Base."""
from __future__ import annotations

import enum
import logging
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
from sqlalchemy.pool import NullPool, StaticPool

from backend.core.config import get_settings

logger = logging.getLogger(__name__)


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
    # SOP v16: original alias address the brand emailed (e.g. hana@taboost.me)
    to_address: Mapped[str | None] = mapped_column(String(256), nullable=True)


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
    # Set atomically immediately before an external Gmail send. This prevents
    # a manual approval and the auto-send worker from sending the same draft
    # concurrently. Successful sends keep the claim; failed sends clear it.
    send_claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Human-touch audit
    human_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    human_edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    human_edited_by: Mapped[str | None] = mapped_column(String(128))
    original_draft_text: Mapped[str | None] = mapped_column(Text)  # AI original before any edits
    triggered_by_job: Mapped[str | None] = mapped_column(String(32))
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    validation_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SOP v16: original alias address the brand emailed (e.g. hana@taboost.me)
    to_address: Mapped[str | None] = mapped_column(String(256), nullable=True)


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
    # subject/snippet/label_ids are TEXT, not String(512): Gmail places no practical
    # bound on any of them, and a long inbound subject line broke inbox sync in
    # production (see CLAUDE.md Incident Log — 2026-08-04 varchar(512) overflow).
    subject: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    email_date: Mapped[datetime | None] = mapped_column(DateTime)
    is_unread: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    label_ids: Mapped[str | None] = mapped_column(Text)
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


class ExternalChannelReview(Base):
    """
    Flags inbound emails where the sender asks to continue on WhatsApp, Discord, etc.

    Informational-only: never blocks triage or draft generation. Managers review these
    and dismiss when handled. Only initial inbound / first-response threads are flagged
    (message count <= 2 at detection time) — deep conversations are skipped.
    """

    __tablename__ = "external_channel_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gmail_message_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    talent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sender: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_requested: Mapped[str] = mapped_column(String(64), nullable=False)  # "WhatsApp", "Discord", "Both"
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class SopVersion(Base):
    """
    Persisted snapshot of sheets/sop.md after each successful docx upload.

    The startup event handler reads the active version from this table and
    writes it to sheets/sop.md so the SOP survives Render redeploys even if
    git commit/push fails from the running container.
    """

    __tablename__ = "sop_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_label: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    talent_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # "sop" (sheets/sop.md) or "workflow" (sheets/Automated Send Workflow.md).
    # Both documents share this table; is_active is tracked per doc_type.
    doc_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sop", server_default="sop"
    )


# ── Engine / session factory ─────────────────────────────────────────────────
# These are created lazily so tests can override DATABASE_URL before import.


_engine = None
_session_factory = None


def _make_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
        if db_url.startswith("sqlite"):
            # SQLite (tests) rejects the QueuePool kwargs used for Postgres below.
            # StaticPool is required, not optional: with the default pool every
            # connection to ":memory:" gets its OWN empty database, so a table
            # created on one connection is invisible to the next and queries fail
            # with "no such table". StaticPool reuses a single connection so the
            # in-memory DB behaves like a real one.
            _engine = create_engine(
                db_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            # Peak connection demand: 1 + (5 talent workers × 4 sessions each) = ~21 for poll,
            # + 6 for draft queue + 5 for other jobs/HTTP = ~32 peak. pool_size=10 + overflow=15
            # gives 25 hard cap, which fits with the reduced MAX_TALENT_WORKERS=5 and
            # MAX_CONCURRENT_EMAILS=3 in poller.py. Increasing these limits further would
            # require Supabase connection limit audit first (free tier: ~60 concurrent).
            _engine = create_engine(
                db_url,
                pool_size=10,       # was 5 — insufficient for 7 concurrent scheduler jobs + HTTP
                max_overflow=15,    # was 10 — total cap: 25
                pool_timeout=15,    # was 10 — extra breathing room under load
                pool_recycle=300,
                pool_pre_ping=True,
            )
    return _engine


def reset_engine() -> None:
    """Drop the cached engine + session factory. Tests call this when they swap DATABASE_URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine():
    return _make_engine()


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_make_engine())
    return _session_factory


def create_tables():
    """Create all tables and run additive column migrations (idempotent)."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    # Additive column migrations — AUTOCOMMIT so each statement is its own transaction.
    # Previously used a single shared connection where one failure aborted all subsequent
    # statements silently. AUTOCOMMIT isolates failures: a DDL error on one statement
    # (e.g. ALTER TYPE inside a transaction) does not affect the ones that follow.
    _MIGRATION_STMTS = [
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS body_text TEXT",
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS email_date TIMESTAMP",
        """CREATE TABLE IF NOT EXISTS guardian_audit_log (
            id SERIAL PRIMARY KEY,
            action VARCHAR(64) NOT NULL,
            talent_key VARCHAR(64),
            reason TEXT NOT NULL,
            detail TEXT,
            triggered_by VARCHAR(64) NOT NULL DEFAULT 'guardian',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS last_poll_at TIMESTAMP",
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0",
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS last_error TEXT",
        "ALTER TABLE inbox_emails ADD COLUMN IF NOT EXISTS body_fetch_attempts INTEGER DEFAULT 0",
        "ALTER TABLE inbox_emails ADD COLUMN IF NOT EXISTS body_fetch_failed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE manager_context ADD COLUMN IF NOT EXISTS talent_key TEXT",
        "ALTER TABLE manager_context ADD COLUMN IF NOT EXISTS voice_profile TEXT",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS message_id_header VARCHAR(512)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS cc_recipients TEXT",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited BOOLEAN DEFAULT FALSE",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited_at TIMESTAMP",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS human_edited_by VARCHAR(128)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS original_draft_text TEXT",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS triggered_by_job VARCHAR(32)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS dismissed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS validation_failed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS validation_error TEXT",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS send_claimed_at TIMESTAMP",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_gmail_message_id ON drafts (gmail_message_id)",
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
        "ALTER TYPE emailstatus ADD VALUE IF NOT EXISTS 'processing'",
        """CREATE TABLE IF NOT EXISTS sop_versions (
            id SERIAL PRIMARY KEY,
            version_label VARCHAR(256) NOT NULL DEFAULT '',
            raw_content TEXT NOT NULL,
            talent_count INTEGER,
            uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN NOT NULL DEFAULT FALSE,
            doc_type VARCHAR(32) NOT NULL DEFAULT 'sop'
        )""",
        # Must run AFTER the CREATE above: on an existing DB the table is
        # already there without this column; on a fresh DB the CREATE supplies it.
        "ALTER TABLE sop_versions ADD COLUMN IF NOT EXISTS doc_type VARCHAR(32) NOT NULL DEFAULT 'sop'",
        """CREATE TABLE IF NOT EXISTS external_channel_reviews (
            id SERIAL PRIMARY KEY,
            gmail_message_id VARCHAR(256) NOT NULL UNIQUE,
            thread_id VARCHAR(256),
            talent_key VARCHAR(64) NOT NULL,
            sender VARCHAR(512),
            subject VARCHAR(512),
            body_text TEXT,
            channel_requested VARCHAR(64) NOT NULL,
            received_at TIMESTAMP,
            dismissed BOOLEAN NOT NULL DEFAULT FALSE,
            dismissed_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        # NOTE: score=0 ghost-row cleanup removed from startup — it can lock processed_emails
        # on a large table and delay port binding, causing Render R10 boot timeouts.
        # This cleanup now runs inside _run_guardian() (cron.py) at +35s after startup.
        # SOP v16 single-inbox alias routing — track original recipient per email
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS to_address VARCHAR(256)",
        "ALTER TABLE drafts ADD COLUMN IF NOT EXISTS to_address VARCHAR(256)",
        # 2026-08-04 incident: Gmail subjects/snippets/label lists have no practical
        # length bound. varchar(512) crashed inbox sync (StringDataRightTruncation)
        # on a long inbound subject, poisoning the SQLAlchemy session and stalling
        # the whole poll cycle every ~45s. Widening to TEXT is metadata-only in
        # Postgres (no table rewrite) and safe to re-run (no-op once already TEXT).
        "ALTER TABLE inbox_emails ALTER COLUMN subject TYPE TEXT",
        "ALTER TABLE inbox_emails ALTER COLUMN snippet TYPE TEXT",
        "ALTER TABLE inbox_emails ALTER COLUMN label_ids TYPE TEXT",
    ]
    settings = get_settings()
    mig_url = settings.database_url.replace("postgres://", "postgresql://", 1)

    if mig_url.startswith("sqlite"):
        # SQLite (tests): Base.metadata.create_all already created the schema above.
        # The PostgreSQL-specific DDL statements (SERIAL, IF NOT EXISTS ALTER TABLE,
        # ALTER TYPE, NullPool connect_args) cannot run on SQLite — skip them.
        logger.debug("SQLite detected — skipping additive migration statements.")
        return

    # Dedicated NullPool migration engine — does NOT draw from the application QueuePool.
    # Each migration statement gets its own connection that is closed immediately after.
    # statement_timeout=25000ms hard-caps any single DDL so a blocked ALTER TABLE or
    # ALTER TYPE cannot hang the startup indefinitely and trigger a Render restart.
    _mig_engine = create_engine(
        mig_url,
        poolclass=NullPool,
        connect_args={"options": "-c statement_timeout=25000"},
    )
    try:
        with _mig_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for stmt in _MIGRATION_STMTS:
                try:
                    conn.execute(text(stmt))
                except Exception as _e:
                    logger.warning("DB migration warning (non-fatal): %s", _e)
    finally:
        _mig_engine.dispose()


def verify_schema_matches_models() -> dict:
    """Compare the live schema against the ORM models. Read-only — runs no DDL.

    Render sets SKIP_MIGRATIONS=true, so create_tables() never runs and a new
    table or column silently fails to appear. That is not hypothetical: the
    sop_versions table was missing for its entire life, and every version-history
    request returned 500 UndefinedTable until it was noticed by hand.

    This surfaces the gap at boot instead. It logs one CRITICAL line per missing
    table/column together with the exact ADD COLUMN / CREATE TABLE statement to
    run, so the fix is a copy-paste into the Supabase SQL editor rather than an
    investigation. It deliberately does NOT execute anything: applying DDL
    automatically is what SKIP_MIGRATIONS exists to prevent.

    Returns {"missing_tables": [...], "missing_columns": [("table", "col"), ...]}.
    """
    from sqlalchemy import inspect as _inspect

    report: dict = {"missing_tables": [], "missing_columns": []}
    try:
        engine = get_engine()
        inspector = _inspect(engine)
        live_tables = set(inspector.get_table_names())
    except Exception:
        logger.warning("Schema check skipped — could not inspect the database.", exc_info=True)
        return report

    for table_name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[table_name]
        if table_name not in live_tables:
            report["missing_tables"].append(table_name)
            logger.critical(
                "SCHEMA DRIFT: table '%s' is defined in the models but does NOT exist in the "
                "database. Every query against it will fail. Create it in the Supabase SQL "
                "editor.", table_name,
            )
            continue

        live_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in live_cols:
                continue
            report["missing_columns"].append((table_name, column.name))
            logger.critical(
                "SCHEMA DRIFT: %s.%s is in the models but missing from the database. "
                "Run:  ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s;",
                table_name, column.name, table_name, column.name,
                column.type.compile(engine.dialect),
            )

    if not report["missing_tables"] and not report["missing_columns"]:
        logger.info("Schema check: database matches all %d models.", len(Base.metadata.tables))
    return report
