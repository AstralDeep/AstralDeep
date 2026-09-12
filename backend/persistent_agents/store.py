"""Bounded async access to the composition-owned assignment repository."""
from __future__ import annotations

import inspect
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
        self.plane_runtime = plane_runtime
        catalog = plane_repositories or plane_runtime.repositories
        self.repository = getattr(catalog, "assignments", None)
        if self.repository is None:
            raise AssignmentError("assignment_repository_unavailable", 503)
        self.async_runtime = async_runtime or AsyncPlaneRuntime(
            plane_runtime, maximum_concurrency=8, admission_timeout_seconds=2.0)

    async def transaction(
        self, callback: Callable[[Any, Any], _T], *, bound_session_waits: bool = False,
    ) -> _T:
        """Run repository work, optionally bounding SQL before any consent receipt read."""
        def work(transaction):
            if bound_session_waits:
                self.plane_runtime.repositories.history.sessions.bound_request_execution_waits(transaction)
            return callback(transaction, self.repository)

        try:
            return await self.async_runtime.run_in_transaction(work)
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
        except Exception:
            if bound_session_waits:
                # Driver lock/statement timeouts need not be PlaneError. Keep
                # bounded request failures data-free at the HTTP boundary.
                raise AssignmentError("assignment_transaction_unavailable", 503) from None
            raise

    async def call(self, method_name: str, **kwargs):
        method = getattr(self.repository, method_name, None)
        if method is None or method_name.startswith("_"):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)
        return await self.transaction(lambda transaction, _: method(transaction, **kwargs))

    async def current_execution_transaction(
        self, *, fence, binding, callback: Callable[[Any, Any, Any], _T], action_id=None,
    ) -> _T:
        """Guard and mutate through one bounded, caller-owned Plane transaction.

        Remote authorization must complete before this call. The callback is
        synchronous repository work only; it may not open another pool or do I/O.
        Older Plane pins cannot fall back to an inverted or incomplete guard.
        """
        guard = getattr(self.repository, "assert_current_assignment_execution", None)
        if not callable(guard):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)

        def guarded(transaction, repository):
            current = guard(transaction, fence=fence, binding=binding, action_id=action_id)
            if inspect.isawaitable(current):
                if inspect.iscoroutine(current):
                    current.close()
                raise AssignmentError("assignment_repository_contract_unavailable", 503)
            result = callback(transaction, repository, current)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise AssignmentError("assignment_transaction_callback_invalid", 500)
            return result

        return await self.transaction(guarded)

    def close(self):
        self.async_runtime.close()
