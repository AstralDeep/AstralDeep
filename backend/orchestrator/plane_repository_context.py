"""Small semantic seam for AstralPlane repository transactions.

Every operation runs through the one initialized ``astralplane.PlaneRuntime``.
The module deliberately contains no connection borrowing, SQL execution, or
placeholder conversion; durable mechanics belong exclusively to AstralPlane.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")


class PlaneRepositoryContext:
    """Bind one typed repository to the application-scoped Plane runtime."""

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
    """Repository-source view of Deep's one application Plane composition."""

    plane_runtime: Any
    plane_repositories: Any


def plane_source_from_orchestrator(orchestrator: Any) -> Any:
    """Resolve the application Plane source without borrowing a Deep database.

    Narrow unit tests may inject ``plane_repository_source`` directly.  Normal
    application callers resolve the initialized runtime/catalog pair from
    ``runtime_composition.plane`` and fail closed when it is unavailable.
    """

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
    """Resolve a named repository from an initialized application runtime.

    ``legacy_database`` is accepted temporarily only as an attribute carrier
    for callers that have not renamed their constructor argument yet.  It must
    expose the already-created Plane runtime/catalog and is never queried.
    """

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
