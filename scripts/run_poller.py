"""
Standalone poller entrypoint — no web server required.

Run directly (e.g. from GitHub Actions or a local terminal):

    python -m scripts.run_poller

All configuration is read from environment variables (same as the FastAPI app).
Required env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OPENAI_API_KEY,
DATABASE_URL, GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON (or _FILE).
"""
from __future__ import annotations

import logging
import sys
import time

from backend.models.db import create_tables, get_session_factory
from backend.services.poller import poll_all_inboxes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_DB_INIT_RETRIES = 3
_DB_INIT_BACKOFF = 5  # seconds between retries


def main() -> None:
    logger.info("Initialising database tables…")
    for attempt in range(1, _DB_INIT_RETRIES + 1):
        try:
            create_tables()
            break
        except Exception as exc:
            if attempt == _DB_INIT_RETRIES:
                logger.error("DB init failed after %d attempts — aborting: %s", attempt, exc)
                sys.exit(1)
            logger.warning(
                "DB init attempt %d/%d failed (%s) — retrying in %ds…",
                attempt, _DB_INIT_RETRIES, exc, _DB_INIT_BACKOFF,
            )
            time.sleep(_DB_INIT_BACKOFF)

    Session = get_session_factory()
    db = Session()
    try:
        logger.info("Starting inbox poll…")
        summary = poll_all_inboxes(db)
        logger.info("Poll finished: %s", summary)
    except Exception:
        logger.exception("Unhandled error during poll")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
