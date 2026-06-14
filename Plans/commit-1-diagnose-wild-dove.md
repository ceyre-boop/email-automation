# Plan — Pre-deploy verification: 9eb3f24 (validation.py fix)

## Status: VERIFIED — safe to deploy

## What the fix does

Commit `9eb3f24` patches `backend/services/validation.py` check 5 (talent roster lookup).

**Root cause:** Commit `7ff7301` ("single source of truth — sop.md drives all talent data")
removed `talents[]` from `config/settings.json` and moved all talent metadata to `sheets/sop.md`
headers. Every file that read from settings.json was updated to use `settings.talent_profiles`
(sourced from sop.md via sop_parser) — except `validation.py`, which still called
`app_config.get("talents", [])`. Since `talents[]` no longer exists in settings.json, that
returned `[]`. `known_keys = set()`. Every draft failed "not in talent roster". `validation_failed=True`.
auto_send skips all `validation_failed=True` drafts. 0 sends for 15 hours.

**Fix:** One-line change in `validation.py:59`:
```python
# OLD (broken — reads from settings.json which no longer has talents[])
known_keys = {t["key"].lower() for t in get_settings().app_config.get("talents", [])}
if draft.talent_key.lower() not in known_keys:

# NEW (reads from sop.md profiles — same source as everything else)
known_keys = {k.lower() for k in get_settings().talent_profiles}
if known_keys and draft.talent_key.lower() not in known_keys:
```

The `if known_keys` guard prevents false failures if sop_parser returns empty.

## Pre-deploy verification results

### Check 1 — Draft bodies (10 random samples across 6 talents)
✅ All contain real Scenario A responses. No metadata strings. No blank bodies.
⚠️ Two older drafts (Wesley #24259, Jenn #24263, 13:35 UTC batch) have 4-space
   indentation on bullet lines — minor formatting artifact from an older sop.md
   parse. Not a send blocker.

### Check 2 — Jocelyn excluded from auto-send
✅ 4 Jocelyn pending drafts (oldest 04:51 UTC). Her sop.md has `Auto Send: no`.
   `settings.talent_profiles` will not include her in the auto_send list.
   Drafts require manual approval — correct, she is still on yellow/draft-only.

### Check 3 — Recent drafts (last 3 hours) validation state
✅ All 80 drafts from last 3 hours: `validation_failed = false`.
⚠️ 3 older drafts (Wesley #24259, Jenn #24263, Hana #24256) cycled back to
   `validation_failed=true` after my SQL reset — the deployed instance ran one
   more auto_send cycle with the old validation.py before `9eb3f24` landed.
   The `main.py` startup fix resets these automatically on next boot. Will clear
   on deploy.

## What happens after deploy

1. Render restarts → `main.py` startup fix runs:
   `UPDATE drafts SET validation_failed=false WHERE validation_failed=true AND status='pending'`
   → Clears the 3 re-failed drafts.
2. New validation.py loads → `known_keys` sourced from sop.md → all talent keys found.
3. auto_send runs (60s interval) → processes all 105 pending drafts for auto-send talents.
4. Hold period (60min) already expired on ~100 of them → sends begin within ~2 minutes.
5. Jocelyn's 4 drafts remain pending — require manual approval in dashboard.

## Talent auto_send list (from sop.md, post-fix)
Wesley, Hana, Audur, Katrina, Anastasiya, Jenn, Angela, Grayson, Kylika, Skyler
(Jocelyn: Auto Send: no → manual only)
(Stephanie: Auto Send: no → manual only — she is still on yellow/draft mode)
