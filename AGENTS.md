# AGENTS.md — Read this before touching this repo

This project is worked on by more than one AI coding tool (Claude Code / Cowork sessions, Codex CLI, possibly others). This file and `CLAUDE.md` are kept in sync — whichever one your tool reads, read the other too if you can; `CLAUDE.md` has the full architecture writeup, this file has the same critical safety rules in case you only load `AGENTS.md`.

**On 2026-08-03 this system had a real production outage caused by two different AI agents working on this repo without knowing about each other, and on 2026-08-04 an unbounded Gmail field crashed the poller for ~7 hours while every health check reported "fine."** Full postmortems are in `CLAUDE.md` under "Incident Log" — read them before making changes that touch the database schema, environment variables, or a production deploy.

## Before you push

- Run `git fetch && git log HEAD..origin/main --oneline`. If it shows anything, someone (human or another agent) already pushed since you last synced — pull/rebase and check for overlapping work before adding yours.
- Don't assume you're the only one who might trigger a Render deploy. Check `GET /v1/services/{id}/deploys?limit=3` for anything that started recently and wasn't you.

## Before you change the database schema

- Adding a column to a SQLAlchemy model in `backend/models/db.py` does **nothing** to the live database by itself. Production runs with `SKIP_MIGRATIONS=true` deliberately (see `CLAUDE.md` "DB migrations" section) — a new column silently doesn't exist until someone runs the migration on purpose. Deploying code that queries a column that isn't there yet crashes every request that touches it. This exact thing already happened once.
- If you add a column: add it to both the model AND the `_MIGRATION_STMTS` list in `create_tables()`, then follow the exact procedure in `CLAUDE.md` to apply it (temporarily unset `SKIP_MIGRATIONS`, one deploy, verify, restore it).

## Before you touch Render environment variables

- Real secrets (`DATABASE_URL`, `OPENAI_API_KEY`, Google OAuth credentials, etc.) live ONLY as Render env vars on the `manager-email-automation` service. Never in this repo, never in `.env` (gitignored), never in `render.yaml` (which only references keys by name via `sync: false`).
- **`PUT /v1/services/{id}/env-vars` (no key in the URL) replaces the ENTIRE env-var set.** Calling it with anything less than every single correct key/value wipes the rest. This destroyed every production secret in one call during the incident above. **Always use `PUT /v1/services/{id}/env-vars/{KEY}`** (the key in the URL) to add or change one value at a time.
- If you ever need a secret value and don't have it, ask the user for it or for the offline backup — don't guess, don't regenerate/rotate a credential without explicit permission (rotating breaks anything still using the old value).

## Where things actually live (so you don't have to rediscover this)

- `render.yaml` is NOT a faithful snapshot of the live service — the production service predates it and has drifted. Query the Render API for actual live config; editing the yaml does not reconfigure the existing service.
- Talent roster + SOP rules: `sheets/sop.md` (source of truth at runtime — restored from a DB row on every boot, not just from git).
- Routing/model config: `config/settings.json` (git-controlled, needs a real deploy to take effect).
- Real secrets: Render env vars only (see above).
- Database: Supabase Postgres, reached via `DATABASE_URL`.

For the full picture — polling architecture, AI provider rules, per-talent OAuth, prompt structure — read `CLAUDE.md`. It's the primary doc; this file exists so Codex-style tools that look for `AGENTS.md` specifically don't miss any of it.
