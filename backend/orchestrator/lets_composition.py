"""Application-scoped composition of Plane-backed LETS enforcement.

This module wires public component contracts only. AstralDeep retains rollout
policy and orchestration; AstralPlane owns durable transactions; LETS owns
finite authority and receipt verification. No component source or private
implementation module is imported here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Mapping

from astralplane import PlaneRuntime
from astralplane.authority import AuthorityRepository

from orchestrator.lets_client import (
    LetsClientBoundaryError,
    LetsWardenClient,
    create_lets_warden_client,
)
from orchestrator.lets_config import LetsConfigLoad, LetsHostConfig, load_lets_config
from orchestrator.lets_effects import PlaneProtectedEffectCoordinator
from orchestrator.lets_gateway import LetsAuthorizationGateway
from orchestrator.lets_probe import (
    LetsProbeConfigError,
    LetsReachabilityProbe,
    probe_interval_seconds,
)
from orchestrator.lets_lifecycle import (
    GovernedLifecycleCoordinator,
    LetsLifecycleError,
    LetsLifecycleService,
)
from orchestrator.lets_reconciler import LetsEffectReconciler, LetsLifecycleReconciler
from orchestrator.user_agents import GovernedByoAgentLifecycle


class LetsCompositionError(RuntimeError):
    """Stable startup refusal with no configuration or credential values."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(slots=True)
class LetsRuntimeComposition:
    """One application-scoped LETS runtime and its bounded reconcilers."""

    loaded: LetsConfigLoad
    plane: PlaneRuntime
    repository: AuthorityRepository
    client: LetsWardenClient | None
    authorization_gateway: LetsAuthorizationGateway | None
    lifecycle_service: LetsLifecycleService | None
    lifecycle: GovernedLifecycleCoordinator | None
    byo_lifecycle: GovernedByoAgentLifecycle | None
    lifecycle_reconciler: LetsLifecycleReconciler | None
    effect_reconciler: LetsEffectReconciler | None
    # Boot-time stamp used by the readiness projection when no live probe can
    # exist (the degraded graph bound no client): the last moment anything
    # about the warden was actually known.
    composed_at_ns: int = field(default_factory=time.time_ns)
    # Cached live reachability (orchestrator.lets_probe); None whenever no
    # client is bound (off mode, invalid configuration, degraded shadow).
    reachability: LetsReachabilityProbe | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _tasks: tuple[asyncio.Task[None], ...] = field(default=(), repr=False)

    @property
    def config(self) -> LetsHostConfig | None:
        return self.loaded.config

    @property
    def ready(self) -> bool:
        if not self.loaded.readiness.application_ready:
            return False
        if self.loaded.readiness.mode == "off":
            return True
        if self.loaded.config is None:
            return False
        return all(
            value is not None
            for value in (
                self.client,
                self.authorization_gateway,
                self.lifecycle,
                self.lifecycle_reconciler,
                self.effect_reconciler,
            )
        )

    def recovery_owner_ids(
        self,
        *,
        now: datetime | None = None,
        effect_stale_after: timedelta = timedelta(minutes=1),
        limit: int = 200,
    ) -> tuple[str, ...]:
        """Discover only owner partitions that contain due recovery work."""

        selected_at = datetime.now(UTC) if now is None else now
        if selected_at.tzinfo is None or selected_at.utcoffset() != timedelta(0):
            raise LetsCompositionError("invalid_recovery_time")
        if not isinstance(effect_stale_after, timedelta) or effect_stale_after <= timedelta(0):
            raise LetsCompositionError("invalid_effect_recovery_staleness")
        if self.loaded.readiness.mode == "off" or self.lifecycle is None:
            return ()
        with self.plane.transaction() as transaction:
            return self.repository.list_recovery_owners(
                transaction,
                lifecycle_due_at=selected_at,
                effect_updated_before=selected_at - effect_stale_after,
                limit=limit,
            )

    def start_reconcilers(
        self,
        *,
        interval_seconds: float = 15.0,
        effect_stale_after: timedelta = timedelta(minutes=1),
    ) -> tuple[asyncio.Task[None], ...]:
        """Start exactly one serialized lifecycle and effect recovery loop."""

        if self._tasks:
            return self._tasks
        if self.loaded.readiness.mode == "off" or not self.ready:
            return ()
        assert self.lifecycle_reconciler is not None
        assert self.effect_reconciler is not None
        self._stop.clear()

        def owners() -> tuple[str, ...]:
            return self.recovery_owner_ids(effect_stale_after=effect_stale_after)

        self._tasks = (
            asyncio.create_task(
                self.lifecycle_reconciler.run_forever(
                    owners,
                    stop=self._stop,
                    interval_seconds=interval_seconds,
                ),
                name="lets-lifecycle-reconciler",
            ),
            asyncio.create_task(
                self.effect_reconciler.run_forever(
                    owners,
                    stop=self._stop,
                    interval_seconds=interval_seconds,
                    stale_after=effect_stale_after,
                ),
                name="lets-effect-reconciler",
            ),
        )
        return self._tasks

    async def stop(self) -> None:
        """Stop recovery loops and close the warden client once."""

        self._stop.set()
        tasks, self._tasks = self._tasks, ()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.reachability is not None:
            self.reachability.close()
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except LetsClientBoundaryError as exc:
                raise LetsCompositionError(exc.code) from None


def compose_lets_runtime(
    *,
    plane: PlaneRuntime,
    repository: AuthorityRepository,
    environ: Mapping[str, str] | None = None,
) -> LetsRuntimeComposition:
    """Build the active LETS graph or return the explicit off/degraded graph."""

    if not isinstance(plane, PlaneRuntime):
        raise TypeError("initialized Plane runtime is required")
    if not isinstance(repository, AuthorityRepository):
        raise TypeError("Plane authority repository is required")
    loaded = load_lets_config(environ)
    if not loaded.readiness.application_ready:
        raise LetsCompositionError(loaded.readiness.reason)
    config = loaded.config
    # Off mode never parses the probe knob: flag-off stays byte-identical.
    probe_interval: float | None = None
    if config is not None and config.mode != "off":
        try:
            probe_interval = probe_interval_seconds(environ)
        except LetsProbeConfigError as exc:
            raise LetsCompositionError(exc.code) from None
    if config is None:
        # Invalid shadow configuration is deliberately nonblocking and binds no
        # client, Plane operation, or fabricated success evidence.
        return LetsRuntimeComposition(
            loaded=loaded,
            plane=plane,
            repository=repository,
            client=None,
            authorization_gateway=None,
            lifecycle_service=None,
            lifecycle=None,
            byo_lifecycle=None,
            lifecycle_reconciler=None,
            effect_reconciler=None,
        )
    if config.mode == "off":
        service = LetsLifecycleService(
            config=config,
            plane=None,
            repository=None,
            client=None,
        )
        lifecycle = GovernedLifecycleCoordinator(service)
        return LetsRuntimeComposition(
            loaded=loaded,
            plane=plane,
            repository=repository,
            client=None,
            authorization_gateway=LetsAuthorizationGateway(config, None),
            lifecycle_service=service,
            lifecycle=lifecycle,
            byo_lifecycle=GovernedByoAgentLifecycle(lifecycle),
            lifecycle_reconciler=None,
            effect_reconciler=None,
        )

    client: LetsWardenClient | None = None
    try:
        client = create_lets_warden_client(config)
        effects = PlaneProtectedEffectCoordinator(
            plane=plane,
            repository=repository,
        )
        gateway = LetsAuthorizationGateway(
            config,
            client,
            effect_coordinator=effects,
        )
        lifecycle_service = LetsLifecycleService(
            config=config,
            plane=plane,
            repository=repository,
            client=client,
        )
        lifecycle = GovernedLifecycleCoordinator(lifecycle_service)
        assert probe_interval is not None
        probe = getattr(client, "probe", None)
        if not callable(probe):
            raise LetsClientBoundaryError("client_configuration")
        reachability = LetsReachabilityProbe(probe, interval_seconds=probe_interval)
        # First live observation at composition. Bounded (≤ PROBE_WAIT_SECONDS)
        # and never fatal: in shadow a failed probe is a degraded reading, in
        # enforce it is reported as blocked on /readyz — boot semantics are
        # deliberately unchanged (a composed enforce graph still boots; the
        # readiness probe, not the constructor, takes the instance out of
        # rotation until the warden answers).
        reachability.refresh_if_due(force=True)
        return LetsRuntimeComposition(
            loaded=loaded,
            plane=plane,
            repository=repository,
            client=client,
            authorization_gateway=gateway,
            lifecycle_service=lifecycle_service,
            lifecycle=lifecycle,
            byo_lifecycle=GovernedByoAgentLifecycle(lifecycle),
            lifecycle_reconciler=LetsLifecycleReconciler(
                plane=plane,
                repository=repository,
                lifecycle=lifecycle_service,
            ),
            effect_reconciler=LetsEffectReconciler(
                plane=plane,
                repository=repository,
            ),
            reachability=reachability,
        )
    except (LetsClientBoundaryError, LetsLifecycleError) as exc:
        if client is not None:
            try:
                client.close()
            except LetsClientBoundaryError:
                pass
        code = getattr(exc, "code", "lets_runtime_unavailable")
        if config.mode == "shadow":
            return LetsRuntimeComposition(
                loaded=loaded,
                plane=plane,
                repository=repository,
                client=None,
                authorization_gateway=None,
                lifecycle_service=None,
                lifecycle=None,
                byo_lifecycle=None,
                lifecycle_reconciler=None,
                effect_reconciler=None,
            )
        raise LetsCompositionError(
            code,
            retryable=bool(getattr(exc, "retryable", False)),
        ) from None
    except Exception:
        if client is not None:
            try:
                client.close()
            except LetsClientBoundaryError:
                pass
        if config.mode == "shadow":
            return LetsRuntimeComposition(
                loaded=loaded,
                plane=plane,
                repository=repository,
                client=None,
                authorization_gateway=None,
                lifecycle_service=None,
                lifecycle=None,
                byo_lifecycle=None,
                lifecycle_reconciler=None,
                effect_reconciler=None,
            )
        raise LetsCompositionError("lets_runtime_unavailable", retryable=True) from None


__all__ = (
    "LetsCompositionError",
    "LetsRuntimeComposition",
    "compose_lets_runtime",
)
