# Compliance Guardrails — Talent Inbox Automation
<!-- This document defines the data handling, privacy, and safety rules for this system. -->
<!-- Review with the agency owner and legal counsel before full deployment. -->

---

## Overview

This system processes email communications on behalf of real people (the talent) and interacts with external parties (brands). Several legal and ethical obligations apply. This document defines the rules that must be enforced — in the system architecture, in the Make automation, and by the supervisor.

This is not legal advice. If the agency operates across jurisdictions (EU talent or brand contacts, California residents, etc.), consult with a qualified attorney before deployment.

---

## Section 1 — Personally Identifiable Information (PII)

### 1.1 What Counts as PII in This System

The following data categories are present in this system and require controlled handling:

| Data | Where It Appears | Classification |
|---|---|---|
| Talent physical addresses | SOP data (Britney's sheet) | **High sensitivity — must be removed** |
| Sender email addresses | Master log, Make modules | Moderate — handle per retention policy |
| Brand contact names | Email body, possibly in reply | Low — business contact data |
| Sender full names | Email body | Moderate if personal (not brand) |
| Talent Gmail content | Processed by Make + OpenAI | High — see Section 1.3 |

### 1.2 Physical Addresses — Immediate Action Required

**Before Phase 2 goes live:** All physical mailing addresses for talent must be removed from Britney's SOP Google Sheet.

Addresses are used in PR gifting replies. The current SOP data includes home and business addresses for most talent. If these addresses are stored in a Google Sheet shared with Make and passed to the OpenAI API, they may:
- Be stored in OpenAI training data (check your API contract)
- Be accessible to Make platform staff
- Be exposed if either platform is breached

**Required action:**
- Remove all addresses from the Response Template column in the SOP sheet.
- Create a separate, access-restricted document (shared only with the supervisor) that lists PR shipping addresses.
- Change the PR Request row for each talent in the SOP sheet to: Auto-Respond Flag = `NO`. Supervisor handles PR replies manually by copying the address from the private document.
- The system will never auto-send a physical address.

### 1.3 Email Content Passing Through OpenAI

Every email processed by this system is sent to the OpenAI API for classification and/or reply drafting. This means:
- **The email body, subject, and sender information are transmitted to a third-party AI service.**
- OpenAI's API data is subject to their data processing agreement and privacy policy.
- As of this writing, OpenAI's API does NOT use API data for model training by default — but confirm this in your current contract.

**Required action:**
- Review the OpenAI API data usage policy before deployment.
- If the agency handles EU talent or brand contacts under GDPR, ensure a Data Processing Addendum (DPA) is in place with OpenAI.
- Do not pass unnecessary PII to the AI. The triage prompt uses email subject + body + sender domain. Do not add talent personal details (home address, phone number, personal notes) to the triage or reply prompts.

### 1.4 Minimization Rules for AI Prompts

The following information must NEVER be included in the triage or reply prompts sent to OpenAI:

- Physical addresses
- Phone numbers
- Social Security, tax ID, or bank account numbers
- Talent health information
- Talent personal relationship or family information
- Any information the talent has not authorized for business use

The current `prompts/triage.md` and `prompts/reply.md` comply with this rule. Any future prompt modifications must be reviewed against this list before deployment.

---

## Section 2 — Data Retention

### 2.1 Master Log Retention

The master log Google Sheet will accumulate rows indefinitely unless a retention policy is enforced.

**Recommended retention window:** 365 days of activity data in the live sheet.

**Implementation:**
- After 12 months of operation, archive rows older than 365 days to a separate "Archive" Google Sheet.
- The Make weekly digest scenario reads the last 7 days only — it is not affected by archival.
- The Make daily digest reads the last 24 hours only — it is not affected by archival.

**Note on GDPR/CCPA:** If the agency receives emails from people in the EU or California, the sender email address in the master log may be considered personal data. Retaining it for longer than necessary (beyond 12 months) may create a compliance obligation. Review with counsel.

### 2.2 Error Log Retention

The Error Log tab (created in Phase 4) should be reviewed monthly. Entries older than 90 days can be archived or deleted — they are operational logs, not business records.

### 2.3 Gmail Trash

Score 1 (trash) emails are moved to Gmail Trash, not permanently deleted. Gmail automatically permanently deletes items in Trash after 30 days. This is the default Gmail behavior — no action required.

If a talent or brand requests deletion of a specific email before the 30-day window: go to the talent's Gmail, find the email in Trash, and permanently delete it manually.

---

## Section 3 — Outbound Reply Safety Rules

### 3.1 Replies the System Must Never Send

The following types of content must never appear in an auto-generated reply:

| Prohibited Content | Risk |
|---|---|
| Physical mailing addresses | Privacy / safety for talent |
| Phone numbers | Privacy for talent |
| Confirmed commitments to specific dates | Creates legal obligation without manager approval |
| Accepted rates without manager CC | Creates a binding negotiation position |
| Trademark or branded content owned by third parties | Copyright infringement |
| Discriminatory, offensive, or inappropriate language | Reputation and legal risk |
| False claims about follower counts, GMV, or performance | FTC / advertising standards |

### 3.2 How the System Enforces These Rules

1. **Auto-Respond Flag = ESCALATE:** Any offer type that could lead to a commitment (Event Appearance, Affiliate, large Sponsored Post deals) is set to ESCALATE in the SOP tab — a human approves before a reply is drafted.

2. **Reply prompt instruction:** `prompts/reply.md` explicitly instructs the AI to "Do NOT invent specifics (dates, deliverable counts, URLs) that are not in the original email."

3. **15-minute send delay:** Gives the supervisor a cancellation window for any reply they catch before it sends.

4. **PII redaction (to be implemented in Phase 2):** Before passing the SOP template to the reply AI, the Make scenario should run a regex check to detect and redact address patterns. See `config/confidence_policy.json → pii_in_replies`.

5. **QA period:** 5 business days of supervisor-reviewed replies before autonomous operation.

### 3.3 FTC Disclosure

Under FTC guidelines, sponsored content must be disclosed. The AI-generated reply templates do NOT draft the actual sponsored posts — they are initial inquiry responses. Sponsored post content and disclosures are handled separately by the talent.

However, if the system ever expands to include drafting actual post captions or scripts (not currently in scope), FTC disclosure rules must be added to the reply prompt.

---

## Section 4 — Authorized Use

### 4.1 Who Can Access the System

| Role | Access Level | What They Can Do |
|---|---|---|
| Supervisor | Full access | View all Make scenarios, master log, error log, SOP sheet |
| Automation admin | Technical access | Edit Make scenarios, update config, run scripts |
| Britney | SOP sheet only | Edit SOP matrix. Cannot view master log or Make scenarios. |
| Talent | Gmail only | Access their own inbox. Cannot view the master log or other inboxes. |
| Managers (Cara, Chenni, Nicole) | No system access | Receive CC'd emails from Phase 2 for high-value deals only |

### 4.2 API Key Security

- The OpenAI API key must be stored in Make as a connection — never hardcoded in scenario JSON files.
- The API key must have a monthly spending cap set in the OpenAI dashboard.
- Rotate the API key every 90 days or immediately if it is suspected to be compromised.
- Do not share the API key via email, Slack, or any unencrypted channel.
- The `config/settings.json` file in this repository does NOT contain the API key — only configuration metadata.

### 4.3 Google Sheets Access

- The SOP matrix sheet should be shared with the Google account used to generate `GOOGLE_SHEETS_REFRESH_TOKEN`, with **Viewer** access only. If you are intentionally using the legacy service-account path, share it with that service account instead.
- The master log sheet must be shared with the Google account used to generate `GOOGLE_SHEETS_REFRESH_TOKEN`, with **Editor** access so the app can append rows. If you are intentionally using the legacy service-account path, share it with that service account instead.
- Do not share either sheet publicly or "Anyone with the link."
- If a talent or brand contact accidentally gains access to the master log, revoke their access and review what data was exposed.

---

## Section 5 — CAN-SPAM Compliance

The system sends replies on behalf of the talent's Gmail account. These are replies to inbound business inquiries — they are not marketing emails. CAN-SPAM requirements for marketing emails do not strictly apply here.

However, as a best practice:
- Replies should always be responsive to the original email — the system only replies to emails it receives, never sends cold outreach.
- If a brand asks to be removed from the talent's reply list, honor the request immediately. Add the brand's domain to the `Brand Blacklist` column in the talent's SOP tab row, and set a special rule to Score 1 any future emails from that domain.

---

## Section 6 — Audit Trail Requirements

Every automated action taken by this system must be logged in the master log with:

| Field | Required | Purpose |
|---|---|---|
| Timestamp | ✅ | Exact time of action (UTC) |
| Talent Name | ✅ | Which inbox was affected |
| Sender Email | ✅ | Who sent the original email |
| AI Score | ✅ | What the system decided |
| Action Taken | ✅ | What happened (archived, replied, flagged, escalated) |
| Reply Sent | ✅ | Boolean — true if a reply was sent |
| Gmail Thread Link | ✅ | Direct link to recover the original email |
| Notes | ✅ | AI reason, error message, or manual note |

This log must be retained per the retention policy in Section 2.1. It provides a complete, auditable record of every decision the system made — essential if a brand disputes a reply or if a talent claims the system sent an incorrect response.

---

## Section 7 — Incident Response

If there is a compliance incident (PII leak, bad reply sent to a high-profile brand, unauthorized system access), follow this process:

1. **Contain:** Pause all Make scenarios immediately (see `config/rollout_controls.json → kill_switches → pause_all_inboxes`).
2. **Assess:** Determine what data was exposed or what action was taken incorrectly.
3. **Notify:** If personal data was exposed, notify affected parties per applicable law. If a brand received a bad reply, contact them directly (see `docs/failure_playbooks.md → Playbook 4`).
4. **Fix:** Address the root cause before reactivating the system.
5. **Document:** Record the incident, response, and fix in a separate incident log (outside the master log).
6. **Review:** After resolving, review whether any compliance guardrail needs to be updated.
