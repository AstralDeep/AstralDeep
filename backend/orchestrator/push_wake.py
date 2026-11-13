"""Content-free wake gateway for a node's mesh: when the node holds events for a device that is not connected and that device's owner registered a push token, it sends one APNs background push that carries no content, at most a few per device per hour.
It reuses the push destinations and APNs credentials of push_destinations.py and push_providers.py, and the node runtimes in node_mesh_chat.py and institution_runtime.py attach a trigger to each mesh peer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

from orchestrator.push_providers import PushProviderError

logger = logging.getLogger("Orchestrator.PushWake")

ENABLE_ENV = "ASTRAL_NODE_WAKE"
MIN_INTERVAL_ENV = "ASTRAL_NODE_WAKE_MIN_INTERVAL_SECONDS"
MAX_PER_HOUR_ENV = "ASTRAL_NODE_WAKE_MAX_PER_HOUR"
DEFAULT_MIN_INTERVAL_SECONDS = 300.0
DEFAULT_MAX_PER_HOUR = 4
HOUR_SECONDS = 3600.0
MAX_TRACKED_DEVICES = 4096
ID_PREFIX = "sha256:"

SENT = "sent"
RATE_LIMITED = "rate_limited"
NO_DESTINATION = "no_destination"
UNSUPPORTED = "unsupported_provider"
FAILED = "failed"


@dataclass(frozen=True)
class WakeOutcome:
    status: str
    code: Optional[str] = None


class WakeTargets(Protocol):
    def target(self, owner_id: str, mesh_device_id: str) -> Optional[Mapping[str, Any]]: ...


def destination_id(mesh_device_id: str) -> str:
    return mesh_device_id[len(ID_PREFIX):] if mesh_device_id.startswith(ID_PREFIX) else mesh_device_id


class DestinationTargets:
    def __init__(self, destinations: Any):
        self._destinations = destinations

    def target(self, owner_id: str, mesh_device_id: str) -> Optional[Mapping[str, Any]]:
        return self._destinations.delivery_target(owner_id, destination_id(mesh_device_id))


class WakeLimiter:
    def __init__(
        self,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._interval = max(0.0, min_interval_seconds)
        self._cap = max(1, max_per_hour)
        self._clock = clock
        self._sent: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WakeLimiter":
        values = os.environ if env is None else env

        def number(name: str, default: float) -> float:
            try:
                parsed = float(values.get(name, "") or default)
            except ValueError:
                return default
            return parsed if parsed >= 0 else default

        return cls(
            min_interval_seconds=number(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_SECONDS),
            max_per_hour=int(number(MAX_PER_HOUR_ENV, DEFAULT_MAX_PER_HOUR)) or DEFAULT_MAX_PER_HOUR,
        )

    def admit(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._sent) >= MAX_TRACKED_DEVICES:
                for stale in [k for k, hits in self._sent.items() if not hits or now - hits[-1] >= HOUR_SECONDS]:
                    self._sent.pop(stale, None)
            hits = [t for t in self._sent.get(key, []) if now - t < HOUR_SECONDS]
            if hits and now - hits[-1] < self._interval:
                self._sent[key] = hits
                return False
            if len(hits) >= self._cap:
                self._sent[key] = hits
                return False
            hits.append(now)
            self._sent[key] = hits
            return True


class WakeGateway:
    def __init__(self, *, targets: WakeTargets, adapters: Mapping[str, Any], limiter: Optional[WakeLimiter] = None):
        self._targets = targets
        self._adapters = adapters
        self._limiter = limiter or WakeLimiter()

    @property
    def available(self) -> bool:
        return bool(getattr(self._adapters.get("apns"), "configured", False))

    async def wake(self, owner_id: str, mesh_device_id: str) -> WakeOutcome:
        key = f"{owner_id}\n{mesh_device_id}"
        if not self._limiter.admit(key):
            return WakeOutcome(RATE_LIMITED)
        try:
            target = await asyncio.to_thread(self._targets.target, owner_id, mesh_device_id)
        except Exception as exc:
            logger.warning("push_wake_lookup_failed: %s", type(exc).__name__)
            return WakeOutcome(FAILED, "destination_unavailable")
        if target is None:
            return WakeOutcome(NO_DESTINATION)
        adapter = self._adapters.get(str(target.get("provider")))
        if str(target.get("provider")) != "apns" or adapter is None:
            return WakeOutcome(UNSUPPORTED)
        try:
            await asyncio.to_thread(adapter.send_wake, device_token=target["push_token"])
        except PushProviderError as exc:
            logger.warning("push_wake_failed: %s", exc.code)
            return WakeOutcome(FAILED, exc.code)
        return WakeOutcome(SENT)


class WakeTrigger:
    def __init__(self, mesh: Any, gateway: WakeGateway, owner: Callable[[], Optional[str]]):
        self._mesh = mesh
        self._gateway = gateway
        self._owner = owner
        self._pending: Dict[str, int] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dirty = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mesh.subscribe(self._on_change)
        self._task = asyncio.create_task(self._run(), name="node-wake-trigger")
        self._dirty.set()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._loop = None

    def _on_change(self, change: Any) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._dirty.set)

    async def _run(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("push_wake_evaluate_failed")

    async def evaluate(self) -> List[WakeOutcome]:
        owner = self._owner()
        if owner is None:
            return []
        sent: List[WakeOutcome] = []
        for peer in await self._mesh.sync_peers():
            waiting = 0 if peer.connected else int(peer.pending_out)
            before = self._pending.get(peer.device_id, 0)
            self._pending[peer.device_id] = waiting
            if waiting > before:
                outcome = await self._gateway.wake(owner, peer.device_id)
                if outcome.status == RATE_LIMITED:
                    self._pending[peer.device_id] = before
                sent.append(outcome)
        return sent


class WakeTriggers:
    def __init__(self, gateway: WakeGateway):
        self._gateway = gateway
        self._triggers: List[WakeTrigger] = []

    def attach(self, mesh: Any, owner: Callable[[], Optional[str]]) -> WakeTrigger:
        trigger = WakeTrigger(mesh, self._gateway, owner)
        trigger.start()
        self._triggers.append(trigger)
        return trigger

    async def stop(self) -> None:
        triggers, self._triggers = self._triggers, []
        for trigger in triggers:
            await trigger.stop()


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    values = os.environ if env is None else env
    return values.get(ENABLE_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def gateway_for(orch: Any, *, adapters: Optional[Mapping[str, Any]] = None) -> Optional[WakeGateway]:
    destinations = getattr(orch, "push_destinations", None)
    if destinations is None or not enabled():
        return None
    if adapters is None:
        from orchestrator.push_providers import adapters_from_env

        adapters = adapters_from_env()
    gateway = WakeGateway(targets=DestinationTargets(destinations), adapters=adapters, limiter=WakeLimiter.from_env())
    return gateway if gateway.available else None
