"""
Google Sheets logging service.

Authenticates using either:
  - An OAuth refresh token (GOOGLE_SHEETS_REFRESH_TOKEN) from a Desktop App
    OAuth client — generated once with scripts/generate_google_refresh_token.py
  - A service account JSON (GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON) as a fallback

Sheet ID comes from config/settings.json → google_sheets.master_log_sheet_id
"""
from __future__ import annotations

import logging
from datetime import datetime

import gspread

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

_LOG_COLUMNS = [
    "timestamp",
    "talent_key",
    "sender",
    "subject",
    "score",
    "brand_name",
    "proposed_rate",
    "offer_type",
    "status",
    "notes",
]


def _get_client() -> gspread.Client:
    settings = get_settings()
    return gspread.authorize(settings.sheets_credentials)


def _get_worksheet() -> gspread.Worksheet:
    settings = get_settings()
    cfg = settings.app_config.get("google_sheets", {})
    sheet_id = cfg.get("master_log_sheet_id", "")
    tab_name = cfg.get("master_log_tab_name", "Master Log")
    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(tab_name)


def log_email(
    talent_key: str,
    sender: str,
    subject: str,
    score: int,
    brand_name: str,
    proposed_rate: float,
    offer_type: str,
    status: str,
    notes: str = "",
) -> bool:
    """
    Append one row to the Master Activity Log sheet.
    Returns True on success, False on failure (logs the error; never raises).
    """
    try:
        ws = _get_worksheet()
        row = [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            talent_key,
            sender,
            subject[:256] if subject else "",
            score,
            brand_name,
            proposed_rate,
            offer_type,
            status,
            notes[:512] if notes else "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Sheets log failed for %s: %s", talent_key, exc)
        return False
