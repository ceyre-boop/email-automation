# Go-Live Setup Checklist — Talent Inbox Automation

Complete these steps **in order** before activating any scenario in Make.

---

## Step 1 — Google Sheets Setup

- [ ] Create a new Google Sheet for the **Master Activity Log**
  - Name the first tab: `Master Log`
  - Copy the headers from `sheets/master_log_template.csv` (includes the `Brand Name` column added between `Offer Type` and `Proposed Rate (USD)`)
  - Paste the Sheet ID into `config/settings.json` → `google_sheets.master_log_sheet_id`

- [ ] The **SOP Matrix** sheet already exists: `Talent Email SOP (1).xlsx` / Google Sheet ID `1NCH4BClugnuGwvDX3NR0cyw5kP1XElyvpjmvEjEnAyQ`
  - Sheet already has the correct 2-column format: `Trigger / Scenario` (col A) and `Response / Action` (col B)
  - Per-talent CSV extracts are in `sheets/talent_sops/` for reference
  - Tab names match exactly: `Sylvia`, `Trin`, `Sam J`, `Britt`, `Allee`, `Lizz`, `Katrina`, `Jenn`, `Angela`, `Colleen`, `Alana`, `Grayson`, `Kylika`, `Anastasiya`, `Katrina D`, `Michaela`
  - The SOP Matrix Sheet ID is already in `config/settings.json` → `google_sheets.sop_matrix_sheet_id`
  - **For the Katrina test**: no SOP sheet access is needed — `make/scenarios/phase2_Katrina.json` has all rules embedded

---

## Step 2 — Make Connections (OAuth — must be done in browser)

Create the following named connections in Make → **Connections**:

| Connection Name | Type | Account |
|---|---|---|
| `OpenAI - Talent Automation` | OpenAI | Your OpenAI API key |
| `Google Sheets - Talent Automation` | Google Sheets | Agency Google account with sheet access |
| `Gmail - Katrina` | Gmail | **katrina@taboost.me** ← active test inbox |

> All other talent Gmail connections are **not needed yet**. Add them after Katrina's pilot is confirmed stable and Marco approves expanding to additional inboxes.

> ⚠️ Connection names must match exactly — the Phase 2 reply scenario dynamically routes Gmail operations using the talent name from the log.

---

## Step 3 — Fill in Remaining TODOs in config/settings.json

- [ ] `digest.recipient_email` — supervisor email for daily digest
- [ ] `digest.send_time` — confirm timing (default: 08:00 ET)
- [ ] `make.workspace_zone` — confirm your Make zone (us1, eu1, etc.)
- [ ] `make.error_alert_email` — email for scenario failure alerts

---

## Step 4 — Import Phase 1 Scenarios into Make

Each talent has a pre-filled scenario in `make/scenarios/`. Import them one by one:

1. In Make: **Scenarios → Create new scenario → ⋯ menu → Import Blueprint**
2. Paste the contents of the relevant file (e.g. `make/scenarios/phase1_Sylvia.json`)
3. After import, set the **3 connections** in each module (Gmail, OpenAI, Google Sheets)
4. Replace `[MASTER_LOG_SHEET_ID]` with your real Sheet ID (in 3 places per scenario)
5. Save. **Do not activate yet.**

Repeat for all 16 scenarios:

| File | Talent | Min Rate |
|---|---|---|
| `phase1_Sylvia.json` | Sylvia Van Hoeven | $1,000/video |
| `phase1_Trin.json` | Trinity Blair | $2,000/video |
| `phase1_Sam.json` | Sam Jones | $700/video |
| `phase1_Britt.json` | Brittanie Hammer | $900/video |
| `phase1_Allee.json` | Allee Baray | $650/video |
| `phase1_Lizz.json` | Lizz Freixas | $600/video |
| `phase1_Katrina.json` | Katrina | $300/video |
| `phase1_Jenn.json` | Jenn Lyles | $300/video |
| `phase1_Angela.json` | Angela Callisto | $600/video |
| `phase1_Colleen.json` | Colleen Fusco | $800/video |
| `phase1_Alana.json` | Alana Calviello | $400/video |
| `phase1_Grayson.json` | Grayson Finks | $300/video |
| `phase1_Kylika.json` | Kylika Miller | $400/video |
| `phase1_Anastasiya.json` | Anastasiya | $600/video |
| `phase1_KatrinaD.json` | Katrina D | $150/hr |
| `phase1_Michaela.json` | Michaela | $3,500/video |

---

## Step 5 — Testing Phase 1 (before activating any inbox)

- [ ] **Activate Katrina's scenario only** (`make/scenarios/phase1_Katrina.json`)
- [ ] Send 3–5 test emails to `katrina@taboost.me` (mix of obvious spam, legit brand inquiries, and edge cases)
- [ ] Check the Master Log — verify scores, labels, and actions look correct
- [ ] Confirm Score 1 emails land in Gmail Trash (not permanently deleted)
- [ ] Confirm Score 3 emails are logged as `queued for reply`
- [ ] Tune `prompts/triage.md` if misclassifications appear
- [ ] After 48h clean run → activate remaining 15 inboxes (after Marco approves expansion)

---

## Step 6 — Phase 2 Activation (Draft-Reply)

> **DRAFT MODE:** Phase 2 saves AI-drafted replies as **Gmail Drafts** — nothing is sent automatically. Supervisor reviews each draft in the talent's Gmail Drafts folder and decides whether to send, edit, or discard it. This mode runs for approximately 1 month until Marco approves full autonomous operation.

### For the Katrina functionality test (do this first):

- [ ] Import **`make/scenarios/phase2_Katrina.json`** into Make
  - This file has all of Katrina's SOP rules embedded directly in the GPT prompt — no SOP sheet lookup needed
  - Fill in `[MASTER_LOG_SHEET_ID]` (appears 3 times in the file)
  - Set the 3 connections: `Google Sheets - Talent Automation`, `OpenAI - Talent Automation`, `Gmail - Katrina`
  - Set the schedule to run every 5 minutes
- [ ] Activate both Katrina scenarios (Phase 1 + Phase 2)
- [ ] Send 3 test emails to `katrina@taboost.me`:
  - **Good email**: real brand name, offer > $600 → should Score 3, save a draft that says "Looping in management..."
  - **Low offer email**: real brand, offer < $300 → should Score 3, save a draft with the counter-rate response
  - **Trash email**: obvious spam, prize win, or personal Gmail domain → should Score 1, archived immediately
- [ ] After each email, check the Master Log — verify scores, actions, and Brand Name are logged
- [ ] Check Gmail Drafts in katrina@taboost.me — confirm the AI draft matches the correct SOP template
- [ ] To run a sample of 30 real inbox emails: temporarily change Phase 1 Gmail trigger `labelIds` to `["INBOX"]` and manually trigger runs

### For full rollout (after Katrina test passes):

- [ ] Confirm all SOP sheet tabs use the 2-column format: `Trigger / Scenario` (col A) and `Response / Action` (col B)
  - All content is already extracted — see `sheets/talent_sops/` for per-talent CSVs matching this format
- [ ] Import `make/phase2_reply_scenario.json` (the multi-talent version)
- [ ] Fill in `[MASTER_LOG_SHEET_ID]` and `[SOP_MATRIX_SHEET_ID]` (SOP matrix ID: `1NCH4BClugnuGwvDX3NR0cyw5kP1XElyvpjmvEjEnAyQ`)
- [ ] Set all connections (Google Sheets, OpenAI, and Gmail connections for each active talent)
- [ ] After ~1 month and Marco's approval: switch to live send by replacing `gmail:CreateDraftReply` with `gmail:ReplyToThread` and updating `config/settings.json → reply.draft_mode` to `false`

---

## Step 7 — Phase 3 Activation (Daily Digest)

- [ ] Import `make/phase3_digest_scenario.json`
- [ ] Fill in `[MASTER_LOG_SHEET_ID]` and `[RECIPIENT_EMAIL]`
- [ ] Set schedule to run at `08:00` in your Make timezone
- [ ] Send a test run manually and verify digest email format

---

## Special Notes Per Talent

| Talent | Special Handling |
|---|---|
| **Katrina** | Dual escalation path: offers >$650 → Cara; ≤$650 → Chenni |
| **Katrina D** | Rate is per hour (not per video). Min $150/hr, standard $300/hr. GPT triage must interpret hourly offers. |
| **Michaela** | Dual escalation: offers >$4,000 → Cara; ≤$4,000 → Chenni. Offers below $1,000 skip to Revisit. Min triage rate: $3,500. |
| **Trin** | Delete all commission-only offers. Fan mail gets its own folder. |
| **KatrinaD** | Livestream-specific. Some offers will quote multi-hour bundles — check math. |

---

## Quick Reference — Minimum Rates

| Talent | Min Rate | Category | Manager |
|---|---|---|---|
| Sylvia Van Hoeven | $1,000/video | Beauty | Cara |
| Trinity Blair | $2,000/video | Lifestyle | Chenni |
| Sam Jones | $700/video | Home/Living | Cara |
| Brittanie Hammer | $900/video | Home/Living | Chenni |
| Allee Baray | $650/video | Fashion | Chenni |
| Lizz Freixas | $600/video | Fashion | Chenni |
| Katrina | $300/video | Fashion | Chenni |
| Jenn Lyles | $300/video | Fashion | Chenni |
| Angela Callisto | $600/video | Fashion | Chenni |
| Colleen Fusco | $800/video | Beauty | Cara |
| Alana Calviello | $400/video | Fashion | Nicole |
| Grayson Finks | $300/video | Fashion | Nicole |
| Kylika Miller | $400/video | Beauty | Nicole |
| Anastasiya | $600/video | Fashion/Beauty | Cara |
| Katrina D | $150/hr | Fashion (livestream) | Cara |
| Michaela | $3,500/video | Fashion/Beauty | Cara |
