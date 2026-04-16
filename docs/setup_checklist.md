# Go-Live Setup Checklist — Talent Inbox Automation

Complete these steps **in order** before activating any scenario in Make.

---

## Step 1 — Google Sheets Setup

- [ ] Create a new Google Sheet for the **Master Activity Log**
  - Name the first tab: `Master Log`
  - Copy the headers from `sheets/master_log_template.csv`
  - Paste the Sheet ID into `config/settings.json` → `google_sheets.master_log_sheet_id`

- [ ] Create a new Google Sheet for the **SOP Matrix** (or reuse the uploaded XLSX converted to Sheets)
  - One tab per talent — tab names must match exactly (case-sensitive):
    `Sylvia`, `Trin`, `Sam J`, `Britt`, `Allee`, `Lizz`, `Katrina`, `Jenn`, `Angela`, `Colleen`, `Alana`, `Grayson`, `Kylika`, `Anastasiya`, `Katrina D`, `Michaela`
  - All SOP content is already extracted — see `sheets/sop_data.json` and `sheets/talent_sops/`
  - Paste the Sheet ID into `config/settings.json` → `google_sheets.sop_matrix_sheet_id`

---

## Step 2 — Make Connections (OAuth — must be done in browser)

Create the following named connections in Make → **Connections**:

| Connection Name | Type | Account |
|---|---|---|
| `OpenAI - Talent Automation` | OpenAI | Your OpenAI API key |
| `Google Sheets - Talent Automation` | Google Sheets | Agency Google account with sheet access |
| `Gmail - Sylvia` | Gmail | Sylvia's inbox |
| `Gmail - Trin` | Gmail | Trinity's inbox |
| `Gmail - Sam` | Gmail | Sam's inbox |
| `Gmail - Britt` | Gmail | Britt's inbox |
| `Gmail - Allee` | Gmail | Allee's inbox |
| `Gmail - Lizz` | Gmail | Lizz's inbox |
| `Gmail - Katrina` | Gmail | Katrina's inbox |
| `Gmail - Jenn` | Gmail | Jenn's inbox |
| `Gmail - Angela` | Gmail | Angela's inbox |
| `Gmail - Colleen` | Gmail | Colleen's inbox |
| `Gmail - Alana` | Gmail | Alana's inbox |
| `Gmail - Grayson` | Gmail | Grayson's inbox |
| `Gmail - Kylika` | Gmail | Kylika's inbox |
| `Gmail - Anastasiya` | Gmail | Anastasiya's inbox |
| `Gmail - KatrinaD` | Gmail | Katrina D's inbox |
| `Gmail - Michaela` | Gmail | Michaela's inbox |

> ⚠️ Connection names must match exactly — the Phase 2 reply scenario dynamically routes Gmail sends using the talent name from the log.

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

- [ ] Pick 2–3 inboxes to test first (recommend: Trin, Sam, Colleen)
- [ ] Activate those 3 scenarios only
- [ ] Send 3–5 test emails to each inbox (mix of obvious spam, legit brand inquiries, and edge cases)
- [ ] Check the Master Log — verify scores, labels, and actions look correct
- [ ] Confirm Score 1 emails land in Gmail Trash (not permanently deleted)
- [ ] Confirm Score 3 emails are logged as `queued for reply`
- [ ] Tune `prompts/triage.md` if misclassifications appear
- [ ] After 48h clean run → activate remaining 13 inboxes

---

## Step 6 — Phase 2 Activation (Auto-Reply)

> **Prerequisite:** All SOP tabs in the SOP Matrix sheet must be complete before this step.

- [ ] Confirm SOP sheet tabs are finalized and match the tab names in Step 1
- [ ] Import `make/phase2_reply_scenario.json`
- [ ] Fill in `[MASTER_LOG_SHEET_ID]` and `[SOP_MATRIX_SHEET_ID]`
- [ ] Set all connections (Google Sheets, OpenAI, Gmail — all 16 talent Gmail connections)
- [ ] **Add a `gmail:GetEmail` module before step 6** to fetch original email body using the thread ID from the log — inject the body into the GPT reply prompt (this is the one step that requires manual wiring in Make)
- [ ] Keep `send_delay_enabled: true` and `send_delay_minutes: 15` during all testing
- [ ] Activate. Monitor outbound replies daily for 5 days minimum
- [ ] After QA sign-off → reduce or remove send delay at supervisor's discretion

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
