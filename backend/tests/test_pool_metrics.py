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
