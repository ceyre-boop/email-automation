# Plan: Address All 6 Outstanding Build Items

## Context

Six items from the decision tree + dashboard review need to be implemented or fixed:
1. Reply gate correctness — verify the decision tree runs in the right order
2. Reply accuracy check — confirm drafts match SOP verbatim
3. Send All per talent card — button exists in HTML but may not render correctly
4. Rule 10 enforcement (A or B or C, never AND) — prompt fix
5. Lost drafts bulk clear — 50 phantom drafts need a purge endpoint
6. Guardian dedup/cooldown — Marco's dashboard shows repeated warn entries every ~60s

---

## Item 1 — Reply Gate: Verify Decision Tree Order

**Current state:** `poller.py` lines 425–460 check thread activity before calling triage. This is correct.

**What to verify + fix:** The Gmail fetch in `gmail.py` may include non-INBOX emails (e.g., already-labeled or archived messages). The first gate — "Is email in INBOX?" — should be enforced at the fetch level (only fetch `label:INBOX is:unread`), not just at processing. Confirm `gmail.py::list_unread_messages()` query string is `"is:unread label:INBOX"` exactly (not just `"is:unread"`). If it's missing `label:INBOX`, add it.

**Files:** `backend/services/gmail.py` — `list_unread_messages()` query string

---

## Item 2 — Reply Accuracy Check

**What's needed:** After `reply.py` generates a draft, add a post-generation check that verifies the draft contains the required SOP markers — specifically: that it includes the talent's name in the opener, a rate/deliverable acknowledgment, and a closing CTA. Log a `human_override_occurred=True` flag on the ProcessedEmail and downgrade to Score 2 (flag for review) if the check fails.

**Implementation:**
- Add `_validate_draft_against_sop(draft_text: str, talent_key: str, triage_result: dict) -> bool` to `reply.py`
- Call it after draft generation in `poller.py::_process_message_in_thread()` before saving the Draft row
- On failure: set `status=EmailStatus.flagged`, `human_override_occurred=True`, log reason to Marco

**Files:** `backend/services/reply.py`, `backend/services/poller.py` (around line 500+)

---

## Item 3 — Send All Per Talent Card

**Current state:** Backend endpoint `POST /api/dashboard/talents/{talent_key}/send-all` exists at `dashboard.py:506`. The HTML button at line 1374 renders it. It shows `card.pending_real_drafts ?? card.pending_drafts`.

**Likely issue:** The `/api/dashboard/talent-health` response may not include `pending_real_drafts` for all talents — if it's `null`, the button shows `undefined` or `0` and may be hidden by a conditional. Audit the talent health API response and ensure `pending_drafts` (count of `Draft.status = pending`) is always returned as an integer (not null) for every talent card.

**Files:** `backend/routers/dashboard.py` — talent health endpoint response builder

---

## Item 4 — Rule 10: A or B or C, Never AND

**What's needed:** The `prompts/reply.md` system prompt needs an explicit enforcement block. After the action selection section, add:

```
## RULE 10 — ONE PATH ONLY
Select exactly ONE of A, B, or C based on the scoring above.
Execute that path completely. Then STOP.
Do NOT combine paths. Do NOT execute A and then B.
Do NOT add commentary outside the selected path's output.
If you cannot determine which path applies, default to B (flag for human review).
```

This should appear immediately before the `## USER PROMPT TEMPLATE` separator so it's part of the system prompt and applies to every completion.

**Files:** `prompts/reply.md`

---

## Item 5 — Lost Drafts Bulk Clear

**What's needed:** A one-shot endpoint to purge Draft rows whose `gmail_draft_id` no longer exists in Gmail (phantom/stale drafts). Also a simpler hard-reset option for drafts older than N days with `status=pending`.

**Implementation:**
- Add `POST /api/admin/drafts/purge-stale` to `routers/dashboard.py` (API key protected)
- Query all `Draft` rows with `status=pending`, check each `gmail_draft_id` against Gmail API, delete rows where Gmail returns 404
- Add optional `?days=N` param to hard-delete pending drafts older than N days without Gmail validation (faster, for known-stale cases)
- Wire a "Clear stale drafts" button to the dashboard Admin section

**Files:** `backend/routers/dashboard.py`, `backend/static/dashboard.html`

---

## Item 6 — Guardian Dedup/Cooldown Fix

**Root cause diagnosis:** `_set_state` does commit (confirmed). The loop in Marco's dashboard between 12:01–12:09 could be caused by:

1. `_check_per_talent_caps` (lines 138–159) generates a `talent_pause` trigger every 60s when daily cap is hit. The `talent_pause` cooldown (lines 289–301) uses key `guardian_pause_sent_at_{talent_key}`. BUT the `already_paused` check (lines 280–285) reads from `settings.json`. If the settings write fails (`_pause_talent` throws), the talent is never marked paused, so the already-paused guard never fires, and `_log_marco` is called every 60s indefinitely.

2. The warn-level `_log_marco` at line 322 writes to MarcoLog unconditionally once per cooldown window — but if Marco's Activity Hub fetches all MarcoLog entries (not just guardian ones), it may be showing per-email processing logs alongside guardian logs, making it APPEAR like the guardian is looping.

**Fix:**
- In `_dispatch` for `talent_pause`: wrap `_pause_talent()` in try/except and still set the pause cooldown key even if the settings write fails — this prevents the 60s re-fire loop
- Add dedup on `_log_marco` itself: before inserting a MarcoLog row, check if an identical `message` + `talent_key` row exists within the last `warn_cooldown` minutes; skip if so
- Add `guardian_warn_at_{talent_key}_{trigger_type}` key format to distinguish `talent_warn` from `ratio_warn` dedup (currently both use the same key — first write wins, second is suppressed even if different trigger type)

**Files:** `backend/services/guardian.py` — `_dispatch()` method (lines 258–323), `_log_marco()` function (lines 446–460)

---

## Verification

1. **Reply gate:** Tail poller logs during a poll cycle, confirm no non-INBOX emails are fetched
2. **Reply accuracy:** Manually trigger a Score-3 email for a test talent; confirm valid drafts pass, and a deliberately malformed draft (remove talent name) gets flagged
3. **Send All:** Load dashboard, confirm each talent card shows "Send All (N)" with correct count
4. **Rule 10:** Run a test reply generation with two applicable paths; confirm output contains only one path's content
5. **Stale drafts:** Call `POST /api/admin/drafts/purge-stale?days=7` on staging; confirm count drops from 50 to 0
6. **Guardian:** Let guardian run 3 cycles after fix; confirm Marco's Activity Hub shows at most 1 warn entry per talent per 30-min window
