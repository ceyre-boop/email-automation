# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run dev server (from repo root)
cd backend && uvicorn backend.main:app --reload --port 8000

# Run all tests
cd backend && python -m pytest tests/

# Run a single test file
cd backend && python -m pytest tests/test_triage.py -v

# Run a single test
cd backend && python -m pytest tests/test_triage.py::test_fallback_returns_score2 -v

# Apply DB migrations (runs automatically on app startup too)
cd backend && python -c "from backend.models.db import create_tables; create_tables()"
```

## Architecture

**Single-file frontend.** The entire UI is `backend/static/dashboard.html` — vanilla JS, no framework, no build step. All state lives in a `state` object; renders are synchronous DOM mutations triggered by API responses.

**Polling loop.** An external cron (Render) hits `GET /cron/poll-inboxes` every 5 min → `services/poller.py` → for each connected `TalentToken`: fetch unread Gmail → GPT triage → score 1 archive, score 2 flag, score 3 draft reply → log to Google Sheets.

**AI providers — critical rule.** `openai` is the TABOOST *business* account used for email triage (`gpt-4o-mini`) and reply drafting (`gpt-4o`). Anthropic/Claude is only for this IDE. **Never migrate triage.py or reply.py away from OpenAI** — if quota errors appear, tell the user to add billing credits at platform.openai.com.

**DB migrations are additive only.** No Alembic migrations — new columns are added via raw `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `models/db.py::create_tables()`, which runs at startup. Always use this pattern for schema changes.

**Per-talent OAuth.** Each talent has a row in the `talents` table (`TalentToken`) with their own Gmail OAuth tokens. `services/oauth.py` refreshes tokens; `services/gmail.py` builds the Gmail API service from a token row. Adding a new talent = connect their Gmail → the system auto-discovers them.

**Triage prompt parsing.** `prompts/triage.md` and `prompts/reply.md` use `## SYSTEM PROMPT` and `## USER PROMPT TEMPLATE` heading markers. `_parse_prompt_sections()` in each service splits on these. Template variables are `{{TALENT_NAME}}`, `{{EMAIL_BODY}}`, etc.

**Settings loaded at runtime.** `config/settings.json` holds the talent roster and model config (not env vars). `core/config.py::get_settings()` is LRU-cached — call `get_settings.cache_clear()` in tests if you mutate config. Secrets come from `backend/.env`.

**Key env vars:** `OPENAI_API_KEY`, `DATABASE_URL` (Supabase Postgres), `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`, `GOOGLE_SHEETS_REFRESH_TOKEN`, `API_KEY` (x-api-key header for protected endpoints), `APP_BASE_URL`.

## Key Files

| File | Purpose |
|---|---|
| `backend/services/triage.py` | GPT-4o-mini email scoring (scores 1/2/3) |
| `backend/services/reply.py` | GPT-4o reply draft generation |
| `backend/services/poller.py` | Main polling loop — orchestrates triage + reply |
| `backend/services/gmail.py` | All Gmail API calls (read, archive, draft, send) |
| `backend/routers/dashboard.py` | Dashboard API + backfill endpoints + archive |
| `backend/routers/cron.py` | `/cron/poll-inboxes` + `/api/status` |
| `backend/models/db.py` | SQLAlchemy models + `create_tables()` migration |
| `backend/static/dashboard.html` | Entire frontend (~1650 lines) |
| `config/settings.json` | Talent roster, model names, rate minimums |
| `prompts/triage.md` | GPT triage prompt (system + user template) |
| `prompts/reply.md` | GPT reply prompt (system + user template) |

## Talent Config

Talents are defined in `config/settings.json` under `"talents"`. Each has `key`, `full_name`, `minimum_rate_usd`, `rate_unit` (`"per video"` or `"per hour"`), and `manager`. The `key` is used everywhere as the identifier and must match the DB `talent_key` (case-sensitive in config, lowercased in DB queries).

Special routing logic for specific talents lives in `triage.py::_apply_special_routing()` and is policy-driven from `config/confidence_policy.json`.
