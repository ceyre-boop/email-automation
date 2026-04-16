# Talent Inbox Automation — AI-Powered Email Triage & Auto-Response System

## Overview

This repository contains all the assets needed to deploy the Talent Inbox Automation system:

| Asset | Location | Purpose |
|---|---|---|
| Global config | `config/settings.json` | Talent list, thresholds, timing |
| AI triage prompt | `prompts/triage.md` | GPT-4o prompt for scoring emails 1/2/3 |
| AI reply prompt | `prompts/reply.md` | GPT-4o prompt for drafting replies |
| SOP sheet template | `sheets/sop_matrix_template.csv` | Column structure for Britney's SOP sheet |
| Master log template | `sheets/master_log_template.csv` | Column structure for activity log |
| Phase 1 Make blueprint | `make/phase1_triage_scenario.json` | Import into Make: triage engine |
| Phase 2 Make blueprint | `make/phase2_reply_scenario.json` | Import into Make: auto-reply |
| Phase 3 Make blueprint | `make/phase3_digest_scenario.json` | Import into Make: daily digest |

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

---

## Cost Estimate

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
