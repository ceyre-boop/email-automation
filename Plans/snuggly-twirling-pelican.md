# Dashboard Rework — Activity Hub + Inbox Feed Redesign

## Context

Marco's feedback: the dashboard UX conflates two different workflows in one view, and
action buttons are confusing. The redesign separates them cleanly:

- **Activity Hub** — only AI-drafted emails awaiting manager approval (send or kick back)
- **Inbox Feed** — only emails needing human triage (score=1 Spam + score=2 Review), with A/B/C
  action buttons replacing the old Create/Send/X pattern

Also fixes: loading overlay on tab switch, silent auto-refresh, and the API key prompt
that fires every new session.

---

## Implementation Order

### 1. `backend/services/gmail.py` — Two new Gmail label functions

**Add after `mark_initial_response_sent` (~line 524):**

```python
def move_to_inbox(token_row, message_id: str, db=None, service=None) -> bool:
    """Removes 'A Initial Response' label, adds INBOX. Inverse of mark_initial_response_sent."""
    if service is None:
        service = _gmail_service(token_row, db)
    label_id = _get_or_create_custom_label(
        service, "A Initial Response", background_color="#16a765", text_color="#ffffff"
    )
    body: dict = {"addLabelIds": ["INBOX"]}
    if label_id:
        body["removeLabelIds"] = [label_id]
    try:
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return True
    except HttpError as exc:
        logger.error("move_to_inbox failed %s/%s: %s", token_row.talent_key, message_id, exc)
        return False

def restore_inbox_label(token_row, message_id: str, db=None, service=None) -> bool:
    """Adds INBOX, removes 'A Initial Response' and 'Spam' labels if present. B-button action."""
    if service is None:
        service = _gmail_service(token_row, db)
    remove_ids = []
    for name, bg, fg in [("A Initial Response", "#16a765", "#ffffff"), ("Spam", "#e8eaed", "#202124")]:
        lid = _get_or_create_label(service, name, bg, fg)
        if lid:
            remove_ids.append(lid)
    body: dict = {"addLabelIds": ["INBOX"]}
    if remove_ids:
        body["removeLabelIds"] = remove_ids
    try:
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return True
    except HttpError as exc:
        logger.error("restore_inbox_label failed %s/%s: %s", token_row.talent_key, message_id, exc)
        return False
```

Note: `_get_or_create_label` (line 697) and `_get_or_create_custom_label` (line 474) both guard
against non-whitelisted labels. "A Initial Response" and "Spam" are in `_ALLOWED_LABELS`. INBOX is
a system label added directly via its system ID — no whitelist check needed.

---

### 2. `backend/routers/drafts.py` — `POST /{draft_id}/move-to-inbox`

**Add after `discard_draft` (~line 440). All imports already present.**

```python
@router.post("/{draft_id}/move-to-inbox")
def move_draft_to_inbox(draft_id: int, body: DiscardBody = DiscardBody(), db: Session = Depends(get_db)):
    """Activity Hub ↩ Inbox button. Discards draft, restores INBOX label, downgrades score to 2."""
    from backend.models.db import EmailStatus
    draft = _get_draft_or_404(db, draft_id)
    if draft.status not in (DraftStatus.pending,):
        raise HTTPException(400, detail=f"Cannot move draft with status '{draft.status}' to inbox.")
    token = _get_token_or_404(db, draft.talent_key)
    if draft.gmail_draft_id:
        gmail_svc.delete_gmail_draft(token, draft.gmail_draft_id, db=db)
    if draft.gmail_message_id:
        gmail_svc.move_to_inbox(token, draft.gmail_message_id, db=db)
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == draft.gmail_message_id
    ).first()
    if pe:
        pe.score = 2
        pe.status = EmailStatus.pending
        pe.processed_at = datetime.utcnow()  # refresh timestamp so it appears in 24h feed window
        db.add(pe)
    draft.status = DraftStatus.discarded
    draft.reviewed_at = datetime.utcnow()
    draft.reviewed_by = body.reviewed_by
    db.add(draft)
    db.commit()
    return {"ok": True}
```

Check that `delete_gmail_draft` exists in `gmail_svc`. If not, skip that call (Gmail draft stays
as an orphan in Gmail Drafts folder — acceptable).

---

### 3. `backend/routers/dashboard.py` — Two new Inbox Feed action endpoints

**Add near the existing `archive_email` endpoint:**

```python
@router.post("/talents/{talent_key}/emails/{gmail_message_id}/keep-in-inbox")
def keep_in_inbox(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Inbox Feed B button. Adds INBOX, removes custom labels. ProcessedEmail stays at score=2."""
    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key), TalentToken.active == True
    ).first()
    if not token:
        raise HTTPException(404, "Talent not connected.")
    gmail_svc.restore_inbox_label(token, gmail_message_id, db=db)
    return {"ok": True}


@router.post("/talents/{talent_key}/emails/{gmail_message_id}/spam")
def mark_as_spam(talent_key: str, gmail_message_id: str, db: Session = Depends(get_db)):
    """Inbox Feed C button. Archives as spam, updates ProcessedEmail to score=1."""
    from backend.models.db import EmailStatus, InboxEmail
    token = db.query(TalentToken).filter(
        TalentToken.talent_key.ilike(talent_key), TalentToken.active == True
    ).first()
    if not token:
        raise HTTPException(404, "Talent not connected.")
    gmail_svc.archive_as_spam(token, gmail_message_id, db=db)
    pe = db.query(ProcessedEmail).filter(
        ProcessedEmail.gmail_message_id == gmail_message_id
    ).first()
    if pe:
        pe.score = 1
        pe.status = EmailStatus.archived
        db.add(pe)
    db.query(InboxEmail).filter(
        InboxEmail.gmail_message_id == gmail_message_id,
        InboxEmail.talent_key == talent_key.lower(),
    ).delete()
    db.commit()
    return {"ok": True}
```

Action A (force-draft) already exists: `POST /api/dashboard/talents/{key}/force-draft/{id}`.
No backend change needed.

---

### 4. `backend/routers/auth.py` — Auto-login session-key endpoint

**Add at the bottom of the file:**

```python
@router.get("/session-key")
def get_session_key():
    """Returns dashboard API key with no auth. Dashboard URL is internal-only."""
    return {"api_key": get_settings().api_key}
```

Route: `GET /auth/session-key` (auth router has prefix `/auth`, mounted without `/api` prefix
at `main.py:55`). Frontend calls `fetch('/auth/session-key')` — no x-api-key header.

---

### 5. `backend/routers/analytics.py` — Filter score=3 from Inbox Feed

**Line 465 — one-line change:**

```python
# Before:
.filter(ProcessedEmail.processed_at >= cutoff, ProcessedEmail.score > 0)

# After:
.filter(ProcessedEmail.processed_at >= cutoff, ProcessedEmail.score > 0, ProcessedEmail.score != 3)
```

Score=3 emails (drafts) now live exclusively in Activity Hub.

---

### 6. `backend/static/dashboard.html` — All frontend changes

#### 6a. Auto-load API key (remove manual prompt)

In `bootstrap()`, before the `if (!API_KEY) { showApiKeyOverlay(); return; }` guard, add:

```javascript
if (!API_KEY) {
  try {
    const r = await fetch('/auth/session-key');
    if (r.ok) { const d = await r.json(); if (d.api_key) { API_KEY = d.api_key; localStorage.setItem('inbox_api_key', d.api_key); } }
  } catch (_) {}
}
```

In `api()` 403 handler (line ~1041), before calling `showApiKeyOverlay()`, attempt key
auto-reload and retry once:

```javascript
if (res.status === 403) {
  localStorage.removeItem('inbox_api_key'); API_KEY = '';
  try {
    const r = await fetch('/auth/session-key');
    if (r.ok) { const d = await r.json(); if (d.api_key) { API_KEY = d.api_key; localStorage.setItem('inbox_api_key', d.api_key); return api(method, path, body); } }
  } catch (_) {}
  showApiKeyOverlay(); throw new Error('Invalid API key');
}
```

#### 6b. Silent overlay — `loadOverview` and `loadEmailFeed`

Add `{ silent = false } = {}` param to both functions. Gate `showOverlay`/`hideOverlay` on `!silent`.

In `refreshData()` (called by `setInterval` at line ~1186):
```javascript
await loadOverview({ silent: true });
```

In `showView()`, for the feed branch:
```javascript
if (name === 'feed') loadEmailFeed({ silent: true }).catch(() => {});
```

For the `nav-overview` click handler, call `showView('overview')` synchronously first (to hide the
feed view immediately), then `loadOverview()` with overlay for the data load:
```javascript
document.getElementById('nav-overview').addEventListener('click', async () => {
  showView('overview');          // instant — hides feed, shows empty overview shell
  await loadOverview();          // then fetch data (with overlay)
});
```

#### 6c. Activity Hub — Replace ✕ with ↩ Inbox button

In `loadDraftQueue()` row template, replace the discard button:

```javascript
// Remove:
<button onclick="event.stopPropagation(); discardDraft(${d.id}, this)">✕</button>

// Add:
<button onclick="event.stopPropagation(); moveToInbox(${d.id}, this)"
  style="background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.3);color:#60a5fa;
         border-radius:7px;padding:5px 10px;font-size:11px;font-weight:700;cursor:pointer;">
  ↩ Inbox
</button>
```

Add `moveToInbox` JS function:

```javascript
async function moveToInbox(id, btn) {
  if (!confirm('Return to Inbox Feed? Draft will be discarded.')) return;
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/drafts/${id}/move-to-inbox`, { reviewed_by: 'dashboard' });
    showToast('Email returned to Inbox Feed.');
    await loadDraftQueue(); renderSidebar();
  } catch(e) { showToast('Move failed: ' + e.message); btn.innerHTML = orig; btn.disabled = false; }
}
```

#### 6d. Inbox Feed — A/B/C buttons

In `loadEmailFeed()` row template, replace the Send/Create/× button block with:

```javascript
<div style="display:flex;gap:4px;align-items:center;flex-shrink:0;">
  <button onclick="feedActionA('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(0,214,143,0.15);border:1px solid rgba(0,214,143,0.3);color:#00d68f;
           border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Force-draft → Activity Hub">A</button>
  <button onclick="feedActionB('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(251,191,36,0.15);border:1px solid rgba(251,191,36,0.3);color:#fbbf24;
           border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Keep in inbox for human review">B</button>
  <button onclick="feedActionC('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);color:#f87171;
           border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Archive as spam">C</button>
</div>
```

Add three JS action functions:

```javascript
async function feedActionA(talentKey, gmailMessageId, btn) {
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/force-draft/${gmailMessageId}`);
    showToast('Draft queued — will appear in Activity Hub shortly.');
    document.getElementById(`feed-row-${gmailMessageId}`)?.remove(); checkFeedEmpty();
  } catch(e) { showToast('Failed: ' + e.message); btn.innerHTML = orig; btn.disabled = false; }
}

async function feedActionB(talentKey, gmailMessageId, btn) {
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/emails/${gmailMessageId}/keep-in-inbox`);
    showToast('Kept in inbox for human review.');
  } catch(e) { showToast('Failed: ' + e.message); }
  btn.innerHTML = orig; btn.disabled = false;
}

async function feedActionC(talentKey, gmailMessageId, btn) {
  if (!confirm('Archive as spam?')) return;
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/emails/${gmailMessageId}/spam`);
    showToast('Archived as spam.');
    document.getElementById(`feed-row-${gmailMessageId}`)?.remove(); checkFeedEmpty();
  } catch(e) { showToast('Failed: ' + e.message); btn.innerHTML = orig; btn.disabled = false; }
}
```

Remove `feedSendDraft` and `feedCreateDraft` — replaced by A/B/C (confirm no other callers first
via grep before deleting).

---

## Files Modified

| File | Change |
|------|--------|
| `backend/services/gmail.py` | Add `move_to_inbox()` + `restore_inbox_label()` |
| `backend/routers/drafts.py` | Add `POST /{draft_id}/move-to-inbox` |
| `backend/routers/dashboard.py` | Add `keep-in-inbox` + `spam` endpoints |
| `backend/routers/auth.py` | Add `GET /auth/session-key` |
| `backend/routers/analytics.py` | Filter `score != 3` in `email_feed` |
| `backend/static/dashboard.html` | API key auto-load, silent refresh, ↩ Inbox button, A/B/C buttons |

---

## Verification

1. **API key auto-load** — Open dashboard in a fresh browser (no localStorage). Should load without
   the "Authorize Access" overlay.
2. **Tab switch** — Click Activity Hub → Inbox Feed → Activity Hub. Should be instant, no 3-second
   overlay on tab switch. Overlay only appears on the very first load.
3. **Auto-refresh** — Wait 60s on the Activity Hub. Data updates silently, no full-screen spinner.
4. **Activity Hub ↩ Inbox** — Click ↩ Inbox on a draft. Confirm row disappears from Activity Hub.
   Switch to Inbox Feed — email should appear as score=2 (Review).
5. **Inbox Feed A button** — Click A on a score=2 email. Confirm row leaves Inbox Feed.
   Check Activity Hub — draft should appear within 1-2 poll cycles.
6. **Inbox Feed B button** — Click B. Row stays. Toast confirms. No label error.
7. **Inbox Feed C button** — Click C. Row disappears. Email archived in Gmail.
8. **No score=3 in Inbox Feed** — Score=3 emails only appear in Activity Hub draft queue.
