# Architecture cleanup: remove dead Sheets logging, disable dead GitHub poller, correct cron.py claim

## Context

You pasted a third-party architecture review with three suggestions. I had two Explore
agents verify each claim against the actual code and production state before touching
anything, since the review's assumptions turned out to be partly wrong:

1. **Sheets logging** — the review says it's "adding failure overhead on every poll
   cycle." **Not true right now.** `backend/services/sheets.py` already has a circuit
   breaker (`_breaker_is_open()`) that's currently tripped (confirmed via production
   `/health`) and short-circuits every call to an in-process no-op before any network
   call — the overhead the review describes was real for the weeks *before* the breaker
   tripped, not today. That said, the token is still dead and the feature is a pure
   write-only "Master Activity Log" — nothing in the dashboard, analytics, or any other
   code path reads from that Sheet. Everything it logs already lives in Postgres
   (`ProcessedEmail`/`Draft`) with a superset of fields. You picked **remove it
   entirely** over fixing it (no functionality lost, no OAuth re-consent needed).

2. **Backup GitHub Actions poller** (`.github/workflows/poll.yml`) — confirmed broken
   every day for weeks (`DATABASE_URL` secret points at a Supabase database that no
   longer exists, fails before ever reaching Gmail). The real finding: **fixing it would
   be actively dangerous, not just pointless.** `scripts/run_poller.py` calls
   `create_tables()` unconditionally with no `SKIP_MIGRATIONS` check — if the secret
   were fixed, this workflow would run the *entire* migration list against production
   every day at 9am UTC, including lock-risk DDL statements, concurrently with live
   traffic. `SKIP_MIGRATIONS=true` exists on the Render service specifically to prevent
   this exact class of incident (documented in this repo's own incident log). The
   workflow is also undocumented in CLAUDE.md (the doc of record for how this system
   actually runs) and provides no real disaster-recovery value — it shares the same
   database as the primary service and has none of the primary service's safety guards
   (burst guard, age guard, draft-queue lock, thread guardrails). **Recommendation:
   disable/delete it**, not fix it.

3. **cron.py "doing too much in sequence"** — this claim is **factually incorrect**.
   `cron.py` already runs ~12 independently-scheduled APScheduler jobs (poll every 90s,
   draft-queue every 60s, auto-send every 60s, guardian, stall-alarm, claim-reaper,
   reconcile, etc.), each with its own thread pool and `max_instances=1`. Scoring,
   drafting, and sending are already on three separate schedules — sending in
   particular only ever happens from `auto_send.py`'s own job, never inline during
   polling or drafting. The only place triage and draft happen in one function call is
   the per-message step inside `_process_one_message`, and that's already parallelized
   up to 10-way concurrent GPT calls. There's no incident-log entry or timing evidence
   of a real "high-volume periods slow everything down" bottleneck. **No action needed
   here** — implementing the review's proposed async split would be solving a problem
   that doesn't exist, and risks the exact kind of DB-pool/concurrency incident this
   repo has already been burned by once (see CLAUDE.md's 2026-08-29 incident).

## What this plan actually does

Two changes, both low-risk and fully reversible via git:

### A. Remove Google Sheets logging entirely

Verified before writing this: every consumer, every dependency, no dashboard/analytics
read path exists (`dashboard.py`, `analytics.py`, `dashboard.html` all source stats from
Postgres only). `gspread`/`gspread-dataframe` in `requirements.txt` are used nowhere
else. `_SCOPES`/`sheets_credentials` in `config.py` are self-contained to the Sheets
feature (they reuse `google_client_id`/`google_client_secret`, which stay — those are
also used by Gmail OAuth). `sop_matrix_sheet_id` in `config/settings.json` is a separate,
already-inert TODO placeholder never read by any code — left untouched, out of scope.

Files to change:
- **Delete** `backend/services/sheets.py`
- **Delete** `backend/tests/test_sheets_breaker.py`
- **Delete** `scripts/generate_google_refresh_token.py`
- `backend/services/poller.py` — remove `_safe_log_sheet()` (lines ~1211-1216), its 5
  call sites (currently at poller.py:963, 1018, 1035, 1158, 1202 — all *after*
  `db.commit()`, so removal is purely subtractive, no reordering needed), and the
  `from backend.services import sheets as sheets_svc` import
- `backend/routers/cron.py` — remove the `breaker_state` import/`_sheets_block()` call
  and the `"sheets_auth"` key from the `/health` response (~cron.py:153-160, 219)
- `backend/core/config.py` — remove `google_sheets_refresh_token`,
  `google_sheets_service_account_json`, `google_sheets_service_account_file` fields and
  the `sheets_credentials`/`service_account_info` properties (~config.py:36-43, 66-107)
- `config/settings.json` — remove `master_log_sheet_id`/`master_log_tab_name` (and their
  `_comment` keys) from the `google_sheets` block; leave `sop_matrix_sheet_id` alone
- `.github/workflows/create-env.yml`, `.github/workflows/export-env.yml` — drop the
  `GOOGLE_SHEETS_REFRESH_TOKEN`/`GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` secret lines
  (`poll.yml` itself is deleted in part B, so no edit needed there)
- `backend/requirements.txt` — remove `gspread==6.1.2` and `gspread-dataframe==4.0.0`

### B. Disable/delete the dead GitHub Actions poller

- **Delete** `.github/workflows/poll.yml`
- **Delete** `scripts/run_poller.py` (its only caller)
- Leave `render.yaml`'s `poller-heartbeat` cron and `scripts/heartbeat.py` untouched —
  that's the real, working mechanism (`GET /cron/poll-inboxes` against the live Render
  service every 5 min), unrelated to this dead workflow.
- Update `README.md`'s "Part 1 — Daily inbox poller (GitHub Actions...)" section, which
  currently documents this as the primary/official polling mechanism — that's stale
  and actively misleading to a future reader. Replace with an accurate one-paragraph
  description of the real architecture (Render web service + APScheduler + the
  `poller-heartbeat` cron nudge), matching what CLAUDE.md already documents.

Not doing (flagged in the investigation but out of scope for this pass): a real
dead-man's-switch alert (e.g. paging if `talents.last_poll_at` goes stale) would be a
more honest replacement for "GitHub Actions as backup" than what existed — worth
considering later, not built now since nothing asked for it.

## Verification

1. `grep -rn "sheets\." backend/ --include="*.py" | grep -v __pycache__ | grep -v "sheets/sop"` returns nothing outside the deleted files — confirms no dangling references.
2. `grep -rln "GOOGLE_SHEETS" . --include="*.yml" --include="*.py" --include="*.json"` (excluding `.venv`) returns nothing.
3. Run the full backend suite: `source .venv/bin/activate && python -m pytest backend/tests/ -q` — expect the same pass count minus the ~4-6 tests deleted with `test_sheets_breaker.py`, zero new failures elsewhere (poller/cron tests must still pass since removal is purely subtractive after an already-committed transaction).
4. Hit production `/health` after deploy and confirm the response no longer includes a `sheets_auth` key, and every other field (`pipeline`, `db_pool`, `db_writes`) is unchanged.
5. Confirm a live poll cycle still triages/drafts normally post-deploy (same check used earlier this session: `pipeline.score3_last_hour` / `drafts_last_hour` moving together, `stalled: false`).
6. Confirm `.github/workflows/poll.yml` and `run_poller.py` are gone from the repo and the Actions tab no longer shows a scheduled "Poll Inboxes" workflow.
