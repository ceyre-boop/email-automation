# Handoff — brand inquiry export (aggregate only)
Date: 2026-10-20. For Claude Code, run locally against the production database.

## Goal in one line
Turn the inbound brand email log into a weekly, aggregate, de-identified count of brand demand — and nothing else.

## Hard rule: what never leaves this repo
The inbox holds deal terms belonging to TABOOST and rates belonging to individual creators. The export carries counts, never content.

**Never export, in any column, hashed or otherwise:**
| Field | Table |
|---|---|
| `proposed_rate` | processed_emails, inbox_emails |
| `rate_detected` | triage_audit |
| `body_text`, `snippet` | processed_emails, inbox_emails |
| `subject` | all |
| `sender`, `to_address`, `email` | all |
| `triage_reason`, `reasoning` | processed_emails, triage_audit |
| `talent_key` in raw form | all — SHA-256 with a salt held outside the export |

**Export:** normalized brand name, ISO week, counts, `offer_type`, and the aggregate score columns below. That is the whole surface.

Do not commit the output CSVs to any repo that is or could become public. Write them to a local path, add that path to `.gitignore`, and pass only the sha256 to Edge.

## Score filter — use the real scale
`config/confidence_policy.json` defines `global_score_routing`: **1 = trash, 2 = uncertain, 3 = respond.**

- Primary signal: `score = 3` only.
- `score = 2` goes in its own column, never merged into the primary count.
- `score = 1` is excluded entirely.

## The dedupe decision — this is the measurement trap
A brand that emails 40 creators in one blast is **one campaign**, not 40 inquiries. Counting raw rows turns the index into a measure of roster size.

`thread_id` does not solve this: a blast creates separate threads in separate inboxes. So:

- **`campaigns`** = count of distinct (`brand_norm`, ISO week). One brand, one week, one campaign.
- **`creators_contacted`** = count of distinct hashed `talent_key` for that (brand, week).

These are two different variables, not one. `campaigns` measures whether a brand is active. `creators_contacted` measures how hard they are pushing. Export both; let Edge decide which predicts.

## Date field
Use **`email_date`** — when the brand acted, which is the upstream moment. It is nullable, so fall back to `first_seen_at`, then `processed_at`, and **emit a per-week count of how many rows used each fallback.** If fallback rows exceed 10% in a week, flag that week.

## Brand normalization
`processed_emails.brand_name` is already AI-extracted, so it is inconsistent.

1. Lowercase, trim, collapse whitespace, strip legal suffixes (`inc`, `llc`, `ltd`, `co`, `gmbh`).
2. Hand-maintained alias map at `brand_aliases.csv` (`raw,normalized`) committed to this repo — aliases are not sensitive.
3. **Never fuzzy-match silently.** Anything unmatched above a similarity threshold goes to `brand_unmatched.csv` for human review and is excluded from the export until mapped. A silent merge of two brands is an untraceable error in every downstream number.

## Two outputs

**`brand_inquiry_weekly.csv`** — one row per (brand_norm, iso_week)
```
brand_norm, iso_week, week_start_date, campaigns, creators_contacted,
inquiries_score3, inquiries_score2, offer_type_mode,
mean_urgency, mean_sentiment, mean_risk,
date_source_email_date, date_source_fallback
```

**`coverage_weekly.csv`** — one row per iso_week. This is the denominator and the stationarity audit, and the study is worthless without it.
```
iso_week, week_start_date, active_talents, talents_polled_ok,
total_rows_processed, rows_score3, distinct_brands,
sop_version_active, triage_models_used, config_changed_flag
```
- `active_talents` from `talents.active = true`.
- `talents_polled_ok` = talents with `last_poll_at` in that week and `consecutive_failures = 0`. A talent whose inbox stopped syncing silently removes brands from the index.
- `sop_version_active` from `sop_versions` (`is_active`, `uploaded_at`), `triage_models_used` from `triage_audit.model_used`.

## Read-only. No exceptions.
This export **reads** production and writes nothing to it. No `ALTER TABLE`, no new columns, no `create_tables()`, no touching `SKIP_MIGRATIONS`. Production deliberately runs with migrations off and the repo has an outage in its log from a column added in code and never created in Postgres. An analytics export has no business anywhere near that.

Other agents work this repo concurrently — `git fetch && git log HEAD..origin/main --oneline` before any push.

## Three documented breaks in the history — find them, report them, don't smooth them
The repo's own incident log and roadmap name three periods where the data generating process changed. Any of them, unflagged, would read as a change in brand demand. Detect each and put the affected weeks in `coverage_weekly.csv`.

1. **Talent onboarding ramp.** Only one inbox was connected as of 2026-04-24; the roster grew toward ~20 after that. Early weeks have almost no observation surface. The honest usable range probably starts well after the first email in the table — let `active_talents` and `talents_polled_ok` say where, and report the first week where the roster was stable.
2. **Alias routing outage, 2026-08-30.** Brands BCC the talent alias, so `to_address` resolved to `None` and mail went unrouted — 220 unrouted against 1 drafted in a 20-minute sample, affecting the ~15 talents on the shared inbox. Count unrouted rows per week (`to_address IS NULL`) and flag any week where they exceed 10% of rows. That window's brand counts are floor values, not measurements.
3. **OpenAI billing gaps force `score = 2`.** When credits run out, triage falls back to score 2 for everything. A gap looks exactly like brand demand collapsing. Per week, report the score 3 / score 2 ratio; a sharp drop in that ratio with flat total volume is a billing artifact, not a signal. Cross-check against `triage_audit.model_used` being null or unchanged.

Add a `break_flag` column to `coverage_weekly.csv` naming which of the three, if any, applies to that week.

## The confound this file exists to expose
**Inbound demand vs replies to TABOOST's own outreach.** If TABOOST pitched the brand first, the arriving email is a result of TABOOST's sales effort, not evidence of brand demand. Mixed together, the index measures TABOOST's BD performance and will correlate with nothing in the market.

So: determine from `is_thread`, `triage_status` and `status` whether unsolicited first-contact can be separated from thread continuations. Then either
- add `campaigns_first_contact` as its own column, or
- if the data cannot distinguish them, **say so explicitly in the run report.**

Do not guess. Edge's pre-registration depends on knowing which of the two the number is.

## Run report (print, don't bury)
Date range covered · first and last `email_date` · total rows by score · distinct brands before and after normalization · unmatched brand count · weeks flagged for fallback dates · weeks where `active_talents` changed more than 20% · whether first-contact separation was possible.

## Finish
1. Write both CSVs to the local output path.
2. Print sha256 of each.
3. Append both hashes to Edge's `data/PROVENANCE.md`.
4. **Run no analysis.** No correlations, no returns, no charts, no "interesting" brands. The study is pre-registered separately in Edge and reading the data first is what the pre-registration exists to prevent.

## Gate
Marco has to approve this research use of TABOOST's inbox before any number touches a trade. The export itself is internal analytics and fine to build now; the trading use is the part that needs the conversation.
