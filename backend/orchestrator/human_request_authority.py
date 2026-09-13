"""Current human metadata requests over the existing IAM and Plane boundaries.

This supplies no operation, tool, grant or publication authority. One host-owned
boundary bounds its async work over the existing pool, independently of Work.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import inspect
import time

from astralplane.async_runtime import AsyncPlaneRuntime
from astralplane.errors import PlaneError
from astralplane.repositories import (
    RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import SessionConsentObservation
from audit.repository import AuditRepository

from orchestrator import auth
from orchestrator.session_store import WebSessionStore
from orchestrator.work_submit_authority import (
    AuthenticatedWorkRequest, _authenticate_work_request, _expiry, capture_human_caller,
)
from orchestrator.work_write_boundary import freeze_work_request
from persistent_agents.models import AssignmentError


def _unauthenticated():
    raise AssignmentError("human_authentication_required", 401)


def _unavailable():
    raise AssignmentError("human_request_unavailable", 503)


def _orchestrator(app):
    value = getattr(getattr(app, "state", None), "orchestrator", None)
    if value is None:
        value = getattr(getattr(getattr(app, "_root_app", None), "state", None), "orchestrator", None)
    return value


class HumanRequestBoundary:
    """One application-owned bounded adapter; never creates a database pool."""

    def __init__(self, orchestrator):
        plane = getattr(getattr(orchestrator, "runtime_composition", None), "plane", None)
        self.orchestrator = orchestrator
        self.plane_runtime = getattr(plane, "runtime", None)
        self.repositories = getattr(plane, "repositories", None)
        self.sessions = getattr(orchestrator, "web_sessions", None)
        self.audit_repo = getattr(orchestrator, "audit_repo", None)
        if (self.plane_runtime is None or self.repositories is not self.plane_runtime.repositories
                or type(self.sessions) is not WebSessionStore or type(self.audit_repo) is not AuditRepository):
            _unavailable()
        self.adapter = AsyncPlaneRuntime(self.plane_runtime, maximum_concurrency=8,
                                         admission_timeout_seconds=2.0)
        self.closed = False

    def close(self):
        self.closed = True
        self.adapter.close()


@dataclass(frozen=True, slots=True, repr=False)
class _Composition:
    app: object
    boundary: HumanRequestBoundary
    orch: object
    runtime: object
    repositories: object
    agents: object
    sessions: object
    session_adapter: object
    session_repository: object
    audit: object
    audit_adapter: object
    audit_repository: object
    adapter: object

    @classmethod
    def capture(cls, request, boundary):
        if type(boundary) is not HumanRequestBoundary:
            _unavailable()
        value = cls(request.scope.get("app"), boundary, boundary.orchestrator,
            boundary.plane_runtime, boundary.repositories, boundary.repositories.agents,
            boundary.sessions, boundary.sessions._sessions, boundary.repositories.history.sessions,
            boundary.audit_repo, boundary.audit_repo._audit, boundary.repositories.audit, boundary.adapter)
        value.assert_current(boundary.orchestrator)
        return value

    def assert_current(self, expected_orchestrator):
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (expected_orchestrator is not self.orch or _orchestrator(self.app) is not self.orch
                or getattr(self.orch, "human_request_boundary", None) is not self.boundary
                or self.boundary.closed or self.boundary.orchestrator is not self.orch
                or self.boundary.plane_runtime is not self.runtime
                or self.boundary.repositories is not self.repositories
                or getattr(plane, "runtime", None) is not self.runtime
                or getattr(plane, "repositories", None) is not self.repositories
                or self.runtime.repositories is not self.repositories
                or self.repositories.agents is not self.agents
                or self.boundary.sessions is not self.sessions
                or getattr(self.orch, "web_sessions", None) is not self.sessions
                or self.sessions._sessions is not self.session_adapter
                or self.session_adapter.plane_runtime is not self.runtime
                or self.session_adapter.repository is not self.session_repository
                or self.repositories.history.sessions is not self.session_repository
                or self.boundary.audit_repo is not self.audit
                or getattr(self.orch, "audit_repo", None) is not self.audit
                or self.audit._audit is not self.audit_adapter
                or self.audit_adapter.plane_runtime is not self.runtime
                or self.audit_adapter.repository is not self.audit_repository
                or self.repositories.audit is not self.audit_repository
                or self.boundary.adapter is not self.adapter
                or self.adapter.repositories is not self.repositories):
            _unavailable()


@dataclass(frozen=True, slots=True, repr=False)
class CurrentHumanCaller:
    """Original normal human IAM and optional issuance, never refreshed/adopted."""

    context: AuthenticatedWorkRequest
    caller: SessionConsentObservation | None
    _binding: _Composition
    _token: str = field(repr=False)
    _deadline: float
    _until: datetime
    _method: str

    @property
    def owner_id(self):
        return self.context.owner_id

    @property
    def claims(self):
        return self.context.claims

    @property
    def plane_runtime(self):
        return self._binding.runtime

    @property
    def runtime(self):
        return self._binding.runtime

    @property
    def repositories(self):
        return self._binding.repositories

    @property
    def audit_repo(self):
        return self._binding.audit

    def _assert_local(self, expected_orchestrator):
        self._binding.assert_current(expected_orchestrator)
        if time.monotonic() >= self._deadline or datetime.now(timezone.utc) >= self._until:
            _unauthenticated()
        try:
            self.context.assert_current(self._binding.runtime)
        except AssignmentError:
            _unauthenticated()

    def require_session(self):
        self._assert_local(self._binding.orch)
        if self.caller is None:
            _unauthenticated()
        return self.caller

    def require_write(self):
        """Only the frozen authenticated write transport may authorize mutation."""
        self._assert_local(self._binding.orch)
        if self._method not in {"POST", "DELETE"}:
            raise AssignmentError("human_write_required", 403)
        return self._method

    def assert_current(self, tx, *, expected_orchestrator):
        self._assert_local(expected_orchestrator)
        state = None
        try:
            if self.caller is not None:
                state = self._binding.session_repository.assert_current_consent(tx, observation=self.caller)
                self.context.assert_current(self._binding.runtime, now=state.observed_at)
        except (AssignmentError, RepositoryConflictError, RepositoryDataError,
                RepositoryNotFoundError, RepositoryValidationError):
            _unauthenticated()
        self._assert_local(expected_orchestrator)
        return state

    async def transaction(self, callback, *, expected_orchestrator):
        """A server-owned synchronous repository callback with outer caller guards."""
        self._assert_local(expected_orchestrator)
        if not callable(callback):
            _unavailable()

        def current(tx):
            self._binding.session_repository.bound_request_execution_waits(tx)
            self.assert_current(tx, expected_orchestrator=expected_orchestrator)
            value = callback(tx, self._binding.repositories)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                _unavailable()
            self.assert_current(tx, expected_orchestrator=expected_orchestrator)
            return value

        try:
            async with asyncio.timeout_at(self._deadline):
                value = await self._binding.adapter.run_in_transaction(current)
                self._assert_local(expected_orchestrator)
                return value
        except AssignmentError:
            raise
        except TimeoutError:
            raise AssignmentError("human_request_timeout", 408) from None
        except PlaneError as exc:
            code = exc.code
            status = (404 if "not_found" in code else 429 if any(part in code for part in
                ("capacity", "quota", "budget", "rate_limit")) else 422 if isinstance(exc, RepositoryValidationError)
                else 409 if isinstance(exc, RepositoryConflictError) else 503)
            raise AssignmentError(code, status) from exc
        except Exception:
            _unavailable()

    async def verify_delivery(self):
        try:
            async with asyncio.timeout_at(self._deadline):
                self._assert_local(self._binding.orch)
                claims = await auth.verify_user(await auth.verify_production_token(self._token))
                if claims != self.claims or _expiry(claims, self.owner_id) != self.context.principal_expires_at:
                    _unauthenticated()
                await self.transaction(lambda tx, _: None, expected_orchestrator=self._binding.orch)
                self._assert_local(self._binding.orch)
        except AssignmentError:
            raise
        except Exception:
            _unauthenticated()


async def authenticate_current_human_request(request, *, boundary):
    """Normal IAM on frozen transport before body/domain work, with original time bounds."""
    deadline = time.monotonic() + 15
    until = datetime.now(timezone.utc) + timedelta(seconds=15)
    try:
        async with asyncio.timeout_at(deadline):
            snapshot = freeze_work_request(request)
            binding = _Composition.capture(snapshot, boundary)
            read_only = snapshot.method == "GET"
            context, token = await _authenticate_work_request(snapshot, sessions=binding.sessions,
                plane_runtime=binding.runtime, methods=("GET",) if read_only else ("POST", "DELETE"),
                read_only=read_only)
            if not isinstance(token, str) or not token:
                _unauthenticated()
            caller = await capture_human_caller(snapshot, context=context, sessions=binding.sessions, until=until)
            guard = CurrentHumanCaller(context, caller, binding, token, deadline, until, snapshot.method)
            await guard.transaction(lambda tx, _: None, expected_orchestrator=binding.orch)
            return guard
    except AssignmentError:
        raise
    except Exception:
        _unavailable()
