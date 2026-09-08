"""Pool telemetry must not lie.

During the 2026-09-07 incident /health reported "13 of 20 checked out" and later
"capacity 5" while the engine was configured for 25+25. Both were fabrications
of a bad formula: pool.overflow() returns the CURRENT overflow counter, which
starts at -pool_size and climbs, not the configured maximum. A monitor that
reports confident wrong numbers is worse than no monitor — decisions get made on
them, as they were here.
"""
from unittest.mock import MagicMock, patch

from backend.routers.cron import _pool_block


def _pool(size=25, max_overflow=25, checked_out=3, current_overflow=-22):
    p = MagicMock()
    p.size.return_value = size
    p.checkedout.return_value = checked_out
    p.overflow.return_value = current_overflow  # the misleading one
    p._max_overflow = max_overflow
    return p


def test_capacity_uses_configured_max_overflow_not_the_live_counter():
    engine = MagicMock()
    engine.pool = _pool()
    with patch("backend.models.db.get_engine", return_value=engine):
        block = _pool_block()

    assert block["capacity"] == 50          # 25 + 25, not 25 + (-22)
    assert block["pool_size"] == 25
    assert block["max_overflow"] == 25
    assert block["available"] == 47
    assert block["saturated"] is False


def test_saturation_is_only_reported_when_genuinely_full():
    engine = MagicMock()
    engine.pool = _pool(size=10, max_overflow=10, checked_out=20, current_overflow=10)
    with patch("backend.models.db.get_engine", return_value=engine):
        block = _pool_block()

    assert block["capacity"] == 20
    assert block["available"] == 0
    assert block["saturated"] is True


def test_a_broken_engine_reports_an_error_rather_than_a_wrong_number():
    with patch("backend.models.db.get_engine", side_effect=RuntimeError("no engine")):
        block = _pool_block()
    assert "error" in block
    assert "checked_out" not in block


# ── Analytics response cache ──────────────────────────────────────────────────

def test_analytics_cache_serves_repeat_calls_without_recomputing():
    """The dashboard polls these continuously; each miss is a GROUP BY over 70k
    rows. On 2026-09-08 one was still being killed by the statement timeout."""
    from backend.routers import analytics

    analytics._analytics_cache.clear()
    calls = []

    def producer():
        calls.append(1)
        return {"value": len(calls)}

    first = analytics._cached("k", producer)
    second = analytics._cached("k", producer)

    assert first == second == {"value": 1}
    assert len(calls) == 1


def test_analytics_cache_is_keyed_so_different_windows_do_not_collide():
    from backend.routers import analytics

    analytics._analytics_cache.clear()
    a = analytics._cached("load:1", lambda: "one-day")
    b = analytics._cached("load:7", lambda: "seven-day")
    assert a == "one-day" and b == "seven-day"


def test_analytics_cache_expires():
    from backend.routers import analytics

    analytics._analytics_cache.clear()
    analytics._cached("k", lambda: "stale")
    # Age the entry past the TTL.
    ts, val = analytics._analytics_cache["k"]
    analytics._analytics_cache["k"] = (ts - analytics._ANALYTICS_TTL_SECONDS - 1, val)

    assert analytics._cached("k", lambda: "fresh") == "fresh"
