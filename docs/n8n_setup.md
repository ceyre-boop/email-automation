# n8n Automation Setup

The Python backend handles all Gmail API calls, OpenAI triage/reply, and DB writes.
n8n handles **triggers and notifications only** — it calls backend API endpoints.

## Required env vars on your Render backend
```
APP_API_KEY=<your key>   # already set
APP_BASE_URL=https://email-automation-qp2v.onrender.com
```

---

## Workflow 1 — Real-time Email Processing (replaces 60s poll for instant response)

**Trigger:** Gmail node → "New Email" (watch inbox)
**For each of the 16 talents:** Create one workflow per talent Gmail account.

**Steps:**
1. Gmail Trigger node — connected to talent's Gmail, trigger on new message in INBOX
2. HTTP Request node:
   - Method: `POST`
   - URL: `{{$env.APP_BASE_URL}}/api/dashboard/process-email`
   - Headers: `x-api-key: {{$env.APP_API_KEY}}`
   - Body (JSON):
     ```json
     {
       "talent_key": "Katrina",
       "gmail_message_id": "{{$json.id}}"
     }
     ```
3. Done — triage + draft runs in under 5 seconds

**Why this beats polling:** New email → processed in ~5s instead of up to 60s.

---

## Workflow 2 — Escalation Notifications

**Trigger:** Schedule — every 5 minutes

**Steps:**
1. HTTP Request — `GET {{APP_BASE_URL}}/api/drafts?status=pending&is_escalate=true`
   - Header: `x-api-key`
2. IF node — check `{{$json.length}} > 0`
3. For each escalation → Send notification:
   - Slack: post to `#talent-escalations` channel
   - OR Email: send to manager with subject "Escalation needed: {{$json[0].talent_key}}"
   - Message: `"{{$json[0].escalate_reason}}" from {{$json[0].sender}} — Subject: {{$json[0].subject}}`

**To avoid duplicate alerts:** Add a Code node that filters to only items created in the last 6 minutes:
```js
return items.filter(i => {
  const age = Date.now() - new Date(i.json.created_at + 'Z').getTime();
  return age < 360000; // 6 minutes
});
```

---

## Workflow 3 — Daily Digest (8am)

**Trigger:** Schedule — `0 8 * * *` (8:00am, set your timezone)

**Steps:**
1. HTTP Request — `GET {{APP_BASE_URL}}/api/dashboard/report`
   - Header: `x-api-key`
2. Send Email / Slack with formatted summary:
   ```
   📬 Daily Inbox Report — {{$json.report_date}}
   
   ✅ Good Deals: {{$json.total_good}}
   ❓ Uncertain: {{$json.total_uncertain}}
   🗑️ Trash: {{$json.total_trash}}
   📋 Pending Drafts: {{$json.pending_drafts}}
   ```

---

## Workflow 4 — Health Monitor (replaces UptimeRobot)

**Trigger:** Schedule — every 5 minutes

**Steps:**
1. HTTP Request — `GET {{APP_BASE_URL}}/health`
   - Continue on error: ON
2. IF node — check `{{$json.status}} !== 'ok'` OR HTTP error
3. Send alert: "⚠️ Email automation backend is down or degraded"

---

## Workflow 5 — Token Health / Reconnect Reminders

**Trigger:** Schedule — every 30 minutes

**Steps:**
1. HTTP Request — `GET {{APP_BASE_URL}}/api/dashboard/health/tokens`
   - Header: `x-api-key`
2. Code node — filter talents needing attention:
   ```js
   return items[0].json
     .filter(t => t.consecutive_failures >= 2 || !t.active)
     .map(t => ({ json: t }));
   ```
3. IF node — check array length > 0
4. For each → Send message to manager:
   ```
   🔴 {{$json.talent_key}} Gmail connection failing ({{$json.consecutive_failures}} failures)
   Last error: {{$json.last_error}}
   Reconnect: https://email-automation-qp2v.onrender.com/auth/connect?talent_key={{$json.talent_key}}
   ```

---

## API Key Setup in n8n

In n8n: Settings → Credentials → Add new credential → HTTP Header Auth
- Name: `TABOOST API`
- Header Name: `x-api-key`
- Header Value: `<your APP_API_KEY from Render>`

Reuse this credential across all HTTP Request nodes.
