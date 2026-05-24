# Email Automation — Full Intelligence Roadmap

> Goal: replace a full human inbox management team with a system that is faster, more consistent,
> and smarter than any team working around the clock. Every feature below moves toward that end state.
>
> Current state as of 2026-05-22: triage + canned response engine, 16 talent inboxes, poller stable.
> Build this after one week of clean data proves the foundation holds.

---

## Phase 1 — Negotiation Engine (Week 2)
*Closes the biggest gap: right now below-minimum offers just get flagged. A good rep counters.*

**What it does:**
- When an offer comes in below the talent's minimum, the system generates a counter-offer reply instead of escalating to human review.
- Counter includes the talent's actual minimum, a one-sentence value justification pulled from the SOP, and a soft closing line.
- If the brand already countered once (thread has a prior exchange), escalate to human — negotiation beyond round 1 is relationship work.

**How it works:**
- Triage returns `score=3` + `proposed_rate < minimum_rate` → new branch in poller: `_counter_offer` path.
- New prompt template: `prompts/counter.md` — same system/user structure as `reply.md`.
- New scenario added to each talent's SOP section: `### Scenario: Counter Offer` with the approved counter language and rate formula.
- Deterministic first pass: if proposed rate is known, compute counter = minimum_rate and fill the template. No GPT needed for the number — only for the surrounding language.

**Files touched:** `prompts/counter.md` (new), `services/reply.py`, `services/poller.py`, `sheets/sop.md` (new scenario per talent).

---

## Phase 2 — Brand Memory (Week 2–3)
*Makes the system smarter than any human: it never forgets a brand.*

**What it does:**
- Every brand that emails any talent is stored in a `brands` table: domain, display name, offer history (rate offered, outcome, date), last contact.
- When the same brand emails again, the system surfaces prior interaction context to GPT in the reply prompt: "This brand offered $400 to Katrina on 2026-04-10. She replied with rates. No follow-up."
- If a brand previously closed a deal (status = `sent` with manager approval), the system flags it as a known partner and the reply warms up accordingly.
- Cross-talent awareness: if a brand emails Wesley after already working with Katrina, the system knows they're an active agency client.

**Schema:**
```sql
CREATE TABLE brands (
  id SERIAL PRIMARY KEY,
  domain TEXT UNIQUE NOT NULL,
  display_name TEXT,
  first_seen TIMESTAMPTZ DEFAULT NOW(),
  last_seen TIMESTAMPTZ,
  total_contacts INT DEFAULT 0,
  known_partner BOOLEAN DEFAULT FALSE
);

CREATE TABLE brand_interactions (
  id SERIAL PRIMARY KEY,
  brand_domain TEXT REFERENCES brands(domain),
  talent_key TEXT NOT NULL,
  gmail_message_id TEXT,
  offer_type TEXT,
  proposed_rate_usd FLOAT,
  outcome TEXT,  -- 'replied', 'closed', 'ghosted', 'escalated'
  contacted_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Files touched:** `models/db.py`, `services/brand_memory.py` (new), `services/triage.py` (brand lookup pre-enrichment), `services/reply.py` (context injection), `routers/dashboard.py` (brand history endpoint for dashboard UI).

---

## Phase 3 — Follow-Up Engine (Week 3)
*The thing human teams forget most. The system never does.*

**What it does:**
- After a rates reply is sent (draft approved + sent), the system waits N days (configurable per talent, default 5).
- If no reply arrives from the brand within that window, one follow-up is sent automatically.
- Follow-up is a short, warm nudge — not a re-pitch. Exact text in the SOP per talent.
- If the brand still doesn't reply after the follow-up, the interaction is marked `ghosted` in brand memory and no further contact is made.
- One follow-up maximum. Never spam.

**How it works:**
- New `follow_ups` table: `draft_id`, `scheduled_at`, `sent_at`, `status`.
- APScheduler job runs every 10 minutes, checks for follow-ups due.
- Before sending, checks Gmail thread for any inbound reply — if the brand came back, cancel the follow-up and flag the thread for human review.
- Follow-up prompt: `prompts/followup.md`. Same structure. Very short approved responses per talent.

**Files touched:** `models/db.py`, `services/followup.py` (new), `routers/cron.py` (new scheduler job), `sheets/sop.md` (follow-up scenario per talent), `prompts/followup.md` (new).

---

## Phase 4 — Deal Intelligence Dashboard (Week 4)
*Turns the system into a revenue analytics platform, not just an inbox filter.*

**What it does:**
- Weekly digest email to Colin: emails processed, drafts sent, reply rate, estimated pipeline value (sum of proposed rates on active threads), brands that ghosted.
- Dashboard tab: Funnel view — Received → Scored 3 → Draft Sent → Approved → Sent → Closed.
- Per-talent close rate and average deal size tracked over time.
- Brand close rate: which brands convert vs. which ones waste time. Surface the time-wasters.
- Flagged: any talent whose inbox has been silent for 48+ hours (possible OAuth disconnect).

**Files touched:** `routers/analytics.py` (extend existing), `static/dashboard.html` (new Funnel tab + brand table), `services/digest.py` (new — weekly email via Gmail API to colin@taboost.me).

---

## Phase 5 — Auto-Send Mode (Week 5–6)
*The end state. Removes the human approval step for high-confidence drafts.*

**What it does:**
- Each draft gets a confidence score: triage score 3 + deterministic SOP match (not GPT-generated) + brand not flagged as risky = HIGH confidence.
- HIGH confidence drafts are sent automatically after a 15-minute hold window (gives managers time to cancel if they see something wrong in the dashboard).
- LOW confidence drafts (GPT-generated response, new brand, unusual offer type) stay in the approval queue as today.
- Setting per talent: `auto_send: true/false` in `settings.json`. Off by default. Colin flips it per talent as trust is established.
- Full audit trail: every auto-sent reply is logged with the confidence score and the hold window timestamp.

**Safeguards:**
- Auto-send never fires for escalated drafts.
- Auto-send never fires for brands flagged as risky in brand memory.
- Auto-send never fires if the thread has any prior sent activity.
- 15-minute cancel window is surfaced prominently in the dashboard (countdown timer per pending auto-send).
- If the system sends more than 3 auto-sends in 10 minutes for a single talent, it pauses and alerts Colin — rate spike guard.

**Files touched:** `services/poller.py`, `services/autosend.py` (new), `routers/cron.py`, `models/db.py` (confidence_score column on drafts), `static/dashboard.html` (countdown UI, auto-send toggle per talent).

---

## Phase 6 — Scenario Intelligence (Ongoing)
*Makes the response layer smarter than canned text.*

**What it does:**
- PR/gifting emails: detect product gifting offers (no cash) and respond with a gifting policy reply per talent rather than rates. Currently these get escalated.
- Long-form negotiation: if a brand explicitly says "our budget is $X, can you do that?" and X is within 20% of minimum, generate a custom reply that meets them partway (e.g. reduce deliverables rather than rate).
- Event invite smart routing: instead of silently leaving in INBOX, generate a classification summary and ping the manager via email so it doesn't get lost.
- Seasonal context: if it's Q4 (Oct–Dec), flag that talent calendars fill fast and add urgency language to replies.

**Files touched:** `services/triage.py` (new offer type detection), `services/reply.py` (new scenario branches), `prompts/gifting.md` (new), `sheets/sop.md` (gifting scenario per talent).

---

## Stability Targets (Before Any Phase Starts)
These are non-negotiable gates. Do not build Phase 1 until all pass.

- [ ] Zero health alarms for 7 consecutive days
- [ ] All 16 talent inboxes connected and polling without OAuth errors
- [ ] OpenAI billing upgraded (RPD limit resolved — see memory note 2026-05-20)
- [ ] Triage fallback rate < 5% (currently unclear — need one clean week of data)
- [ ] Zero stuck emails (silent-skip loop fix confirmed holding)

---

## What This Replaces

| Human team task | System capability after Phase |
|---|---|
| Read every email | Today (poller) |
| Score / triage | Today (GPT triage) |
| Send initial rates reply | Today (SOP engine) |
| Counter-offer below-minimum | Phase 1 |
| Remember past brand interactions | Phase 2 |
| Follow up on ghosted leads | Phase 3 |
| Report pipeline to management | Phase 4 |
| Send approved replies without human touch | Phase 5 |
| Handle gifting, PR, edge cases | Phase 6 |

A team of 4 working 8 hours a day misses emails, forgets follow-ups, inconsistently applies rates, and can't track brand history across 16 inboxes simultaneously. This system does all of it in under 60 seconds per email, 24/7, with a full audit trail.

---

*Written 2026-05-22. Build starts after stability gates pass. Show Marco after Phase 3 is live.*
