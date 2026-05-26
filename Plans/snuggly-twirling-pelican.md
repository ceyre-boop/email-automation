# Three Fixes: Remove Colin's Inbox, Clean Ghost Records, Inbox Feed as Separate Tab

## Context

Post-launch cleanup:
1. `colineyre222` was connected as a test inbox on day one — it's not a talent, it's polluting Marco's health alerts with a 92% spam rate and fake escalations
2. Ghost ProcessedEmail rows (score=0, no subject/sender) from crashed poll cycles are showing as "no subject" rows in Anastasiya's feed and elsewhere
3. The Inbox Feed panel inside Activity Hub creates double-scroll UX — Marco called it; move it to its own sidebar nav tab

---

## Fix 1 — Remove Colin's Inbox

`colineyre222` is NOT in `config/settings.json` talents array — it only appears in `manager_emails`, `never_reply.emails`, and `error_alert_email` (all correct, leave those). The TalentToken row in the DB is the problem.

**Add endpoint** `POST /api/auth/disconnect/{talent_key}` in `backend/routers/auth.py`:
```python
@router.post("/disconnect/{talent_key}")
def disconnect_talent(talent_key: str, db: Session = Depends(get_db)):
    token = db.query(TalentToken).filter(TalentToken.talent_key.ilike(talent_key)).first()
    if not token:
        raise HTTPException(status_code=404, detail="Talent not connected.")
    db.query(ProcessedEmail).filter(ProcessedEmail.talent_key.ilike(talent_key)).delete()
    db.query(Draft).filter(Draft.talent_key.ilike(talent_key)).delete()
    db.query(InboxEmail).filter(InboxEmail.talent_key.ilike(talent_key)).delete()
    db.query(PollHealth).filter(PollHealth.talent_key.ilike(talent_key)).delete()
    db.delete(token)
    db.commit()
    return {"ok": True, "removed": talent_key}
```

Imports needed: `TalentToken`, `ProcessedEmail`, `Draft`, `InboxEmail`, `PollHealth` — all already imported in dashboard.py; add same imports to auth.py.

**After deploy:** call `POST /api/auth/disconnect/colineyre222` once. This endpoint is also useful for future talent offboarding.

---

## Fix 2 — Clean Ghost Records (score=0)

**Root cause:** `_process_one_message()` in `backend/services/poller.py` inserts a claim row with `score=0, sender="", subject=""` at the start of processing. If the process crashes between the INSERT and the UPDATE, the row stays at score=0 forever.

**Two-part fix:**

**Part A — Startup cleanup** in `backend/models/db.py::create_tables()`: after the table creation block, add a one-time cleanup that deletes stale score=0 rows:
```python
# Delete ghost claim rows stuck at score=0 (crashed mid-process)
with engine.connect() as conn:
    conn.execute(text(
        "DELETE FROM processed_emails WHERE score = 0 AND processed_at < NOW() - INTERVAL '10 minutes'"
    ))
    conn.commit()
```

**Part B — Poll-start guard** in `backend/services/poller.py`, at the top of the per-talent poll loop (where active tokens are iterated), add before processing each talent:
```python
db.query(ProcessedEmail).filter(
    ProcessedEmail.talent_key == talent_key,
    ProcessedEmail.score == 0,
    ProcessedEmail.processed_at < datetime.utcnow() - timedelta(minutes=10),
).delete()
db.commit()
```

This ensures ghost claims are always cleaned up before new polls run, preventing infinite accumulation.

---

## Fix 3 — Inbox Feed as Separate Sidebar Tab

Currently the only sidebar nav button is `Activity Hub` (`#nav-overview` / `#view-overview`). The Inbox Feed is embedded at the bottom of that view.

**Pattern:** Mirror the existing `showView()` system exactly.

### Sidebar nav button (add after Activity Hub button, `backend/static/dashboard.html` ~line 711):
```html
<button class="nav-btn" id="nav-feed" onclick="showView('feed')">
  <span class="nb-icon">📬</span> Inbox Feed
</button>
```

### New view section (add after `#view-overview` closing tag):
```html
<div id="view-feed" style="display:none; padding:20px;">
  <!-- header with Refresh + Re-triage buttons -->
  <!-- #email-feed-list and #email-feed-empty containers -->
</div>
```

Move the entire Inbox Feed `<div>` block (currently lines ~903–917 inside `#view-overview`) into `#view-feed`. Remove it from `#view-overview`.

### Update `showView()`:
```javascript
function showView(name) {
  state.view = name;
  document.getElementById('view-overview').style.display = name === 'overview' ? '' : 'none';
  document.getElementById('view-feed').style.display = name === 'feed' ? '' : 'none';
  document.getElementById('nav-overview').classList.toggle('active', name === 'overview');
  document.getElementById('nav-feed').classList.toggle('active', name === 'feed');
  if (name === 'feed') loadEmailFeed().catch(() => {});
  autoTheme(name);
}
```

Remove `loadEmailFeed()` from `bootstrap()` since it now only loads on tab open.

The feed view gets the full height of the main panel — no `max-height:340px` constraint. Remove that inline style from `#email-feed-list`.

---

## Files Modified

| File | Change |
|------|--------|
| `backend/routers/auth.py` | Add `POST /disconnect/{talent_key}` endpoint |
| `backend/models/db.py` | Add ghost-record cleanup to `create_tables()` startup |
| `backend/services/poller.py` | Add per-talent ghost cleanup at poll-start |
| `backend/static/dashboard.html` | Add Inbox Feed nav + view; remove from Activity Hub; update `showView()`; remove max-height cap |

---

## Verification

1. Deploy → call `POST /api/auth/disconnect/colineyre222` → returns `{"ok": true}` → colineyre222 gone from sidebar talent list and Marco health alerts
2. Restart the server (triggers `create_tables()` cleanup) → score=0 rows deleted; check DB
3. Open dashboard → sidebar shows two nav buttons: Activity Hub + Inbox Feed
4. Click Inbox Feed tab → feed loads, full-height list, no double scroll
5. Activity Hub no longer has Inbox Feed panel at the bottom
