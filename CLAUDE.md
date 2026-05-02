# CLAUDE.md — Email Automation

## What This Is (The Why)

TABOOST manages TikTok Shop creators who receive hundreds of inbound brand deal emails every week. Reading and responding to all of them manually is impossible at scale — good deals get missed, bad deals waste time.

This system automates the inbox for each talent: it reads every email, scores it (junk / flag for review / draft a reply), and generates ready-to-send responses for the good ones. **The goal is to close the maximum number of profitable brand deals without the talent ever opening their inbox.** Managers review drafted replies, approve, and send — that's the only human step.

This is a direct revenue multiplier for the agency. Every good deal that previously slipped through the noise is now caught and responded to within minutes.

---

## What This Is (The How)

FastAPI backend deployed on Render. Each talent connects their Gmail via OAuth. A polling loop (every 3 minutes) reads unread emails, runs GPT triage to score them 1/2/3, and for score-3 emails generates a draft reply using the talent's SOP rules.

---

## Commands

```bash
# Dev server
cd backend && uvicorn backend.main:app --reload --port 8000

# Tests
cd backend && python -m pytest tests/

# Single test file
cd backend && python -m pytest tests/test_triage.py -v

# Apply DB schema (also runs on startup)
cd backend && python -c "from backend.models.db import create_tables; create_tables()"
```

---

## Architecture

### Polling loop
```
Render cron → GET /cron/poll-inboxes (every 5 min, + APScheduler every 3 min)
  → services/poller.py
  → for each connected TalentToken:
      fetch unread Gmail
      → triage.py (GPT-4o-mini) → score 1/2/3
          score 1: archive (junk)
          score 2: flag for review
          score 3: generate draft reply (GPT-4o) → save to DB
      → log to Google Sheets
```

### Frontend
Single-file SPA at `backend/static/dashboard.html` (~1650 lines). Vanilla JS, no framework, no build step. All state in a `state` object; renders are synchronous DOM mutations triggered by API responses.

### AI provider — critical rule
`openai` is the TABOOST *business* account used for email triage (`gpt-4o-mini`) and reply drafting (`gpt-4o`). Anthropic/Claude is only for this IDE.

**Never migrate `triage.py` or `reply.py` to any other AI provider.** If quota errors appear, tell Colin to add billing credits at platform.openai.com — do not switch models.

### DB migrations — additive only
No Alembic. New columns added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `models/db.py::create_tables()`, which runs at every startup. Always use this pattern — never destructive migrations.

### Per-talent OAuth
Each talent has a row in the `talents` table (`TalentToken`) with their own Gmail OAuth tokens. `services/oauth.py` refreshes tokens; `services/gmail.py` builds the Gmail API service from that token. Adding a new talent = connect their Gmail account → system auto-discovers them.

### Prompt architecture
`prompts/triage.md` and `prompts/reply.md` use `## SYSTEM PROMPT` and `## USER PROMPT TEMPLATE` heading markers. `_parse_prompt_sections()` in each service splits on these. Template variables are `{{TALENT_NAME}}`, `{{EMAIL_BODY}}`, etc.

### Settings
`config/settings.json` holds talent roster and model config (not env vars). `core/config.py::get_settings()` is LRU-cached — call `get_settings.cache_clear()` in tests if you mutate config.

---

## Key Files

| File | Purpose |
|---|---|
| `backend/services/triage.py` | GPT-4o-mini email scoring (1/2/3) |
| `backend/services/reply.py` | GPT-4o reply draft generation |
| `backend/services/poller.py` | Main polling loop — orchestrates triage + reply |
| `backend/services/gmail.py` | All Gmail API calls (read, archive, draft, send) |
| `backend/routers/dashboard.py` | Dashboard API + backfill endpoints |
| `backend/routers/cron.py` | `/cron/poll-inboxes` + `/api/status` |
| `backend/models/db.py` | SQLAlchemy models + `create_tables()` |
| `backend/static/dashboard.html` | Entire frontend SPA |
| `config/settings.json` | Talent roster, model names, rate minimums |
| `config/confidence_policy.json` | Score routing + special talent routing rules |
| `prompts/triage.md` | GPT triage prompt (system + user template) |
| `prompts/reply.md` | GPT reply prompt (system + user template) |

---

## Talent Config

Defined in `config/settings.json` under `"talents"`. Each talent has: `key`, `full_name`, `minimum_rate_usd`, `rate_unit` (`"per video"` or `"per hour"`), `manager`.

`key` is case-sensitive in config and lowercased in DB queries. Special routing rules per talent live in `triage.py::_apply_special_routing()`, driven by `config/confidence_policy.json`.

---

## Key Env Vars

`OPENAI_API_KEY`, `DATABASE_URL` (Supabase Postgres), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `GOOGLE_SHEETS_REFRESH_TOKEN`, `API_KEY` (x-api-key header for protected endpoints), `APP_BASE_URL`

---

## Per-Talent SOP Files

`sheets/talent_sops/` — one CSV per talent defining their specific routing rules, rate floors, and communication preferences. These feed into the reply prompt context.

---

## Roadmap

**Now — Stability**
- Connect all 16 talent Gmail accounts (only Katrina connected as of 2026-04-24)
- Verify OpenAI billing is active so triage stops falling back to score=2
- Trigger `POST /api/dashboard/backfill-all?days=30` once all talents are connected

**Next — Dashboard UX**
- Sent tab (show emails where reply was actually sent, status=`sent`)
- Mobile-friendly layout
- Unread badge on sidebar talent names
- Draft approval flow — one-click approve from email list
- Email threading — group replies under original

**Soon — Intelligence**
- Rate negotiation replies — when offer is below minimum, counter-offer instead of flagging
- Brand recognition list — known brands always get Score 3
- Duplicate detection — same brand in 30 days → surface prior interaction in reply
- Daily digest email to Colin/managers

**Later — Autonomy**
- Auto-send mode — flip `draft_mode: false` in settings to send without human review
- Gmail Pub/Sub push notifications — replace polling with real-time triggers
- Multi-manager portal — Cara, Chenni, Nicole each see only their talents

---

## Memory Protocol

At the end of any session where something non-obvious was learned, update:
`C:\Users\Admin\.claude\projects\C--Users-Admin-email-automation\memory\`

Write individual files per memory, link from `MEMORY.md` index. See global memory system instructions for format.

**What to save:** decisions future Claude can't infer from code (e.g. why OpenAI stays), corrections to approaches, new talents added, new env vars required, non-obvious bug root causes.

**What not to save:** anything already in this file, git history, or directly readable from code.
