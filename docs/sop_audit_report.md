# SOP Audit Report — Talent Inbox Automation
<!-- Generated from sheets/sop_data.json + config/settings.json -->
<!-- Run this audit with Britney BEFORE Phase 2 full rollout -->
<!-- Last reviewed: pre-deployment (update date when completed) -->

---

## Purpose

This report documents the current state of the SOP data extracted from Britney's Google Sheet, flags issues that need to be resolved before the auto-reply engine (Phase 2) can roll out beyond Katrina's test inbox, and defines the required column structure that every talent tab must follow.

The auto-reply system reads the SOP sheet dynamically. If the structure or response text contains internal routing instructions, addresses, or placeholder URLs, the system may include them in auto-sent replies.

---

## Required Column Structure (All Tabs — Non-Negotiable)

Every talent tab in the Google Sheet SOP matrix **must** have exactly these two columns:

| Column | A | B |
|---|---|---|
| **Header** | `Trigger / Scenario` | `Response / Action` |
| **Format** | Plain text describing the email scenario | Either a complete reply text (ready to send), or an action instruction for a human (e.g. "Move to Revisit", "CC Cara") |

The Make automation reads all rows and passes them to GPT as context. GPT picks the best matching rule and either:
- Returns the response text verbatim (if it is an email template), or
- Returns `ESCALATE: [action needed]` (if the response is an action instruction)

**Key rules for the `Response / Action` column:**
- If the response is an email template to send: write complete, ready-to-send email text with no internal notes mixed in
- If the response requires human action: write a short action description (e.g. "Move to Commission Only folder", "CC Cara and loop in management", "Move to Revisit")
- Do NOT mix email content and routing instructions in the same row

---

## Talent-by-Talent Status

### Katrina ✅ READY FOR TEST
- **Manager:** Chenni (≤$650) / Cara (>$650) | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 15 — all extracted to `sheets/talent_sops/Katrina_sop.csv`
- **Status:** ✅ SOP rules embedded in `make/scenarios/phase2_Katrina.json` — ready for functionality test
- **Issues before full rollout:**
  - [ ] Remove physical address from SOP sheet tab (PII — keep in a separate document; set PR row response to "ESCALATE: PR request — handle address sharing manually")
  - [ ] Confirm PR address is only shared after human review

---

### Sylvia Van Hoeven
- **Manager:** Cara | **Category:** Beauty | **Min Rate:** $1,000/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Sylvia_sop.csv`
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder with the real URL in all response texts
  - [ ] Commission-only row: response should be "ESCALATE: Commission-only offer — route to Commission Only folder if beauty/fashion product, otherwise archive"

---

### Trinity Blair
- **Manager:** Chenni | **Category:** Lifestyle | **Min Rate:** $2,000/video
- **SOP Rules Found:** 16 — extracted to `sheets/talent_sops/Trin_sop.csv`
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder
  - [ ] Commission-only row: response should be "ESCALATE: Commission-only — archive immediately, do not reply"
  - [ ] Fan mail row: response should be "ESCALATE: Fan mail — move to Fan Mail folder, do not reply"

---

### Sam Jones
- **Manager:** Cara | **Category:** Home/Living | **Min Rate:** $700/video
- **SOP Rules Found:** 16 — extracted to `sheets/talent_sops/Sam_sop.csv`
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder
  - [ ] Commission-only row: "ESCALATE: Commission-only — route to Commission Only folder if home/living product"

---

### Brittanie Hammer
- **Manager:** Chenni | **Category:** Home/Living | **Min Rate:** $900/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Britt_sop.csv`
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Confirm "TikTok Creator of the Year 2025" mention in templates is still approved
  - [ ] Commission-only row response defined

---

### Allee Baray
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $650/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Allee_sop.csv`
- **Status:** ⚠️ Multi-account complexity.
- **Issues before Phase 2 rollout:**
  - [ ] **Multi-account:** SOP lists 3 TikTok account tiers ($850/$700/$500). Response for rate inquiry should include all tiers and ask brand to specify account
  - [ ] Remove physical address from Google Sheet tab (PII)

---

### Lizz Freixas
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $600/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Lizz_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Commission-only row response defined

---

### Jenn Lyles
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Jenn_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder

---

### Angela Callisto
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $600/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Angela_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Confirm current rates are up to date in response texts

---

### Colleen Fusco
- **Manager:** Cara | **Category:** Beauty | **Min Rate:** $800/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Colleen_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder

---

### Alana Calviello
- **Manager:** Nicole | **Category:** Fashion | **Min Rate:** $400/video
- **SOP Rules Found:** 14 — extracted to `sheets/talent_sops/Alana_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Confirm commission-only handling row is present

---

### Grayson Finks
- **Manager:** Nicole | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 14 — extracted to `sheets/talent_sops/Grayson_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Confirm commission-only handling row is present

---

### Kylika Miller
- **Manager:** Nicole | **Category:** Beauty | **Min Rate:** $400/video
- **SOP Rules Found:** 14 — extracted to `sheets/talent_sops/Kylika_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Confirm commission-only handling row is present

---

### Anastasiya
- **Manager:** Cara | **Category:** Fashion/Beauty | **Min Rate:** $600/video
- **SOP Rules Found:** 15 — extracted to `sheets/talent_sops/Anastasiya_sop.csv`
- **Issues before Phase 2 rollout:**
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder

---

### Katrina D
- **Manager:** Cara | **Category:** Fashion (livestream) | **Min Rate:** $150/hr
- **SOP Rules Found:** 13 — extracted to `sheets/talent_sops/KatrinaD_sop.csv`
- **Status:** ⚠️ Hourly rate model — activate last.
- **Issues before Phase 2 rollout:**
  - [ ] **Rate unit is per-hour, not per-video.** Add a note to the "Trigger / Scenario" column rows that reference dollar amounts: include the equivalent hourly rate in the response so GPT can apply the correct rule. Example: trigger "Initially offered a rate below $150/hr (minimum)" should clearly note the hourly math.
  - [ ] Remove physical address from Google Sheet tab (PII)

---

### Michaela
- **Manager:** Cara (default) | **Category:** Fashion/Beauty | **Min Rate:** $3,500/video
- **SOP Rules Found:** 16 — extracted to `sheets/talent_sops/Michaela_sop.csv`
- **Status:** ⚠️ Dual manager escalation + high-value thresholds — activate last.
- **Issues before Phase 2 rollout:**
  - [ ] **Dual manager rule:** Offers >$4,000 → CC Cara. Offers ≤$4,000 → CC Chenni. Offers below $1,000 → Revisit. All "initial response" rows should have response text "ESCALATE: [manager routing logic]"
  - [ ] Remove physical address from Google Sheet tab (PII)
  - [ ] Replace "HERE" media kit URL placeholder

---

## Cross-Talent Issues

### 1. Physical Addresses in SOP Data (PII — ACTION REQUIRED BEFORE FULL ROLLOUT)
The SOP data contains physical mailing addresses for PR requests. If left in the Google Sheet, these addresses are accessible to Make and passed to OpenAI's API.

**Required action:**
- Remove all physical addresses from the SOP matrix Google Sheet.
- For each talent's PR Request row, change the response to: "ESCALATE: PR gifting request from [brand] — share address manually after verifying brand legitimacy"
- Store addresses in a separate, access-restricted document outside of Make/OpenAI scope.

See `docs/compliance_guardrails.md` for full PII policy.

---

### 2. Media Kit URLs
Every response text that references "HERE" as a media kit link must be updated with the real URL. A broken link in an auto-sent reply is unprofessional.

**Action:** Replace all "HERE" with the actual media kit URL per talent before that talent's Phase 2 goes live.

---

### 3. Internal Routing Language in Response Text
Some response texts contain internal instructions ("Move to A Initial Response", "CC Cara", "Move to Revisit"). These are **action instructions**, not email reply text.

**Required action:** Any response row that describes an action (not an email to send) should be clearly written as an action instruction only. Do not mix email reply text and routing instructions in the same cell.

---

## Audit Sign-Off Checklist (Before Full Rollout)

- [ ] All 16 talent tabs exist in the Google Sheet with correct tab names (case-sensitive): `Sylvia`, `Trin`, `Sam J`, `Britt`, `Allee`, `Lizz`, `Katrina`, `Jenn`, `Angela`, `Colleen`, `Alana`, `Grayson`, `Kylika`, `Anastasiya`, `Katrina D`, `Michaela`
- [ ] All 16 tabs have exactly 2 columns: `Trigger / Scenario` (col A) and `Response / Action` (col B)
- [ ] All physical addresses removed from the sheet
- [ ] All media kit URLs updated (no "HERE" placeholders remaining)
- [ ] All action-type responses are clearly action instructions (not mixed with email text)
- [ ] Katrina dual-manager routing handled in response text
- [ ] Michaela dual-manager routing handled in response text
- [ ] KatrinaD hourly rate triggers worded clearly with rate math
- [ ] Sheet shared with the Make service account (read access only)
- [ ] Sheet ID confirmed in `config/settings.json → google_sheets.sop_matrix_sheet_id`

