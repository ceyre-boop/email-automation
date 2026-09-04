"""Shared OpenAI call wrapper: bounded time, bounded retries.

Two failure modes this exists to stop, both found in the 2026-09-04 audit:

1. NO TIMEOUT. Neither triage nor reply set one, so a hung connection blocked a
   worker indefinitely. The poller runs those calls inside a thread pool holding
   a DB session, so one hung request stalls a whole poll cycle — and the
   scheduler still logs the job as "executed successfully".

2. NO RETRY IN reply.py. triage had a retry loop; reply had none, so a single
   transient 429 or 503 turned a real brand deal into an ESCALATE and dumped it
   on a human. Silent revenue loss, invisible in any metric.

Retry policy: transient failures only (429 RPM, 5xx, timeouts, connection
resets) with exponential backoff. A daily-cap 429 is NOT retried — it will not
clear until midnight UTC, so burning three attempts and 35 seconds on it just
slows the queue down. Non-transient errors propagate immediately.

The SDK's own retries are disabled (max_retries=0) so attempts are not
multiplied by a second layer underneath this one.
"""
from __future__ import annotations

import logging
import time

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_ATTEMPTS = 3

# Substrings that mark a failure as worth retrying. Matched against str(exc)
# because the SDK raises several unrelated exception types for these.
_TRANSIENT_MARKERS = (
    "429", "rate_limit_exceeded",
    "500", "502", "503", "504",
    "timeout", "timed out",
    "connection", "connection error", "connection reset",
    "apiconnectionerror", "internalservererror", "service unavailable",
)

# A daily/monthly cap will not clear on any timescale we can wait for.
_HARD_CAP_MARKERS = ("requests per day", "rpd", "insufficient_quota", "exceeded your current quota")


def build_client(api_key: str, timeout_seconds: float | None = None) -> OpenAI:
    """Return an OpenAI client with a hard per-request timeout and no SDK retries."""
    return OpenAI(
        api_key=api_key,
        timeout=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        max_retries=0,
    )


def is_transient(exc: Exception) -> bool:
    err = str(exc).lower()
    if any(marker in err for marker in _HARD_CAP_MARKERS):
        return False
    return any(marker in err for marker in _TRANSIENT_MARKERS)


def call_with_retry(client: OpenAI, label: str, max_attempts: int | None = None, **kwargs):
    """chat.completions.create with backoff on transient failures.

    ``label`` is only for logging (a talent key, usually). Raises the last
    exception once attempts are exhausted, so callers keep their own handling.
    """
    attempts = max_attempts or DEFAULT_MAX_ATTEMPTS
    last_exc: Exception | None = None

    for attempt in range(attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_transient(exc):
                raise
            if attempt == attempts - 1:
                logger.error(
                    "OpenAI call for %s failed after %d attempts: %s", label, attempts, exc
                )
                raise
            wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
            logger.warning(
                "OpenAI transient failure for %s (attempt %d/%d) — retrying in %ds: %s",
                label, attempt + 1, attempts, wait, str(exc)[:200],
            )
            time.sleep(wait)

    raise last_exc  # type: ignore[misc]
