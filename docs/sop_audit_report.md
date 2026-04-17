# SOP Audit Report — Talent Inbox Automation
<!-- Generated from sheets/sop_data.json + config/settings.json -->
<!-- Run this audit with Britney BEFORE Phase 2 activation -->
<!-- Last reviewed: pre-deployment (update date when completed) -->

---

## Purpose

This report documents the current state of the SOP data extracted from Britney's Google Sheet, flags inconsistencies that need to be resolved before the auto-reply engine (Phase 2) can go live, and defines the required standard column structure that every talent tab must follow.

The auto-reply system reads the SOP sheet dynamically. If the column structure or data values are inconsistent, the system will silently produce bad replies or route emails incorrectly.

---

## Required Column Structure (All Tabs — Non-Negotiable)

Every talent tab in the Google Sheet SOP matrix **must** have exactly these six columns in this exact order:

| Column | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| **Header** | Offer Type | Minimum Rate | Response Template | Auto-Respond Flag | Brand Blacklist | Special Rules |
| **Format** | Standardized text (see below) | Integer (USD, no $ sign) | Full reply text with placeholders | YES / NO / ESCALATE | Comma-separated brand names or blank | Free text or blank |

**Offer Type valid values (column A):** Sponsored Post, Story, UGC, Affiliate, Event Appearance, Other  
— Do not use any other values. The Make automation matches on these exact strings (case-sensitive).

**Auto-Respond Flag valid values (column D):** YES, NO, ESCALATE  
— YES = system auto-sends. NO = system holds for human. ESCALATE = system flags and includes in digest.

---

## Talent-by-Talent Status

### Sylvia Van Hoeven
- **Manager:** Cara | **Category:** Beauty | **Min Rate:** $1,000/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Confirm media kit URL placeholder in templates (currently shows "HERE" — needs real URL)
  - [ ] Verify `Minimum Rate` column in her Google Sheet tab reflects $1,000 (not $2,000 standard rate)
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII — see compliance_guardrails.md)
  - [ ] Confirm `Auto-Respond Flag` for Sponsored Post, Story, UGC rows is set to YES
  - [ ] Set commission-only rule: route to `Commission Only` folder if product is beauty/fashion; Score 1 otherwise
  - [ ] Set bundle inquiry offer type to `Sponsored Post` with Special Rules noting bundle pricing

---

### Trinity Blair
- **Manager:** Chenni | **Category:** Lifestyle | **Min Rate:** $2,000/video
- **SOP Rules Found:** 16
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Confirm media kit URL placeholder (shows "HERE")
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Commission-only rule: delete immediately (no commission collabs for Trin) — set Auto-Respond Flag to NO, Special Rules: "Commission-only offers — send Score 1 (trash). Do not reply."
  - [ ] Fan mail rule: set offer type to `Other`, Auto-Respond Flag: `NO`, Special Rules: "Move to Fan Mail folder. Do not reply."
  - [ ] Podcast Feature is listed as an offer type in SOP — add as a row in her tab using `Other` as the standardized offer type, note podcast rate ($1,500) in Special Rules
  - [ ] Dual escalation threshold: brands offering ≥ $2,000 → CC Chenni. Add to Special Rules.

---

### Sam Jones
- **Manager:** Cara | **Category:** Home/Living | **Min Rate:** $700/video
- **SOP Rules Found:** 16
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Confirm media kit URL placeholder (shows "HERE" — Sam's tab does not have a media kit link in SOP data)
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] UGC rate ($1,500) is higher than standard rate ($900) — ensure the UGC row in her tab has Minimum Rate = 1500
  - [ ] Commission-only rule: route to `Commission Only` if product is good; Score 1 otherwise
  - [ ] Confirm bundle pricing thresholds match: brand must agree to ≥ $500/video for bundle CC to Cara

---

### Brittanie Hammer
- **Manager:** Chenni | **Category:** Home/Living | **Min Rate:** $900/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm TikTok Creator of the Year 2025 mention in templates is still accurate / approved
  - [ ] Commission-only rule: route if home product, Score 1 if not
  - [ ] Verify UGC rate ($1,000 per SOP) is correctly entered in Minimum Rate column

---

### Allee Baray
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $650/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Multiple account tiers present.
- **Issues to resolve before Phase 2:**
  - [ ] **Multi-account complexity:** SOP lists 3 TikTok account tiers ($850 main / $700 2nd / $500 3rd). The system cannot auto-select account tier — set Auto-Respond Flag to ESCALATE for Sponsored Post unless brand specifies account. Add to Special Rules: "If brand has not specified which TikTok account, include all three rate tiers in reply and ask brand to specify."
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] UGC rate ($800) should be in Minimum Rate for UGC row
  - [ ] Commission-only rule: route if product is good fashion content; Score 1 otherwise

---

### Lizz Freixas
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $600/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm TikTok rate ($750) vs minimum rate ($600) — the Minimum Rate column should reflect $600 (the floor); $750 is the standard rate. Use Special Rules to note standard rate.
  - [ ] UGC rate ($900) — enter in Minimum Rate for UGC row
  - [ ] Commission-only: route if fashion product is good; Score 1 otherwise

---

### Katrina
- **Manager:** Chenni (default) | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 15
- **Status:** ⚠️ SOP data extracted. **Dual manager escalation logic requires special handling.**
- **Issues to resolve before Phase 2:**
  - [ ] **Dual manager rule:** Offers > $650 → Cara. Offers ≤ $650 → Chenni. This cannot be expressed as a single Auto-Respond Flag. Set the Special Rules column to: "Offers above $650: CC Cara. Offers at or below $650: CC Chenni." Set Auto-Respond Flag = ESCALATE until dual-path logic is confirmed working in Make.
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Verify media kit URL
  - [ ] Commission-only: route if fashion product is good

---

### Jenn Lyles
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Verify media kit URL placeholder
  - [ ] Commission-only rule defined

---

### Angela Callisto
- **Manager:** Chenni | **Category:** Fashion | **Min Rate:** $600/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm all SOP responses have been updated with Angela's current rates
  - [ ] Commission-only rule defined

---

### Colleen Fusco
- **Manager:** Cara | **Category:** Beauty | **Min Rate:** $800/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Verify media kit URL
  - [ ] Commission-only rule: route if beauty product; Score 1 otherwise

---

### Alana Calviello
- **Manager:** Nicole | **Category:** Fashion | **Min Rate:** $400/video
- **SOP Rules Found:** 14
- **Status:** ✅ SOP data extracted.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm commission-only handling is defined (not present in extracted data)
  - [ ] Confirm Auto-Respond Flag values for each offer type row

---

### Grayson Finks
- **Manager:** Nicole | **Category:** Fashion | **Min Rate:** $300/video
- **SOP Rules Found:** 14
- **Status:** ✅ SOP data extracted.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm commission-only handling (not present in extracted data)
  - [ ] Confirm Auto-Respond Flag values for all rows

---

### Kylika Miller
- **Manager:** Nicole | **Category:** Beauty | **Min Rate:** $400/video
- **SOP Rules Found:** 14
- **Status:** ✅ SOP data extracted.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm commission-only handling
  - [ ] Confirm Auto-Respond Flag values for all rows

---

### Anastasiya
- **Manager:** Cara | **Category:** Fashion/Beauty | **Min Rate:** $600/video
- **SOP Rules Found:** 15
- **Status:** ✅ SOP data extracted. Rates and templates present.
- **Issues to resolve before Phase 2:**
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm media kit URL
  - [ ] Commission-only rule defined

---

### Katrina D
- **Manager:** Cara | **Category:** Fashion (livestream) | **Min Rate:** $150/hr (standard: $300/hr)
- **SOP Rules Found:** 13
- **Status:** ⚠️ SOP data extracted. **Hourly rate model requires special triage handling.**
- **Issues to resolve before Phase 2:**
  - [ ] **Rate unit is per-hour, not per-video.** Triage prompt must interpret "per hour" offers correctly. Brands may quote $500 for a 2-hour session — that is $250/hr, which is above minimum. Add to Special Rules: "Rate is per hour for livestreams. Standard rate is $300/hr. Minimum is $150/hr. If brand quotes a flat fee, divide by expected hours to determine hourly rate."
  - [ ] **Multi-hour bundle detection:** Some brands will quote multi-hour packages. Triage should flag these as Score 2 for human review unless the math clearly exceeds the minimum.
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Confirm Auto-Respond Flag for livestream-specific offer types

---

### Michaela
- **Manager:** Cara (default) | **Category:** Fashion/Beauty | **Min Rate:** $3,500/video
- **SOP Rules Found:** 16
- **Status:** ⚠️ SOP data extracted. **Dual manager escalation + high-value thresholds require special handling.**
- **Issues to resolve before Phase 2:**
  - [ ] **Dual manager rule:** Offers > $4,000 → Cara. Offers ≤ $4,000 → Chenni. Offers below $1,000 → skip directly to Revisit (Score 1 behavior). Set Special Rules to reflect this. Set Auto-Respond Flag = ESCALATE for all Sponsored Post rows until dual-path logic is confirmed.
  - [ ] Address in SOP data (PR response) — **remove from Google Sheet tab** (PII)
  - [ ] Verify media kit URL
  - [ ] $3,500 minimum rate is the highest in the roster — ensure triage prompt receives this value correctly

---

## Cross-Talent Issues

### 1. Physical Addresses in SOP Data (PII — ACTION REQUIRED)
The SOP data (and likely the Google Sheet) contains physical mailing addresses for PR requests for most talents. These are used in PR inquiry replies.

**Risk:** If the Google Sheet is broadly shared or if the master log logs the full reply text, these addresses will be stored in plaintext in a database accessible to Make and OpenAI.

**Required action before Phase 2:**
- Remove all physical addresses from the SOP matrix Google Sheet.
- Store addresses in a separate, access-restricted document (not shared with Make or OpenAI).
- Replace address rows with: `PR Request — see talent PR doc [LINK]` and have the human supervisor handle all PR replies manually.
- Set `Auto-Respond Flag = NO` for all PR Request rows.

See `docs/compliance_guardrails.md` for full PII policy.

---

### 2. Media Kit URLs
Every template that references "HERE" as a media kit link must be updated with the real URL before Phase 2. A broken link in an auto-sent reply is a professional embarrassment.

**Action:** Replace all "HERE" placeholders with the actual media kit URL for each talent in her Google Sheet tab. If media kits are not ready, set those offer type rows to `Auto-Respond Flag = NO` temporarily.

---

### 3. Commission-Only Handling
Commission-only offers are handled inconsistently across talent tabs. Some say "move to Commission Only" folder, others say "delete." The automated system cannot move emails to custom folders without additional Make configuration.

**Recommended standardization:**
- If talent accepts commission collabs: `Auto-Respond Flag = ESCALATE`, Special Rules: "Commission-only — human reviews product before deciding."
- If talent does not accept commission collabs: `Auto-Respond Flag = NO`, Special Rules: "Commission-only — Score 1 (archive, no reply)."

---

### 4. Bundle Pricing
Bundle pricing is present in most SOP response templates as free text. The AI reply prompt can use this, but the Make automation cannot easily match "bundle inquiry" as an offer type.

**Recommended standardization:** Add a `Sponsored Post` row variant in Special Rules that says "If brand asks for bundle pricing, include the bundle pricing table from this note: [bundle pricing text]."

---

### 5. Escalation Language Consistency
Different talents use different terms: "CC Cara", "Looping in management", "Move to Ongoing TBC". The AI reply prompt may reproduce this internal language verbatim.

**Required action:** Review all response templates and remove any internal routing instructions (e.g. "CC Cara", "Move to Revisit") from the `Response Template` column. Keep internal instructions only in the `Special Rules` column, which is fed to the AI as context, not as direct reply content. The AI prompt already handles this distinction — but only if the columns are correctly separated.

---

## Audit Sign-Off Checklist

Complete with Britney before Phase 2 activation:

- [ ] All 16 talent tabs exist in the Google Sheet with the correct tab names (case-sensitive)
- [ ] All 16 tabs follow the 6-column structure (A–F as defined above)
- [ ] All physical addresses removed from the sheet
- [ ] All media kit URLs updated (no "HERE" placeholders remaining)
- [ ] All Auto-Respond Flag values are YES, NO, or ESCALATE (no blanks)
- [ ] Commission-only rows standardized to ESCALATE or NO across all tabs
- [ ] Katrina dual-manager escalation rule captured in Special Rules column
- [ ] Michaela dual-manager escalation rule captured in Special Rules column
- [ ] KatrinaD hourly rate interpretation note in Special Rules column
- [ ] All internal routing instructions (CC Cara, Move to Revisit) removed from Response Template column
- [ ] Britney has reviewed and approved each talent's Response Template copy
- [ ] Sheet shared with the Make service account (read access only)
- [ ] Sheet ID pasted into `config/settings.json → google_sheets.sop_matrix_sheet_id`
