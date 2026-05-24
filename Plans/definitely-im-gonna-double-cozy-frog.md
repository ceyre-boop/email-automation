# Self-Healing Guardian System — Implementation Plan

## Context

Today's incident: a race condition between `_run_draft_queue` (every 20s) and `_run_backlog_blaster` (every 30s) caused Wesley to accumulate 5,350 Gmail drafts and Audur 12,006 — over hours, with zero automated detection. Recovery was fully manual and took most of the workday.

Root cause: both jobs run 50-worker thread pools that query the same `NOT IN (drafts)` subquery before any commits land. The DB unique constraint on `drafts.gmail_message_id` protects the database but not the Gmail API — so duplicate drafts pile up in Gmail while the DB looks clean.

**What self-healing would have caught this:** a draft velocity check firing after ~20 drafts in 10 minutes, auto-pausing that talent and emailing Colin with a one-click kill link. Total exposure: <2 minutes instead of hours.

---

## The 10-Line Fix First (Phase 0 — do this immediately)

Before anything else, close the race condition in `backend/routers/cron.py`:

```python
# Add at module level:
_draft_queue_lock = threading.Lock()

# Wrap _run_draft_queue:
def _run_draft_queue(batch_size=60):
    if not _draft_queue_lock.acquire(blocking=False):
        logger.info("Draft queue skipped — already running")
        return
    try:
        ... # existing code unchanged
    finally:
        _draft_queue_lock.release()
```

`_run_backlog_blaster` calls `_run_draft_queue(batch_size=300)` — with this lock in place, they can never overlap. This alone prevents the incident from recurring.

---

## Architecture: 5 Layers

```
Layer 1: Detection     → measure draft velocity, ratio, caps
Layer 2: Circuit Break → auto-pause talent or kill ai_enabled
Layer 3: Alerting      → email Colin with one-click kill link
Layer 4: Remediation   → clear stuck rows, schedule recovery
Layer 5: Audit         → immutable log of every automated action
```

All implemented in a single new service: `backend/services/guardian.py`, run as an APScheduler job every 60 seconds.

---

## Phase 1 — Core Watchdog (Day 1, ~4 hours)

**What it delivers:** Would have fully caught today's incident.

### 1. DB Migrations — add to `create_tables()` in `backend/models/db.py`

```sql
-- Immutable audit trail of every automated action
CREATE TABLE IF NOT EXISTS guardian_audit_log (
    id           SERIAL PRIMARY KEY,
    action       VARCHAR(64) NOT NULL,
    talent_key   VARCHAR(64),
    reason       TEXT NOT NULL,
    detail       TEXT,
    triggered_by VARCHAR(64) NOT NULL DEFAULT 'guardian',
    created_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- New columns on drafts table (additive)
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS triggered_by_job VARCHAR(32);
```

Add two new SQLAlchemy models: `GuardianAuditLog` (mirrors table above). No model needed for the velocity snapshots — queries are computed live against the `drafts` table.

### 2. New file: `backend/services/guardian.py`

**Core class: `GuardianWatchdog`**

```python
class GuardianWatchdog:
    def run(self, db: Session):
        triggers = []
        triggers += self.check_draft_velocity(db)
        triggers += self.check_per_talent_caps(db)
        triggers += self.check_draft_email_ratio(db)
        triggers += self.check_stuck_processing(db)
        self.check_backlog_blaster_safety(db)
        self.maybe_schedule_recovery(db)
        for t in triggers:
            self._dispatch(db, t)
```

**`check_draft_velocity(db)` — the primary detector**

Thresholds (all configurable in `settings.json` under `guardian` key):
- `per_talent_warn_limit`: 15 drafts/10min → Marco message only
- `per_talent_hard_limit`: 30 drafts/10min → auto-pause that talent
- `global_hard_limit`: 50 drafts/10min across all talents → disable `ai_enabled`

Query: one `GROUP BY talent_key` on `drafts.created_at >= now - 10min`.

**`check_per_talent_caps(db)` — daily absolute cap**

Default: 50 drafts/day per talent (configurable via `max_drafts_per_day` on each talent entry in `settings.json`). If exceeded, pause that talent.

**`check_draft_email_ratio(db)`**

`ratio = drafts_last_10min / max(emails_processed_last_10min, 1)`
- ratio > 3.0 and count > 10 → Marco warning
- ratio > 5.0 and count > 20 → global kill

**`check_stuck_processing(db)`**

`ProcessedEmail` rows with `status='processing'` and `processed_at < now - 5min` are stale locks from crashed workers. Reset them to `status='flagged'` so they can be reprocessed. Log count to audit log.

**`_set_ai_enabled(enabled: bool)`**

Writes `ai_enabled` to `config/settings.json` on disk. Since `app_config` is a `@property` that reads from disk on every access (confirmed in `config.py`), this takes effect on the next poll cycle (~45 seconds) with no redeploy.

**`_pause_talent(talent_key, reason)`**

Writes `paused: true` to that talent's entry in `settings.json`. The draft queue already checks `talent_cfg.get("paused")` and returns early — change takes effect within 20 seconds.

### 3. Wire guardian into scheduler — `backend/main.py`

```python
# Expose scheduler at module level (guardian needs to pause/resume jobs)
_scheduler: BackgroundScheduler | None = None

@app.on_event("startup")
def on_startup():
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    # ... existing jobs ...
    _scheduler.add_job(_run_guardian, "interval", seconds=60,
                       id="guardian", replace_existing=True, max_instances=1)
    _scheduler.start()
```

### 4. Add `_run_guardian()` — `backend/routers/cron.py`

Standard pattern matching existing job wrappers — opens DB session, instantiates `GuardianWatchdog`, calls `.run(db)`, closes session.

### 5. `config/settings.json` additions

```json
"guardian": {
    "enabled": true,
    "velocity_window_minutes": 10,
    "global_draft_hard_limit": 50,
    "per_talent_draft_warn_limit": 15,
    "per_talent_draft_hard_limit": 30,
    "draft_email_ratio_warn": 3.0,
    "draft_email_ratio_kill": 5.0,
    "stuck_processing_threshold_minutes": 5,
    "alert_cooldown_minutes": 30,
    "alert_email": "colineyre222@gmail.com",
    "recovery_wait_minutes": 30,
    "recovery_health_threshold": 0.7
}
```

Add `"max_drafts_per_day": 50` to each talent entry.

---

## Phase 2 — Alerting (Day 2, ~3 hours)

### Alert email via existing Gmail API

Uses `send_standalone_message()` from `backend/services/gmail.py` (already wired). Sends from the first active talent token. Recipient: `colineyre222@gmail.com`.

Email contains:
- What triggered, which talent, current counts
- One-click kill link (HMAC-signed, 15-minute expiry)
- Dashboard URL
- Cooldown: max 1 alert per 30 minutes (stored in `app_state`)

**HMAC kill link** — `GET /api/guardian/kill?token=<hmac>`
- Token = `HMAC-SHA256(AGENCY_SECRET_KEY, "kill:{expiry_timestamp}")`
- 15-minute window
- On valid hit: sets `ai_enabled=false`, logs audit, returns a simple HTML confirmation page
- No API key required (it's the emergency button from email)

### Marco message integration

Every guardian trigger also writes a `MarcoMessage` with `severity="critical"`, `category="guardian"`. Visible on dashboard within 60 seconds of the incident.

---

## Phase 3 — Recovery + Admin Endpoints (Day 3, ~2 hours)

### Self-recovery

`maybe_schedule_recovery(db)`: if guardian disabled AI, checks every 60s whether:
1. 30+ minutes have passed since disable
2. `health_score >= 0.7`

If both true: re-enables `ai_enabled`, logs audit, sends recovery alert email.

### New admin endpoints — new router `backend/routers/guardian.py`

All require `verify_api_key` except the kill endpoint:

| Endpoint | Action |
|---|---|
| `POST /api/admin/guardian/disable-ai` | Manual kill switch |
| `POST /api/admin/guardian/enable-ai` | Manual re-enable |
| `POST /api/admin/guardian/pause-talent/{talent_key}` | Pause one inbox |
| `POST /api/admin/guardian/unpause-talent/{talent_key}` | Unpause one inbox |
| `GET /api/admin/guardian/status` | Current state, counts, last run |
| `GET /api/admin/guardian/audit-log` | Last 100 actions |
| `GET /api/guardian/kill?token=X` | One-click kill from email (HMAC) |

---

## Phase 4 — Health Score + Audit Trail (Day 4, ~2 hours)

### Update `backend/services/health.py`

Add two components, rebalance weights:

| Component | Old Weight | New Weight | Why |
|---|---|---|---|
| triage_reliability | 0.30 | 0.25 | — |
| queue_liveness | 0.25 | 0.20 | — |
| draft_freshness | 0.25 | 0.15 | ↓ runaway drafts look "fresh" |
| **draft_velocity** | — | 0.20 | ← NEW, most important after today |
| **per_talent_balance** | — | 0.10 | ← NEW |
| token_health | 0.10 | 0.05 | — |
| poll_health | 0.10 | 0.05 | — |

`draft_velocity` score: `1.0` if <20 drafts/10min, `0.5` if <40, `0.0` if ≥40 (adds to issues list).

### Audit trail on drafts

Pass `job_name` through `_process_one_message()` → stored in `drafts.triggered_by_job`. Values: `'poller'`, `'draft_queue'`, `'backlog_blaster'`. Answers "who created this draft" in post-incident review.

---

## Implementation Order

```
Phase 0 (10 min):  threading.Lock() in cron.py — closes race condition NOW
Phase 1 (4 hrs):   DB migrations, guardian.py skeleton, scheduler wiring, settings.json
Phase 2 (3 hrs):   Alert email, HMAC kill link, Marco integration
Phase 3 (2 hrs):   Recovery scheduler, admin endpoints
Phase 4 (2 hrs):   Health score reweight, triggered_by_job audit column
```

Total: ~12 hours of implementation + 2 hours testing = ~2 days of focused work.

---

## Verification

### Layer 1 (Detection)
- Insert 35 `Draft` rows for one talent with `created_at = now()` via SQL
- Wait 60s, hit `GET /api/admin/guardian/status` — should show that talent above warn threshold
- Verify `guardian_audit_log` and `MarcoMessage` rows exist

### Layer 2 (Circuit Breakers)
- Insert 55 Draft rows across all talents in last 10 min
- Wait 60s, check `settings.json` — `ai_enabled` should be `false`
- Verify next poll cycle skips drafting

### Layer 3 (Alerting)
- Trigger guardian manually via `POST /api/admin/guardian/run` (add test-only endpoint)
- Verify email arrives at `colineyre222@gmail.com` within 2 minutes
- Click kill link, verify `ai_enabled` flips to `false`
- Replay link after 16 minutes, verify it rejects (expired)

### Layer 4 (Remediation)
- Set 10 `ProcessedEmail` rows to `status='processing'` with `processed_at = now() - 6min`
- Wait 60s, verify all 10 rows → `status='flagged'`
- Verify `guardian_audit_log` row: `action='clear_stuck_processing', detail contains count=10`

### Layer 5 (Audit)
- `GET /api/admin/guardian/audit-log` — verify all actions from tests 1-4 appear
- Confirm no `DELETE` calls exist in guardian.py (audit log is append-only)

---

## Critical Files

| File | Change |
|---|---|
| `backend/routers/cron.py` | Phase 0 lock fix; `_run_guardian()` wrapper; `triggered_by_job` passthrough |
| `backend/services/guardian.py` | **New** — entire watchdog |
| `backend/routers/guardian.py` | **New** — admin endpoints + kill route |
| `backend/models/db.py` | `GuardianAuditLog` model; `create_tables()` migrations; `drafts.triggered_by_job` column |
| `backend/main.py` | Expose `_scheduler` at module level; add guardian job |
| `backend/services/health.py` | Add `draft_velocity` + `per_talent_balance` components, reweight |
| `config/settings.json` | `guardian` block; `max_drafts_per_day` on each talent |
