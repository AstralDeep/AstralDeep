"""LETS posture on readiness and the admin-only health route.

Both surfaces read only the application-scoped composition that was bound at
boot (``orchestrator.lets_runtime``); neither opens a warden connection, reads
secret material, or re-validates configuration files (the only exception is a
process that bound no composition at all, where the environment posture is
loaded once per call as a fail-closed fallback).  Every value they emit is
a bounded status code or a redacted configuration field.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.auth import verify_admin
from orchestrator.lets_config import LetsConfigError, LetsConfigLoad, LetsReadiness
from orchestrator.lets_health import (
    LetsHealthSnapshot,
    health_report,
    project_runtime_health,
    readiness_entry,
)

lets_router = APIRouter(tags=["System"])


def _fallback_load() -> LetsConfigLoad:
    """Configuration posture for a process that bound no composition.

    The real boot always binds one (``Orchestrator.__init__`` composes before
    serving), so this only covers partially constructed instances.  A load
    refusal is projected fail-closed as an enforce block: the process cannot
    prove any posture, so it must not claim readiness.
    """

    from orchestrator.lets_config import load_lets_config

    try:
        return load_lets_config()
    except LetsConfigError as exc:
        return LetsConfigLoad(
            config=None,
            readiness=LetsReadiness(
                mode="enforce",
                status="blocked",
                reason=exc.code,
                application_ready=False,
                lets_configured=False,
                governed_effects_permitted=False,
                diagnostic_only=False,
            ),
        )


def lets_snapshot(orchestrator: Any) -> LetsHealthSnapshot:
    runtime = getattr(orchestrator, "lets_runtime", None)
    fallback = None if runtime is not None else _fallback_load()
    return project_runtime_health(runtime, fallback=fallback)


def readyz_body(orchestrator: Any) -> tuple[dict[str, object], int]:
    """The ``/readyz`` body and HTTP status once persistence has answered.

    Flag-off keeps every pre-existing key unchanged and adds only
    ``{"mode": "off", "status": "disabled"}``.  Enforce with a blocked LETS
    posture is the one case that turns readiness into 503: governed effects
    cannot run, so the instance must not receive traffic.  Shadow degradation
    stays 200 because existing behavior is unchanged.
    """

    snapshot = lets_snapshot(orchestrator)
    body: dict[str, object] = {
        "status": "ok" if snapshot.application_ready else "degraded",
        "db": "ok",
        "generated_agent_publication": "ok",
        "agents": len(orchestrator.agent_cards),
        "lets": readiness_entry(snapshot),
    }
    return body, (200 if snapshot.application_ready else 503)


@lets_router.get(
    "/lets/health",
    include_in_schema=False,
)
async def get_lets_health(
    request: Request,
    _admin: dict = Depends(verify_admin),
) -> dict[str, object]:
    """Full redacted LETS projection (admin role required)."""

    orchestrator = request.app.state.orchestrator
    runtime = getattr(orchestrator, "lets_runtime", None)
    fallback = None if runtime is not None else _fallback_load()
    return health_report(runtime, fallback=fallback)


__all__ = ("get_lets_health", "lets_router", "lets_snapshot", "readyz_body")
