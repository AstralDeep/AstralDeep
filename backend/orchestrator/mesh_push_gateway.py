"""Sends publisher-owned silent APNs wakes only after capability admission and a final current-target check.
Personal and institution nodes cannot instantiate this publisher; push_wake.py uses their separate HTTPS client."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from orchestrator.mesh_push_registry import MeshPushError, MeshPushRegistry
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.push_providers import PushProviderError
from shared import node_identity

PUBLISHER_ENABLED_ENV = "ASTRAL_MESH_PUSH_PUBLISHER_ENABLED"


class PublisherWakeLimiter:
    def __init__(self, *, capacity: int = 4096, burst: int = 120, window_seconds: float = 60,
                 clock: Callable[[], float] = time.monotonic):
        self._capacity = capacity
        self._burst = burst
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def admit(self, caller: str) -> bool:
        now = self._clock()
        with self._lock:
            self._hits = {key: [hit for hit in hits if now - hit < self._window]
                          for key, hits in self._hits.items() if hits and now - hits[-1] < self._window}
            if caller not in self._hits and len(self._hits) >= self._capacity:
                return False
            hits = self._hits.get(caller, [])
            if len(hits) >= self._burst:
                return False
            self._hits[caller] = [*hits, now]
            return True


class MeshPushPublisher:
    def __init__(self, *, registry: MeshPushRegistry, adapter: Any, limiter: PublisherWakeLimiter | None = None):
        self.registry = registry
        self.adapter = adapter
        self.limiter = limiter or PublisherWakeLimiter()

    @property
    def available(self) -> bool:
        return bool(getattr(self.adapter, "configured", False))

    async def wake(self, capability: str, *, mesh_id: str, device_id: str) -> None:
        if not self.available:
            raise MeshPushError("publisher_unavailable", 503)
        target = await asyncio.to_thread(self.registry.admit, capability, mesh_id=mesh_id, device_id=device_id)

        def dispatch():
            if not self.registry.current(target):
                raise MeshPushError("capability_invalid", 403)
            if not self.available:
                raise MeshPushError("publisher_unavailable", 503)
            self.adapter.send_wake(device_token=target.token)

        try:
            await asyncio.to_thread(dispatch)
        except PushProviderError as error:
            if error.code == "token_unregistered":
                await asyncio.to_thread(self.registry.invalidate, target)
                raise MeshPushError("destination_unregistered", 410) from None
            raise MeshPushError("publisher_unavailable", 503) from None
        except MeshPushError:
            raise
        except Exception:
            raise MeshPushError("publisher_unavailable", 503) from None


def publisher_for(orch: Any, *, enabled: bool | None = None, adapter: Any = None) -> MeshPushPublisher | None:
    if node_identity.is_personal() or node_identity.is_institution():
        return None
    if enabled is None:
        enabled = os.environ.get(PUBLISHER_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    if adapter is None:
        from orchestrator.push_providers import adapters_from_env

        adapter = adapters_from_env().get("apns")
    if not getattr(adapter, "configured", False):
        return None
    try:
        plane = plane_source_from_orchestrator(orch)
        registry = MeshPushRegistry(plane_runtime=plane.plane_runtime, audit_repository=orch.audit_repo)
        return MeshPushPublisher(registry=registry, adapter=adapter)
    except (AttributeError, ValueError, RuntimeError):
        return None
