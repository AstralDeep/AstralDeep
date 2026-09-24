"""Test-only composition of a scheduler ScheduledJobStore and work-admission repository
over an isolated Plane runtime, shared by the scheduler test suite (occurrence
claims, fairness, atomic chat publication).
"""

from __future__ import annotations

from typing import Any

from orchestrator.work_admission import PlaneWorkAdmissionRepository
from tests.helpers.voice_plane_runtime import PlaneTestRuntime


def ensure_plane_runtime(runtime: PlaneTestRuntime) -> PlaneTestRuntime:
    if not hasattr(runtime.repositories, "scheduler"):
        raise TypeError("scheduler repository is missing from the Plane catalog")
    return runtime


def work_admission_repository(
    runtime: PlaneTestRuntime,
) -> PlaneWorkAdmissionRepository:
    runtime = ensure_plane_runtime(runtime)
    return PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def scheduled_job_store(
    runtime: PlaneTestRuntime,
    *,
    coordinator: Any | None = None,
) -> Any:
    from scheduler.store import ScheduledJobStore

    runtime = ensure_plane_runtime(runtime)
    return ScheduledJobStore(
        coordinator=coordinator,
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


__all__ = (
    "ensure_plane_runtime",
    "scheduled_job_store",
    "work_admission_repository",
)
