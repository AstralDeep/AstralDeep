"""Private current-caller fences for Work writes; never dispatch authority."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import time
import json

from astralplane.repositories import (
    RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.assignment_models import AssignmentRecord
from astralplane.repositories.history import (
    SessionConsentObservation, SessionCredentialFence, SessionExecutionObservation,
)
from audit.repository import AuditRepository
from fastapi import Request

from orchestrator import auth, web_auth
from orchestrator.session_store import SessionRefreshUnavailable, WebSessionStore
from orchestrator.work_continuation_authority import OperationControlAuthority
from orchestrator.work_submit_authority import AuthenticatedWorkRequest, _authenticate_work_request, _expiry
from orchestrator.work_write_boundary import freeze_work_request
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore


def _unauthenticated():
    raise AssignmentError("work_authentication_required", 401)


def _unavailable():
    raise AssignmentError("work_control_unavailable", 503)


def _same_credential(left, right):
    if type(left) is not SessionCredentialFence or type(right) is not SessionCredentialFence:
        return False
    return json.dumps(asdict(left), sort_keys=True, allow_nan=False) == json.dumps(
        asdict(right), sort_keys=True, allow_nan=False)


def _orchestrator(app):
    value = getattr(getattr(app, "state", None), "orchestrator", None)
    if value is None:
        value = getattr(getattr(getattr(app, "_root_app", None), "state", None), "orchestrator", None)
    return value


@dataclass(frozen=True, slots=True, repr=False)
class _Composition:
    app: object
    assignments: object
    sessions: object
    orch: object
    store: object
    runtime: object
    repository: object
    adapter: object
    session_adapter: object
    audit: object

    @classmethod
    def capture(cls, request, assignments, sessions):
        if (type(assignments) is not AssignmentService or type(sessions) is not WebSessionStore
                or type(assignments.store) is not AssignmentStore):
            _unavailable()
        store = assignments.store
        value = cls(request.scope.get("app"), assignments, sessions, assignments.orch,
                    store, store.plane_runtime, store.repository, store.async_runtime,
                    sessions._sessions, getattr(assignments.orch, "audit_repo", None))
        value.assert_current(assignments)
        return value

    def assert_current(self, assignments):
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (assignments is not self.assignments or self.assignments.orch is not self.orch
                or _orchestrator(self.app) is not self.orch
                or getattr(self.orch, "persistent_assignments", None) is not self.assignments
                or getattr(self.orch, "web_sessions", None) is not self.sessions
                or self.assignments.store is not self.store
                or self.store.plane_runtime is not self.runtime
                or getattr(plane, "runtime", None) is not self.runtime
                or getattr(plane, "repositories", None) is not self.runtime.repositories
                or self.store.repository is not self.repository
                or self.repository is not self.runtime.repositories.assignments
                or self.store.async_runtime is not self.adapter
                or self.adapter.repositories is not self.runtime.repositories
                or self.sessions._sessions is not self.session_adapter
                or self.session_adapter.plane_runtime is not self.runtime
                or self.session_adapter.repository is not self.runtime.repositories.history.sessions
                or type(self.audit) is not AuditRepository
                or getattr(self.orch, "audit_repo", None) is not self.audit
                or self.audit._audit.plane_runtime is not self.runtime
                or self.audit._audit.repository is not self.runtime.repositories.audit):
            _unavailable()


@dataclass(frozen=True, slots=True, repr=False)
class WorkCallerAuthority:
    """One original IAM request and optional caller issuance, bounded to 15 seconds.

    Safe bare-Bearer writes retain normal human IAM plus the original expiry and
    deadline. They cannot invent a persisted session or prove instantaneous remote
    revocation. Cookie callers additionally hold a real consent fence in SQL.
    """

    context: AuthenticatedWorkRequest
    caller: SessionConsentObservation | None
    _binding: _Composition
    _token: str = field(repr=False)
    _deadline: float
    _until: datetime
    _original: OperationControlAuthority | None = field(default=None, repr=False)

    def _assert_local(self, assignments):
        self._binding.assert_current(assignments)
        if time.monotonic() >= self._deadline or datetime.now(timezone.utc) >= self._until:
            _unauthenticated()
        try:
            self.context.assert_current(self._binding.runtime)
        except AssignmentError:
            _unauthenticated()
        assignments._owner(self.context.owner_id, self.context.claims)

    def require_session(self) -> SessionConsentObservation:
        """Require the already selected caller B; never select or refresh a row."""
        self._assert_local(self._binding.assignments)
        if self.caller is None:
            _unauthenticated()
        return self.caller

    def assert_current(self, tx, *, assignments):
        """Check before assignment locks and again after every later write/wait.

        The caller owns SQL wait caps and the outer transaction. Any refusal must
        roll back its mutation/audit. Original A checks never replace caller B.
        """
        self._assert_local(assignments)
        repository = self._binding.runtime.repositories.history.sessions
        state = None
        try:
            if self.caller is not None:
                state = repository.assert_current_consent(tx, observation=self.caller)
                self.context.assert_current(self._binding.runtime, now=state.observed_at)
            if self._original is not None:
                current = repository.assert_current_execution(tx, observation=self._original.observation)
                # A's row lock can wait after B was checked. B's locked identity
                # cannot change, but its shorter observation may expire meanwhile.
                if self.caller is not None and not (
                        self.caller.started_at <= current.observed_at < self.caller.valid_until):
                    _unauthenticated()
                operation = self._original.record.operation
                if min(datetime.fromisoformat(operation["deadline_at"]),
                       datetime.fromisoformat(operation["authority"]["expires_at"])) <= current.observed_at:
                    _unauthenticated()
        except (RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
                RepositoryValidationError):
            _unauthenticated()
        self._assert_local(assignments)
        return state

    def with_original(self, authority: OperationControlAuthority) -> WorkCallerAuthority:
        """Bind original A; same-session rotation requires the exact pre-CAS proof."""
        caller = self.require_session()
        if (self._original is not None or type(authority) is not OperationControlAuthority
                or authority.request_context is not self.context
                or authority.plane_runtime is not self._binding.runtime
                or authority.command not in {"resume", "wake"}
                or type(authority.record) is not AssignmentRecord
                or authority.record.owner_id != self.context.owner_id
                or type(authority.observation) is not SessionExecutionObservation
                or authority.observation.credential.owner_id != self.context.owner_id):
            _unauthenticated()
        operation = authority.record.operation
        if (operation is None or type(operation.get("version")) is not int or operation["version"] != 2
                or operation["authority"].get("origin") != "interactive"
                or operation["authority"].get("reference_kind") != "session_incarnation"
                or operation["authority"].get("reference_id") != authority.observation.credential.incarnation_id):
            _unauthenticated()
        original = authority.observation.credential
        if original.incarnation_id == caller.credential.incarnation_id:
            if (original.session_id != caller.credential.session_id
                    or not _same_credential(authority.request_credential, caller.credential)):
                _unauthenticated()
            caller = replace(caller, credential=original,
                             valid_until=min(caller.valid_until, authority.observation.valid_until))
        elif authority.request_credential is not None:
            _unauthenticated()
        return replace(self, caller=caller, _original=authority)

    async def verify_delivery(self) -> None:
        """Verify only the original JWT and selected lineage before private output."""
        try:
            async with asyncio.timeout_at(self._deadline):
                self._assert_local(self._binding.assignments)
                claims = await auth.verify_user(await auth.verify_production_token(self._token))
                if (claims != self.context.claims
                        or _expiry(claims, self.context.owner_id) != self.context.principal_expires_at):
                    _unauthenticated()
                await self._binding.store.transaction(
                    lambda tx, _: self.assert_current(tx, assignments=self._binding.assignments),
                    bound_session_waits=True)
                self._assert_local(self._binding.assignments)
        except AssignmentError:
            raise
        except Exception:
            _unauthenticated()


async def authenticate_work_control_request(
    request: Request, *, assignments, sessions,
) -> WorkCallerAuthority:
    """Freeze/verify ordinary IAM, then capture exactly one caller issuance B."""
    deadline = time.monotonic() + 15
    until = datetime.now(timezone.utc) + timedelta(seconds=15)
    try:
        async with asyncio.timeout_at(deadline):
            snapshot = freeze_work_request(request)
            binding = _Composition.capture(snapshot, assignments, sessions)
            context, token = await _authenticate_work_request(
                snapshot, sessions=sessions, plane_runtime=binding.runtime, methods=("POST", "DELETE"))
            if not isinstance(token, str) or not token:
                _unauthenticated()
            supplied = [part.strip().split("=", 1)[0] for header in snapshot.headers.getlist("cookie")
                        for part in header.split(";")]
            if web_auth.COOKIE_NAME in supplied and context.session_id is None:
                _unauthenticated()
            caller = None
            if context.session_id is not None:
                reference = await asyncio.to_thread(sessions.capture_execution_reference,
                    owner_id=context.owner_id, session_id=context.session_id)
                state = reference.state
                credential = state.credential
                if (credential.owner_id != context.owner_id or credential.session_id != context.session_id
                        or (context.cookie_session is not None and context.cookie_session != (
                            credential.session_id, credential.incarnation_id))):
                    _unauthenticated()
                caller = SessionConsentObservation(credential, state.observed_at,
                    min(state.observed_at + timedelta(seconds=15), until, context.principal_expires_at,
                        datetime.fromtimestamp(credential.hard_expires_at, timezone.utc)))
            guard = WorkCallerAuthority(context, caller, binding, token, deadline, until)
            await binding.store.transaction(
                lambda tx, _: guard.assert_current(tx, assignments=assignments), bound_session_waits=True)
            guard._assert_local(assignments)
            return guard
    except AssignmentError:
        raise
    except SessionRefreshUnavailable:
        _unauthenticated()
    except Exception:
        _unavailable()
