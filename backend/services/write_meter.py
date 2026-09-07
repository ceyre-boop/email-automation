"""Rolling counter of database write statements.

The write storm that took this database to 100% CPU ran for months in plain
sight: ~1,700 UPDATEs a minute, none of them changing a value, and no metric
anywhere counted them. Every dashboard showed green because every individual
query succeeded. This is the instrument that makes that visible — the point is
not to measure success, it is to measure COST.

Hooks SQLAlchemy's cursor-execute event, counts INSERT/UPDATE/DELETE, and keeps
per-minute buckets for the last hour. Cheap: one string check and an int bump
per statement, no I/O.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_WRITE_PREFIXES = ("insert", "update", "delete")

_lock = threading.Lock()
# (minute_start, count) newest last; one hour of history is plenty.
_buckets: deque[list] = deque(maxlen=60)
_installed = False


def _bucket_now() -> datetime:
    now = datetime.utcnow()
    return now.replace(second=0, microsecond=0)


def record_write(n: int = 1) -> None:
    minute = _bucket_now()
    with _lock:
        if _buckets and _buckets[-1][0] == minute:
            _buckets[-1][1] += n
        else:
            _buckets.append([minute, n])


def writes_per_minute(window_minutes: int = 5) -> float:
    """Mean writes/min over the last `window_minutes` completed-ish minutes."""
    cutoff = _bucket_now() - timedelta(minutes=window_minutes)
    with _lock:
        recent = [c for (m, c) in _buckets if m >= cutoff]
    if not recent:
        return 0.0
    return round(sum(recent) / max(len(recent), 1), 1)


def snapshot(window_minutes: int = 5) -> dict:
    cutoff = _bucket_now() - timedelta(minutes=window_minutes)
    with _lock:
        recent = [(m, c) for (m, c) in _buckets if m >= cutoff]
    return {
        "writes_per_minute": writes_per_minute(window_minutes),
        "window_minutes": window_minutes,
        "peak_minute": max((c for _, c in recent), default=0),
        "sampled_minutes": len(recent),
    }


def install(engine) -> None:
    """Attach the counter to an Engine. Safe to call more than once."""
    global _installed
    if _installed:
        return
    try:
        from sqlalchemy import event

        @event.listens_for(engine, "after_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            head = statement.lstrip()[:6].lower()
            if head.startswith(_WRITE_PREFIXES):
                # executemany batches many rows into one statement — count rows, since
                # rows written is what actually costs the database.
                n = cursor.rowcount if executemany and cursor.rowcount and cursor.rowcount > 0 else 1
                record_write(n)

        _installed = True
        logger.info("write_meter: installed on engine")
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the app
        logger.warning("write_meter: could not install: %s", exc)
