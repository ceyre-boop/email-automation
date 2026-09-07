"""DB write-rate instrumentation.

The write storm that pinned the database at 100% CPU ran for months undetected:
~1,700 UPDATEs a minute, none of them changing a value. Every health check was
green because every individual query succeeded — nothing anywhere measured cost.
This is the meter that makes the next one visible while it is still cheap to fix.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.services import write_meter


@pytest.fixture(autouse=True)
def _clean_meter():
    write_meter._buckets.clear()
    yield
    write_meter._buckets.clear()


def test_writes_are_counted_per_minute():
    for _ in range(10):
        write_meter.record_write()
    assert write_meter.writes_per_minute() == 10.0


def test_batched_writes_count_rows_not_statements():
    """An executemany that rewrites 500 rows costs the database 500 rows, not one
    statement — counting statements would have hidden the original storm."""
    write_meter.record_write(500)
    assert write_meter.writes_per_minute() == 500.0


def test_old_buckets_fall_out_of_the_window():
    stale = write_meter._bucket_now() - timedelta(minutes=30)
    write_meter._buckets.append([stale, 9999])
    write_meter.record_write(5)
    assert write_meter.writes_per_minute(window_minutes=5) == 5.0


def test_snapshot_reports_peak_and_sample_size():
    write_meter.record_write(3)
    snap = write_meter.snapshot()
    assert snap["writes_per_minute"] == 3.0
    assert snap["peak_minute"] == 3
    assert snap["sampled_minutes"] == 1
    assert snap["window_minutes"] == 5


def test_no_writes_reads_as_zero_not_an_error():
    assert write_meter.writes_per_minute() == 0.0
    assert write_meter.snapshot()["peak_minute"] == 0


def test_install_is_idempotent_and_never_raises():
    write_meter._installed = False
    try:
        write_meter.install(MagicMock())      # a mock engine cannot register events
        write_meter.install(MagicMock())      # second call must be a no-op
    finally:
        write_meter._installed = False


def test_health_score_drops_when_write_rate_is_high(db_session):
    """The whole point: a write storm has to show up as a falling number."""
    from backend.services.health import compute_health_score

    baseline = compute_health_score(db_session)
    assert baseline["components"]["db_write_rate"] == 1.0

    write_meter.record_write(2000)  # storm
    stormy = compute_health_score(db_session)

    assert stormy["components"]["db_write_rate"] == 0.0
    assert stormy["score"] < baseline["score"]
    assert any("write rate" in i.lower() for i in stormy["issues"])
