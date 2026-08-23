"""LETS posture on readiness and the admin-only health route.

Both surfaces read the application-scoped composition that was bound at boot
(``orchestrator.lets_runtime``).  The only network they ever cause is the
bounded, cached reachability probe (``orchestrator.lets_probe``): when the
cached observation is older than ``LETS_HEALTH_PROBE_INTERVAL_SECONDS`` a
cheap ``GET /health/ready`` runs on a worker thread and the caller waits at
most ``PROBE_WAIT_SECONDS``; otherwise the cache answers instantly.  Off mode
binds no probe, so flag-off causes no network at all.  Neither surface reads
secret material or re-validates configuration files (the one exception is a
process that bound no composition, where the environment posture is loaded
once per call as a fail-closed fallback).  Every value they emit is a
bounded status code or a redacted configuration field.
"""

from __future__ import annotations

import asyncio
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


def refresh_lets_reachability(orchestrator: Any) -> None:
    """Refresh the cached warden observation when it is due (blocking, ≤ 2 s).

    Call off the event loop (``asyncio.to_thread``).  No-op without a bound
    composition, in off mode, or on the degraded graph (no client, no probe).
    """

    runtime = getattr(orchestrator, "lets_runtime", None)
    probe = getattr(runtime, "reachability", None)
    refresh = getattr(probe, "refresh_if_due", None)
    if callable(refresh):
        refresh()


def lets_snapshot(orchestrator: Any) -> LetsHealthSnapshot:
    runtime = getattr(orchestrator, "lets_runtime", None)
    fallback = None if runtime is not None else _fallback_load()
    return project_runtime_health(runtime, fallback=fallback)


def readyz_body(orchestrator: Any) -> tuple[dict[str, object], int]:
    """The ``/readyz`` body and HTTP status once persistence has answered.

    Flag-off keeps every pre-existing key unchanged and adds only
    ``{"mode": "off", "status": "disabled"}``.  Enforce with a blocked LETS
    posture is the one case that turns readiness into 503: governed effects
    cannot run, so the instance must not receive traffic.  Since the live
    probe landed that case is reached whenever the last probe found the
    warden down, unresolvable, mis-certified, or rejecting the credential —
    not only when composition bound no client.  Shadow degradation stays 200
    because existing behavior is unchanged.  This function reads the cache
    only; the caller refreshes it first via ``refresh_lets_reachability``.
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
    await asyncio.to_thread(refresh_lets_reachability, orchestrator)
    runtime = getattr(orchestrator, "lets_runtime", None)
    fallback = None if runtime is not None else _fallback_load()
    return health_report(runtime, fallback=fallback)


__all__ = (
    "get_lets_health",
    "lets_router",
    "lets_snapshot",
    "readyz_body",
    "refresh_lets_reachability",
)
