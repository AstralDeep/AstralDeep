"""Application-scoped AstralPlane and LETS composition for AstralDeep."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.lets_composition import (
    LetsRuntimeComposition,
    compose_lets_runtime,
)
from orchestrator.plane_composition import (
    InitializedPlaneComposition,
    compose_plane_from_environment,
)


@dataclass(slots=True)
class AstralRuntimeComposition:
    """One initialized data plane and its product-owned LETS adapters."""

    plane: InitializedPlaneComposition
    lets: LetsRuntimeComposition
    _bound: bool = False
    _lifecycle_state: str = "open"
    _close_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def bind(self, orchestrator: Any) -> None:
        """Publish component seams only after every dependency is ready."""

        if self._lifecycle_state != "open":
            raise RuntimeError("runtime composition is closing")
        if self._bound:
            return
        lifecycle_manager = getattr(orchestrator, "lifecycle_manager", None)
        if lifecycle_manager is None:
            raise RuntimeError("agent lifecycle manager is unavailable")
        if self.lets.lifecycle is not None:
            lifecycle_manager.bind_governed_lifecycle(self.lets.lifecycle)
        orchestrator.governed_byo_lifecycle = self.lets.byo_lifecycle
        orchestrator.plane_runtime = self.plane.runtime
        orchestrator.plane_repositories = self.plane.repositories
        orchestrator.attachment_materialization_service = self.plane.attachment_materializer
        orchestrator.attachment_purge_coordinator = self.plane.attachment_purges
        orchestrator.lets_runtime = self.lets

        config = self.lets.config
        gateway = self.lets.authorization_gateway
        if config is not None and config.mode != "off" and gateway is not None:
            orchestrator.bind_governed_final_dispatch(
                gateway=gateway,
                plane=self.plane.runtime,
                authority_repository=self.plane.repositories.authority,
            )
        self._bound = True

    def start(self) -> tuple[object, ...]:
        """Start bounded recovery only after local binding is complete."""

        if self._lifecycle_state != "open":
            raise RuntimeError("runtime composition is closing")
        if not self._bound:
            raise RuntimeError("runtime composition is not bound")
        purge_task = self.plane.attachment_purges.start()
        return (*tuple(self.lets.start_reconcilers()), purge_task)

    def abort(self) -> None:
        """Synchronously release a graph that never entered background work."""

        if self._lifecycle_state == "closed":
            return
        if self._lifecycle_state != "open":
            raise RuntimeError("runtime composition is closing")
        if getattr(self.lets, "_tasks", ()) or self.plane.attachment_purges.started:
            raise RuntimeError("started runtime composition requires async close")
        self._lifecycle_state = "closing"
        errors: list[BaseException] = []
        try:
            self.plane.attachment_purges.abort()
        except BaseException as exc:
            errors.append(exc)
        try:
            client = self.lets.client
            if client is not None:
                client.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            _unbind_plane_consumers(self.plane)
        except BaseException as exc:
            errors.append(exc)
        try:
            self.plane.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            self._lifecycle_state = "close_failed"
            _raise_cleanup_errors("runtime composition abort failed", errors)
        self._lifecycle_state = "closed"

    async def close(self) -> None:
        """Join one cancellation-safe teardown shared by every close caller."""

        loop = asyncio.get_running_loop()
        task = self._close_task
        if task is None:
            if self._lifecycle_state == "closed":
                return
            if self._lifecycle_state not in {"open", "close_failed"}:
                raise RuntimeError("runtime composition is closing")
            self._lifecycle_state = "closing"
            task = loop.create_task(
                self._close_components(),
                name="astral-runtime-composition-close",
            )
            self._close_task = task
        await _join_close_through_cancellation(task)

    async def _close_components(self) -> None:
        """Close the graph in dependency order without blocking the event loop."""

        current = asyncio.current_task()
        errors: list[BaseException] = []
        try:
            await self.plane.attachment_materializer.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            await _run_sync_close(self.plane.attachment_materializations.close)
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.plane.attachment_purges.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.lets.stop()
        except BaseException as exc:
            errors.append(exc)
        try:
            _unbind_plane_consumers(self.plane)
        except BaseException as exc:
            errors.append(exc)
        try:
            await _run_sync_close(self.plane.close)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            self._lifecycle_state = "close_failed"
            if self._close_task is current:
                self._close_task = None
            _raise_cleanup_errors("runtime composition close failed", errors)
        self._lifecycle_state = "closed"


def _unbind_plane_consumers(plane: InitializedPlaneComposition) -> None:
    """Release process bindings owned by this exact application composition."""

    from agents.general.file_tools import unregister_plane_dependencies
    from shared.attachment_materializer import unregister_materialization_service
    from shared.attachment_resolver import unregister_plane_runtime

    errors: list[BaseException] = []
    for callback, arguments in (
        (unregister_materialization_service, (plane.attachment_materializer,)),
        (
            unregister_plane_runtime,
            (plane.runtime, plane.repositories, plane.blobs),
        ),
        (
            unregister_plane_dependencies,
            (plane.runtime, plane.repositories, plane.blobs),
        ),
    ):
        try:
            callback(*arguments)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        _raise_cleanup_errors("Plane process consumer unbind failed", errors)


def _raise_cleanup_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup(message, errors)


async def _run_sync_close(callback: Any) -> None:
    """Run final blocking Plane/blob/pool teardown on its own one-shot lane."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="astral-runtime-close",
    )
    try:
        await loop.run_in_executor(executor, callback)
    finally:
        # The submitted callback has completed before this boundary, so this
        # never waits on the event-loop thread.  A dedicated lane prevents
        # shutdown from consuming the shared default executor used elsewhere.
        executor.shutdown(wait=False, cancel_futures=False)


async def close_blocking_component(callback: Any) -> None:
    """Join one blocking component close through repeated task cancellation."""

    task = asyncio.create_task(
        _run_sync_close(callback),
        name="astral-blocking-component-close",
    )
    await _join_close_through_cancellation(task)


async def _join_close_through_cancellation(task: asyncio.Task[None]) -> None:
    """Observe the shared teardown before propagating repeated cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    task.result()
    if cancellation is not None:
        raise cancellation


def compose_astral_runtime(
    manifest_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AstralRuntimeComposition:
    """Construct the full component graph without leaking a partial Plane."""

    plane = compose_plane_from_environment(manifest_path, environ=environ)
    try:
        lets = compose_lets_runtime(
            plane=plane.runtime,
            repository=plane.repositories.authority,
            environ=environ,
        )
    except BaseException:
        plane.close()
        raise
    return AstralRuntimeComposition(plane=plane, lets=lets)


__all__ = (
    "AstralRuntimeComposition",
    "close_blocking_component",
    "compose_astral_runtime",
)
