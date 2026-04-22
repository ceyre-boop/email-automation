# Talent Inbox Automation — AI-Powered Email Triage & Auto-Response System

## Overview

This repository contains all the assets needed to deploy the Talent Inbox Automation system:

### Core Assets

| Asset | Location | Purpose |
|---|---|---|
| Global config | `config/settings.json` | Talent list, thresholds, timing |
| AI triage prompt | `prompts/triage.md` | GPT-4o prompt for scoring emails 1/2/3 |
| AI reply prompt | `prompts/reply.md` | GPT-4o prompt for drafting replies |
| SOP sheet template | `sheets/sop_matrix_template.csv` | Column structure for Britney's SOP sheet |
| Master log template | `sheets/master_log_template.csv` | Column structure for activity log |
| Phase 1 Make blueprint | `make/phase1_triage_scenario.json` | Import into Make: triage engine (template) |
| Phase 2 Make blueprint | `make/phase2_reply_scenario.json` | Import into Make: auto-reply |
| Phase 3 Make blueprint | `make/phase3_digest_scenario.json` | Import into Make: daily digest |
| Per-talent Phase 1 blueprints | `make/scenarios/phase1_*.json` | Pre-filled triage scenarios — one per talent inbox |

### Advanced Assets

| Asset | Location | Purpose |
|---|---|---|
| Preflight validator | `scripts/preflight_validator.js` | Validates all config, tab names, connections before go-live |
| Confidence & fallback policy | `config/confidence_policy.json` | Explicit routing rules for every edge case and failure mode |
| Rollout controls | `config/rollout_controls.json` | Pilot cohort, staged activation order, per-inbox kill switches |
| Phase 4 error alert scenario | `make/phase4_error_alert_scenario.json` | Real-time failure alerts via webhook |
| Phase 5 weekly dashboard scenario | `make/phase5_weekly_digest_scenario.json` | Weekly ops metrics by talent |
| Override queue template | `sheets/override_queue_template.csv` | Manual review queue with pending/approved/rejected/retried states |
| SOP audit report | `docs/sop_audit_report.md` | Per-talent SOP issues to resolve before Phase 2 |
| Failure playbooks | `docs/failure_playbooks.md` | Step-by-step recovery for 7 failure types |
| Compliance guardrails | `docs/compliance_guardrails.md` | PII, retention, reply safety rules, audit trail |
| Triage QA test cases | `tests/triage_test_cases.json` | 20 test cases with expected scores — run after prompt changes |
| Reply QA test cases | `tests/reply_test_cases.json` | 12 test cases with expected output criteria — run after prompt changes |

---

## Deployment

### Part 1 — Daily inbox poller (GitHub Actions, already configured)

The backend poller runs as a **scheduled GitHub Actions job** (`poll.yml`). It starts once a day at **09:00 UTC**, processes every connected talent inbox end-to-end (triage → draft/archive/flag → log to Sheets), then exits. No server needed for this part.

### Part 2 — Talent onboarding server (Render.com, free)

The one-time Gmail onboarding flow (`/connect?talent=<key>`) requires a live HTTPS endpoint so Google can redirect back after the talent authorises. Deploy it to **Render.com** for free — it auto-deploys from this GitHub repo whenever you push.

#### Step 1 — Deploy to Render

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub account and select the `ceyre-boop/email-automation` repo
3. Render detects `render.yaml` automatically — click **Apply**
4. Your service URL will be something like `https://email-automation.onrender.com`

#### Step 2 — Set environment variables in Render

In Render → **Dashboard → email-automation → Environment**, add:

| Variable | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | Google Cloud Console → OAuth 2.0 Client ID (type: **Web application**) |
| `GOOGLE_CLIENT_SECRET` | Same OAuth client |
| `GOOGLE_REDIRECT_URI` | `https://email-automation.onrender.com/auth/callback` |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| `DATABASE_URL` | Supabase → Project Settings → Database → Connection string (URI) |
| `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` | Full service account JSON pasted as one line |
| `APP_BASE_URL` | `https://email-automation.onrender.com` |

#### Step 3 — Add the redirect URI in Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services → Credentials → your OAuth 2.0 Client ID**
2. Under **Authorized redirect URIs**, add:
   `https://email-automation.onrender.com/auth/callback`
3. Save

#### Step 3.1 — OAuth consent screen URLs (required for verification)

In **APIs & Services → OAuth consent screen → App information**, set:

- **Application home page**: `https://<your-public-domain>/`
- **Privacy policy**: `https://<your-public-domain>/privacy`

Important:
- Do **not** use `/auth/callback` as your privacy policy URL.
- The home page must be publicly accessible without login.
- Use a domain you control/own (custom domain is recommended for verification).

#### Step 4 — Add GitHub Secrets for the daily poller

Go to your repo → *Settings → Secrets and variables → Actions → New repository secret* and add the same credentials:

| Secret | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | Same as above |
| `GOOGLE_CLIENT_SECRET` | Same as above |
| `OPENAI_API_KEY` | Same as above |
| `DATABASE_URL` | Same Supabase connection string |
| `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` | Same service account JSON |

#### Step 5 — Send the onboarding link to each talent

```
https://email-automation.onrender.com/connect?talent=Katrina
```

Replace `Katrina` with the talent's key from `settings.json`. The talent:
1. Opens the link — sees a branded page with their name
2. Clicks **Sign in with Google** → Google consent screen
3. Approves Gmail access
4. Lands on ✅ "You're connected!" — done

Their token is stored in Supabase automatically. The daily GitHub Actions poller uses it from the next run onward.

**Talent keys:** `Sylvia`, `Trin`, `Sam`, `Britt`, `Allee`, `Lizz`, `Katrina`, `Jenn`, `Angela`, `Colleen`, `Alana`, `Grayson`, `Kylika`, `Anastasiya`, `KatrinaD`, `Michaela`

> **Note:** Render's free tier spins down after 15 minutes of inactivity. The onboarding link may take ~30 seconds to load on first click. This is fine — it only needs to be live while talents are connecting.

### Adjusting the poller run time

The workflow fires daily at **09:00 UTC** by default. Edit the `cron` line in `.github/workflows/poll.yml` to suit your timezone.

### Running the poller manually

Trigger a run any time from the *Actions* tab → *Poll Inboxes* → *Run workflow*.

### Reviewing drafts

Drafts (score-3 emails) are saved directly in the talent's Gmail Drafts folder and also recorded in the `drafts` table in Supabase. Review them in:
- **Gmail** — open the talent's Drafts folder, edit/send/delete as needed.
- **Supabase dashboard** — Table Editor → `drafts` for a full list with status, brand name, proposed rate, and the draft text.

---

## Pre-Launch Validation

Before activating any scenario in Make, run the preflight validator:

```bash
node scripts/preflight_validator.js
```

This checks every required config value, tab name, connection name, scenario file, and template column — and exits with a clear pass/fail result. Resolve all failures before proceeding.

---

## Prerequisites

Before deploying anything, complete these one-time setup steps:

1. **OpenAI API key** — create at https://platform.openai.com/api-keys and add to Make as a connection named `OpenAI - Talent Automation`.
2. **Gmail OAuth** — connect each talent Gmail account to Make via OAuth. Name each connection `Gmail - [TalentFirstName]`.
3. **Google Sheets** — share Britney's SOP sheet and the master log sheet with the Make service account (or use OAuth). Note the Sheet IDs and fill them into `config/settings.json`.
4. **SOP sheet audit** — before Phase 2, standardize every talent tab to match `sheets/sop_matrix_template.csv`. All column names must match exactly.
5. **Make Pro Plan** — confirm the workspace is on the Pro plan (required for multiple active scenarios).

---

## Deployment Phases

### Phase 1 — Triage Engine (Days 1–7)
- Import `make/phase1_triage_scenario.json` into Make **once per talent inbox**.
- Update each scenario's Gmail connection to the correct talent's account.
- Fill in the talent name and master log Sheet ID (from `config/settings.json`).
- Test on 2–3 inboxes with real historical emails before rolling out to all inboxes.
- Monitor the master log daily. Adjust the triage prompt in `prompts/triage.md` based on misclassifications.

### Phase 2 — Auto-Reply (Days 8–18)
- Complete the SOP sheet audit with Britney first.
- Import `make/phase2_reply_scenario.json` into Make.
- Update the SOP Sheet ID and each talent's tab name in the scenario or in `config/settings.json`.
- The 15-minute send delay is enabled by default — do not disable during testing.
- Review every outbound reply manually until QA sign-off.

### Phase 3 — Daily Digest & Alerts (Days 19–28)
- Import `make/phase3_digest_scenario.json` into Make.
- Set the trigger time to 8:00 AM in the supervisor's timezone.
- Fill in the supervisor's email address.
- Run a 5-day supervised QA period. After sign-off, the system goes fully autonomous.

### Phase 4 — Error Alerting (activate before Phase 1)
- Import `make/phase4_error_alert_scenario.json` into Make.
- This scenario fires immediately when any other scenario fails.
- Copy the generated webhook URL and add it to the error handler of every Phase 1/2/3 scenario.
- Fill in `[ERROR_ALERT_EMAIL]` and `[MASTER_LOG_SHEET_ID]`.
- Create the "Error Log" tab in the master log sheet.

### Phase 5 — Weekly Ops Dashboard (activate after 7 days of data)
- Import `make/phase5_weekly_digest_scenario.json` into Make.
- Set schedule: Monday 8:30 AM.
- Fill in `[MASTER_LOG_SHEET_ID]` and `[SUPERVISOR_EMAIL]`.

---

## Rollout Strategy

Use `config/rollout_controls.json` to control which inboxes are active at any time.

**Pilot cohort (activate first):** Trin, Sam, Colleen — monitor for 48 hours before rolling out to remaining inboxes in batches.

**Activate last:** Michaela and KatrinaD require special routing logic (dual manager escalation / hourly rates). Confirm `config/confidence_policy.json` rules are working before enabling them.

---

## Testing Prompt Changes

After any change to `prompts/triage.md` or `prompts/reply.md`, run the QA test suites manually in the OpenAI Playground before deploying to production. See `tests/triage_test_cases.json` (20 cases, 18/20 pass threshold) and `tests/reply_test_cases.json` (12 cases, all must pass).

---

## Reference Documents

- `docs/sop_audit_report.md` — complete SOP review with per-talent issues to fix before Phase 2
- `docs/failure_playbooks.md` — step-by-step recovery for every known failure type
- `docs/compliance_guardrails.md` — PII rules, data retention, reply safety, audit trail requirements
- `docs/setup_checklist.md` — step-by-step go-live checklist

---

| Tool | Monthly Cost |
|---|---|
| Make Pro | ~$99/mo |
| OpenAI GPT-4o API | ~$30–80/mo at full volume |
| Gmail / Google Workspace | Already paying |
| Google Sheets | Free |

**Total new spend: ~$130–180/mo.** To reduce API costs, swap the triage step from `gpt-4o` to `gpt-4o-mini` in `config/settings.json` — the reply step should stay on `gpt-4o`.

---

## TODO — Open Questions (must be resolved before/during Phase 1)

<!-- TODO: Confirm the daily digest send time and supervisor timezone -->
<!-- TODO: Confirm who receives the daily digest (name + email address) -->
<!-- TODO: Decide whether the 15-minute send delay is always-on or opt-in during Month 1 -->
<!-- TODO: Confirm which Gmail inboxes are available now vs added later -->
<!-- TODO: Schedule the 30-minute SOP audit call with Britney (required for Phase 2) -->
<!-- TODO: Decide global minimum dollar threshold for Score 1 (e.g. any offer under $50 = trash) -->
<!-- TODO: Decide if thresholds are per-talent or global -->
