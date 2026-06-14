# Failure Modes — Email Automation

Institutional memory for recurring failure patterns. When something breaks, check here first.
When something new breaks, add it here before closing the incident.

---

## FM-01 — Dollar amount in GPT context → draft corruption

**Incident:** Drafts included proposed rate dollar amounts in the reply body.
**Root cause:** `proposed_rate` was injected into the reply prompt context → GPT included it verbatim in drafts.
**Fix:** Removed `proposed_rate` from prompt context.
**Prevention:** CI grep for `proposed_rate` in `prompts/` files (sop-guard.yml).

---

## FM-02 — Scenario B bundle logic → wrong draft type

**Incident:** System generated bundle-offer drafts that SOP no longer supports.
**Root cause:** Scenario B code path remained after SOP removed it.
**Fix:** Fully removed Scenario B code path from reply.py.
**Prevention:** SOP is single source of truth — if SOP doesn't have it, code shouldn't either.

---

## FM-03 — sop_data.json drift → SOP-pending errors

**Incident:** Drafts showed "SOP pending" for talents that had approved responses in sop.md.
**Root cause:** Hand-maintained `sop_data.json` fell out of sync with `sop.md`.
**Fix:** `sop_data.json` auto-generated at startup from `sop.md`.
**Prevention:** Never edit `sop_data.json` manually. Startup regeneration is authoritative.

---

## FM-04 — settings.json talent array removed → INVALID flood (2026-06-14)

**Incident:** Overnight, all pending drafts gained INVALID badges. System appeared to stop at 7pm. 100+ drafts created in 2 minutes when fixed code deployed. Jocelyn showed "not in talent roster."
**Root cause:** Commit `7ff7301` removed `talents[]` from `settings.json`. Six code paths still read from it (now empty array). `validation.py` Check 5 compared talent keys against empty set → every draft got `validation_failed=True`.
**Fix:** Fixed all 7 legacy reads to use `talent_list` (sourced from `sop.md`). Added startup SQL that resets `validation_failed=True` drafts on every deploy. Added burst guard (>20 drafts in 60s → skip cycle).
**Prevention:** CI grep for `app_config.get("talents"` (sop-guard.yml). `/health` endpoint exposes `sop_hash` and `talent_count` to detect stale containers.

---

## FM-05 — Render cache → stale container deployed

**Incident:** Render served a cached build without the updated `sop.md`. System behaved as if old rules were still in effect.
**Root cause:** Render can cache a build layer and not pick up file changes that aren't in Python source.
**Fix:** Commit a no-op change to a Python file to force a full rebuild.
**Prevention:** Compare `sop_hash` from `/health` to local `sha256sum sheets/sop.md | cut -c1-12`. If they differ, force redeploy.

---

## FM-06 — Send window logic re-appearing → sends blocked 7am-7pm

**Incident:** Sends stopped during business hours — appeared to follow an operating hours schedule.
**Root cause:** Time window logic was removed from the codebase but a reference crept back in via merge conflict resolution.
**Fix:** Deleted entirely. Confirmed: no `ZoneInfo`, no `send_window`, no `_is_within_send_window` anywhere.
**Prevention:** CI grep for `ZoneInfo|send_window|_is_within_send_window` (sop-guard.yml).

---

## FM-07 — From header parsing → personal email filter broken

**Incident:** Emails from talent personal addresses were not being suppressed (they should be score 1 / archived).
**Root cause:** Raw `From:` header includes display name (e.g. `"Jane Smith" <jane@gmail.com>`). Bare address comparison failed on the full string.
**Fix:** `parseaddr()` to extract the bare email address before comparison.
**Prevention:** Integration test verifies personal email suppression for each connected talent.

---

## FM-08 — Guardian `_pause_talent` orphan write (2026-06-14)

**Incident:** Guardian auto-paused a talent for rate violations. Talent continued receiving drafts.
**Root cause:** `_pause_talent()` wrote `paused=True` to `settings.json talents[]`. System reads `paused` from `sop.md`. Since `settings.json` has no `talents[]` array anymore, the write iterated an empty list → no-op.
**Fix:** `_pause_talent()` now writes `Paused: yes` directly to `sop.md` via regex replace, then calls `get_settings.cache_clear()`.
**Prevention:** Task 1 fix in Sunday Resilience Sprint (2026-06-14). Startup cross-check catches disconnected tokens.

---

## FM-09 — 100+ draft spike in 2 minutes (2026-06-14)

**Incident:** When fixed code deployed after FM-04, backlog blaster found 100+ accumulated Score-3 emails and created drafts for all of them simultaneously.
**Root cause:** Backlog blaster had no burst guard. Large backlog + working code = instantaneous flood.
**Fix:** Burst guard added to `_run_draft_queue_inner`: skip cycle if >20 drafts created in last 60 seconds.
**Prevention:** Burst guard is permanent. Guardian `global_draft_hard_limit=200` also acts as a ceiling.

---

## FM-10 — Wrong rates/manager in drafts → incorrect responses sent (2026-06-14)

**Incident:** Drafts were generated with `minimum_rate=0` and empty `manager=""`. Rate negotiation logic was wrong.
**Root cause:** `_run_draft_queue_inner` built `talent_map` from `settings.app_config.get("talents", [])` → empty dict. All talent lookups returned `{}` → defaults of 0 and "".
**Fix:** `talent_map` rebuilt from `get_active_profiles(settings.talent_profiles)` sourced from `sop.md`. Fail-fast guard added: if `talent_cfg` is empty or `minimum_rate_usd == 0`, skip with ERROR log — never create draft.
**Prevention:** Fail-fast guard in `_draft_one`. CI guard prevents legacy reads returning. `sop.md` is single source of truth for all talent data.

---

## Diagnostic Quick Reference

| Symptom | First check | Second check |
|---------|-------------|--------------|
| INVALID badges flooding | `GET /health` → `talent_count` | `grep 'app_config.get("talents"'` in backend/ |
| Sends stopped unexpectedly | `GET /api/admin/guardian/status` | Check `auto_send_enabled` in settings.json |
| Wrong rates in drafts | `GET /health` → `sop_hash` (compare to local) | Check `sop.md` has correct `Min Rate:` for each talent |
| Talent "not in roster" | Confirm talent exists in `sop.md` | Check `Key:` field matches exactly (case-insensitive) |
| Draft spike (100+) | Check burst guard logs | Look for backlog blaster + large accumulated Score-3 backlog |
| Paused talent still getting drafts | Check `sop.md` for `Paused: yes` | Guardian writes to sop.md (not settings.json) — verify FM-08 fix deployed |
| System appears to stop at specific hour | Search codebase for `ZoneInfo|send_window` | Should be 0 results |
| Stale rules after deploy | `GET /health` → compare `sop_hash` | Trigger no-op commit to force Render rebuild |
