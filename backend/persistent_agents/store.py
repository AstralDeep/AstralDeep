"""Bounded async access to the composition-owned assignment repository."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from astralplane.async_runtime import AsyncPlaneRuntime
from astralplane.errors import PlaneError
from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from orchestrator.plane_repository_context import plane_source_from_orchestrator

from .models import AssignmentError

_T = TypeVar("_T")


class AssignmentStore:
    def __init__(self, orchestrator=None, *, plane_runtime=None, plane_repositories=None,
                 async_runtime=None):
        if orchestrator is not None:
            source = plane_source_from_orchestrator(orchestrator)
            plane_runtime = source.plane_runtime
            plane_repositories = source.plane_repositories
        if plane_runtime is None:
            raise AssignmentError("assignment_runtime_unavailable", 503)
        catalog = plane_repositories or plane_runtime.repositories
        self.repository = getattr(catalog, "assignments", None)
        if self.repository is None:
            raise AssignmentError("assignment_repository_unavailable", 503)
        self.async_runtime = async_runtime or AsyncPlaneRuntime(
            plane_runtime, maximum_concurrency=8, admission_timeout_seconds=2.0)

    async def transaction(self, callback: Callable[[Any, Any], _T]) -> _T:
        try:
            return await self.async_runtime.run_in_transaction(
                lambda transaction: callback(transaction, self.repository))
        except AssignmentError:
            raise
        except PlaneError as exc:
            code = exc.code
            if "not_found" in code:
                status = 404
            elif any(word in code for word in ("capacity", "quota", "budget", "rate_limit")):
                status = 429
            elif isinstance(exc, RepositoryValidationError):
                status = 422
            elif isinstance(exc, RepositoryConflictError):
                status = 409
            else:
                status = 503
            raise AssignmentError(code, status) from exc

    async def call(self, method_name: str, **kwargs):
        method = getattr(self.repository, method_name, None)
        if method is None or method_name.startswith("_"):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)
        return await self.transaction(lambda transaction, _: method(transaction, **kwargs))

    def close(self):
        self.async_runtime.close()
