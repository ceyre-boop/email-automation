# Plan: Revert Inbox Feed buttons to last confirmed-working `[A][B][C]` state

## Context

Marco confirmed `[A]` worked from the Inbox Feed on a real email. That was the state where every
row showed three buttons — `[A]` force-draft, `[B]` keep-in-inbox, `[C]` archive — with **no gating
logic** hiding any of them.

After that confirmation, four commits layered changes onto the button logic, and the feed has been
unreliable since:

| Commit | What it added to button logic |
|--------|-------------------------------|
| `19cca1d` | LOST badge **+** rerouted `[A]` to a regenerate endpoint when `is_lost` |
| `0862055` | Renamed `[A][B][C]` → `Draft` / `Archive`, dropped `[B]` keep-in-inbox |
| `b1e53af` | Gated Draft off when `score===2` or `triage_reason` matched "ongoing thread / prior sent activity" |
| `836f8d5` (HEAD) | Replaced that with gating off when `thread_message_count > 1` |

The user wants the button logic restored to the pre-`19cca1d` state — plain `[A][B][C]`, no
conditions, `[A]` works for every row — while **keeping the LOST badge display only** (the red `LOST`
badge that `feedBadge` renders). All button gating goes.

This is a single-file change to `backend/static/dashboard.html`. Nothing else is touched.

## Verified facts

- All backend endpoints the restored buttons call still exist:
  - `[A]` → `POST /api/dashboard/talents/{key}/force-draft/{id}` (`dashboard.py:1366`)
  - `[B]` → `POST /api/dashboard/talents/{key}/emails/{id}/keep-in-inbox` (`dashboard.py:1556`)
  - `[C]` → `POST /api/dashboard/talents/{key}/emails/{id}/spam` (`dashboard.py:1573`)
- The LOST badge display lives in `feedBadge(r)` (`dashboard.html:2498-2508`) via the
  `if (r.is_lost) return {color:'#ef4444', label:'LOST'}` line — this stays untouched.

## Changes — all in `backend/static/dashboard.html`

### 1. Render block inside `loadEmailFeed` (currently lines ~2529-2547)

Remove the three lines added after Marco's confirmation:
- `const aTitle = r.is_lost ? ... ` (line 2529)
- `// Hide Draft button only when ...` comment + `const archiveOnly = (r.thread_message_count || 1) > 1;` (lines 2530-2531)
- `const draftBtn = archiveOnly ? '' : ...` (lines 2532-2534)

Leave `const { color, label } = feedBadge(r);` exactly as-is (keeps LOST badge display).

Replace the button `<div>` (currently `${draftBtn}` + the `Archive` button) with the original
three-button block:

```html
<div style="display:flex;gap:4px;align-items:center;flex-shrink:0;">
  <button onclick="feedActionA('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(0,214,143,0.15);border:1px solid rgba(0,214,143,0.3);color:#00d68f;border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Force-draft → Activity Hub">A</button>
  <button onclick="feedActionB('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(251,191,36,0.15);border:1px solid rgba(251,191,36,0.3);color:#fbbf24;border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Keep in inbox for human review">B</button>
  <button onclick="feedActionC('${r.talent_key}','${r.gmail_message_id}',this)"
    style="background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);color:#f87171;border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;"
    title="Archive as spam">C</button>
</div>
```

### 2. Action functions (currently lines ~2585-2607)

Replace `feedDraft` and `feedArchive` with the original three handlers:

```js
async function feedActionA(talentKey, gmailMessageId, btn) {
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/force-draft/${gmailMessageId}`);
    showToast('Draft queued — will appear in Activity Hub shortly.');
    document.getElementById(`feed-row-${gmailMessageId}`)?.remove(); checkFeedEmpty();
    loadDraftQueue().catch(() => {});
  } catch(e) { btn.innerHTML = orig; btn.disabled = false; }
}

async function feedActionB(talentKey, gmailMessageId, btn) {
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/emails/${gmailMessageId}/keep-in-inbox`);
    showToast('Kept in inbox for human review.');
    document.getElementById(`feed-row-${gmailMessageId}`)?.remove();
    checkFeedEmpty();
  } catch(e) { btn.innerHTML = orig; btn.disabled = false; }
}

async function feedActionC(talentKey, gmailMessageId, btn) {
  if (!confirm('Archive?')) return;
  const orig = btn.innerHTML; btn.innerHTML = '…'; btn.disabled = true;
  try {
    await api('POST', `/api/dashboard/talents/${talentKey}/emails/${gmailMessageId}/spam`);
    showToast('Archived.');
    document.getElementById(`feed-row-${gmailMessageId}`)?.remove(); checkFeedEmpty();
  } catch(e) { btn.innerHTML = orig; btn.disabled = false; }
}
```

This restores `[C]` to the `/spam` endpoint exactly as it was at the confirmed-working point
(`/archive` arrived later with the rename — both endpoints exist, so this is a behavior choice to
match "exactly as they were").

## What is NOT touched

- `feedBadge(r)` — LOST badge display stays.
- The orphaned-drafts section, `regenerate` endpoints, `loadDraftQueue`, `moveToInbox`, all spans/markup in the row.
- Any backend file.

## Verification

1. `cd backend && uvicorn backend.main:app --reload --port 8000`
2. Open the dashboard with the Interceptor skill (`interceptor open http://localhost:8000/static/dashboard.html`), go to the Inbox Feed tab.
3. Confirm **every** row shows three buttons `A` `B` `C` with no rows missing buttons, regardless of score, triage_reason, or thread length.
4. Confirm a LOST row still shows the red `LOST` badge (display preserved).
5. Click `A` on a row → toast "Draft queued", row removed. Click `B` → "Kept in inbox", row removed. Click `C` → confirm prompt → "Archived", row removed.
6. Check the browser network log: `A`→`/force-draft/`, `B`→`/keep-in-inbox`, `C`→`/spam` all return 2xx.
