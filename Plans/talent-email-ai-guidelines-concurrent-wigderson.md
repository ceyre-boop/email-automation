# Plan: SOP Rule 10 Compliance + Skyler Bug Fix

## Context

Marco tested the system with Skyler and reported: "it created the Draft, then deleted it and created these labels — no bueno. AI is acting on its own again."

The root cause is Rule 10 enforcement. The SOP was updated with a critical new version of Rule 10 that inverts Option B behavior. The old code violates this in multiple paths.

**Old Rule 10B**: Remove INBOX label, Apply "Revisit" label
**New Rule 10B**: Remove INBOX = NO. Apply Label = NONE. Leave email untouched in INBOX.

This means every score=2 path and every escalated score=3 path was actively relabeling emails and removing them from inbox — when the new SOP says to leave them completely alone.

**Marco's Skyler bug explained:**
1. Test email → score=3 → reply service called
2. Reply service returned `is_escalate=True` (GPT returned ESCALATE or Gmail draft creation failed)
3. Code then: created a DB Draft (phantom — no Gmail draft exists behind it) + applied "A Initial Response" label (Option A) + called `move_to_revisit()` which applied "Revisit" label AND removed email from INBOX (Option B)
4. Result: Option A AND Option B applied to same email — both violations of Rule 10, plus an unsendable phantom draft on the dashboard

**Additional user requests from meeting notes:**
- Reply check logic: ensure ongoing thread detection is accurate
- Add "Send All" button per individual talent inbox
- Update SOP file to new Rule 10

---

## Rule 10 Decision Table (new)

| Path | Gmail Action | DB Action |
|------|-------------|-----------|
| Score=3 + real Gmail draft created (Option A) | `mark_initial_response_sent()` → removes INBOX/UNREAD, adds "A Initial Response" | Create Draft row (pending) |
| Score=3 + escalated / draft failed (Option B) | **Nothing** — leave Gmail untouched | Record ProcessedEmail (flagged), NO Draft row |
| Score=2 — flag for review (Option B) | **Nothing** — leave Gmail untouched | Record ProcessedEmail (flagged) |
| Pre-triage thread check — ongoing (Option B) | **Nothing** — leave Gmail untouched | Record ProcessedEmail (flagged) |
| Score=3 Guard 2 — thread already replied (Option B) | **Nothing** — leave Gmail untouched | Record ProcessedEmail (flagged) |
| Score=1 — Spam (Option C) | `archive_message()` + `apply_triage_label(1)` → removes INBOX, adds "Spam" | Record ProcessedEmail (archived) |

---

## Changes

### 1. `backend/services/poller.py` — Core behavioral fixes

**A. Pre-triage thread check (lines ~448–460):**
Remove `gmail_svc.move_to_revisit(...)` entirely. The email is an ongoing thread — just record it and return. Gmail stays untouched.
```python
# Remove this line:
gmail_svc.move_to_revisit(token_row, message_id, db=db, service=service)
```

**B. Score=1 path (lines ~497–513):**
Revert the change from the last session. Score=1 is Option C (Spam), not Option B (Revisit).
Remove `gmail_svc.move_to_revisit(...)`.
Replace with `gmail_svc.archive_message(...)` to remove INBOX.
Keep `gmail_svc.apply_triage_label(token_row, message_id, 1, ...)` which applies the "Spam" label.
```python
# Remove: gmail_svc.move_to_revisit(...)
# Keep:   gmail_svc.apply_triage_label(..., 1, ...)
# Add:    gmail_svc.archive_message(...)
```

**C. Score=2 path (lines ~514–533):**
Remove ALL Gmail label calls from the `else` branch:
- Remove `gmail_svc.move_to_revisit(...)`
- Remove `gmail_svc.apply_triage_label(..., 2, ...)`
- Remove `gmail_svc.apply_manager_review_label(...)`
- Remove `gmail_svc.apply_extra_label(..., "rate_negotiation", ...)`

Keep the `ignore_leave_inbox` branch as-is (it already does nothing to Gmail — correct for event invites/personal email).
Keep the DB record and commit.

**D. Score=3, Guard 2 (thread already replied, lines ~546–562):**
Remove `gmail_svc.move_to_revisit(...)`. Leave Gmail untouched. Keep DB record.

**E. Score=3, escalated (is_escalate=True, lines ~631–682):**
The phantom draft problem. When `is_escalate=True`:
- Do NOT create a `Draft` DB row. Route to the same outcome as score=2 (flagged ProcessedEmail, no draft).
- Do NOT call `gmail_svc.apply_triage_label(..., 3, ...)`.
- Do NOT call `gmail_svc.move_to_revisit(...)`.
- DO still call `gmail_svc.mark_as_read(...)` — reading is fine.
- DO still call the `record_processed` helper with `EmailStatus.flagged`.

Implementation: split the score=3 post-draft-generation block into two branches:
```python
if is_escalate:
    # Option B — leave Gmail untouched, no draft
    _record_processed(..., EmailStatus.flagged, ...)
    db.commit()
    summary["flagged"] += 1
else:
    # Option A — create draft, apply A Initial Response label
    draft_row = Draft(...)
    db.add(draft_row)
    _record_processed(..., EmailStatus.draft_saved, ...)
    db.commit()
    gmail_svc.mark_initial_response_sent(token_row, message_id, ...)
    gmail_svc.mark_as_read(token_row, message_id, ...)
    # known_brand extra label still OK here
    summary["drafted"] += 1
```

**F. Score=3, successful draft (is_escalate=False):**
Replace `gmail_svc.apply_triage_label(token_row, message_id, 3, ...)` with `gmail_svc.mark_initial_response_sent(token_row, message_id, ...)`.

`mark_initial_response_sent()` already exists in `backend/services/gmail.py` (line ~523). It removes INBOX/UNREAD AND adds "A Initial Response" label in one call. This replaces the current `apply_triage_label(3)` which only adds the label without removing INBOX.

### 2. `prompts/triage.md` — Reinforce Rule 4 in AI prompt

Add an explicit rule above the scoring definitions:

```
**Rule 0 — Initial inbound only.**
This workflow processes INITIAL inbound emails only. If the email is clearly a reply, 
follow-up, continuation of a negotiation, or part of an ongoing conversation (look for 
"Re:" subject prefix, prior back-and-forth in the thread, or explicit references to 
previous communications), score it 2 (Human Review). Do not score these 3.
```

Also add to Score 2 definition: "Emails with a 'Re:' subject prefix or that reference prior communications should be treated as follow-ups (Score 2) unless the content is clearly a new unsolicited outreach."

### 3. `sheets/sop.md` — Update Rule 10 to new version

Replace the existing Rule 10 section (lines 151–189) with the new version the user provided. The new Rule 10 includes:
- Eligibility check preamble
- Explicit mutual exclusivity language ("exactly ONE of the following")
- Option A (Approved Response — Default, preferred)
- Option B (Ignore/Human Review — leave in INBOX untouched, no label)
- Option C (Spam — remove INBOX, apply Spam label)

Talent sections (lines 191 onward) are unchanged.

### 4. `backend/static/dashboard.html` — Add per-talent "Send All" button

In the Talent Health section, each talent card already has action buttons. Add a "Send All" button that calls a new per-talent endpoint. Place it alongside or below existing per-talent controls.

Add JS function:
```javascript
async function sendAllForTalent(talentKey, btn) {
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    const r = await api('POST', `/api/dashboard/talents/${talentKey}/send-all`);
    showToast(`Sent ${r.sent_count} draft(s) for ${talentKey}`);
    await loadReport();
  } catch(e) {
    showToast('Send all failed: ' + (e.message || e), 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send All';
  }
}
```

### 5. `backend/routers/dashboard.py` — Add per-talent send-all endpoint

Add endpoint `POST /api/dashboard/talents/{talent_key}/send-all` that:
1. Queries all `Draft` records for that talent with `status=pending, is_escalate=False, gmail_draft_id IS NOT NULL`
2. For each: calls `gmail_svc.send_gmail_draft(token_row, draft.gmail_draft_id, ...)`
3. Updates `Draft.status = DraftStatus.sent`, sets `Draft.reviewed_at`
4. Returns `{"sent_count": N, "failed_count": M}`

Reuse the existing send logic already in `dashboard.py` (the per-draft approve endpoint). The `send_gmail_draft` function already exists in `gmail.py`.

---

## Key Functions (reuse, don't reinvent)

| Function | File | Used for |
|----------|------|---------|
| `mark_initial_response_sent()` | `gmail.py:523` | Option A — removes INBOX, adds "A Initial Response" |
| `archive_message()` | `gmail.py:442` | Option C — removes INBOX/UNREAD |
| `apply_triage_label(token, id, 1)` | `gmail.py:738` | Option C — adds "Spam" label |
| `mark_as_read()` | `gmail.py:459` | All scored paths |
| `send_gmail_draft()` | `gmail.py` | Per-talent send-all |
| `_record_processed()` | `poller.py:695` | All paths |

---

## What is NOT changing

- Score=2 `ignore_leave_inbox=True` branch (event invites, personal email): already correct — does nothing to Gmail
- `thread_has_prior_sent_reply()` logic: keep as-is, just change what we DO with the result
- Triage AI scoring rules (1/2/3 scoring thresholds): unchanged
- Reply prompt: unchanged — already correct
- DB schema: no changes needed

---

## Verification

1. **Marco's bug**: Send a fresh test email to Skyler's inbox. Confirm: (a) draft appears on dashboard with `is_escalate=False`, (b) email in Gmail has "A Initial Response" label and is removed from INBOX, (c) NO Revisit label applied, (d) email does NOT appear in INBOX after draft created
2. **Option B**: Send a follow-up email to a thread where a draft was already sent. Confirm: (a) email stays in INBOX untouched, (b) NO labels added, (c) flagged in DB only
3. **Option C**: Find a clear spam test. Confirm: (a) email gets "Spam" label, (b) removed from INBOX, (c) NOT labeled "Revisit"
4. **Send All**: Click "Send All" for a talent with pending drafts. Confirm all pending drafts send and disappear from queue
5. **Phantom draft check**: Trigger an escalation (e.g., disconnect Gmail temporarily). Confirm no DB Draft row created, email left in INBOX untouched
