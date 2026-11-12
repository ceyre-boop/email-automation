"""SQLAlchemy models and Alembic-compatible Base."""
from __future__ import annotations

import enum
import logging
import os
from urllib.parse import urlsplit, urlunsplit
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
    retry_pending = "retry_pending"  # Transient triage failure — re-triaged after triage_next_retry_at


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
    # One row per talent instead of one write per cached email per cycle. Stamping
    # InboxEmail.last_synced_at on every row every sync was ~1,700 UPDATEs a minute
    # of pure churn and is what drove the database to 100% CPU on 2026-09-06.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the token was auto-deactivated after repeated auth failures (None if it
    # was never auto-deactivated). last_error holds the reason. Surfaced on /health
    # and the dashboard so a dead inbox is loud instead of silently excluded.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    # Triage retry: transient OpenAI failures park the row as retry_pending instead of
    # finalizing it as a score-2 fallback. See poller._park_for_triage_retry.
    triage_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    triage_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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

# Max worker threads any code path may run when each thread holds its own DB
# session. Kept well under the engine pool cap (see _make_engine) so a manual
# backfill/blast can never starve the poller or trip the Supabase session-mode
# client limit.
MAX_DB_THREAD_WORKERS = int(os.getenv("MAX_DB_THREAD_WORKERS", "6"))


# Supabase pooler ports. Session mode (5432) caps this project at ~15 concurrent
# clients; transaction mode (6543) multiplexes and allows far more.
_SESSION_POOLER_PORT = 5432
_TRANSACTION_POOLER_PORT = 6543


def _resolve_pooler_url(db_url: str) -> tuple[str, bool]:
    """Point a Supabase *pooler* URL at the transaction-mode port.

    Returns (url, transaction_mode). Only rewrites when the host is a Supabase
    pooler host on the session-mode port — a direct db.<ref>.supabase.co URL or
    any other Postgres host is returned untouched. Set DB_POOLER_MODE=session to
    opt out without editing DATABASE_URL.

    Safe for this codebase because nothing here relies on session-scoped Postgres
    state (no LISTEN/NOTIFY, advisory locks held across transactions, temp tables
    or session-level SET), and psycopg2 does not use server-side prepared
    statements, which is the usual incompatibility with transaction pooling.
    """
    if os.getenv("DB_POOLER_MODE", "").lower() == "session":
        return db_url, False
    try:
        parts = urlsplit(db_url)
        host = (parts.hostname or "")
        if "pooler.supabase.com" not in host:
            return db_url, False
        if parts.port == _TRANSACTION_POOLER_PORT:
            return db_url, True
        if parts.port not in (None, _SESSION_POOLER_PORT):
            return db_url, False
        userinfo = ""
        if parts.username:
            userinfo = parts.username
            if parts.password:
                userinfo += f":{parts.password}"
            userinfo += "@"
        netloc = f"{userinfo}{host}:{_TRANSACTION_POOLER_PORT}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), True
    except Exception:  # noqa: BLE001 — never let URL parsing break startup
        logger.warning("Could not parse DATABASE_URL for pooler port rewrite; using it as-is")
        return db_url, False


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
            # Connection budget must stay strictly BELOW whatever the pooler in
            # front of Postgres allows. Over its client limit Supabase's pooler
            # does not queue politely — it refuses the connect with
            # "EMAXCONNSESSION ... max clients are limited to pool_size: N" and
            # the caller (a poll cycle, a dashboard request) dies outright.
            #
            # Two modes, decided by which pooler PORT the URL points at:
            #   5432 = SESSION mode   → ~15 clients for this project. Tight.
            #   6543 = TRANSACTION mode → server connections are multiplexed, so
            #                             many more clients are allowed.
            # _resolve_pooler_url() below rewrites 5432 → 6543 automatically, so
            # DATABASE_URL itself never has to be touched. The invariant to keep:
            #   MAX_TALENT_WORKERS × (1 + MAX_CONCURRENT_EMAILS) + 1
            #     <= pool_size + max_overflow < pooler client limit
            db_url, transaction_mode = _resolve_pooler_url(db_url)
            default_pool, default_overflow = (10, 10) if transaction_mode else (6, 6)
            pool_size = int(os.getenv("DB_POOL_SIZE", str(default_pool)))
            max_overflow = int(os.getenv("DB_MAX_OVERFLOW", str(default_overflow)))
            if not transaction_mode:
                # Session mode: hard-clamp regardless of env, 12 total < 15 limit.
                pool_size, max_overflow = min(pool_size, 6), min(max_overflow, 6)
            logger.info(
                "DB engine: pooler=%s pool_size=%d max_overflow=%d",
                "transaction(6543)" if transaction_mode else "session(5432)",
                pool_size, max_overflow,
            )
            # statement_timeout is the single most important guardrail here. Without
            # it one slow query pins a connection until it finishes, connections pile
            # up, the pool empties and every scheduled job fails — which is how this
            # database reached 100% CPU on 2026-09-06 and stopped answering at all.
            # A capped statement dies instead of taking the instance with it.
            statement_timeout_ms = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "20000"))
            _engine = create_engine(
                db_url,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=30,    # wait for a pooled conn instead of failing fast
                pool_recycle=300,
                pool_pre_ping=True,
                # SQLAlchemy's psycopg2 dialect probes pg_type for hstore OIDs on
                # EVERY new connection. We do not use hstore, and through a
                # transaction-mode pooler (where connections churn constantly) that
                # is a wasted catalog round-trip per connect — and a failure point:
                # it is exactly where "SSL connection has been closed unexpectedly"
                # was surfacing on 2026-09-08.
                use_native_hstore=False,
                connect_args={
                    "options": f"-c statement_timeout={statement_timeout_ms}",
                    # Kill sessions left idle mid-transaction (a crashed worker), which
                    # otherwise hold locks and block autovacuum indefinitely.
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 5,
                },
            )
    try:
        from backend.services.write_meter import install as _install_write_meter
        _install_write_meter(_engine)
    except Exception:  # noqa: BLE001 — instrumentation must never block startup
        pass
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


# ── Required migrations ──────────────────────────────────────────────────────
#
# The few additive statements that CURRENT CODE cannot run without. They are applied
# at every boot, including when SKIP_MIGRATIONS is set: the full _MIGRATION_STMTS list
# below is deliberately skipped on Render (it re-runs ~60 statements, some with real
# lock risk, on every deploy) but that skip meant the retry-logic columns/enum value
# were never created and every query touching them would have failed.
#
# Each entry is (label, "already applied?" SQL, DDL). The check runs first and the DDL
# only runs when it comes back empty, so a healthy boot takes no table lock at all.
# The DDL is itself idempotent (IF NOT EXISTS) as a second line of defence. Additive
# only — never add a drop/rename/type change here.
def _column_exists_sql(table: str, column: str) -> str:
    return (
        "SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema = current_schema() AND table_name = '{table}' "
        f"AND column_name = '{column}'"
    )


def _enum_label_exists_sql(enum_type: str, label: str) -> str:
    return (
        "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        f"WHERE t.typname = '{enum_type}' AND e.enumlabel = '{label}'"
    )


REQUIRED_MIGRATIONS: list[tuple[str, str, str]] = [
    (
        "emailstatus.retry_pending",
        _enum_label_exists_sql("emailstatus", "retry_pending"),
        "ALTER TYPE emailstatus ADD VALUE IF NOT EXISTS 'retry_pending'",
    ),
    (
        "processed_emails.triage_retry_count",
        _column_exists_sql("processed_emails", "triage_retry_count"),
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS triage_retry_count INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "processed_emails.triage_next_retry_at",
        _column_exists_sql("processed_emails", "triage_next_retry_at"),
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS triage_next_retry_at TIMESTAMP",
    ),
    (
        "talents.deactivated_at",
        _column_exists_sql("talents", "deactivated_at"),
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
    ),
]


def apply_required_migrations() -> dict:
    """Apply REQUIRED_MIGRATIONS idempotently. Never raises.

    Safe to run on every boot, with or without SKIP_MIGRATIONS. Uses a dedicated
    NullPool engine in AUTOCOMMIT (ALTER TYPE ... ADD VALUE cannot run inside a
    transaction block on older Postgres) with a short lock_timeout, so if a
    long-running query holds processed_emails the DDL fails fast and is retried on
    the next boot instead of queueing behind it and blocking every other query.

    Returns {"applied": [labels], "already": [labels], "failed": [(label, error)]}.
    """
    report: dict = {"applied": [], "already": [], "failed": []}
    settings = get_settings()
    mig_url = (settings.database_url or "").replace("postgres://", "postgresql://", 1)
    if not mig_url or mig_url.startswith("sqlite"):
        return report  # Base.metadata.create_all already built the schema (tests/dev)

    eng = create_engine(
        mig_url,
        poolclass=NullPool,
        connect_args={"options": "-c statement_timeout=25000 -c lock_timeout=5000"},
    )
    try:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for label, check_sql, ddl in REQUIRED_MIGRATIONS:
                try:
                    if conn.execute(text(check_sql)).first() is not None:
                        report["already"].append(label)
                        continue
                    logger.warning("Required migration %s is missing — applying: %s", label, ddl)
                    conn.execute(text(ddl))
                    report["applied"].append(label)
                except Exception as exc:  # noqa: BLE001
                    report["failed"].append((label, str(exc)[:300]))
                    logger.error("Required migration %s FAILED: %s", label, exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Required migrations could not run — database unreachable: %s", exc)
        report["failed"].append(("connect", str(exc)[:300]))
    finally:
        eng.dispose()
    if report["applied"]:
        logger.warning("Required migrations applied at startup: %s", ", ".join(report["applied"]))
    return report


# Indexes the hot paths need, built at every boot (independent of SKIP_MIGRATIONS).
# 2026-11-12: the poller's "earlier mail in this thread" lookups filter
# processed_emails/drafts by thread_id, which had no index, so each one scanned the
# whole table. As the tables grew they crossed the 20s statement_timeout, jobs
# overlapped and were skipped, and drafting/sending crawled.
# (index name, CREATE INDEX CONCURRENTLY statement)
REQUIRED_INDEXES: list[tuple[str, str]] = [
    ("idx_processed_emails_thread_id",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_processed_emails_thread_id "
     "ON processed_emails (thread_id)"),
    ("idx_drafts_thread_id",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_drafts_thread_id ON drafts (thread_id)"),
    ("idx_processed_emails_processed_at",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_processed_emails_processed_at "
     "ON processed_emails (processed_at)"),
    ("idx_inbox_emails_unread_msg",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_inbox_emails_unread_msg "
     "ON inbox_emails (gmail_message_id) WHERE is_unread"),
]


def apply_required_indexes() -> dict:
    """Build REQUIRED_INDEXES without blocking writes. Never raises.

    CONCURRENTLY takes no write lock, so the live poller keeps working while the
    index builds; it must run outside a transaction (AUTOCOMMIT) and with no
    statement_timeout, since a large table can take longer than the app's 20s cap.
    A CONCURRENTLY build that is interrupted leaves an INVALID index behind which
    IF NOT EXISTS would then skip forever, so invalid leftovers are dropped first.
    Meant to run on a background thread at boot.

    Returns {"built": [...], "already": [...], "failed": [(name, error)]}.
    """
    report: dict = {"built": [], "already": [], "failed": []}
    settings = get_settings()
    url = (settings.database_url or "").replace("postgres://", "postgresql://", 1)
    if not url or url.startswith("sqlite"):
        return report

    eng = create_engine(
        url,
        poolclass=NullPool,
        # lock_timeout=0: CONCURRENTLY waits for older transactions to finish, which
        # on a slow DB can exceed any short timeout and would leave an INVALID index
        # every boot. Waiting is safe — a CONCURRENTLY build does not block writers.
        connect_args={"options": "-c statement_timeout=0 -c lock_timeout=0"},
    )
    try:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for name, ddl in REQUIRED_INDEXES:
                try:
                    row = conn.execute(text(
                        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                        "WHERE c.relname = :n"), {"n": name}).first()
                    if row is not None and row[0]:
                        report["already"].append(name)
                        continue
                    if row is not None:
                        # Invalid may mean "another instance is building it right now"
                        # (e.g. overlapping deploys) — never drop a live build.
                        busy = conn.execute(text(
                            "SELECT 1 FROM pg_stat_progress_create_index p "
                            "JOIN pg_class c ON c.oid = p.index_relid WHERE c.relname = :n"),
                            {"n": name}).first()
                        if busy:
                            report["already"].append(name + " (build in progress elsewhere)")
                            continue
                        logger.warning("Index %s is INVALID (interrupted build) — dropping and rebuilding", name)
                        conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
                    logger.warning("Building missing index %s (CONCURRENTLY): %s", name, ddl)
                    conn.execute(text(ddl))
                    report["built"].append(name)
                    logger.warning("Index %s built", name)
                except Exception as exc:  # noqa: BLE001
                    report["failed"].append((name, str(exc)[:300]))
                    logger.error("Index %s FAILED: %s", name, exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Required indexes could not run — database unreachable: %s", exc)
        report["failed"].append(("connect", str(exc)[:300]))
    finally:
        eng.dispose()
    return report


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
        "ALTER TYPE emailstatus ADD VALUE IF NOT EXISTS 'retry_pending'",
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS triage_retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE processed_emails ADD COLUMN IF NOT EXISTS triage_next_retry_at TIMESTAMP",
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
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
        "ALTER TABLE talents ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP",
        # Performance: auto_send runs its eligibility query once per talent per
        # minute. talent_key is filtered with ILIKE, which cannot use the plain
        # btree index, so the query seq-scanned all ~33k drafts every time and
        # eventually tripped Postgres' statement timeout — drafting stalled for
        # three hours on 2026-09-03 with QueryCanceled errors. This partial index
        # covers exactly the pending/undismissed/unedited slice the job reads.
        """CREATE INDEX IF NOT EXISTS idx_drafts_pending_eligible
           ON drafts (created_at)
           WHERE status = 'pending' AND dismissed = false AND human_edited = false""",
        # Trigram indexes so the many ILIKE talent_key filters across the
        # dashboard and poller can use an index instead of a full scan.
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS idx_drafts_talent_key_trgm ON drafts USING gin (talent_key gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_processed_emails_talent_key_trgm ON processed_emails USING gin (talent_key gin_trgm_ops)",
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


# Result of the most recent verify_schema_matches_models() run, exposed on /health.
# ok is None until the first check has completed (boot work runs on a background thread).
_schema_status: dict = {"ok": None, "checked_at": None, "missing_tables": [],
                        "missing_columns": [], "missing_enum_values": [], "error": None}


def get_schema_status() -> dict:
    """Snapshot of the last schema verification, for /health. Never raises."""
    return dict(_schema_status)


def verify_schema_matches_models() -> dict:
    """Compare the live schema against the ORM models. Read-only — runs no DDL.

    Render sets SKIP_MIGRATIONS=true, so the full create_tables() list never runs
    and a new table, column or enum value silently fails to appear. That is not
    hypothetical: the sop_versions table was missing for its entire life, and
    every version-history request returned 500 UndefinedTable until it was
    noticed by hand.

    This surfaces the gap at boot instead. It logs one ERROR line per missing
    table/column/enum value together with the exact statement to run, and records
    the result so /health can report `schema_ok: false`. It deliberately does NOT
    execute anything itself (see apply_required_migrations for the small set of
    statements that ARE applied automatically).

    Returns {"ok", "missing_tables": [...], "missing_columns": [("table", "col"), ...],
    "missing_enum_values": [("type", "label"), ...]}.
    """
    from sqlalchemy import Enum as _SAEnum, inspect as _inspect
    from datetime import datetime as _dt

    report: dict = {"ok": True, "missing_tables": [], "missing_columns": [],
                    "missing_enum_values": []}

    def _publish(error: str | None = None) -> dict:
        report["ok"] = (
            error is None
            and not report["missing_tables"]
            and not report["missing_columns"]
            and not report["missing_enum_values"]
        )
        _schema_status.update(
            ok=None if error else report["ok"],
            checked_at=_dt.utcnow().isoformat(),
            missing_tables=list(report["missing_tables"]),
            missing_columns=[list(c) for c in report["missing_columns"]],
            missing_enum_values=[list(c) for c in report["missing_enum_values"]],
            error=error,
        )
        return report

    try:
        engine = get_engine()
        inspector = _inspect(engine)
        live_tables = set(inspector.get_table_names())
    except Exception as exc:  # noqa: BLE001
        logger.error("Schema check could not inspect the database: %s", exc, exc_info=True)
        return _publish(error=str(exc)[:300])

    for table_name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[table_name]
        if table_name not in live_tables:
            report["missing_tables"].append(table_name)
            logger.error(
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
            logger.error(
                "SCHEMA DRIFT: %s.%s is in the models but missing from the database. "
                "Run:  ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s;",
                table_name, column.name, table_name, column.name,
                column.type.compile(engine.dialect),
            )

    # Postgres enum types are separate catalog objects: a new Python enum member is
    # not a valid value until `ALTER TYPE ... ADD VALUE` runs. The retry_pending
    # status needed exactly this. SQLite stores enums as VARCHAR, so skip there.
    if engine.dialect.name == "postgresql":
        try:
            with engine.connect() as conn:
                seen: set[str] = set()
                for table in Base.metadata.tables.values():
                    for column in table.columns:
                        if not isinstance(column.type, _SAEnum) or not column.type.name:
                            continue
                        type_name = column.type.name
                        if type_name in seen:
                            continue
                        seen.add(type_name)
                        live_labels = {
                            r[0] for r in conn.execute(
                                text(
                                    "SELECT e.enumlabel FROM pg_enum e "
                                    "JOIN pg_type t ON t.oid = e.enumtypid "
                                    "WHERE t.typname = :n"
                                ),
                                {"n": type_name},
                            )
                        }
                        if not live_labels:
                            continue  # type absent: reported as a missing table/column already
                        for label in column.type.enums:
                            if label in live_labels:
                                continue
                            report["missing_enum_values"].append((type_name, label))
                            logger.error(
                                "SCHEMA DRIFT: enum %s has no value '%s' in the database. "
                                "Run:  ALTER TYPE %s ADD VALUE IF NOT EXISTS '%s';",
                                type_name, label, type_name, label,
                            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Schema check could not read enum values: %s", exc, exc_info=True)
            return _publish(error=str(exc)[:300])

    _publish()
    if report["ok"]:
        logger.info("Schema check: database matches all %d models.", len(Base.metadata.tables))
    else:
        logger.error(
            "SCHEMA MISMATCH: missing_tables=%s missing_columns=%s missing_enum_values=%s — "
            "/health reports schema_ok=false until this is fixed.",
            report["missing_tables"], report["missing_columns"], report["missing_enum_values"],
        )
    return report
