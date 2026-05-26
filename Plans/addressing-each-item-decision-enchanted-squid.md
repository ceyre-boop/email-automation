# Plan: Addressing Each Item — Guardian Loop + SOP Decision Tree

## Context

Two issues surfaced from Marco's dashboard screenshots and the post-reset review:

1. **Guardian alert loop** — The guardian fires `talent_pause` for Audur and Kylika every 60 seconds without stopping, flooding Marco's dashboard with repeated GUARDIAN WARNING messages. Root cause: the `_dispatch` method's `talent_pause` branch has no idempotency check and calls `_log_marco` unconditionally on every cycle, even after the talent is already paused in `settings.json`.

2. **Triage prompt out of sync with updated SOP Rule 10** — The application-level check order (INBOX → thread guard → triage → scoring) is already correct in `poller.py`. However, `prompts/triage.md`'s eligibility gate language predates the new Rule 10 (Option A/B/C, mutually exclusive, prefer responding). The prompt needs to match the updated SOP so GPT's text-level reply detection aligns with the new framing.

The "Send All" button already exists in the dashboard (lines 1369–1382 of `dashboard.html`, endpoint `/api/dashboard/talents/{key}/send-all`). Lost drafts bulk-clear also exists (`/api/drafts/orphaned/trash-all` and `/api/drafts/discard-all`). These do not need code changes — they need a cache bust + redeploy to take effect.

---

## Fix 1 — Guardian Idempotency (primary, critical)

**File:** `backend/services/guardian.py` — `_dispatch()` method, lines 272–277

**Problem:** Every 60s, if draft velocity for Audur/Kylika still exceeds the threshold, the guardian calls `_pause_talent()` (no-op since already paused), `_log_marco()` (NOT suppressed — no cooldown), and `_send_guardian_alert()` (suppressed by cooldown, but `_log_marco` isn't). Result: Marco sees a new warning entry every minute indefinitely.

**Fix:** Before executing the `talent_pause` branch, check if the talent is already paused in `settings.json`. If already paused, skip entirely — no re-pause, no marco log, no alert.

```python
elif t == "talent_pause":
    # Idempotency: if already paused, suppress all downstream actions
    data = json.loads(_CONFIG_PATH.read_text())
    already_paused = next(
        (t_cfg.get("paused") for t_cfg in data.get("talents", [])
         if t_cfg.get("key", "").lower() == (talent_key or "").lower()),
        False,
    )
    if already_paused:
        logger.info("Guardian: %s already paused — skipping re-dispatch", talent_key)
        return
    # ... existing pause logic follows unchanged
```

**Also add per-talent pause cooldown as defense-in-depth** (for the window between pause write and next guardian cycle):

```python
    pause_key = f"guardian_pause_sent_at_{talent_key or 'global'}"
    last_pause_str = _get_state(db, pause_key)
    if last_pause_str:
        try:
            last_pause = datetime.fromisoformat(last_pause_str)
            if (datetime.utcnow() - last_pause).total_seconds() < cfg.get("alert_cooldown_minutes", 30) * 60:
                logger.info("Guardian: pause cooldown active for %s — skipping", talent_key)
                return
        except ValueError:
            pass
    _set_state(db, pause_key, datetime.utcnow().isoformat())
    # ... then existing: _pause_talent, _log_audit, _log_marco, _send_guardian_alert
```

---

## Fix 2 — Triage Prompt Eligibility Gate

**File:** `prompts/triage.md`

**Problem:** The triage prompt's eligibility gate language is stale relative to the new SOP Rule 10. Specifically: the "prefer responding" doctrine (Option A is default/preferred) and the "mutually exclusive" framing are not in the prompt, so GPT's text-level reply detection can over-trigger on vague language and route legitimate initial emails to Human Admin Required.

**Fix:** Update the eligibility gate section of `prompts/triage.md` to:
- Lead with "prefer responding — only classify as non-initial if clearly a reply/thread"
- Match the new Rule 10 framing: Option A (draft) is the default outcome for any valid inbound opportunity
- Preserve the existing "Re:", quoted text, and follow-up signal checks

Read the current `prompts/triage.md` at execution time to make a targeted edit to the eligibility gate section only. Do not touch scoring definitions or other sections.

---

## Fix 3 — Cache Bust After SOP Update

**File:** No code change needed — just a POST to the existing endpoint.

After deploy, call:
```
POST /api/admin/clear-cache
x-api-key: <API_KEY>
```

This calls `clear_sop_cache()` and `clear_triage_cache()` in reply.py and triage.py, forcing both to reload from the updated files on the next email.

---

## Files to Modify

| File | Change |
|------|--------|
| `backend/services/guardian.py` | Add idempotency check + per-talent pause cooldown in `_dispatch()` |
| `prompts/triage.md` | Update eligibility gate section to match new SOP Rule 10 language |

---

## Verification

1. **Guardian fix** — trigger a test by manually setting a talent's draft count above the warn threshold, confirm guardian fires once, logs once to Marco, and then suppresses on subsequent 60s cycles.
2. **Triage prompt** — send a test email with "Re:" subject to a connected talent inbox, confirm it routes to Human Admin Required (not a draft). Then send a fresh collab email, confirm it gets a draft.
3. **Cache bust** — after deploy, hit `/api/admin/clear-cache` and confirm 200 OK. Run a test poll and verify the new SOP rates appear in the generated draft.
