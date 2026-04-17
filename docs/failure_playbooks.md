# Failure Playbooks — Talent Inbox Automation
<!-- Keep this document open during go-live week. Every failure has a known fix. -->

---

## How to Use This Document

When something breaks, find the matching failure type below, follow the steps in order, and confirm the fix before moving on. All failures have a recovery path — nothing is lost.

**Who should read this:** Whoever is responsible for monitoring the daily digest and error alerts.

**How failures are reported:** You will receive an immediate error alert email (if Phase 4 is active) and/or the affected inbox will go missing from the daily digest summary.

---

## Playbook 1 — Gmail OAuth Token Expired

### What Happened
A talent's Gmail inbox disconnected from Make. The Make scenario for that inbox stopped firing. New emails are arriving in the inbox but are not being processed.

### Symptoms
- Error alert email with subject: `ALERT: [Talent] inbox automation failed — OAuth`
- Talent's inbox missing from the daily digest
- Make scenario history shows "Connection error" or "OAuth token expired"

### Recovery Steps (under 5 minutes)

1. Log in to Make (make.com).
2. Go to **Connections** (left sidebar).
3. Find the connection named `Gmail - [TalentFirstName]` (e.g. `Gmail - Sylvia`).
4. Click the connection → click **Re-authorize**.
5. A Google OAuth popup will open. Sign in as the talent's Gmail account.
6. Click **Allow** to grant Make access.
7. The connection will show a green checkmark.
8. Go back to **Scenarios** → find the affected talent's Phase 1 scenario.
9. The scenario should resume automatically. If it does not, click **Run once** to process any queued emails.
10. Check the master log — emails received during the outage will now be processed.

### Notes
- Emails that arrived during the outage are NOT lost. Gmail holds them. The Make scenario will process them when it resumes.
- OAuth tokens typically expire every 60-90 days. You will see this playbook approximately every 2-3 months per inbox.
- To reduce reconnection frequency: use a Google Workspace service account instead of individual OAuth (more advanced — set up in Phase 2 if needed).

---

## Playbook 2 — OpenAI API Timeout or Downtime

### What Happened
The OpenAI API did not respond in time, or OpenAI experienced an outage. Affected emails were not scored and were logged as `pending — api error` in the master log.

### Symptoms
- Error alert email with error type "OpenAI API error"
- Emails in master log with Action Taken = `pending — api error`
- Make scenario execution history shows a timeout or 500/503 error on the OpenAI module

### Recovery Steps

**If OpenAI is down (check status.openai.com):**
1. Wait for OpenAI to restore service (most outages resolve within 30-60 minutes).
2. No action needed — pending emails will be retried by the next scheduled Phase 2 run.
3. Once OpenAI is back, confirm the Phase 1 and Phase 2 scenarios are active in Make.
4. Check the master log for rows with Action Taken = `pending — api error` and confirm they are being processed.

**If the timeout was a one-off (not a full outage):**
1. Go to Make → Scenarios → find the affected scenario.
2. Click **Run once** to manually trigger a re-run.
3. The scenario will retry the OpenAI call for any pending emails.

**If the API call is consistently failing:**
1. Check your OpenAI API key — it may have been rotated or hit its spending limit.
2. Go to platform.openai.com → API Keys → confirm the key is active.
3. Go to platform.openai.com → Usage → check if the monthly spending cap has been hit.
4. If the cap is hit: increase the limit in the OpenAI dashboard.
5. In Make: go to Connections → `OpenAI - Talent Automation` → update the API key if it changed.

### Monitoring
- Set a spending alert in the OpenAI dashboard at 80% of your monthly budget cap.
- Check platform.openai.com/usage weekly during the first month.

---

## Playbook 3 — Google Sheets Schema Drift

### What Happened
Someone modified the structure of the master log or SOP matrix Google Sheet — renamed a column, deleted a tab, or changed column order. The Make automation can no longer read or write data correctly.

### Symptoms
- Emails being logged with missing fields (blank talent name, blank score, etc.)
- Phase 2 replies going out with placeholder text (e.g. "[BRAND_NAME]" in the reply)
- Error alert email mentioning a Google Sheets error
- Make scenario history shows a "Column not found" or "Tab not found" error

### Recovery Steps — Master Log Schema Drift

1. Open the master log Google Sheet.
2. Compare the headers in row 1 to `sheets/master_log_template.csv`.
3. Required headers in exact order: `Timestamp, Talent Name, Sender Email, Sender Domain, Subject, AI Score, AI Score Label, Offer Type, Proposed Rate (USD), Action Taken, Reply Sent, Gmail Thread Link, Notes`.
4. If any header was renamed or deleted: restore it to the exact name from the template.
5. If a column was moved: the Make module maps by column name, so order doesn't matter — as long as names are exact.
6. After fixing: go to Make → Scenarios → run the affected scenario once to confirm writes succeed.

### Recovery Steps — SOP Matrix Schema Drift

1. Open Britney's SOP matrix Google Sheet.
2. For the affected talent's tab, compare column headers to `sheets/sop_matrix_template.csv`.
3. Required headers: `Offer Type, Minimum Rate, Response Template, Auto-Respond Flag, Brand Blacklist, Special Rules`.
4. The Make automation reads columns A–F positionally (not by name). If columns were inserted or reordered:
   - **Column A must be Offer Type**
   - **Column B must be Minimum Rate**
   - **Column C must be Response Template**
   - **Column D must be Auto-Respond Flag**
   - **Column E must be Brand Blacklist**
   - **Column F must be Special Rules**
5. Restore the column order if any columns were moved.
6. After fixing: trigger a test in Make to confirm the SOP data is being read correctly.

### Prevention
- Protect the header rows in both Google Sheets (right-click row → Protect range → set permissions to "Only you").
- Add a note at the top of both sheets: "Column order and names are automation-critical. Do not rename, move, or delete columns without updating the Make scenario."

---

## Playbook 4 — A Bad Reply Was Sent Before QA Sign-Off

### What Happened
An AI-generated reply went out to a brand that contained an error — wrong rate, placeholder text, confusing language, or an inappropriate tone.

### Symptoms
- You notice a bad reply in the daily digest "Replies Sent" section
- A brand responds with confusion
- You check Gmail and see a problematic outbound message in the sent folder

### Recovery Steps

**Immediate (within the hour):**
1. Go to the talent's Gmail account.
2. Find the sent email in the Sent folder.
3. Open the thread and manually compose a follow-up reply apologizing for the error. Keep it brief: "Apologies for any confusion in my last message — [correction]. Please disregard the previous reply." Send from the talent's Gmail account.
4. Log the incident in the master log: find the row for that email, update Notes: "Bad reply sent — manual correction sent at [TIME]".

**Fix the root cause:**
5. Identify what went wrong:
   - **Placeholder text in reply** (e.g. `[BRAND_NAME]`): The SOP template was not audited. Open the Google Sheet, fix the template, and confirm all placeholders are real values.
   - **Wrong rate**: The Minimum Rate column in the SOP tab has the wrong value. Correct it.
   - **Wrong tone or invented details**: Update `prompts/reply.md` to add a rule preventing this. Run `tests/reply_test_cases.json` to confirm the fix works before re-deploying.
   - **Reply sent for wrong talent**: A Gmail connection name mismatch. Check that every Phase 1 scenario has the correct Gmail connection set.
6. After fixing the root cause, run the full reply test suite (`tests/reply_test_cases.json`) before re-activating the scenario.

**Preventative:**
- Keep the 15-minute send delay active for the entire first month. Use those 15 minutes to check the master log each time a reply is queued.
- During QA (Days 24-28), the supervisor reviews 100% of outbound replies before approving autonomous operation.

---

## Playbook 5 — Make Scenario Stopped Running (Not an OAuth Issue)

### What Happened
A Make scenario went inactive or stopped executing, and it is not an OAuth or API problem.

### Possible Causes
- Make account hit its monthly operation limit (Pro plan: 10,000 operations/month)
- Scenario was manually turned off
- Make platform maintenance
- Scenario hit maxErrors (3 consecutive failures) and auto-deactivated

### Recovery Steps

1. Log in to Make.
2. Go to **Scenarios** → find the affected scenario.
3. Check the status indicator — if it shows "Off" or "Inactive":
   - Click the toggle to activate it.
4. If the scenario deactivated due to consecutive errors:
   - Click on the scenario → **History** tab → review the last 3 failed executions to identify the error.
   - Fix the root cause (see relevant playbook above).
   - Reactivate the scenario.
5. Check **Usage** in Make settings — if the operation count is near the monthly limit:
   - Review which scenarios are consuming the most operations.
   - Consider upgrading the Make plan if needed.
   - As a temporary fix: reduce the Phase 2 polling frequency from every 5 minutes to every 15 minutes.

### Monitoring
- Check Make's Usage dashboard weekly during the first month.
- At full capacity (16 inboxes, 50 emails/day), estimated monthly operations: ~10,000-15,000. Make Pro plan supports 10,000 — consider upgrading to the Teams plan if volume exceeds this.

---

## Playbook 6 — Master Log Sheet Not Updating

### What Happened
Emails are being triaged (Gmail is receiving them and Make is firing), but new rows are not appearing in the master log Google Sheet.

### Symptoms
- Emails appear to be processed (Gmail shows activity) but the log has no new rows
- Make scenario execution history shows success but no Sheets row was written
- Error in Make history on the Google Sheets module

### Recovery Steps

1. In Make → go to Connections → find `Google Sheets - Talent Automation`.
2. Check if the connection is still authorized (green checkmark).
3. If the connection shows an error: re-authorize it (same process as Gmail OAuth in Playbook 1).
4. Open the master log Google Sheet → confirm the "Master Log" tab still exists with the exact name (case-sensitive).
5. Confirm Make's service account or OAuth account still has **Editor** access to the sheet (Share settings in Google Sheets).
6. Try a manual test: go to a Phase 1 scenario in Make → Run once → send a test email to the inbox → check if the log row appears.

### If the Google Sheet hit its row limit
- Google Sheets supports up to 10 million cells per spreadsheet. At 50 emails/day across 16 inboxes, you will NOT hit this limit for years. This is not the issue.

---

## Playbook 7 — Talent Address Accidentally Included in a Reply

### What Happened
The AI-generated reply included a physical mailing address for a talent in the outgoing email text. This is a PII exposure incident.

### Immediate Response

1. **Act within 1 hour.** Go to the talent's Gmail → find the sent reply.
2. Note the brand and email address the reply was sent to.
3. Contact the brand immediately — by phone if possible, email if not — and ask them to disregard and delete the message containing the address.
4. Log the incident internally.
5. Remove the address from the SOP matrix Google Sheet immediately (see `docs/sop_audit_report.md` for the full PII audit).

### Root Cause Fix

6. The SOP template for that talent contained a physical address in the Response Template column. This must be removed.
7. Set the Auto-Respond Flag for that offer type row to `NO` temporarily until the template is corrected.
8. Review all other talent tabs for addresses in the Response Template column and remove them all.
9. Add the PII redaction step to the Phase 2 Make scenario (see `config/confidence_policy.json → pii_in_replies`).

### Prevention
- Complete the SOP audit (`docs/sop_audit_report.md`) before Phase 2 activation — addresses must be removed from all Google Sheet templates.
- PR request replies should always have Auto-Respond Flag = NO. Addresses are shared manually by the supervisor.

---

## Quick Reference — Who Owns What

| Area | Owner | Contact |
|---|---|---|
| Make scenarios and connections | Automation admin | — |
| OpenAI API key and billing | Automation admin | platform.openai.com |
| Google Sheets (SOP + log) | Britney (SOP) + Automation admin (log) | — |
| Gmail OAuth connections | Automation admin + each talent | — |
| Daily digest review | Supervisor | — |
| SOP content approval | Britney | — |

---

## Escalation Path

If none of the above playbooks resolve the issue:

1. Pause all active Make scenarios immediately (to prevent further errors or bad replies).
2. Review the Make execution history for the last 10 executions to identify the pattern.
3. Check the Error Log tab in the master log Google Sheet.
4. Contact Make support if the issue appears to be a platform bug (support.make.com).
5. Contact OpenAI support if the API is returning unexpected responses.
6. Resume scenarios one at a time after the fix is confirmed — do not resume all at once.
