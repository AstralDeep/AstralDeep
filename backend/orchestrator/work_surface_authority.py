"""Captures and rechecks one WebSocket caller's original session for Work reads without
refresh or execution authority. Used by chrome_events.py, human_request_authority.py,
and orchestrator.py before each delivery.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import os
import time
from uuid import UUID

from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketState

from orchestrator.session_store import SessionRefreshUnavailable, WebSessionStore
from orchestrator.work_api import _ReadDelivery, _read_expiry
from orchestrator.work_service import WorkService
from orchestrator.work_submit_authority import _signed_selection
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService


def invalidate(orch, websocket):
    pending = getattr(orch, "_work_surface_reads", None)
    if pending is not None:
        read = pending.pop(websocket, None)
        if read is not None:
            read.close()


def _transport_headers(websocket):
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict) and "headers" in scope:
        headers = scope["headers"]
    else:
        source = getattr(websocket, "request_headers", None)
        if source is None:
            source = getattr(getattr(websocket, "request", None), "headers", None)
        if source is None:
            headers = []
        elif callable(getattr(source, "raw_items", None)):
            headers = source.raw_items()
        elif hasattr(source, "raw"):
            headers = source.raw
        elif callable(getattr(source, "items", None)):
            headers = source.items()
        else:
            raise AssignmentError("work_authentication_required", 401)
    frozen, size = [], 0
    try:
        for key, value in headers:
            if not isinstance(key, (str, bytes)) or not isinstance(value, (str, bytes)):
                raise ValueError
            if len(key) + len(value) > 65536:
                raise ValueError
            key = key.encode("latin-1") if isinstance(key, str) else bytes(key)
            value = value.encode("latin-1") if isinstance(value, str) else bytes(value)
            size += len(key) + len(value)
            if len(frozen) >= 256 or size > 65536:
                raise ValueError
            frozen.append((key.lower(), value))
    except (TypeError, ValueError, UnicodeError):
        raise AssignmentError("work_authentication_required", 401) from None
    return frozen


class WorkSurfaceRead:
    def __init__(self, orch, websocket, owner_id, *, request_generation=None, context=None):
        self.orch, self.websocket, self.owner_id = orch, websocket, owner_id
        self.context = context
        self.connection_generation = getattr(context, "connection_generation", None)
        self.request_generation = request_generation
        self._closed = False
        self._capture_started = False
        self._session_captured = False
        self._capture_lock = asyncio.Lock()
        if context is not None or request_generation is not None:
            try:
                identity = UUID(request_generation)
                if identity.version != 4 or str(identity) != request_generation:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise AssignmentError("work_query_invalid", 422) from None
        self.registration = getattr(orch, "ui_sessions", {}).get(websocket)
        self.captured = deepcopy(self.registration)
        self.assignments = getattr(orch, "persistent_assignments", None)
        if (type(self.assignments) is not AssignmentService
                or not isinstance(self.registration, dict)
                or self.registration.get("sub") != owner_id):
            raise AssignmentError("work_authentication_required", 401)
        self.service = WorkService(self.assignments)
        self.store = self.assignments.store
        self.runtime = self.store.plane_runtime
        self.repository = self.store.repository
        self.adapter = self.store.async_runtime
        self.sessions = getattr(orch, "web_sessions", None)
        token = self.registration.get("_raw_token")
        if not isinstance(token, str) or not token:
            raise AssignmentError("work_authentication_required", 401)
        self.delivery = _ReadDelivery(owner_id, _read_expiry(self.registration), token, None)
        self.deadline = time.monotonic() + 15
        headers = _transport_headers(websocket)
        self.connection = HTTPConnection({"type": "websocket", "headers": headers})
        self.session_id = _signed_selection(self.connection)
        from orchestrator import web_auth
        supplied = [part.strip().split("=", 1)[0] for header in
                    self.connection.headers.getlist("cookie") for part in header.split(";")]
        if web_auth.COOKIE_NAME in supplied and self.session_id is None:
            raise AssignmentError("work_authentication_required", 401)
        if not hasattr(orch, "_work_surface_reads"):
            orch._work_surface_reads = {}
        orch._work_surface_reads[websocket] = self
        try:
            self.assert_current()
        except Exception:
            self.close()
            raise

    def assert_current(self):
        if (self._closed or getattr(self.orch, "ui_sessions", {}).get(self.websocket) is not self.registration
                or self.registration != self.captured
                or getattr(self.orch, "_work_surface_reads", {}).get(self.websocket) is not self
                or getattr(self.websocket, "closed", False)
                or getattr(self.websocket, "client_state", None) == WebSocketState.DISCONNECTED
                or getattr(self.websocket, "application_state", None) == WebSocketState.DISCONNECTED
                or time.monotonic() >= self.deadline
                or time.time() >= self.delivery.expires_at
                or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
            raise AssignmentError("work_authentication_required", 401)
        if self.context is not None and (
                getattr(self.orch, "_connection_contexts", {}).get(id(self.websocket)) is not self.context
                or self.context.websocket is not self.websocket
                or not self.context.registered or self.context.closing
                or self.context.work_registrations_pending != 0
                or self.context.connection_generation != self.connection_generation):
            raise AssignmentError("work_authentication_required", 401)
        if (getattr(self.orch, "persistent_assignments", None) is not self.assignments
                or self.assignments.orch is not self.orch
                or self.assignments.store is not self.store
                or self.service.store is not self.store
                or self.store.plane_runtime is not self.runtime
                or self.store.repository is not self.repository
                or self.repository is not self.runtime.repositories.assignments
                or self.store.async_runtime is not self.adapter
                or self.adapter.repositories is not self.runtime.repositories):
            raise AssignmentError("work_read_unavailable", 503)
        if self.session_id is not None and (
                type(self.sessions) is not WebSessionStore
                or getattr(self.orch, "web_sessions", None) is not self.sessions
                or self.sessions._sessions.plane_runtime is not self.runtime
                or self.sessions._sessions.repository is not self.runtime.repositories.history.sessions):
            raise AssignmentError("work_read_unavailable", 503)
        self.service._owner(self.owner_id, self.captured)

    def assert_request(self, orch, websocket, owner_id, request_generation):
        self.assert_current()
        if (orch is not self.orch or websocket is not self.websocket
                or owner_id != self.owner_id or request_generation != self.request_generation):
            raise AssignmentError("work_authentication_required", 401)

    async def capture_session(self):
        async with asyncio.timeout_at(self.deadline), self._capture_lock:
            self.assert_current()
            if self._session_captured:
                return
            if self._capture_started:
                raise AssignmentError("work_authentication_required", 401)
            self._capture_started = True
            if self.session_id is not None:
                try:
                    reference = await asyncio.to_thread(self.sessions.capture_execution_reference,
                        owner_id=self.owner_id, session_id=self.session_id)
                except SessionRefreshUnavailable:
                    raise AssignmentError("work_authentication_required", 401) from None
                self.assert_current()
                credential = reference.state.credential
                self.delivery = replace(self.delivery,
                    cookie_session=(credential.session_id, credential.incarnation_id))
            self._session_captured = True

    async def authenticate(self):
        await self.capture_session()
        await self.verify()

    async def verify(self):
        self.assert_current()
        await self.delivery.verify(self.service)
        self.assert_current()

    def close(self):
        self._closed = True
        if getattr(self.orch, "_work_surface_reads", {}).get(self.websocket) is self:
            self.orch._work_surface_reads.pop(self.websocket, None)
