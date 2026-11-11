"""Cross-process mutual exclusion via Postgres advisory locks.

Why this exists
---------------
The service has several triggers for the same work: APScheduler jobs inside every
web process, the Render cron hitting /cron/poll-inboxes, manual admin endpoints,
and (during a deploy) two overlapping instances. An in-process ``threading.Lock``
or a module-level dict only excludes callers in the *same* process, so two
instances could poll the same inbox and double-draft the same email.

``pg_try_advisory_lock`` is held by the database, so it excludes every process.
One key per job name; a caller that does not get the lock skips its run (INFO log)
rather than queueing behind it.

Connection mode matters
-----------------------
A session-level advisory lock lives on one server connection, so it must be taken
and released on the SAME connection. The app's main engine points at Supabase's
*transaction-mode* pooler (port 6543), which hands a different server connection
to each transaction — a lock taken there could be left held on a connection nobody
owns. So locks use their own small NullPool engine, pointed at the session-mode
port (5432) of the same pooler (or the same direct URL when DATABASE_URL is not a
pooler). While a job runs, its lock holds one such connection open; if the process
dies the connection drops and Postgres releases the lock on its own.

Failure policy
--------------
If the lock database cannot be reached, jobs still run, guarded by an in-process
lock, with an ERROR log. Failing closed would turn a pooler hiccup into a silent
total outage of polling and drafting, which is the failure mode this change exists
to prevent. On SQLite (tests, local dev) only the in-process lock is used.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import threading
from contextlib import contextmanager
from typing import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

_KEY_NAMESPACE = "email-automation:"

_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()
_lock_engine = None
_lock_engine_url: str | None = None
_engine_guard = threading.Lock()


def lock_key(name: str) -> int:
    """Stable signed 64-bit advisory-lock key for a lock name."""
    digest = hashlib.sha1((_KEY_NAMESPACE + name).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _session_mode_url(db_url: str) -> str:
    """Return a URL whose connections are session-scoped (safe for session locks)."""
    try:
        parts = urlsplit(db_url)
        if "pooler.supabase.com" in (parts.hostname or "") and parts.port == 6543:
            userinfo = ""
            if parts.username:
                userinfo = parts.username
                if parts.password:
                    userinfo += f":{parts.password}"
                userinfo += "@"
            netloc = f"{userinfo}{parts.hostname}:5432"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse DATABASE_URL for advisory-lock URL; using it as-is")
    return db_url


def _database_url() -> str:
    return (get_settings().database_url or "").replace("postgres://", "postgresql://", 1)


def _is_postgres() -> bool:
    url = _database_url()
    return bool(url) and not url.startswith("sqlite")


def _get_lock_engine():
    global _lock_engine, _lock_engine_url
    url = _session_mode_url(_database_url())
    with _engine_guard:
        if _lock_engine is None or _lock_engine_url != url:
            if _lock_engine is not None:
                _lock_engine.dispose()
            _lock_engine = create_engine(
                url,
                poolclass=NullPool,
                use_native_hstore=False,
                connect_args={
                    "connect_timeout": 10,
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 5,
                },
            )
            _lock_engine_url = url
        return _lock_engine


@contextmanager
def _local_lock(name: str) -> Iterator[bool]:
    with _local_guard:
        lock = _local_locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


@contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    """Try to take the cluster-wide lock ``name``. Yields True if held, else False.

    Never blocks. The lock is released when the ``with`` block exits (and, if the
    process dies, when Postgres notices the connection is gone).
    """
    if not _is_postgres():
        with _local_lock(name) as got:
            yield got
        return

    key = lock_key(name)
    conn = None
    got = False
    try:
        conn = _get_lock_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Advisory lock %r unavailable (%s) — falling back to an in-process lock; "
            "cross-process exclusion is NOT in effect for this run.", name, exc,
        )
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with _local_lock(name) as local_got:
            yield local_got
        return

    if not got:
        conn.close()
        yield False
        return

    try:
        yield True
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        except Exception as exc:  # noqa: BLE001
            # Closing the connection below drops the session lock anyway.
            logger.warning("Advisory unlock %r failed (%s) — closing connection to release", name, exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def locked_job(name: str) -> Callable:
    """Decorator: run the job only if the advisory lock ``job:<name>`` is free.

    A skipped run returns None and logs at INFO. Apply to every scheduled job and
    to the manual endpoints' entry points so a scheduler tick, the Render cron and
    an admin click can never overlap.
    """
    lock_name = f"job:{name}"

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with advisory_lock(lock_name) as got:
                if not got:
                    logger.info("Job %s skipped — lock %s is held by another run", name, lock_name)
                    return None
                return fn(*args, **kwargs)

        wrapper.lock_name = lock_name  # type: ignore[attr-defined]
        wrapper.__wrapped_job__ = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator
