"""
Application settings — loaded from environment variables / .env file.
Never commit secrets. Copy .env.example → .env and fill in values.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google OAuth
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "https://yourapp.com/auth/callback"

    # OpenAI
    openai_api_key: str

    # Database
    database_url: str

    # Google Sheets service account
    # Either raw JSON string or a file path
    google_sheets_service_account_json: str = ""
    google_sheets_service_account_file: str = ""

    # App
    app_base_url: str = "https://yourapp.com"
    agency_secret_key: str = "change_me"
    allowed_origins: str = "http://localhost:3000"
    # API key required in x-api-key header for protected endpoints (drafts, status)
    api_key: str = ""

    # Polling
    poll_interval_minutes: int = 5

    # ── Derived helpers ─────────────────────────────────────────────────────

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def service_account_info(self) -> dict:
        """Return the service account credentials as a dict."""
        if self.google_sheets_service_account_json:
            return json.loads(self.google_sheets_service_account_json)
        if self.google_sheets_service_account_file:
            path = Path(self.google_sheets_service_account_file)
            return json.loads(path.read_text())
        raise RuntimeError(
            "Set GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON or GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE"
        )

    # ── Repo-level static config ─────────────────────────────────────────────
    # Loaded once at startup; used throughout the app.

    @property
    def app_config(self) -> dict:
        """Return the parsed config/settings.json from the repo root."""
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.json"
        return json.loads(config_path.read_text())

    @property
    def sop_data(self) -> dict:
        """Return the parsed sheets/sop_data.json from the repo root."""
        sop_path = Path(__file__).parent.parent.parent / "sheets" / "sop_data.json"
        return json.loads(sop_path.read_text())

    @property
    def confidence_policy(self) -> dict:
        policy_path = (
            Path(__file__).parent.parent.parent / "config" / "confidence_policy.json"
        )
        return json.loads(policy_path.read_text())

    @property
    def triage_prompt(self) -> str:
        p = Path(__file__).parent.parent.parent / "prompts" / "triage.md"
        return p.read_text()

    @property
    def reply_prompt(self) -> str:
        p = Path(__file__).parent.parent.parent / "prompts" / "reply.md"
        return p.read_text()


@lru_cache
def get_settings() -> Settings:
    return Settings()
