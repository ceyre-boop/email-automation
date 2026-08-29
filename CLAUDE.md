# CLAUDE.md — Email Automation

## What This Is (The Why)

TABOOST manages TikTok Shop creators who receive hundreds of inbound brand deal emails every week. Reading and responding to all of them manually is impossible at scale — good deals get missed, bad deals waste time.

This system automates the inbox for each talent: it reads every email, scores it (junk / flag for review / draft a reply), and generates ready-to-send responses for the good ones. **The goal is to close the maximum number of profitable brand deals without the talent ever opening their inbox.** Managers review drafted replies, approve, and send — that's the only human step.

This is a direct revenue multiplier for the agency. Every good deal that previously slipped through the noise is now caught and responded to within minutes.

---

## What This Is (The How)

FastAPI backend deployed on Render. Each talent connects their Gmail via OAuth. A polling loop (every 3 minutes) reads unread emails, runs GPT triage to score them 1/2/3, and for score-3 emails generates a draft reply using the talent's SOP rules.

---

## CRITICAL — Read this before touching production. It exists because of a real incident (see Incident Log at the bottom).

**This repo is sometimes worked on by more than one AI agent/tool at the same time** (Claude Code / Cowork sessions, Codex CLI, possibly others Colin runs locally). None of them can see each other's work except through git and the live Render/Supabase state. That means:

- **Always `git fetch && git log HEAD..origin/main --oneline` before pushing.** If it shows commits, someone else already pushed — pull/rebase and check for overlapping changes before adding yours. Never push blind.
- **Never assume you're the only one who might redeploy.** Before triggering a Render deploy, check `GET /v1/services/{id}/deploys?limit=3` for a deploy that started in the last few minutes you didn't trigger.
- **A code change that adds a DB column is not live until the migration actually runs.** See "DB migrations" below — this got missed once already and broke production for ~25 minutes.

**Where secrets actually live — read before you "fix" anything here:**
- All real secrets (`DATABASE_URL`, `OPENAI_API_KEY`, `GOOGLE_CLIENT_ID/SECRET`, `GOOGLE_SHEETS_REFRESH_TOKEN`, `AGENCY_SECRET_KEY`, `API_KEY`) live **only** as Render environment variables on the `manager-email-automation` service (`srv-d7lb7oe47okc738vjkg0`). They are never committed to git (`.env` is gitignored) and `config/settings.json`/`render.yaml` only ever reference them by name (`sync: false`), never by value.
- **`render.yaml` is NOT a faithful snapshot of the live service.** The production service was created before/outside this Blueprint and has drifted: env vars set via dashboard/API don't appear in the file, and editing `render.yaml` does not reconfigure the existing service. Treat the Render API (`GET /v1/services/{id}`, `/env-vars`, `/deploys`) as the only source of truth for what production actually runs.
- **Render's env-var API does not let you read back a value once it's gone, and `PUT /v1/services/{id}/env-vars` (bulk, no key in the URL) REPLACES THE ENTIRE SET — it does not merge.** Wiping every secret on the live service with one malformed call is exactly what happened in the incident below. **Always use the per-key endpoint** — `PUT /v1/services/{id}/env-vars/{KEY}` — to change or add a single value. Never call the bulk endpoint unless you are deliberately writing the complete, correct set of every key at once.
- A local backup of every secret (as of 2026-08-03) was generated and handed directly to Colin — it is NOT in this repo. If you are an agent and need a value you don't have, **ask Colin for the backup file or the specific value** — do not try to reconstruct or guess a secret, and do not regenerate/rotate a credential (e.g. Google OAuth client secret, DB password) without his explicit go-ahead, since rotating breaks whatever still has the old value cached.
- There is a second, older Render service in the same account (`email-automation`, `srv-d7jdjfjbc2fs73c0apc0`, https://email-automation-qp2v.onrender.com) that shares the same Supabase DB and Google OAuth client as production but may have **stale** secret values (its `GOOGLE_CLIENT_SECRET` was already out of date as of this writing). It is not a sanctioned backup — treat anything pulled from it as unverified until cross-checked against a current source (e.g. Google Cloud Console, Supabase dashboard).

---

## Commands

```bash
# Dev server
cd backend && uvicorn backend.main:app --reload --port 8000

# Tests
cd backend && python -m pytest tests/

# Single test file
cd backend && python -m pytest tests/test_triage.py -v

# Apply DB schema — LOCAL/DEV ONLY. Production does NOT run this on startup
# (SKIP_MIGRATIONS=true — see "DB migrations" section before touching schema)
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

### DB migrations — additive only, and NOT automatic in production
No Alembic. New columns are added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `models/db.py::create_tables()`. **This function does NOT run on every production startup** — the live Render service sets `SKIP_MIGRATIONS=true` deliberately, because running the full migration list at every boot risked locking a large table (`processed_emails`) and causing Render R10 boot timeouts. That is a real, working safeguard — do not remove it as a "fix."

The consequence: **adding a new column to a model in code does nothing to the live database by itself.** `verify_schema_matches_models()` (also in `db.py`) runs read-only on every boot and logs any mismatch, but does not fix it. This has already caused one production outage (see Incident Log) when a new column (`drafts.send_claimed_at`) was added in code, deployed, and never actually created in Postgres — every query touching that column crashed with `UndefinedColumn`.

**Correct procedure any time you add a column:**
1. Add it to the SQLAlchemy model AND to the `_MIGRATION_STMTS` list in `create_tables()` (additive, `IF NOT EXISTS`, no destructive statements — same as always).
2. Before or immediately after deploying that code, temporarily unset `SKIP_MIGRATIONS` on the Render service (`DELETE /v1/services/{id}/env-vars/SKIP_MIGRATIONS`), trigger one deploy so `create_tables()` runs and applies the new statement, confirm via logs / `verify_schema_matches_models()` that it worked, then set `SKIP_MIGRATIONS=true` back (`PUT` the single key, value `"true"`).
3. Never leave `SKIP_MIGRATIONS` unset/false on a deploy you're not actively watching — it re-runs the entire statement list, including the ones with real lock risk.

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

---

## Incident Log

Real production incidents, kept so the next agent doesn't re-derive (or repeat) them. Add to this, don't delete past entries.

### 2026-08-03 — Production outage: missing DB column, then a full env-var wipe during recovery

**What broke, in order:**
1. A separate AI coding session (using Codex CLI, working on the same GitHub repo from Colin's local machine) independently built the same fix a Claude session was already mid-way through — an atomic "send claim" guard to stop duplicate Gmail sends — and pushed it straight to `main`, which auto-deployed to production.
2. That change added a new column, `drafts.send_claimed_at`, to the SQLAlchemy model and referenced it in queries. It did **not** get added to the actual Postgres table, because production runs with `SKIP_MIGRATIONS=true` (see "DB migrations" above) and nobody ran the manual migration step. Every query touching `drafts` started failing with `psycopg2.errors.UndefinedColumn`, which broke the dashboard (pending drafts, recently-sent — everything showed "Failed to load") and silently stalled the email-polling loop for every talent (0 emails processed per cycle).
3. While diagnosing this, the Claude session investigating it made an unrelated, unforced error: intending to update one Render environment variable, it called the **bulk** `PUT /v1/services/{id}/env-vars` endpoint (which replaces the entire env-var set) instead of the **per-key** endpoint, with a single placeholder value. This deleted every other environment variable on the live service — `DATABASE_URL`, `OPENAI_API_KEY`, `GOOGLE_CLIENT_ID`/`SECRET`, `GOOGLE_SHEETS_REFRESH_TOKEN`, `AGENCY_SECRET_KEY`, `N8N_WEBHOOK_SECRET`, `SKIP_MIGRATIONS` — gone, with no way to read the old values back via the Render API.

**Why it wasn't worse:** Render does not hot-restart a running instance just because its stored env vars changed via the API — the already-running process kept working normally (module the pre-existing column bug) until something triggered a fresh boot. That gave a window to fix the env vars before any deploy/restart would have applied the broken (empty) set and taken the whole app down (no DB connection at all).

**How it got fixed:** A second, older Render deployment of the same repo (`email-automation`, see "Where secrets actually live" above) turned out to still have a full, working copy of the same secrets — confirmed genuinely matching (not coincidence) because its `API_KEY` was byte-for-byte identical to production's. Every value was copied over via the safe per-key endpoint. `GOOGLE_CLIENT_SECRET` from that sibling turned out to be stale (rotated since), which Colin caught by comparing against the live Google Cloud Console value directly — a reminder that anything pulled from that sibling service should be cross-checked, not trusted blindly. `SKIP_MIGRATIONS` was intentionally unset for exactly one deploy to let the missing column get created, then restored to `true`.

**Root causes:**
- No coordination mechanism between AI agents/tools working on the same repo — two independent fixes for the same bug landed on `main` within the same session, and nobody checked for a schema/migration gap before deploying.
- `CLAUDE.md` itself was stale — it flatly said migrations "run at every startup," which is false in production and could easily lead an agent (human or AI) to assume a new column "just appears."
- No backup of production secrets existed anywhere outside Render's own env-var store, which does not support reading old values back. Recovery only worked because a lucky, unverified fallback (the sibling deployment) happened to exist.
- The bulk vs. per-key env-var API distinction is a sharp edge with no safety rail — one wrong endpoint choice destroys the entire config with a 200 OK response and no confirmation prompt.

**What changed as a result:** the "CRITICAL" section above, the corrected migration guidance, a `.env.example` added to the repo (documents every required var, no real values), and a real secrets backup handed directly to Colin to store outside of Render (a password manager, not this repo). If you're an agent reading this and about to make a similar env-var or migration change: re-read the CRITICAL section first.

### 2026-08-04 — Production outage: `varchar(512)` overflow crash-looped the poller for ~7 hours, masked as healthy

**What broke:** `inbox_emails.subject`, `.snippet`, and `.label_ids` were `VARCHAR(512)`. Gmail places no practical length bound on any of these fields, and consolidating 20 talents into one shared inbox raised inbound volume enough that a subject/snippet/label-list over 512 characters became statistically inevitable. One such row hit the batch upsert and failed with `psycopg2.errors.StringDataRightTruncation: value too long for type character varying(512)`. That failure poisoned the SQLAlchemy session for the rest of the cycle — every subsequent step (body fetch, shared-inbox Gmail list, the background poll itself) then failed with `This Session's transaction has been rolled back due to a previous exception during flush`, cascading from one bad row to a fully dead poll cycle, every ~45 seconds, from 08:45 UTC until the fix.

**Why nothing alerted:** the APScheduler job wrapper catches the exception at the top level and logs the job as executed successfully — so `/health`, `/api/status`, and the dashboard all read "fine" the entire time. No new drafts, no new inbox rows, no poll_health rows — but no error surface showed it either. This was caught by a human noticing drafting had stopped, not by any monitor.

**How it got fixed:** confirmed independently (Render log query for the exact `StringDataRightTruncation` signature, live and recurring every cycle; `information_schema.columns` query confirming all three columns were `varchar(512)`) before touching anything. Applied `ALTER TABLE inbox_emails ALTER COLUMN {subject,snippet,label_ids} TYPE TEXT` directly via the Supabase SQL editor — this is metadata-only in Postgres for a varchar→text widen (no table rewrite, brief catalog-only lock), unlike adding a new column, so it did not need the `SKIP_MIGRATIONS`-unset dance described above. Verified via a fresh `information_schema.columns` read (all three now `text`) and fresh Render logs (`Inbox sync talentmgmt: {'upserted': 146, 'updated': 65, 'errors': 0}`, zero `StringDataRightTruncation` on the next cycle). Followed up with the code-side fix so it can't regress: `backend/models/db.py` `InboxEmail.subject/snippet/label_ids` changed from `String(512)` to `Text`, and matching `ALTER COLUMN ... TYPE TEXT` statements added to `_MIGRATION_STMTS` (idempotent — a no-op if a given environment's column is already `TEXT`).

**Root cause:** an unbounded external input (Gmail subject/snippet/label data) stored in a bounded column with no truncation or validation at the write site, and a global exception handler at the scheduler level that reports "success" even when the wrapped job threw. The column-size problem is fixed; the misleading "job executed successfully" logging is not — a future contributor should not assume `_run_poll` succeeding in the logs means the poll actually completed without error.

**Also flagged during this incident, not yet acted on:** several drafts held stale send claims (`send_claimed_at` set, still `pending`) from workers that likely died mid-flight during the crash loop — per the existing safety design (see `_reject_if_send_in_progress` in `drafts.py`), these need a human to check the actual Gmail thread before clearing, not an automated clear. Also, Supabase's connection pooler hit `max clients reached` under concurrent 20-talent load — a separate capacity ceiling, unrelated to this bug, not yet sized or addressed.


### 2026-08-29 — Poller crash loop: Supabase pooler session-mode client limit (15) exceeded

**What broke:** every per-token poll cycle logged `psycopg2.OperationalError ... FATAL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15` for several talents (Katrina, Brittanie, Skyler), and the follow-up `PollHealth` write failed for the same reason — so those inboxes were silently not being processed while the shared inbox kept working.

**Root cause:** the app was configured to open far more Postgres connections than the pooler allows. SQLAlchemy's engine was `pool_size=10 + max_overflow=15` (hard cap 25) and `poller.py` ran `MAX_TALENT_WORKERS=5 × (1 + MAX_CONCURRENT_EMAILS=3)` + 1 = 21 concurrent sessions per cycle, plus the draft queue, auto_send, guardian and dashboard HTTP. Supabase's pooler for this project runs in **session mode** and caps this service at **15 concurrent clients**. Over that limit the pooler does not queue — it refuses the connect, so the error surfaces as a per-talent poll failure rather than backpressure.

**Fix (code, no infra change):** engine capped at `pool_size=6 + max_overflow=6` = 12 (overridable via `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`), `pool_timeout` raised 15→30 so callers wait for a pooled connection instead of failing; poller concurrency reduced to `MAX_TALENT_WORKERS=3`, `MAX_CONCURRENT_EMAILS=2` → peak 10; the three dashboard backfill/force-blast paths that spun 15 threads *each holding its own DB session* now use `MAX_DB_THREAD_WORKERS` (default 6) from `db.py`. Gmail-only thread pools (`inbox_sync` header/body fetch, dashboard header prefetch) were left at 20/15 — they hold no DB session.

**Follow-up, same day — moved to the transaction-mode pooler in code, not in env:** `_resolve_pooler_url()` in `db.py` now rewrites a Supabase *pooler* URL from port **5432 (session mode)** to **6543 (transaction mode)** at engine creation. `DATABASE_URL` on Render is deliberately NOT edited — the rewrite is code-side so no one has to touch a secret (see the env-var incident above), and `DB_POOLER_MODE=session` opts out without editing it either. Only a `*.pooler.supabase.com` host on 5432 (or with no port) is rewritten; a direct `db.<ref>.supabase.co` URL or any other host is left alone. This is safe here because nothing in this codebase uses session-scoped Postgres state (no LISTEN/NOTIFY, no advisory locks held across transactions, no temp tables, no session-level `SET`) and psycopg2 does not use server-side prepared statements — those are the usual transaction-pooling incompatibilities. Budgets follow the mode: transaction → `pool_size=10 + max_overflow=10`; session → hard-clamped to `6 + 6` regardless of env. Poller back up to `MAX_TALENT_WORKERS=4 × MAX_CONCURRENT_EMAILS=3` (peak 17). Covered by `backend/tests/test_pooler_url.py`.

**The invariant to preserve either way:** `MAX_TALENT_WORKERS × (1 + MAX_CONCURRENT_EMAILS) + 1 <= DB_POOL_SIZE + DB_MAX_OVERFLOW < pooler client limit`. Exceeding SQLAlchemy's own pool is harmless (threads wait on `pool_timeout`); exceeding the *pooler's* client limit is not — it refuses the connect and kills the cycle.

**Also visible in the same logs, not fixed here:** `Sheets log failed ... invalid_grant` (dead `GOOGLE_SHEETS_REFRESH_TOKEN`, known) and 74 pending drafts holding stale send claims from workers killed mid-flight — per existing design those need a human to check the Gmail thread before clearing, never an automated sweep.

---

## Session End Protocol

At the end of EVERY session — before your final message — write a session note to the Obsidian vault using the `obsidian-vault` MCP tool (`write_file`).

**File path:** `C:\Users\Admin\clawdbot-vault\Projects\email-automation\Sessions\YYYY-MM-DD-[topic].md`

**Template:**
```
# Session: [topic] — YYYY-MM-DD

## What we did
[What was discussed or built]

## What changed
[Files modified, logic updated, talents connected]

## Decisions made
[Any non-obvious decisions and why]

## System state
[How many talents connected, any OpenAI/DB issues, poller status]

## Next
[Open tasks, what to pick up next session]
```
