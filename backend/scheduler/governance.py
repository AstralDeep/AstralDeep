"""Pure validation for scheduled-job limits: per-user active-job cap and minimum
recurring-interval floor, both raising GovernanceError; called from scheduler/api.py
before a job is created.
"""

from __future__ import annotations

from .cron import interval_seconds


class GovernanceError(ValueError):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def check_job_cap(active_job_count: int, max_active: int) -> None:
    if active_job_count >= max_active:
        raise GovernanceError(
            "job_cap_reached",
            f"You already have the maximum of {max_active} active scheduled jobs.",
            limit=max_active,
        )


def check_interval_floor(schedule_kind: str, schedule_expr: str, min_interval_seconds: int) -> None:
    secs = interval_seconds(schedule_kind, schedule_expr)
    if secs is None:
        return
    if secs < min_interval_seconds:
        raise GovernanceError(
            "interval_too_small",
            f"Recurring jobs must be at least {min_interval_seconds} seconds apart.",
            min_interval_seconds=min_interval_seconds,
        )


def validate_new_job(
    *,
    active_job_count: int,
    max_active: int,
    schedule_kind: str,
    schedule_expr: str,
    min_interval_seconds: int,
) -> None:
    check_job_cap(active_job_count, max_active)
    check_interval_floor(schedule_kind, schedule_expr, min_interval_seconds)
