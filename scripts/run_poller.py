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

from backend.models.db import create_tables, get_session_factory
from backend.services.poller import poll_all_inboxes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Initialising database tables…")
    create_tables()

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
