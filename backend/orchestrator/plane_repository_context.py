"""Binds a typed repository to the initialized AstralPlane runtime and runs its
operations inside plane_runtime.transaction(). repository_from() and
plane_source_from_orchestrator() resolve that runtime for callers across the backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")


class PlaneRepositoryContext:
    def __init__(
        self,
        *,
        repository: Any,
        plane_runtime: Any | None = None,
        legacy_database: Any | None = None,
    ) -> None:
        runtime = plane_runtime or getattr(legacy_database, "plane_runtime", None)
        if runtime is None:
            raise ValueError("an initialized Plane runtime is required")
        self.repository = repository
        self._runtime = runtime

    @property
    def plane_runtime(self) -> Any:
        return self._runtime

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._runtime.transaction() as transaction:
            yield transaction

    def call(self, operation: Callable[..., _T], /, **kwargs: object) -> _T:
        with self.transaction() as transaction:
            return operation(transaction, **kwargs)

    async def call_async(
        self,
        operation: Callable[..., _T],
        /,
        **kwargs: object,
    ) -> _T:
        return await asyncio.to_thread(self.call, operation, **kwargs)


@dataclass(frozen=True, slots=True)
class ApplicationPlaneSource:
    plane_runtime: Any
    plane_repositories: Any


def plane_source_from_orchestrator(orchestrator: Any) -> Any:
    injected = getattr(orchestrator, "plane_repository_source", None)
    if injected is not None:
        return injected
    composition = getattr(orchestrator, "runtime_composition", None)
    plane = getattr(composition, "plane", None)
    runtime = getattr(plane, "runtime", None)
    repositories = getattr(plane, "repositories", None)
    if runtime is None or repositories is None:
        raise RuntimeError("the application AstralPlane runtime is not initialized")
    return ApplicationPlaneSource(
        plane_runtime=runtime,
        plane_repositories=repositories,
    )


def repository_from(
    name: str,
    *,
    plane_runtime: Any | None,
    repositories: Any | None,
    legacy_database: Any | None = None,
) -> tuple[Any, Any]:
    runtime = plane_runtime or getattr(legacy_database, "plane_runtime", None)
    if runtime is None:
        raise ValueError("an initialized Plane runtime is required")
    catalog = repositories or getattr(legacy_database, "plane_repositories", None)
    if catalog is None:
        catalog = getattr(runtime, "repositories", None)
    if catalog is None:
        raise ValueError("the initialized Plane repository catalog is required")
    return getattr(catalog, name), runtime


__all__ = (
    "ApplicationPlaneSource",
    "PlaneRepositoryContext",
    "plane_source_from_orchestrator",
    "repository_from",
)
