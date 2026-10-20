"""Fences framework credential reads over WorkService and owner status readers.
Work operations and MCP resources share current Plane authority without sharing transport or PHI policy.
"""

from __future__ import annotations

import asyncio

from orchestrator.framework_credentials import FrameworkCaller, FrameworkCredentialService
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError
from persistent_agents.store import AssignmentStore

_SERVICE_READ_SCOPES = {"get": "operations.read", "list": "operations.read",
                        "poll": "operations.read", "result": "artifacts.read"}
_READ_SCOPES = {**_SERVICE_READ_SCOPES, "subscriptions": "operations.read",
                "emergency_status": "operations.read"}


def read_scope(method):
    return _READ_SCOPES.get(method)


class _AuthorizedStore:
    def __init__(self, reader):
        self.reader = reader

    async def transaction(self, callback, *, bound_session_waits=False):
        observation = await asyncio.to_thread(self.reader.observation)

        def checked(tx, repository):
            self.reader.assert_execution(tx, observation)
            return callback(tx, repository)

        return await self.reader.store.transaction(
            checked, bound_session_waits=bound_session_waits,
        )


class FrameworkWorkReader:
    def __init__(self, operations, caller: FrameworkCaller, scope: str):
        if (scope not in _READ_SCOPES.values() or not isinstance(caller, FrameworkCaller)
                or not caller.has_scope(scope)):
            raise AssignmentError("framework_scope_required", 403)
        self.operations = operations
        self.caller = caller
        self.scope = scope
        self.assignments = operations.assignments
        self.store = operations.store
        self.credentials = operations.credentials
        self._bound()
        self.service = WorkService(self.assignments)
        self.service.store = _AuthorizedStore(self)

    def _bound(self):
        if (self.operations.assignments is not self.assignments
                or self.operations.store is not self.store
                or self.operations.credentials is not self.credentials
                or not isinstance(self.credentials, FrameworkCredentialService)
                or not isinstance(self.store, AssignmentStore)
                or self.assignments.store is not self.store
                or self.credentials.plane_runtime is not self.store.plane_runtime
                or self.store.repository is not self.store.plane_runtime.repositories.assignments):
            raise AssignmentError("framework_credential_authority_unavailable", 409)

    def observation(self):
        self._bound()
        observation = self.credentials.fresh_observation(self.caller)
        if observation is None:
            raise AssignmentError("framework_credential_authority_unavailable", 409)
        if self.scope not in observation.credential.scopes:
            raise AssignmentError("framework_scope_required", 403)
        return observation

    def assert_execution(self, tx, observation):
        self._bound()
        current = self.credentials.assert_execution(tx, observation)
        if self.scope not in current.credential.scopes:
            raise AssignmentError("framework_scope_required", 403)

    async def recheck(self):
        await self.service.store.transaction(lambda _tx, _repository: None)

    async def read(self, method, *args, **kwargs):
        if _SERVICE_READ_SCOPES.get(method) != self.scope:
            raise AssignmentError("framework_scope_required", 403)
        result = await getattr(self.service, method)(
            self.caller.owner_id, {"sub": self.caller.owner_id}, *args, **kwargs,
        )
        await self.recheck()
        return result
