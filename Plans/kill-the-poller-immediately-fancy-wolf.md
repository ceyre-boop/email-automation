# Plan: Revert Aggressive Gating, Use Gmail Thread Message Count

## Context
The previous Draft-button gating (`score === 2 || triage_reason matches "ongoing thread|prior sent activity"`) hid the Draft button on brand-new inbound emails that need it. The triage_reason string is too noisy a signal — it gets attached to lots of legitimate first-touch emails.

**The correct signal is the actual Gmail thread message count.** If Gmail reports the thread has more than one message, there's been prior activity (either an inbound reply or a sent reply from Marco). Otherwise it's a fresh inbound — Draft must be available.

This signal is not currently in the database. We will surface it at `/email-feed` render time.

## Changes

### 1. `backend/static/dashboard.html` — revert + new gate

Remove the score-2 / triage_reason gating block. Replace with a single check:

```js
const archiveOnly = (r.thread_message_count || 1) > 1;
```

`thread_message_count` will come from the backend. Default of 1 ("treat as new") covers any row where the count couldn't be resolved (Gmail error, deleted thread) — bias toward showing Draft so we never hide it on a legitimate inbound.

### 2. `backend/routers/analytics.py::/email-feed` — fetch counts at render time

After building the merged result list, before serialising:

1. Collect unique `(talent_key, thread_id)` pairs from rows (skip rows with no thread_id).
2. Group by talent_key. For each talent, build the Gmail service once (using `gmail_svc.build_service(token, db)` — same pattern as the LOST label lookup already does in this file).
3. Use a `ThreadPoolExecutor(max_workers=8)` to fan out `threads().get(userId="me", id=thread_id, format="minimal").execute()` calls in parallel. The minimal format returns only `messages: [{id, threadId, labelIds}]` — exactly what we need to count.
4. Build a `{(talent_key, thread_id): count}` map. On any per-thread exception, default to 1.
5. Pass into `_serialize()` so each row gets `"thread_message_count": int`.

Set a per-call timeout (e.g. `socket.setdefaulttimeout` is not safe here; use a future-level `future.result(timeout=4)` and treat timeouts as count=1).

Why this is acceptable cost: `/email-feed` is **on-demand** — fires when Marco opens the Inbox Feed tab, not on a polling timer. Typical load: ~50 distinct threads × 8-way parallelism ≈ <1s.

### What I considered and rejected
- **Add a column + populate at sync time**: would need a migration plus backfill plus a synchronous-fallback for existing rows. More moving parts than the user needs right now. Easy to add later if the at-render fetch proves slow.
- **DB-only proxy** (count of ProcessedEmail rows sharing thread_id, or presence of a `sent` Draft): misses the critical case Marco cares about — a reply he sent manually in Gmail that never touched our DB.

## Critical files
- `backend/static/dashboard.html` — `loadEmailFeed()` row render
- `backend/routers/analytics.py` — `/email-feed` endpoint

## Existing utilities reused
- `gmail_svc.build_service(token, db)` — already used in `/email-feed` for the LOST label lookup; pattern is established
- `gmail_svc.thread_has_prior_sent_reply()` already calls `threads().get(format="minimal")` — confirms the minimal format works and is cheap

## Verification
1. Open Inbox Feed → all SPAM / MISSED / LOST rows that previously got Draft hidden should now show **Draft + Archive** again.
2. Any row whose Gmail thread has 2+ messages (e.g. a reply chain where Marco already responded once in Gmail) shows **Archive only** — no Draft button.
3. Click Draft on a fresh single-message thread → existing force-draft / regenerate flow works.
4. Confirm in Render logs: no flood of errors from the per-thread fetch; total feed load time stays sub-second under normal volume.

## Commit
`"feed: gate Draft on Gmail thread message count; revert score/reason heuristics"`
