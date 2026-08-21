"""Test-only scheduler composition over an isolated current Plane runtime."""

from __future__ import annotations

from typing import Any

from orchestrator.work_admission import PlaneWorkAdmissionRepository
from tests.helpers.voice_plane_runtime import PlaneTestRuntime


def ensure_plane_runtime(runtime: PlaneTestRuntime) -> PlaneTestRuntime:
    """Validate and reuse the already-composed application Plane boundary."""

    if not hasattr(runtime.repositories, "scheduler"):
        raise TypeError("scheduler repository is missing from the Plane catalog")
    return runtime


def work_admission_repository(
    runtime: PlaneTestRuntime,
) -> PlaneWorkAdmissionRepository:
    """Return the production Plane adapter against the isolated scheduler DB."""

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
    """Construct the strict product store from the isolated Plane runtime."""

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
