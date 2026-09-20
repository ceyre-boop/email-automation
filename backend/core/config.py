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


_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    # OpenAI
    openai_api_key: str = ""

    # Database
    database_url: str = ""

    # App
    app_base_url: str = ""
    agency_secret_key: str = "change_me"
    allowed_origins: str = "http://localhost:3000"

    # API key required in x-api-key header for protected endpoints (drafts, status)
    api_key: str = ""

    # Polling
    poll_interval_minutes: int = 5

    # Render deploy hook (POST to this URL to trigger a redeploy)
    # Set RENDER_DEPLOY_HOOK_URL in Render → Environment Variables
    render_deploy_hook_url: str = ""

    # ── Derived helpers ─────────────────────────────────────────────────────

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

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
        if not sop_path.exists():
            return {}
        return json.loads(sop_path.read_text())

    @property
    def talent_profiles(self) -> "dict[str, object]":
        """Return parsed talent profiles from sheets/sop.md — single source of truth."""
        from backend.services.sop_parser import parse_sop_md
        from backend.services.reply import _load_sop_md
        return parse_sop_md(_load_sop_md())

    @property
    def talent_list(self) -> "list[dict]":
        """Talent roster as list of dicts sourced from sop.md profiles.

        Provides the same shape as the legacy settings.json talents[] array so
        existing dashboard code can migrate without interface changes.
        """
        return [
            {
                "key": p.key,
                "full_name": p.full_name,
                "manager": p.manager,
                "manager_email": p.manager_email,
                "gmail_connection_name": p.gmail_connection_name,
                "minimum_rate_usd": p.minimum_rate_usd,
                "rate_unit": p.rate_unit,
                "auto_send": p.auto_send,
                "paused": p.paused,
                "category": None,
                "inbox_email": None,
            }
            for p in self.talent_profiles.values()
        ]

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
