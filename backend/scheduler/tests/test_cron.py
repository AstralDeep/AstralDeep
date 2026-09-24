"""Tests for scheduler/cron.py and governance.py: interval/one-shot/cron evaluation
including timezone-aware and weekday-skipping cases, invalid-expression errors, and
the job-cap/interval-floor governance checks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scheduler.cron import ScheduleError, compute_next_run_ms, interval_seconds
from scheduler.governance import GovernanceError, validate_new_job


def _ms(y, mo, d, h, mi, tz=timezone.utc) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def test_interval_adds_duration():
    after = _ms(2026, 5, 27, 9, 0)
    nxt = compute_next_run_ms("interval", "15m", "UTC", after)
    assert nxt == after + 15 * 60 * 1000


def test_interval_units():
    after = _ms(2026, 5, 27, 9, 0)
    assert compute_next_run_ms("interval", "2h", "UTC", after) == after + 2 * 3600 * 1000
    assert compute_next_run_ms("interval", "1d", "UTC", after) == after + 86400 * 1000


def test_interval_invalid_raises():
    with pytest.raises(ScheduleError):
        compute_next_run_ms("interval", "soon", "UTC", _ms(2026, 5, 27, 9, 0))


def test_one_shot_future_returns_time():
    after = _ms(2026, 5, 27, 9, 0)
    nxt = compute_next_run_ms("one_shot", "2026-05-27T11:00:00Z", "UTC", after)
    assert _dt(nxt) == datetime(2026, 5, 27, 11, 0, tzinfo=timezone.utc)


def test_one_shot_past_returns_none():
    after = _ms(2026, 5, 27, 9, 0)
    assert compute_next_run_ms("one_shot", "2026-05-27T08:00:00Z", "UTC", after) is None


def test_cron_daily_at_7am_utc():
    after = _ms(2026, 5, 27, 9, 0)
    nxt = _dt(compute_next_run_ms("cron", "0 7 * * *", "UTC", after))
    assert (nxt.hour, nxt.minute) == (7, 0)
    assert nxt.date() == datetime(2026, 5, 28).date()


def test_cron_weekday_mornings_skips_weekend():
    after = _ms(2026, 5, 29, 8, 0)
    nxt = _dt(compute_next_run_ms("cron", "0 7 * * 1-5", "UTC", after))
    assert (nxt.hour, nxt.minute) == (7, 0)
    assert nxt.weekday() == 0


def test_cron_timezone_aware():
    pytest.importorskip("zoneinfo")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("IANA tz database not installed (add the `tzdata` package)")
    after = _ms(2026, 5, 27, 0, 0)
    nxt = _dt(compute_next_run_ms("cron", "0 9 * * *", "America/New_York", after))
    assert nxt.hour in (13, 14)


def test_cron_invalid_field_raises():
    with pytest.raises(ScheduleError):
        compute_next_run_ms("cron", "99 7 * * *", "UTC", _ms(2026, 5, 27, 9, 0))


def test_cron_must_have_five_fields():
    with pytest.raises(ScheduleError):
        compute_next_run_ms("cron", "0 7 * *", "UTC", _ms(2026, 5, 27, 9, 0))


def test_job_cap_enforced():
    with pytest.raises(GovernanceError) as ei:
        validate_new_job(active_job_count=25, max_active=25,
                         schedule_kind="cron", schedule_expr="0 7 * * *",
                         min_interval_seconds=60)
    assert ei.value.code == "job_cap_reached"


def test_interval_floor_enforced():
    with pytest.raises(GovernanceError) as ei:
        validate_new_job(active_job_count=0, max_active=25,
                         schedule_kind="interval", schedule_expr="30s",
                         min_interval_seconds=60)
    assert ei.value.code == "interval_too_small"


def test_valid_job_passes_governance():
    validate_new_job(active_job_count=3, max_active=25,
                     schedule_kind="interval", schedule_expr="5m",
                     min_interval_seconds=60)
    assert interval_seconds("interval", "5m") == 300
