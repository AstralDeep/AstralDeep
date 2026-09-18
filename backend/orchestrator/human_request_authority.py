"""Current human metadata requests over the existing IAM and Plane boundaries.

This supplies no operation, tool, grant or publication authority. One host-owned
boundary bounds its async work over the existing pool, independently of Work.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import contextvars
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from astralplane.async_runtime import AsyncPlaneRuntime
from astralplane.errors import PlaneError
from astralplane.repositories import (
    RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import SessionConsentObservation
from audit.repository import AuditRepository
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketState

from orchestrator import auth
from orchestrator.session_store import WebSessionStore
from orchestrator.work_submit_authority import (
    AuthenticatedWorkRequest, _authenticate_work_request, _expiry, capture_human_caller,
    _signed_selection,
)
from orchestrator.work_write_boundary import freeze_work_request
from persistent_agents.models import AssignmentError

_HUMAN_CALLER = contextvars.ContextVar("current_human_metadata_caller", default=None)
_SOCKET_READS = frozenset({"chrome_author_list", "chrome_user_skill_edit", "chrome_declarative_view",
                           "chrome_note_search", "chrome_turn_selection_set"})
_SOCKET_WRITES = frozenset({"chrome_user_skill_save", "chrome_user_skill_toggle",
                           "chrome_user_skill_delete", "chrome_declarative_command",
                           "chrome_note_save", "chrome_note_toggle", "chrome_note_forget"})
MAX_SOCKET_MESSAGE_BYTES = 128 * 1024


def _socket_method(message):
    if type(message) is not dict or message.get("type") != "ui_event":
        return None
    action = message.get("action")
    if type(action) is not str:
        return None
    if action in _SOCKET_WRITES:
        return "WS_WRITE"
    if action in _SOCKET_READS:
        return "WS_READ"
    payload = message.get("payload")
    if (action == "chrome_open" and type(payload) is dict
            and type(payload.get("surface")) is str
            and payload.get("surface") in {"agent_authoring", "guidance"}):
        return "WS_READ"
    return None


def _socket_policy():
    # Change detection only; normal production IAM remains the sole evaluator.
    return tuple(os.getenv(key) for key in (
        "KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_ALLOWED_AZP",
        "USE_MOCK_AUTH", "PUBLIC_BASE_URL", "BACKEND_PUBLIC_URL",
    ))


def _socket_message(message):
    try:
        value = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False)
        if len(value.encode("utf-8")) > MAX_SOCKET_MESSAGE_BYTES:
            raise ValueError
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _unauthenticated()


def _uuid4(value):
    try:
        parsed = UUID(value)
        if type(value) is not str or parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        _unauthenticated()


def _socket_identity(message, key, *, default=None):
    sources = (message, message.get("payload"), message.get("config"))
    values = [source[key] for source in sources if type(source) is dict and key in source]
    if not values:
        return _uuid4(default)
    if any(value != values[0] for value in values[1:]):
        _unauthenticated()
    return _uuid4(values[0])


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
    socket_request: object = field(default=None, repr=False)

    @classmethod
    def capture_host(cls, boundary):
        """Capture storage identities only; this grants no caller authority."""
        if type(boundary) is not HumanRequestBoundary:
            _unavailable()
        value = cls(None, boundary, boundary.orchestrator, boundary.plane_runtime,
            boundary.repositories, boundary.repositories.agents, boundary.sessions,
            boundary.sessions._sessions, boundary.repositories.history.sessions,
            boundary.audit_repo, boundary.audit_repo._audit, boundary.repositories.audit,
            boundary.adapter)
        value.assert_host_current(boundary.orchestrator)
        return value

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

    @classmethod
    def capture_socket(cls, pending):
        boundary = pending.boundary
        value = cls(None, boundary, boundary.orchestrator, boundary.plane_runtime,
            boundary.repositories, boundary.repositories.agents, boundary.sessions,
            boundary.sessions._sessions, boundary.repositories.history.sessions,
            boundary.audit_repo, boundary.audit_repo._audit, boundary.repositories.audit,
            boundary.adapter, pending)
        value.assert_current(boundary.orchestrator)
        return value

    def assert_host_current(self, expected_orchestrator):
        """Check the original host composition, without asserting a transport grant."""
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (expected_orchestrator is not self.orch
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

    def assert_current(self, expected_orchestrator):
        self.assert_host_current(expected_orchestrator)
        if self.socket_request is None and _orchestrator(self.app) is not self.orch:
            _unavailable()
        if self.socket_request is not None:
            self.socket_request.assert_socket()


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
        if self._method not in {"POST", "DELETE", "WS_WRITE"}:
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


class _HumanSocketRequest:
    """One private original message lifetime, captured before queue/admission waits."""

    def __init__(self, boundary, *, websocket, context, message, method, purpose):
        if type(boundary) is not HumanRequestBoundary:
            _unavailable()
        self.boundary, self.websocket, self.connection = boundary, websocket, context
        self.message = message
        self.purpose = purpose
        self.policy = _socket_policy()
        self.method, self.message_json = method, _socket_message(message)
        self.deadline = time.monotonic() + 15
        self.until = datetime.now(timezone.utc) + timedelta(seconds=15)
        self.closed = False
        self.registration = getattr(boundary.orchestrator, "ui_sessions", {}).get(websocket)
        if type(self.registration) is not dict:
            _unauthenticated()
        try:
            self.captured = deepcopy(self.registration)
            self.token = self.captured["_raw_token"]
            self.owner_id = self.captured["sub"]
            self.expiry = _expiry(self.captured)
            if type(self.token) is not str or not self.token:
                raise ValueError
            self.request_generation = _socket_identity(message, "request_generation")
            self.submission_id = _socket_identity(message, "submission_id")
            self.connection_generation = _uuid4(str(context.connection_generation))
            if _socket_identity(message, "connection_generation", default=self.connection_generation) != self.connection_generation:
                raise ValueError
            from orchestrator.work_surface_authority import _transport_headers
            headers = _transport_headers(websocket)
            scope = getattr(websocket, "scope", None)
            query = scope.get("query_string", b"") if type(scope) is dict else urlsplit(
                getattr(getattr(websocket, "request", None), "path", "")
            ).query.encode("utf-8")
            if type(query) is not bytes or len(query) > 65536:
                raise ValueError
            if any(key == "token" for key, _ in parse_qsl(query.decode("utf-8"), keep_blank_values=True)):
                raise AssignmentError("human_query_token_refused", 403)
            # Header parsing only: this is the actual WebSocket transport, never
            # a fabricated HTTP Request or an HTTP authentication invocation.
            transport = HTTPConnection({"type": "websocket", "headers": headers})
            self.session_id = _signed_selection(transport)
            from orchestrator import web_auth
            supplied = [part.strip().split("=", 1)[0] for header in
                transport.headers.getlist("cookie") for part in header.split(";")]
            if web_auth.COOKIE_NAME in supplied and self.session_id is None:
                raise ValueError
            if self.session_id is not None and method == "WS_WRITE":
                from orchestrator.work_api import _origin
                origins = transport.headers.getlist("origin")
                base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL")
                if not base or len(origins) != 1 or _origin(origins[0]) != _origin(base, base=True):
                    raise AssignmentError("human_origin_refused", 403)
        except AssignmentError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
            _unauthenticated()
        self.observation = None
        self._captured_session = False
        self._capture_started = False
        self._capture_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._caller = None
        self.binding = _Composition.capture_socket(self)

    def __repr__(self):
        return "<HumanSocketRequest private>"

    def assert_socket(self):
        context, orch = self.connection, self.boundary.orchestrator
        if (self.closed or time.monotonic() >= self.deadline
                or datetime.now(timezone.utc) >= min(self.until, self.expiry)
                or getattr(orch, "ui_sessions", {}).get(self.websocket) is not self.registration
                or self.registration != self.captured
                or getattr(orch, "_connection_contexts", {}).get(id(self.websocket)) is not context
                or getattr(context, "websocket", None) is not self.websocket
                or not getattr(context, "registered", False) or getattr(context, "closing", True)
                or getattr(context, "work_registrations_pending", None) != 0
                or str(getattr(context, "connection_generation", None)) != self.connection_generation
                or getattr(self.websocket, "closed", False)
                or getattr(self.websocket, "client_state", None) == WebSocketState.DISCONNECTED
                or getattr(self.websocket, "application_state", None) == WebSocketState.DISCONNECTED
                or _socket_policy() != self.policy
                or _socket_message(self.message) != self.message_json
                or (self.purpose == "metadata" and _socket_method(self.message) != self.method)
                or (self.purpose == "skill_lookup" and self.message.get("action") != "chat_message")
                or (self.purpose == "voice_guidance" and not _voice_guidance_message(self.message))):
            _unauthenticated()

    async def capture_session(self):
        """Capture issued B once before IAM/admission; no token decryption/refresh."""
        async with asyncio.timeout_at(self.deadline), self._capture_lock:
            self.binding.assert_current(self.boundary.orchestrator)
            if self._captured_session:
                return
            if self._capture_started:
                _unauthenticated()
            self._capture_started = True
            if self.session_id is not None:
                def capture(tx):
                    repository = self.binding.session_repository
                    repository.bound_request_execution_waits(tx)
                    self.binding.assert_current(self.boundary.orchestrator)
                    state = repository.get_execution_state(tx, owner_id=self.owner_id,
                                                            session_id=self.session_id)
                    if state is None:
                        _unauthenticated()
                    observation = SessionConsentObservation(state.credential, state.observed_at,
                        min(state.observed_at + timedelta(seconds=15), self.until, self.expiry,
                            datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)))
                    repository.assert_current_consent(tx, observation=observation)
                    self.binding.assert_current(self.boundary.orchestrator)
                    return observation
                try:
                    self.observation = await self.binding.adapter.run_in_transaction(capture)
                except (RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
                        RepositoryValidationError):
                    _unauthenticated()
                except AssignmentError:
                    raise
                except Exception:
                    _unavailable()
            self.binding.assert_current(self.boundary.orchestrator)
            self._captured_session = True

    async def authenticate(self):
        """Normal institutional JWT verification over the exact registered token."""
        try:
            async with asyncio.timeout_at(self.deadline), self._auth_lock:
                await self.capture_session()
                if self._caller is not None:
                    await self._caller.verify_delivery()
                    return self._caller
                claims = await auth.verify_user(await auth.verify_production_token(self.token))
                self.binding.assert_current(self.boundary.orchestrator)
                expiry = _expiry(claims, self.owner_id)
                if expiry != self.expiry:
                    _unauthenticated()
                cookie = (self.observation.credential.session_id, self.observation.credential.incarnation_id
                          ) if self.observation is not None else None
                context = AuthenticatedWorkRequest(self.owner_id, expiry,
                    json.dumps(claims, separators=(",", ":"), allow_nan=False), self.session_id,
                    cookie, self.binding.runtime)
                caller = CurrentHumanCaller(context, self.observation, self.binding, self.token,
                                            self.deadline, self.until, self.method)
                await caller.transaction(lambda tx, _: None, expected_orchestrator=self.boundary.orchestrator)
                self._caller = caller
                return caller
        except AssignmentError:
            raise
        except TimeoutError:
            raise AssignmentError("human_request_timeout", 408) from None
        except Exception:
            _unauthenticated()

    def close(self):
        self.closed = True
        self.token = ""
        self.message_json = ""
        self.captured = {}
        self._caller = None


def capture_human_socket_request(boundary, *, websocket, context, message, purpose="metadata"):
    """Classify only server-owned metadata actions; payload flags grant no method."""
    if purpose == "skill_lookup":
        method = ("WS_READ" if type(message) is dict and message.get("type") == "ui_event"
                  and message.get("action") == "chat_message" else None)
    elif purpose == "metadata":
        method = _socket_method(message)
    elif purpose == "voice_guidance":
        method = "WS_READ" if _voice_guidance_message(message) else None
    else:
        _unauthenticated()
    return None if method is None else _HumanSocketRequest(boundary, websocket=websocket,
        context=context, message=message, method=method, purpose=purpose)


def _voice_guidance_message(message):
    return type(message) is dict and (message.get("type") == "voice_local_final" or (
        message.get("type") == "ui_event" and message.get("action") == "chat_message"
        and type(message.get("payload")) is dict
        and type(message["payload"].get("voice_origin")) is dict))


async def current_socket_human_read(*, expected_orchestrator, websocket,
                                    operation_context=None):
    """Original registered chat caller for one internal skill lookup, never a write.

    The consumer verifies delivery before using its read result, then calls
    retire_socket_human_read in finally. This does not bound the chat/model run.

    ``operation_context`` is the connection operation context the caller already
    holds. Pass it whenever you have it. Re-reading the ContextVar here is a
    race: the admission executor sets it around a frame and resets it in its
    ``finally``, while a chat turn keeps running past that point, so a turn can
    observe the variable populated when it enters ``handle_chat_message`` and
    empty a few milliseconds later inside this function. That is not
    theoretical -- it made every chat turn fail with ``skill_lookup_unavailable``
    for any signed-in user whenever ``FF_USER_SKILLS`` was on, which is the
    default. The threaded value is an ordinary argument and cannot be reset out
    from under the turn.
    """
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT
    context = operation_context
    if not isinstance(context, dict):
        context = _CONNECTION_OPERATION_CONTEXT.get() or {}
    pending = context.get("human_request")
    if (type(pending) is not _HumanSocketRequest or pending.purpose != "skill_lookup"
            or pending.boundary.orchestrator is not expected_orchestrator
            or pending.websocket is not websocket or pending.method != "WS_READ"):
        raise AssignmentError("human_skill_lookup_unavailable", 503)
    return await pending.authenticate()


def retire_socket_human_read(caller):
    """Retire an internal lookup even when its caller already failed currentness."""
    if type(caller) is not CurrentHumanCaller:
        _unauthenticated()
    pending = caller._binding.socket_request
    if type(pending) is not _HumanSocketRequest or pending.purpose != "skill_lookup":
        _unauthenticated()
    pending.close()


@contextmanager
def bind_human_caller(caller):
    if type(caller) is not CurrentHumanCaller:
        _unauthenticated()
    caller._assert_local(caller._binding.orch)
    token = _HUMAN_CALLER.set(caller)
    try:
        yield caller
    finally:
        _HUMAN_CALLER.reset(token)


def current_human_caller(*, expected_orchestrator=None):
    """Private dispatch/render context; never reconstruct a caller from user_id."""
    caller = _HUMAN_CALLER.get()
    if caller is not None:
        caller._assert_local(expected_orchestrator or caller._binding.orch)
    return caller
