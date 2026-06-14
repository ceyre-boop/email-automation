# Plan — Auto-send cap + remove send window

## Context

Two operational changes to the auto-send pipeline, requested in the morning task summary. (Tasks 3 "Creator-first default screen" and 4 "shop campaign to calendar" belong to a different project and are **out of scope** here.)

1. **Raise the auto-send cap to 200.** Current per-talent hourly velocity cap is 100. As more talents go live and backlogs are worked down, 100/hour/talent throttles legitimate sends. Raising to 200 doubles headroom while keeping a safety ceiling.
2. **Remove the 7am–7pm send window.** A time-of-day gate exists (`_is_within_send_window`) that holds sends outside business hours. It is currently *disabled* (`auto_send_window_enabled: false`) and was misconfigured to `America/Detroit` (Eastern), not Pacific. Intent is to remove the mechanism entirely so sends are never time-gated and the dead config/code can't be accidentally re-enabled.

Outcome: auto-send runs around the clock with a 200/hour/talent cap.

## Changes

### 1. Raise velocity cap — `config/settings.json`
- Line 8: `"auto_send_velocity_cap": 100,` → `"auto_send_velocity_cap": 200,`
- No default-value change needed in code; the fallback in `auto_send.py:98` (`get("auto_send_velocity_cap", 25)`) reads from this config.

### 2. Remove send window — two files

**`config/settings.json`** — delete the four window keys + comment (lines 9–13):
```
"auto_send_window_enabled": false,
"auto_send_window_start": "07:00",
"auto_send_window_end": "19:00",
"auto_send_timezone": "America/Detroit",
"_auto_send_window_comment": "...",
```
(Ensure the preceding line — `auto_send_velocity_cap` — keeps a trailing comma and JSON stays valid.)

**`backend/services/auto_send.py`**:
- Delete the `_is_within_send_window` function (lines 32–54).
- Delete its call site in `run_auto_send` (lines 71–78, the `if not _is_within_send_window(...)` block).
- Remove now-unused imports: `from zoneinfo import ZoneInfo` (line 19); drop `timezone` from `from datetime import datetime, timedelta, timezone` (line 18) — it is only referenced inside the removed function (verify no other usage in the file before removing).
- Update the module docstring (lines 3–13): remove the `auto_send_window_enabled` line (7) from the "Controlled entirely by" list.

## Out of scope
- Tasks 3 (Creator-first default screen) and 4 (shop campaign → calendar) — different project, not done here.
- No change to `auto_send_hold_minutes`, talent roster, or any other safeguard (thread-count, already-sent, human-touch guards stay).

## Verification
1. **JSON validity:** `cd /Users/taboost/email-automation && python -c "import json; json.load(open('config/settings.json'))"` → no error; confirm `auto_send_velocity_cap == 200` and no `auto_send_window_*` keys remain.
2. **Imports/syntax:** `cd backend && python -c "from backend.services import auto_send"` → imports cleanly (catches dangling `ZoneInfo`/`timezone` references).
3. **Behavior:** `python -m pytest tests/ -q` if auto-send tests exist; otherwise confirm `run_auto_send` no longer references a window and proceeds straight to the talent loop after the hold-cutoff calc.
4. Optional: grep to confirm zero remaining references — `rg -n "send_window|_is_within_send_window|ZoneInfo" backend/ config/` returns nothing.
