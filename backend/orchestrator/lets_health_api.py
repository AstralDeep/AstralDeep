"""Serves LETS posture on /readyz and the admin-only health route by reading the
composition bound at boot; the only network call is the cached reachability probe in
lets_probe.py, and off mode causes none. Used by orchestrator.py.
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
