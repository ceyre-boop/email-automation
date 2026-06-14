---
task: "sop.md single source of truth — talent data consolidation"
slug: 20260613-sop-md-single-source-of-truth
effort: E4
effort_source: classifier
phase: plan
progress: 0/130
mode: interactive
started: 2026-06-13T00:00:00Z
updated: 2026-06-13T00:00:00Z
---

## Problem

Talent data is split across three files that can — and do — drift from each other:

| File | What it holds | Who edits it |
|------|--------------|--------------|
| `sheets/sop.md` | Approved responses, personal emails, scenario triggers | Human (manager / ops) |
| `sheets/sop_data.json` | sop_status, min_rate, manager_email, rules (duplicate of sop.md) | Human (must stay in sync with sop.md) |
| `config/settings.json` | Talent roster, minimum_rate_usd, personal_email, paused flag, auto_send list | Human (must stay in sync with sop.md) |

This caused two real bugs already caught this session:
1. Jocelyn's rates in sop.md drifted from sop_data.json — deployed server showed old rates because sop.md was uncommitted
2. Stephanie + Jocelyn were missing from sop_data.json → their inboxes created Lost Drafts instead of drafts

The structural reason: adding a new talent requires editing ALL THREE files in a specific order. Missing any one step causes silent failures. There is no validator that catches drift before the poll cycle hits it.

## Vision

A manager adds a new talent by editing exactly ONE file — `sheets/sop.md` — following a one-page checklist. On the next Render deploy, the startup validator reads sop.md, extracts every talent's profile, confirms all required fields are present, and logs a human-readable warning for anything missing. The poll cycle, triage, auto-send, and dashboard all read from the same in-memory talent registry. `sop_data.json` is generated from sop.md at startup, never hand-edited. `settings.json` holds only system-level config (rate limits, model names, draft mode). The next time Jocelyn's rates change, one edit + one push is all it takes.

## Out of Scope

- Real-time hot-reload of sop.md without a server restart (requires fsnotify infrastructure)
- A UI form for adding talents (sop.md is authoritative; a form would need to write back to it)
- Database-level talent storage (overkill for this roster size)
- Multi-tenant / multi-agency support
- Auto-discovery of Gmail OAuth connections — those still require manual `Make Connections` setup per talent
- Removing `sheets/sop_data.json` from the filesystem (it stays but becomes generated, not source)

## Principles

1. **One edit point.** Adding or updating a talent touches exactly one file: sop.md.
2. **Validate at startup, not at failure time.** Drift caught at boot (warning in Render logs) is infinitely cheaper than drift caught when Marco asks why a draft has wrong rates.
3. **Parse structure, don't duplicate it.** Personal emails already exist in sop.md Scenario C blocks — extract them there, don't store them separately in settings.json.
4. **Non-fatal warnings.** The startup validator warns but does not crash. A misconfigured talent causes skipped drafts; it does not take down the whole system.
5. **Backwards-safe migration.** The parser is added, consumers are migrated one by one, then settings.json is cleaned. Each step independently deployable.

## Constraints

- Reply draft content must not change — the sop.md → `_get_talent_section_raw()` → draft path is unchanged.
- OpenAI stays as the AI provider for triage and drafting — never change this.
- No destructive DB migrations — additive only, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- Render auto-deploys on push to main. Startup must remain fast (<10s).
- Gmail OAuth connections (`gmail_connection_name`) are a connection label string that must match the Make (formerly Integromat) connection name exactly — cannot be inferred programmatically, must be explicit.

## Goal

Build `backend/services/sop_parser.py` that turns `sheets/sop.md` into an in-memory `dict[str, TalentProfile]`, wire it into `backend/core/config.py` as `settings.talent_profiles`, migrate all consumers (poller, triage, auto_send, dashboard) to read from it, remove talent arrays from `settings.json`, generate `sop_data.json` from parsed profiles at startup, add a startup validator, and ship a one-page `docs/ADDING_A_TALENT.md` onboarding checklist.

## Criteria

### Parser — `backend/services/sop_parser.py`
- [ ] ISC-1: `backend/services/sop_parser.py` exists
- [ ] ISC-2: `TalentProfile` dataclass defined with fields: `key`, `full_name`, `manager`, `manager_email`, `gmail_connection_name`, `minimum_rate_usd`, `rate_unit`, `auto_send`, `paused`, `personal_emails`, `has_approved_response`
- [ ] ISC-3: `parse_sop_md(text: str) -> dict[str, TalentProfile]` function exists
- [ ] ISC-4: `validate_profiles(profiles: dict) -> list[str]` function exists
- [ ] ISC-5: Parser finds exactly 12 active talent sections in current sop.md (Katrina, Anastasiya, Wesley, Hana, Jenn, Angela, Grayson, Kylika, Audur, Skyler, Stephanie, Jocelyn)
- [ ] ISC-6: Parser extracts `key` from `Key:` metadata line (e.g., `Key: Jocelyn`)
- [ ] ISC-7: Parser extracts `full_name` from `Talent:` section header
- [ ] ISC-8: Parser extracts `manager` name from `Manager: Name <email>` line
- [ ] ISC-9: Parser extracts `manager_email` from `Manager: Name <email>` format via `<...>` extraction
- [ ] ISC-10: Parser extracts `gmail_connection_name` from `Gmail:` metadata line
- [ ] ISC-11: Parser extracts `minimum_rate_usd` as `int` from `Min Rate: $850 per video`
- [ ] ISC-12: Parser extracts `rate_unit` as string from `Min Rate: $850 per video` → `"per video"`
- [ ] ISC-13: Parser extracts `auto_send: bool` from `Auto Send: yes` / `Auto Send: no` (case-insensitive)
- [ ] ISC-14: Parser extracts `paused: bool` from `Paused: yes` / `Paused: no` (case-insensitive)
- [ ] ISC-15: Parser extracts `personal_emails: list[str]` from Scenario C `Personal Email:` block
- [ ] ISC-16: Parser handles multi-email Scenario C blocks (Stephanie has 2 personal emails)
- [ ] ISC-17: Parser sets `has_approved_response: bool` True when `Approved Response:` block present
- [ ] ISC-18: Parser sets `has_approved_response: bool` False when no `Approved Response:` block
- [ ] ISC-19: Parser returns `{}` for empty sop.md text (no crash)
- [ ] ISC-20: Parser handles both `## Talent:` (markdown) and `Talent:` (plain) section headers
- [ ] ISC-21: `parse_sop_md` is idempotent — same input always produces same output
- [ ] ISC-22: Parser handles missing `Key:` line → falls back to first word of `full_name` as key
- [ ] ISC-23: Parser handles missing `manager_email` gracefully → `manager_email = None`
- [ ] ISC-24: Parser handles min rate without dollar sign: `850 per video` → `minimum_rate_usd = 850`
- [ ] ISC-25: `TalentProfile.personal_emails` is always `list[str]` even for single email entry
- [ ] ISC-26: `get_active_profiles(profiles) -> dict[str, TalentProfile]` helper filters out `paused=True` entries
- [ ] ISC-27: Parser extracts Jocelyn `minimum_rate_usd = 850`
- [ ] ISC-28: Parser extracts Skyler `auto_send = True`
- [ ] ISC-29: Parser extracts Sylvia `paused = True` (19 total, 12 active)
- [ ] ISC-30: `parse_sop_md` does not read from disk — caller supplies the text string (testable without filesystem)

### Validator
- [ ] ISC-31: Validator returns warning string when talent has `has_approved_response = False`
- [ ] ISC-32: Validator returns warning string when talent has `personal_emails = []`
- [ ] ISC-33: Validator returns warning string when talent has `gmail_connection_name = None`
- [ ] ISC-34: Validator returns warning string when talent has `minimum_rate_usd = 0`
- [ ] ISC-35: Validator returns warning string when talent has `manager_email = None`
- [ ] ISC-36: Each warning string mentions the talent's name or key
- [ ] ISC-37: Validator returns `[]` (no warnings) for Jocelyn profile (fully configured)
- [ ] ISC-38: Validator does not raise exceptions — returns warnings, never crashes

### `sheets/sop.md` format extension
- [ ] ISC-39: All 12 active talent sections have a `Key:` line immediately after `Talent:`
- [ ] ISC-40: All 12 active talent sections have a `Gmail:` line
- [ ] ISC-41: All 12 active talent sections have a `Min Rate: $N per video` line
- [ ] ISC-42: All 12 active talent sections have an `Auto Send: yes` or `Auto Send: no` line
- [ ] ISC-43: All 12 active talent sections have a `Paused: yes` or `Paused: no` line
- [ ] ISC-44: All 12 active manager lines follow `Manager: Full Name <email>` format
- [ ] ISC-45: `grep -c "^Key:" sheets/sop.md` returns 12
- [ ] ISC-46: `grep -c "^Gmail:" sheets/sop.md` returns 12
- [ ] ISC-47: `grep -c "^Min Rate:" sheets/sop.md` returns 12
- [ ] ISC-48: `grep -c "^Auto Send:" sheets/sop.md` returns 12
- [ ] ISC-49: `grep -c "^Paused:" sheets/sop.md` returns 12
- [ ] ISC-50: Jocelyn section: `Auto Send: no` (not in current auto_send_talents)
- [ ] ISC-51: Skyler section: `Auto Send: yes`
- [ ] ISC-52: Jocelyn section: `Min Rate: $850 per video`
- [ ] ISC-53: Paused talents (Sylvia, Trin, Britt, etc.) have `Paused: yes` in their sections
- [ ] ISC-54: Active talents have `Paused: no` in their sections

### `backend/core/config.py` changes
- [ ] ISC-55: `settings.talent_profiles` property exists, returns `dict[str, TalentProfile]`
- [ ] ISC-56: `settings.talent_profiles` reads sop.md fresh each call (no module-level cache; cache lives in `_sop_md_cache`)
- [ ] ISC-57: `settings.talent_profiles["Jocelyn"].minimum_rate_usd == 850`
- [ ] ISC-58: `settings.talent_profiles["Skyler"].auto_send == True`
- [ ] ISC-59: `settings.talent_profiles["Sylvia"].paused == True`
- [ ] ISC-60: `settings.talent_profiles` returns 19 profiles (all sop.md sections, active + paused)
- [ ] ISC-61: `settings.talent_profiles` import of `sop_parser` does not cause circular import

### `backend/main.py` — startup validator
- [ ] ISC-62: Startup calls `parse_sop_md()` after cache clear and logs result
- [ ] ISC-63: Startup calls `validate_profiles()` and logs each warning at `WARNING` level
- [ ] ISC-64: Startup logs `"N talents loaded from sop.md, N warnings"` at INFO level
- [ ] ISC-65: Startup completes (does not crash) even when validator returns warnings
- [ ] ISC-66: Startup generates `sheets/sop_data.json` from parsed profiles before first poll

### `backend/services/poller.py` migration
- [ ] ISC-67: `_talent_config_map()` is removed from `poller.py`
- [ ] ISC-68: poller.py imports `TalentProfile` from `backend.services.sop_parser`
- [ ] ISC-69: poller builds talent map from `settings.talent_profiles` (filtered: `paused=False` + has OAuth token)
- [ ] ISC-70: poller `talent_cfg.get("paused")` → `profile.paused`
- [ ] ISC-71: poller `talent_cfg.get("minimum_rate_usd")` → `profile.minimum_rate_usd`
- [ ] ISC-72: poller `talent_cfg.get("manager")` → `profile.manager`
- [ ] ISC-73: Polling still skips talents with `paused=True`
- [ ] ISC-74: All 10 current auto_send talents are still polled after migration
- [ ] ISC-75: poller no longer reads `settings.app_config.get("talents", [])`

### `backend/services/triage.py` migration
- [ ] ISC-76: triage.py `personal_email` lookup reads from `settings.talent_profiles[talent_key].personal_emails`
- [ ] ISC-77: triage.py no longer reads `settings.app_config.get("talents", [])`
- [ ] ISC-78: Personal email detection still correctly filters Skyler's personal email (`crashingskydrummer@gmail.com`)
- [ ] ISC-79: Personal email detection handles list format (Stephanie has 2 personal emails)

### `backend/services/auto_send.py` migration
- [ ] ISC-80: auto_send.py derives auto-send list from `settings.talent_profiles` (profiles where `.auto_send == True`)
- [ ] ISC-81: auto_send.py no longer reads `settings.app_config.get("auto_send_talents", [])`
- [ ] ISC-82: Auto-send still fires for: Wesley, Hana, Audur, Katrina, Anastasiya, Jenn, Angela, Grayson, Kylika, Skyler (the 10 current auto_send_talents)

### `backend/routers/dashboard.py` migration
- [ ] ISC-83: dashboard.py talent list reads from `settings.talent_profiles`
- [ ] ISC-84: dashboard.py SOP status reads from `profile.has_approved_response` (not sop_data.json)
- [ ] ISC-85: dashboard.py no longer has any `sop_data.json` direct reads
- [ ] ISC-86: Dashboard SOP status display still shows approved/pending correctly
- [ ] ISC-87: Dashboard `/api/dashboard/talents` endpoint returns all 12 active talents

### `config/settings.json` cleanup
- [ ] ISC-88: `settings.json` no longer contains a `"talents"` key
- [ ] ISC-89: `settings.json` no longer contains an `"auto_send_talents"` key
- [ ] ISC-90: `grep '"talents"' config/settings.json` returns no matches
- [ ] ISC-91: `grep '"auto_send_talents"' config/settings.json` returns no matches
- [ ] ISC-92: `settings.json` still contains `model_name`, `draft_mode`, `velocity_cap`
- [ ] ISC-93: `settings.json` is valid JSON after removal (`python3 -m json.tool config/settings.json`)
- [ ] ISC-94: `reply.manager_emails` map removed from settings.json (now derived from `profile.manager_email` at runtime)

### `sheets/sop_data.json` — generated cache
- [ ] ISC-95: sop_data.json is regenerated at startup from parsed profiles
- [ ] ISC-96: Generated sop_data.json has `sop_status: "approved"` for talents with `has_approved_response=True`
- [ ] ISC-97: Generated sop_data.json has `sop_status: "pending"` for talents with `has_approved_response=False`
- [ ] ISC-98: sop_data.json has a `"_generated"` timestamp key so it's clearly auto-generated
- [ ] ISC-99: Existing reply.py SOP gate still works after sop_data.json becomes generated
- [ ] ISC-100: `sheets/sop_data.json` added to `.gitignore` (generated file, not committed)

### Tests — `tests/test_sop_parser.py`
- [ ] ISC-101: `tests/test_sop_parser.py` exists
- [ ] ISC-102: Test: `parse_sop_md(fixture)` returns correct count of profiles
- [ ] ISC-103: Test: Jocelyn profile extracted correctly (key, rates, auto_send, personal_emails)
- [ ] ISC-104: Test: `validate_profiles()` returns `[]` for fully-valid profile
- [ ] ISC-105: Test: `validate_profiles()` returns 1 warning for profile missing Approved Response
- [ ] ISC-106: Test: `validate_profiles()` returns 1 warning for profile missing personal email
- [ ] ISC-107: Test: parser handles empty sop.md without crashing

### Onboarding checklist
- [ ] ISC-108: `docs/ADDING_A_TALENT.md` exists
- [ ] ISC-109: Checklist has ≥5 numbered steps
- [ ] ISC-110: Step 1: "Add a talent section to `sheets/sop.md`" with required metadata field list
- [ ] ISC-111: Required metadata fields named: `Key`, `Gmail`, `Min Rate`, `Auto Send`, `Paused`, `Manager: Name <email>`
- [ ] ISC-112: Checklist includes step: connect Gmail OAuth in Render/Make and test connection
- [ ] ISC-113: Checklist includes step: push to main and verify startup validator shows 0 warnings for new talent
- [ ] ISC-114: Checklist includes step: trigger backfill and confirm drafts are created
- [ ] ISC-115: Checklist is ≤2 pages

### Anti-criteria
- [ ] ISC-116: Anti: reply.py draft text does NOT change post-migration — same sop.md → same draft output (sop.md parse path is untouched)
- [ ] ISC-117: Anti: existing auto_send set of 10 talents is preserved exactly after migration
- [ ] ISC-118: Anti: no Python file has hardcoded talent names post-migration (grep: `"Jocelyn"\|"Skyler"\|"Katrina"` in backend/ returns 0 hits)
- [ ] ISC-119: Anti: `uvicorn` startup does not crash when sop_data.json is missing (first boot)
- [ ] ISC-120: Anti: talent with `paused=True` is NOT polled after migration
- [ ] ISC-121: Anti: `settings.app_config.get("talents")` returns `None` or `[]` after cleanup (no callers get stale data)
- [ ] ISC-122: Anti: sop_data.json is not committed to git (in .gitignore)
- [ ] ISC-123: Anti: dashboard SOP status display does not break after sop_data.json becomes generated

### Integration
- [ ] ISC-124: Full poll cycle for Katrina (the connected inbox) completes after migration
- [ ] ISC-125: Score-3 email for Jocelyn generates correct draft with current rates after migration
- [ ] ISC-126: `GET /api/status` returns 200 after migration
- [ ] ISC-127: Dashboard loads and shows all 12 active talents after migration
- [ ] ISC-128: New talent added ONLY to sop.md is discovered by validator without sop_data.json edit
- [ ] ISC-129: `uvicorn backend.main:app --reload` starts cleanly after migration
- [ ] ISC-130: No circular Python imports introduced by sop_parser.py

## Test Strategy

| isc | type | check | threshold | tool |
|-----|------|-------|-----------|------|
| ISC-1..4 | file existence | Read/Grep file | present | Read |
| ISC-5..30 | unit | pytest tests/test_sop_parser.py | all pass | Bash |
| ISC-31..38 | unit | pytest test_sop_parser.py validator tests | all pass | Bash |
| ISC-39..54 | grep | `grep -c "^Key:" sheets/sop.md` etc | N=12 | Bash |
| ISC-55..61 | code read | Read config.py talent_profiles property | present | Read |
| ISC-62..66 | code read | Read main.py startup section | calls validator | Read |
| ISC-67..75 | code read / grep | Read poller.py, grep for removed patterns | absent | Bash |
| ISC-76..79 | code read | Read triage.py | uses profiles | Read |
| ISC-80..82 | code read | Read auto_send.py | uses profiles | Read |
| ISC-83..87 | code read | Read dashboard.py SOP status block | uses profiles | Read |
| ISC-88..94 | json tool | `python3 -m json.tool settings.json` | valid, no talents key | Bash |
| ISC-95..100 | startup | `uvicorn` logs show sop_data.json regenerated | present | Bash |
| ISC-101..107 | pytest | `pytest tests/test_sop_parser.py -v` | all green | Bash |
| ISC-108..115 | file read | Read docs/ADDING_A_TALENT.md | steps present | Read |
| ISC-116..123 | anti | grep + startup + integration | absent/unchanged | Bash |
| ISC-124..130 | integration | startup + poll cycle + API | end-to-end pass | Bash/curl |

## Features

| name | description | satisfies | depends_on | parallelizable |
|------|-------------|-----------|------------|----------------|
| sop-parser | New `backend/services/sop_parser.py` with TalentProfile dataclass and parse/validate functions | ISC-1..30 | — | true |
| sop-md-format | Add Key/Gmail/Min Rate/Auto Send/Paused/Manager-email metadata to all 12 active sop.md sections | ISC-39..54 | — | true (with sop-parser) |
| config-property | Add `settings.talent_profiles` to `backend/core/config.py` | ISC-55..61 | sop-parser | false |
| startup-validator | Startup parse + validate + generate sop_data.json in `backend/main.py` | ISC-62..66, ISC-95..100 | config-property, sop-md-format | false |
| migrate-poller | Replace `_talent_config_map()` with `settings.talent_profiles` in poller.py | ISC-67..75 | config-property | false |
| migrate-triage | Replace `personal_email` source in triage.py | ISC-76..79 | config-property | true (with migrate-poller) |
| migrate-autosend | Replace `auto_send_talents` source in auto_send.py | ISC-80..82 | config-property | true (with migrate-poller) |
| migrate-dashboard | Replace sop_data.json reads in dashboard.py | ISC-83..87 | config-property | true (with migrate-poller) |
| settings-cleanup | Remove `talents[]` and `auto_send_talents` from settings.json | ISC-88..94 | migrate-poller, migrate-triage, migrate-autosend, migrate-dashboard | false |
| tests | `tests/test_sop_parser.py` unit tests | ISC-101..107 | sop-parser | true (with sop-parser) |
| checklist | `docs/ADDING_A_TALENT.md` onboarding guide | ISC-108..115 | settings-cleanup | false |

## Decisions

2026-06-13 — Chose `sheets/sop.md` as source of truth (not sop_data.json) because: sop.md is what managers already edit for content, and reply.py already reads it for draft text. Flipping the generation direction (sop.md → sop_data.json) aligns the code with how the team actually works.

2026-06-13 — `gmail_connection_name` must be explicit in sop.md (not inferred). Though it follows the pattern `Gmail - [FirstName]`, Make connection names are user-defined strings. A typo breaks polling silently. Explicit is safer and validated at startup.

2026-06-13 — Personal emails stay in the Scenario C block of sop.md (not in a new metadata section). They're already there. The parser extracts them from that existing location. This preserves the human-readable SOP structure without duplicating data.

2026-06-13 — `reply.manager_emails` map in settings.json can be derived from `profile.manager_email` at runtime. Remove it from settings.json in the cleanup step. The CC routing code in reply.py should build the map from `settings.talent_profiles` at call time.

2026-06-13 — sop_data.json added to .gitignore after it becomes generated. During transition it stays in git (so the live system can still use it). After startup-validator lands and generates it on boot, it's removed from tracking.

2026-06-13 — Migration order chosen to be safe: (1) parser + tests, (2) sop.md format, (3) config property, (4) startup validator + sop_data.json generation, (5) consumer migrations in parallel, (6) settings.json cleanup. Each step is independently deployable.

2026-06-13 — Delegation: Forge for sop_parser.py implementation (most complex new file) and consumer migrations. Two Explore agents already ran. Cato audit at VERIFY (E4 mandatory).

## Changelog

(populated at LEARN phase)

## Verification

(populated at VERIFY phase)

---

## Implementation Notes

### New sop.md metadata format per talent

```
Talent: Jocelyn Chardon
Key: Jocelyn
Manager: Cara Best <cara@taboost.me>
Gmail: Gmail - Jocelyn
Min Rate: $850 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
[existing text unchanged]

Scenario C: Personal Email Forward
Personal Emails:
- jocelynsagec@gmail.com
```

### `TalentProfile` dataclass

```python
@dataclass
class TalentProfile:
    key: str
    full_name: str
    manager: str
    manager_email: str | None
    gmail_connection_name: str | None
    minimum_rate_usd: int
    rate_unit: str
    auto_send: bool
    paused: bool
    personal_emails: list[str]
    has_approved_response: bool
```

### `config.py` property

```python
@property
def talent_profiles(self) -> dict[str, "TalentProfile"]:
    from backend.services.sop_parser import parse_sop_md
    return parse_sop_md(_load_sop_md())
```

Note: `_load_sop_md()` already caches the file read. The parse step is lightweight.

### `_talent_config_map` replacement in poller.py

```python
# Replace:
talent_map = _talent_config_map(settings)

# With:
from backend.services.sop_parser import get_active_profiles
talent_map = get_active_profiles(settings.talent_profiles)
# talent_map: dict[str, TalentProfile]
```

### sop_data.json generation at startup

```python
def _generate_sop_data(profiles: dict[str, TalentProfile]) -> None:
    data = {
        key: {
            "full_name": p.full_name,
            "sop_status": "approved" if p.has_approved_response else "pending",
            "_generated": datetime.utcnow().isoformat(),
        }
        for key, p in profiles.items()
    }
    sop_path = Path(__file__).parent.parent.parent / "sheets" / "sop_data.json"
    sop_path.write_text(json.dumps(data, indent=2))
```

### Migration sequencing

1. **Deploy 1**: `sop_parser.py` + tests + sop.md metadata lines. No consumer changes yet. Startup logs "N talents loaded" but consumers still read settings.json. ✅ Safe to deploy.
2. **Deploy 2**: `config.py talent_profiles` property + startup validator + sop_data.json generation. Consumers still read settings.json. ✅ Safe to deploy.
3. **Deploy 3**: Migrate poller, triage, auto_send, dashboard to `talent_profiles`. settings.json `talents[]` still present (redundant but harmless). ✅ Safe to deploy.
4. **Deploy 4**: Remove `talents[]` and `auto_send_talents` from settings.json. Add sop_data.json to .gitignore. ✅ Final cleanup.

Each deploy is independently safe. If anything regresses, one config revert fixes it.
