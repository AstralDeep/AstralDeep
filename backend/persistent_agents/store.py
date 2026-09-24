"""Bounded async wrapper over the composition-owned assignment repository; translates
driver/timeout failures into data-free AssignmentError at the HTTP boundary. Used by
AssignmentService, AssignmentRunner and AssignmentApprovalBridge.
"""

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


def _operation_result(value):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise AssignmentError("assignment_repository_contract_unavailable", 503)
    return value


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
                raise AssignmentError("assignment_transaction_unavailable", 503) from None
            raise

    async def call(self, method_name: str, **kwargs):
        method = getattr(self.repository, method_name, None)
        if method is None or method_name.startswith("_"):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)
        return await self.transaction(lambda transaction, _: method(transaction, **kwargs))

    async def call_for_operation(self, method_name: str, **kwargs):
        if method_name not in {
            "assert_current_assignment_execution", "put_action_for_execution",
            "reserve_action_for_execution", "start_action_for_execution", "record_action_outcome",
        }:
            raise AssignmentError("assignment_repository_contract_unavailable", 503)
        method = getattr(self.repository, method_name, None)
        if not callable(method):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)

        def invoke(transaction, _):
            return _operation_result(method(transaction, **kwargs))

        return await self.transaction(invoke, bound_session_waits=True)

    async def read_current_action(self, *, fence, binding, action_id, authority):
        guard = getattr(self.repository, "assert_current_assignment_execution", None)
        if not callable(guard):
            raise AssignmentError("assignment_repository_contract_unavailable", 503)

        def read(transaction, repository):
            values = dict(fence=fence, binding=binding, action_id=action_id, authority=authority)
            _operation_result(guard(transaction, **values))
            action = _operation_result(repository.get_action(transaction, owner_id=fence.owner_id,
                assignment_id=fence.assignment_id, action_id=action_id))
            current = _operation_result(guard(transaction, **values))
            return current, action

        return await self.transaction(read, bound_session_waits=True)

    async def current_execution_transaction(
        self, *, fence, binding, callback: Callable[[Any, Any, Any], _T], action_id=None,
    ) -> _T:
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

    async def operation_lifecycle_transaction(
        self, *, authority, callback: Callable[[Any, Any, Any], _T], fence=None, binding=None,
        final_check: Callable[[], None] | None = None,
    ) -> _T:
        from orchestrator.session_authority import OperationExecutionAuthority

        if (not isinstance(authority, OperationExecutionAuthority)
                or authority.plane_runtime is not self.plane_runtime
                or (binding is not None and fence is None)):
            raise AssignmentError("assignment_authorization_unavailable", 403)
        if final_check is not None and not callable(final_check):
            raise AssignmentError("assignment_transaction_callback_invalid", 500)
        sessions = self.plane_runtime.repositories.history.sessions

        def synchronous(value):
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise AssignmentError("assignment_transaction_callback_invalid", 500)
            return value

        def guarded(tx, repository):
            if binding is not None:
                current = synchronous(repository.assert_current_assignment_execution(
                    tx, fence=fence, binding=binding, authority=authority.observation))
            else:
                # Owner is locked before session, then assignment rows
                synchronous(sessions.assert_current_execution(tx, observation=authority.observation))
                if fence is not None:
                    current = synchronous(repository.assert_current_claim(tx, fence=fence))
                else:
                    current = synchronous(repository.get_operation(
                        tx, owner_id=authority.record.owner_id,
                        assignment_id=authority.record.assignment_id)).assignment
            original = authority.record
            if (current.owner_id != original.owner_id
                    or current.assignment_id != original.assignment_id
                    or current.execution_profile != "one_shot"
                    or current.instruction_revision != original.instruction_revision
                    or current.control_epoch != original.control_epoch
                    or current.definition != original.definition
                    or current.operation != original.operation
                    or (fence is None and current.state_version != original.state_version)):
                raise AssignmentError("assignment_state_changed", 409)
            result = synchronous(callback(tx, repository, current))
            synchronous(sessions.assert_current_execution(tx, observation=authority.observation))
            after = synchronous(repository.get_operation(
                tx, owner_id=current.owner_id,
                assignment_id=current.assignment_id)).assignment
            synchronous(repository.assert_guidance_current(
                tx, owner_id=after.owner_id, assignment_id=after.assignment_id,
                expected_instruction_revision=after.instruction_revision,
                expected_control_epoch=after.control_epoch,
                expected_state_version=after.state_version,
                authority_valid_until=authority.observation.valid_until))
            if final_check is not None and synchronous(final_check()) is not None:
                raise AssignmentError("assignment_transaction_callback_invalid", 500)
            return result

        return await self.transaction(guarded, bound_session_waits=True)
