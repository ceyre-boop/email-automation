# Sunday Resilience Sprint — Post-Incident Hardening Plan
## June 14–15 2026

## Context

Immediate cascade fix already deployed (commit 445e2ce). This sprint makes the system so resilient this class of failure is structurally impossible, and builds tools so talent data can be managed without touching .md files directly.

**Sources of truth confirmed:**
- `sheets/sop.md` — all talent data (rates, manager, personal emails, auto_send, approved responses)
- `sheets/Automated Send Workflow.md` — send behavior rules
- `config/settings.json` — system config only (no talent data since 7ff7301)

---

## Task 0 — Clear INVALID Drafts from DB (SQL only, run FIRST)

Run in Supabase SQL editor in order. No Python. No Gmail touches. No status updates — DELETE only.

**Step 1 — Verify before deleting:**
```sql
SELECT id, talent_key, status, validation_failed, created_at
FROM drafts
WHERE validation_failed = true
ORDER BY created_at DESC;
```
Check the row count. If count > 200 or any row looks wrong (unexpected talent, unexpected status), STOP and report. Do not proceed.

**Step 2 — Delete:**
```sql
DELETE FROM drafts
WHERE validation_failed = true;
```

**Step 3 — Confirm zero remain:**
```sql
SELECT COUNT(*) FROM drafts
WHERE validation_failed = true;
```
Expected: 0

Hard rules: SELECT before DELETE always. If count in Step 1 is > 200 or anything looks off, stop.

---

## Task 1 — Complete the Migration: 6 Remaining Legacy Reads

The audit found 6 files still reading from `settings.json talents[]` (which is now empty). These are all secondary code paths — they don't break the primary pipeline — but they return empty results or wrong data.

**Additionally found: guardian writes paused flag to settings.json instead of sop.md — pausing a talent via guardian is an orphan write since sop.md is the source for the `paused` flag.**

### Files to fix and exactly what to change:

**1. `backend/services/talent_access.py` (line ~12)**
```python
# LEGACY:
for talent in get_settings().app_config.get("talents", []):
# NEW:
for talent in get_settings().talent_list:
```
Both `get_talent_config()` and `is_talent_paused()` need this. `talent_list` returns the same dict shape.

**2. `backend/routers/auth.py` (line ~50)**
```python
# LEGACY:
valid_keys = {t["key"].lower() for t in get_settings().app_config.get("talents", [])}
# NEW:
valid_keys = {t["key"].lower() for t in get_settings().talent_list}
```

**3. `backend/routers/analytics.py` (line ~113)**
```python
# LEGACY:
talent_configs = {t["key"].lower(): t for t in settings.app_config.get("talents", [])}
# NEW:
talent_configs = {t["key"].lower(): t for t in settings.talent_list}
```

**4. `backend/routers/cron.py` (line ~507 — the /api/status endpoint)**
```python
# LEGACY:
talents = settings.app_config.get("talents", [])
# NEW:
talents = settings.talent_list
```

**5. `backend/routers/drafts.py` (line ~310)**
```python
# LEGACY:
(t for t in settings.app_config.get("talents", []) if t["key"].lower() == pe.talent_key.lower())
# NEW:
(t for t in settings.talent_list if t["key"].lower() == pe.talent_key.lower())
```

**6. `backend/services/guardian.py` (line ~143)**
```python
# LEGACY:
talent_map = {t["key"].lower(): t for t in get_settings().app_config.get("talents", [])}
# NEW:
talent_map = {t["key"].lower(): t for t in get_settings().talent_list}
```

**7. `backend/services/guardian.py` — `_pause_talent()` writes to settings.json (line ~359)**
This is the hardest fix. When guardian auto-pauses a talent for violating rate limits, it currently writes `"paused": true` into `settings.json talents[]`. Since `sop.md` is now the source of truth for the `paused` flag, this write is orphaned — the system reads `paused` from sop.md, so guardian's write has zero effect.

Fix: `_pause_talent()` should update sop.md instead. Parse the sop.md, find the talent's section, replace `Paused: no` with `Paused: yes`, write the file back. Then call `get_settings.cache_clear()` so the change is picked up. This is a careful regex-replace on sop.md — same approach as the SOP Admin UI (Task 3).

### Verification for Task 1
After all 6 (7) changes:
```bash
grep -rn 'app_config.get("talents"' backend/ --include="*.py"
```
Expected: 0 results.

---

## Task 2 — Startup Screamer: Loud Failures on Boot

The startup already parses sop.md and logs talent count (added in 445e2ce). This task makes the health check surface that data publicly and makes failures impossible to miss.

### 2a — Enhanced `/health` response

Current (post-445e2ce): `{"status": "ok", "deployed_at": "...", "sop_hash": "abc123"}`

Target:
```json
{
  "status": "ok",
  "deployed_at": "2026-06-14T19:00:00Z",
  "sop_hash": "abc123def456",
  "talent_count": 12,
  "talent_warnings": 0,
  "any_warnings": false,
  "last_parse_timestamp": "2026-06-14T19:00:01Z"
}
```

**File:** `backend/routers/cron.py`

Compute these at module load time (alongside `_SOP_HASH`):
```python
from backend.services.sop_parser import parse_sop_md, validate_profiles
_SOP_TEXT = _SOP_PATH.read_text() if _SOP_PATH.exists() else ""
_PROFILES = parse_sop_md(_SOP_TEXT)
_PROFILE_WARNINGS = validate_profiles(_PROFILES)
_TALENT_COUNT = len(_PROFILES)
_PARSE_TIMESTAMP = _dt.utcnow().isoformat()
_ANY_WARNINGS = len(_PROFILE_WARNINGS) > 0
```

Update `/health` to return all these fields. Colin can hit `/health` and immediately know:
- Is the right sop.md deployed? (compare hash to local `git show HEAD:sheets/sop.md | sha256sum`)
- Are all 12 talents loaded?
- Are there any missing fields on any talent?

### 2b — Per-talent profile validation on boot

The existing `validate_profiles()` in sop_parser.py already checks for:
- missing approved response
- missing personal emails
- missing Gmail connection name
- missing minimum rate
- missing manager email

The startup already logs these warnings (in `on_startup()` in main.py). No code change needed — just make sure `_ANY_WARNINGS` and `talent_count` surface in the `/health` endpoint.

**If `_TALENT_COUNT < 5`:** current code logs CRITICAL (added in 445e2ce). Keep this.

### 2c — Cross-component source check on boot

After the profile parse, log a one-liner that confirms all critical components agree on the source:
```
Startup: sop.md loaded | hash=abc123 | talents=12 | warnings=0 | auto_send_eligible=10
```
`auto_send_eligible` = count of profiles where `auto_send=True and paused=False`.

**File:** `backend/main.py` in `on_startup()`

---

## Task 3 — SOP Admin UI: `/admin/sop`

A password-protected admin page that lets Colin or Marco manage talent data without touching files. This eliminates the "edited locally but forgot to commit" failure mode.

### Route and auth

New router: `backend/routers/sop_admin.py`, prefix `/admin`

Auth: same `x-api-key` pattern as other protected endpoints (`Depends(verify_api_key)`).

New HTML page: `backend/static/sop_admin.html`

Add to `main.py`:
```python
from backend.routers import sop_admin
app.include_router(sop_admin.router)

@app.get("/admin/sop", response_class=HTMLResponse, include_in_schema=False)
def sop_admin_page():
    return HTMLResponse(content=Path("backend/static/sop_admin.html").read_text())
```

### API endpoints (in `sop_admin.py`)

```
GET  /admin/api/talents              → List all 12 talents with all fields
GET  /admin/api/talents/{key}        → Full profile for one talent (approved response, rates, personal emails)
PUT  /admin/api/talents/{key}        → Update a talent's fields (writes back to sop.md)
POST /admin/api/talents              → Add a new talent (writes to sop.md)
POST /admin/api/talents/{key}/toggle-auto-send  → Toggle auto_send flag
GET  /admin/api/sop/raw              → Return raw sop.md text (for review before save)
POST /admin/api/sop/redeploy         → Commit + git push (triggers Render redeploy)
```

### sop.md write-back approach

Reading sop.md is handled by `sop_parser.py`. Writing is the new piece. Use a `sop_writer.py` service:

```python
# backend/services/sop_writer.py

def update_talent_field(sop_text: str, talent_key: str, field: str, value: str) -> str:
    """Replace a specific metadata field for a talent in sop.md text."""
    # Find the talent's section, replace the field line, return updated text

def update_approved_response(sop_text: str, talent_key: str, new_response: str) -> str:
    """Replace the Approved Response block for a talent."""

def update_personal_emails(sop_text: str, talent_key: str, emails: list[str]) -> str:
    """Replace the personal emails list for a talent."""

def add_talent_section(sop_text: str, profile: TalentProfile, approved_response: str) -> str:
    """Append a new talent section to sop.md."""

def write_sop_md(new_text: str) -> None:
    """Write updated text to sheets/sop.md and invalidate the settings cache."""
    Path("sheets/sop.md").write_text(new_text)
    get_settings.cache_clear()  # force re-parse on next request
```

The writer works on raw text using regex/string operations. The sop.md format is simple enough (field-per-line, Scenario A block) that this is reliable.

**Validation before save:**
```python
def validate_before_save(profile: TalentProfile) -> list[str]:
    errors = []
    if not profile.full_name: errors.append("full_name required")
    if not profile.key: errors.append("key required")
    if not profile.manager: errors.append("manager required")
    if not profile.manager_email: errors.append("manager_email required")
    if profile.minimum_rate_usd <= 0: errors.append("min_rate must be > 0")
    if not profile.personal_emails: errors.append("at least one personal email required")
    if not profile.has_approved_response: errors.append("approved response required")
    return errors
```

If `errors` is non-empty, return 400 with the list. Never write a partial or invalid profile to sop.md.

### Git commit on save

When a PUT/POST succeeds, optionally commit and push:
```python
import subprocess
subprocess.run(["git", "add", "sheets/sop.md"], cwd=REPO_ROOT)
subprocess.run(["git", "commit", "-m", f"admin: update {talent_key} via SOP Admin UI"], cwd=REPO_ROOT)
subprocess.run(["git", "push"], cwd=REPO_ROOT)
```

This triggers a Render redeploy automatically (Render watches the main branch). The UI should show a spinner and then confirm "Pushed to git — Render will redeploy in ~2 minutes."

### UI (sop_admin.html)

Single-file SPA (same pattern as dashboard.html). Features:
- Sidebar: talent list with color-coded status (green = complete, yellow = warnings, red = missing required field)
- Main panel: talent detail editor with fields for name, key, manager, min rate, auto_send toggle, approved response textarea, personal emails list
- Save button → PUT request → success toast OR error list
- Add Talent button → blank form → POST request
- "Push to Render" button → POST /admin/api/sop/redeploy
- Raw sop.md viewer (expandable) → GET /admin/api/sop/raw

---

## Task 4 — Pre-Deploy Checklist: GitHub Action

A GitHub Action that fails the push if any of these checks fail. This is server-side and cannot be bypassed.

**File:** `.github/workflows/sop-guard.yml`

```yaml
name: SOP Guard
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  sop-guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Parse sop.md and validate profiles
        run: |
          python - <<'EOF'
          import sys
          sys.path.insert(0, '.')
          from backend.services.sop_parser import parse_sop_md, validate_profiles
          text = open('sheets/sop.md').read()
          profiles = parse_sop_md(text)
          if len(profiles) < 5:
              print(f"FAIL: only {len(profiles)} talent profiles found in sop.md (expected >= 5)")
              sys.exit(1)
          warnings = validate_profiles(profiles)
          for w in warnings:
              print(f"WARNING: {w}")
          print(f"PASS: {len(profiles)} talent profiles loaded, {len(warnings)} warnings")
          EOF
      
      - name: Grep for legacy talent array reads
        run: |
          FOUND=$(grep -rn 'app_config.get("talents"' backend/ --include="*.py" || true)
          if [ -n "$FOUND" ]; then
            echo "FAIL: legacy settings.json talents[] reads found:"
            echo "$FOUND"
            exit 1
          fi
          echo "PASS: no legacy talent array reads found"
      
      - name: Grep for banned patterns
        run: |
          FAIL=0
          
          # Send window (was deleted, must stay deleted)
          if grep -rn "send_window\|ZoneInfo\|_is_within_send_window" backend/ --include="*.py" -q; then
            echo "FAIL: send_window/ZoneInfo found (operating hours logic must stay removed)"
            FAIL=1
          fi
          
          # Proposed rate in prompt files (was removed, must stay removed)
          if grep -rn "proposed_rate" prompts/ --include="*.md" -q; then
            echo "FAIL: proposed_rate found in prompt files"
            FAIL=1
          fi
          
          # Hard-coded manager emails in Python (not in sop.md)
          if grep -rn 'lizz@\|britt@\|sylvia@\|trinity@\|michaela@' backend/ --include="*.py" -q; then
            echo "FAIL: hard-coded talent-specific emails found in Python"
            FAIL=1
          fi
          
          if [ $FAIL -eq 1 ]; then exit 1; fi
          echo "PASS: no banned patterns found"
```

This runs on every push to main and every PR. A failed check blocks the merge.

**Local pre-commit hook** (optional, for speed — catches issues before push):

```bash
#!/bin/bash
# .git/hooks/pre-commit
echo "Running SOP guard..."
python - <<'EOF'
import sys
sys.path.insert(0, '.')
from backend.services.sop_parser import parse_sop_md
text = open('sheets/sop.md').read()
profiles = parse_sop_md(text)
if len(profiles) < 5:
    print(f"ABORT: only {len(profiles)} talent profiles in sop.md")
    sys.exit(1)
EOF
FOUND=$(grep -rn 'app_config.get("talents"' backend/ --include="*.py" 2>/dev/null || true)
if [ -n "$FOUND" ]; then
  echo "ABORT: legacy settings.json talent reads found:"
  echo "$FOUND"
  exit 1
fi
echo "SOP guard passed."
```

Make executable: `chmod +x .git/hooks/pre-commit`

---

## Task 5 — INVALID Draft Prevention: Fail Fast at Creation

Drafts should never be created with incomplete talent data. The burst of wrong-rate drafts happened because `talent_map={}` silently allowed creation with `minimum_rate=0`.

### 5a — Guard in `_draft_one` (cron.py)

After building `talent_cfg`, before calling `_process_one_message`:

```python
def _draft_one(row):
    _tk = row.talent_key.lower()
    talent_cfg = talent_map.get(_tk, {})
    
    # Fail fast: never create a draft for an unknown or incomplete talent
    if not talent_cfg:
        logger.error(
            "Draft queue: talent '%s' not found in sop.md profiles — skipping (email %s)",
            _tk, row.gmail_message_id
        )
        return
    if talent_cfg.get("minimum_rate_usd", 0) == 0:
        logger.error(
            "Draft queue: talent '%s' has minimum_rate=0 — incomplete profile, skipping",
            _tk
        )
        return
    if talent_cfg.get("paused"):
        return
    ...
```

This means: if a talent somehow doesn't appear in sop.md, or their rate is 0, the draft is skipped with an ERROR log — not silently processed with wrong data.

### 5b — Dashboard metric: "Drafts with incomplete data"

In the `/api/status` or `/health` endpoint, add:
```python
bad_drafts = db.query(Draft).filter(
    Draft.status == DraftStatus.pending,
    Draft.validation_failed == True
).count()
```
Return as `invalid_draft_count`. Should always be 0 in healthy state.

Also add a banner in dashboard.html: if `invalid_draft_count > 0`, show a red banner at the top: `"⚠️ {N} drafts marked INVALID — click Re-validate INVALID to clear them."`

### 5c — Startup cross-check

In `on_startup()`, after parsing sop.md profiles, verify that every active talent token in the DB has a corresponding sop.md profile:

```python
from backend.models.db import TalentToken
with Session(get_engine()) as db:
    active_tokens = db.query(TalentToken).filter(TalentToken.active == True).all()
    for token in active_tokens:
        if token.talent_key.lower() not in {k.lower() for k in profiles}:
            logger.error(
                "Startup: active Gmail token for '%s' has NO matching sop.md profile — "
                "this talent's emails will be processed but drafts may fail validation",
                token.talent_key
            )
```

This catches the case where a talent is connected to Gmail but their sop.md entry was accidentally deleted.

---

## Task 6 — Failure Mode Documentation

**File:** `docs/FAILURE_MODES.md`

Create this document to capture institutional memory. Each entry: incident, root cause, fix, prevention.

| # | Incident | Root Cause | Fix | Prevention |
|---|---|---|---|---|
| 1 | Dollar amount in GPT context → draft corruption | Proposed rate injected into prompt → GPT included it in reply | Removed `proposed_rate` from prompt context | CI grep for `proposed_rate` in prompt files |
| 2 | Scenario B bundle logic → wrong draft type | Bundle-offer scenario code remained after SOP removed it | Fully removed Scenario B code path | sop.md is single source — if SOP doesn't have it, code shouldn't either |
| 3 | sop_data.json drift → SOP pending errors | Hand-maintained sop_data.json fell out of sync with sop.md | sop_data.json auto-generated at startup from sop.md | Startup regeneration; never edit sop_data.json manually |
| 4 | settings.json talent array → INVALID flood | 7ff7301 removed talents[] but 6 code paths still read it | Fixed each read to use talent_profiles (sop.md) | CI grep for `app_config.get("talents"` |
| 5 | Render cache → stale file deployed | Render served cached build without new sop.md version | No-op commit pattern; sop_hash in /health endpoint | Compare sop_hash at /health to local hash; startup CRITICAL if count < 5 |
| 6 | Send window re-appearing → sends blocked 7am-7pm | Time window logic was removed but reference crept back | Deleted; now caught by CI grep | CI grep for `ZoneInfo`, `send_window`, `_is_within_send_window` |
| 7 | From header parsing → personal email filter broken for all 10 talents | Raw From header includes display name; bare address comparison failed | `parseaddr()` to extract bare address | Covered by integration test; personal email check verified in poller |
| 8 | Guardian pause writes to settings.json (orphan write) | Guardian writes `paused=true` to settings.json but system reads `paused` from sop.md → pause has no effect | Fix `_pause_talent()` to write to sop.md instead | Task 1 fix; startup cross-check catches disconnected tokens |
| 9 | 100+ drafts spike in 2 minutes | Backlog blaster + large accumulated backlog + no burst guard | Burst guard: skip if > 20 drafts created in last 60s | Burst guard deployed in 445e2ce |
| 10 | Jocelyn "not in talent roster" for manual send | Render running stale container where `_validate_talent` used settings.json talents[] (empty) | Fixed `_validate_talent` to use talent_list (sop.md) | sop_hash at /health lets Colin detect stale containers |

---

## Execution Order

1. **Task 0** — Run SQL to clear INVALID drafts (Supabase SQL editor, before any code changes)
2. **Task 1** — Fix 6+1 remaining legacy reads (code changes, commit, push)
3. **Task 2** — Enhanced /health endpoint with talent_count, warnings, timestamp
4. **Task 5** — Fail-fast guard in `_draft_one`, dashboard INVALID banner
5. **Task 4** — GitHub Action (SOP guard CI)
6. **Task 3** — SOP Admin UI (largest task, do last)
7. **Task 6** — docs/FAILURE_MODES.md (document as we go)

---

## Critical Files Modified in This Sprint

| File | Task | Change |
|---|---|---|
| `backend/services/talent_access.py` | T1 | app_config → talent_list |
| `backend/routers/auth.py` | T1 | app_config → talent_list |
| `backend/routers/analytics.py` | T1 | app_config → talent_list |
| `backend/routers/cron.py` | T1, T2 | app_config → talent_list; /health enhancements |
| `backend/routers/drafts.py` | T1 | app_config → talent_list |
| `backend/services/guardian.py` | T1 | app_config → talent_list; _pause_talent writes to sop.md |
| `backend/main.py` | T2, T5 | startup cross-check; INVALID banner support |
| `backend/routers/cron.py` | T5 | _draft_one fail-fast guard |
| `backend/static/dashboard.html` | T5 | INVALID banner |
| `backend/routers/sop_admin.py` | T3 | new file |
| `backend/services/sop_writer.py` | T3 | new file |
| `backend/static/sop_admin.html` | T3 | new file |
| `.github/workflows/sop-guard.yml` | T4 | new file |
| `.git/hooks/pre-commit` | T4 | new file (local hook) |
| `docs/FAILURE_MODES.md` | T6 | new file |

---

## Verification After Sprint

1. `grep -rn 'app_config.get("talents"' backend/ --include="*.py"` → 0 results
2. `grep -rn 'ZoneInfo\|send_window' backend/ --include="*.py"` → 0 results
3. `/health` returns `talent_count: 12`, `any_warnings: false`, `sop_hash` present
4. Pause a talent via Guardian (trigger a rate violation test) → confirm sop.md `Paused: yes` is written, not settings.json
5. Try adding a talent via `/admin/sop` UI → confirm sop.md updates, commit visible in git log
6. Push a commit with `app_config.get("talents"` → GitHub Action should FAIL
7. `POST /api/dashboard/admin/revalidate-drafts` → returns `{"cleared": 0, "still_failed": 0}`
8. Check Render startup logs: "12 talent profiles loaded from sop.md, 0 warnings"
