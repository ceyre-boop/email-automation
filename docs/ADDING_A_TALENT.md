# Adding a New Talent

`sheets/sop.md` is the single source of truth for all talent data. Adding a new talent requires editing exactly one file. The system discovers, validates, and configures the talent automatically on the next deploy.

---

## Checklist

### Step 1 — Add a section to `sheets/sop.md`

Copy the block below and paste it at the end of the "Part 3 — Talent Approved Responses" section. Fill in every field — no blanks.

```
Talent: Full Name Here
Key: FirstName
Manager: Manager Full Name <manager@taboost.me>
Gmail: Gmail - FirstName
Min Rate: $750 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
[Paste the exact approved response text here. Do not modify the format.]

Scenario C: Personal Email Forward
Personal Emails:

- talent@personalemail.com
```

**Required metadata fields — all must be present:**

| Field | Example | Notes |
|-------|---------|-------|
| `Key:` | `Key: Jocelyn` | First name only, matches DB talent_key exactly |
| `Manager:` | `Manager: Cara Best <cara@taboost.me>` | Must use `Name <email>` format |
| `Gmail:` | `Gmail - Jocelyn` | Must match the Make connection name exactly |
| `Min Rate:` | `$850 per video` | Dollar sign optional; "per video" or "per hour" |
| `Auto Send:` | `yes` or `no` | Case-insensitive |
| `Paused:` | `yes` or `no` | Set `yes` until testing is complete |
| `Approved Response:` | (verbatim reply text) | Required for drafts to generate |
| `Personal Emails:` | `- email@gmail.com` | One per line with `- ` prefix |

### Step 2 — Connect Gmail OAuth in Make

1. Go to Make → Connections → Add a connection
2. Choose Gmail, name it exactly **`Gmail - [Key]`** (must match the `Gmail:` line in sop.md)
3. Authenticate with the talent's Gmail account
4. Verify the connection shows as "Connected"

### Step 3 — Push to main and verify startup logs

```bash
git add sheets/sop.md
git commit -m "Add [TalentName] to sop.md"
git push origin main
```

After Render deploys (1-2 min), check Render logs for:
```
INFO  SOP validator: 13 talents loaded from sop.md, 0 warnings
```

If you see warnings like `[TalentName]: missing Approved Response block`, fix the sop.md entry and push again.

### Step 4 — Connect the Gmail account in the Render app

Navigate to `[APP_BASE_URL]/auth/connect?talent=[Key]` (e.g. `/auth/connect?talent=Jocelyn`) and complete the OAuth flow with the talent's Gmail account. Confirm the token row appears as active in the DB.

### Step 5 — Run a backfill and confirm drafts

Once the Gmail account is connected, trigger a backfill to process existing unread emails:

```
POST [APP_BASE_URL]/api/dashboard/backfill-all?days=7
```

Check the dashboard — the new talent should appear with processed emails and drafts. Verify the draft text uses the correct rates from the `Approved Response:` block you wrote.

### Step 6 — Enable Auto Send (when ready)

When testing is complete and the talent is approved for auto-send:

1. Edit `sheets/sop.md` — change `Auto Send: no` → `Auto Send: yes`
2. Change `Paused: yes` → `Paused: no` if it was paused during testing
3. Commit and push

The auto-send list is derived automatically from sop.md — no other file needs to change.

---

## What happens automatically (no manual steps needed)

- `sheets/sop_data.json` is regenerated on every deploy — do not edit it manually
- The talent appears in the dashboard once their Gmail OAuth token is connected
- Rate changes: edit `Approved Response:` in sop.md → commit → push → done

## Common mistakes

| Symptom | Likely cause |
|---------|-------------|
| "No sop.md profile for talent_key=X" in logs | `Key:` in sop.md doesn't match `talent_key` in DB (check case) |
| Startup warning: "missing Approved Response block" | `Approved Response:` section missing or misspelled |
| Personal email not filtering | Email in `Personal Emails:` list doesn't match sender address exactly |
| Gmail OAuth fails | Make connection name doesn't match `Gmail:` line in sop.md |
| Drafts not generating | Check `Paused: no` and `Auto Send:` values; verify OAuth token is active |
