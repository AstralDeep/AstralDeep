"""Binds qualified SSH mutators to a current session and one durable dispatch reservation.
The orchestrator installs private selection context; Paramiko commits it after authentication and before exec.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import uuid

from astralplane.repositories.history import SessionExecutionObservation
from starlette.requests import HTTPConnection

from orchestrator import auth, web_auth
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.remote_effect_authority import authority_digest, lock_owner
from orchestrator.work_admission import ExecutionFence, OwnerScope
from orchestrator.work_reconciliation import AttemptOutcome, ReconciliationRefused
from orchestrator.work_submit_authority import _expiry, _signed_selection
from orchestrator.work_surface_authority import _transport_headers

_SELECTION = ContextVar("remote_effect_selection", default=None)
_ACTION = ContextVar("remote_effect_action", default=None)
_TOOLS = frozenset({"make_directory", "remove_path"})


def qualified_tool(agent_id, tool_name):
    return agent_id == "remote-compute-1" and tool_name in _TOOLS


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


class RemoteEffectSelection:
    def __init__(self, orch, websocket, connection, owner_id, fence, tool, arguments, reference):
        self.orch, self.websocket, self.connection = orch, websocket, connection
        self.runtime = orch.runtime_composition.plane.runtime
        self.ledger, self.coordinator, self.sessions = orch.effect_attempts, orch.work_admission, orch.web_sessions
        self.registration = getattr(orch, "ui_sessions", {}).get(websocket)
        self.connection_generation = getattr(connection, "connection_generation", None)
        admitted = _CONNECTION_OPERATION_CONTEXT.get() or {}
        self.human_request = (admitted.get("human_request")
            if admitted.get("operation_kind") == "remote_effect_approval" else None)
        work = getattr(connection, "operations", {}).get(fence.operation_id)
        self.review = work.frame.remote_effect_call if self.human_request is not None and work is not None else None
        self.owner_id, self.fence, self.tool = owner_id, fence, tool
        self.original_digest = _digest(arguments)
        self.reference = reference
        self.dispatch_id = uuid.uuid4().hex
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.attempt = None
        self._lock = threading.Lock()

    def _assert_placement(self):
        if self.human_request is not None:
            try:
                self.human_request.assert_socket()
                from orchestrator.hitl_confirmation import _current_session

                if self.review is None or not _current_session(self.orch, self.websocket, self.review):
                    raise ValueError
                if (self.human_request.boundary.orchestrator is not self.orch
                        or self.human_request.websocket is not self.websocket
                        or self.human_request.purpose != "remote_effect"):
                    raise ValueError
            except Exception:
                raise ReconciliationRefused("effect_current_authority_unavailable") from None
        if (self.closed or self.orch.runtime_composition.plane.runtime is not self.runtime
                or self.orch.effect_attempts is not self.ledger or self.orch.work_admission is not self.coordinator
                or self.orch.web_sessions is not self.sessions
                or getattr(self.orch, "ui_sessions", {}).get(self.websocket) is not self.registration
                or getattr(self.connection, "connection_generation", None) != self.connection_generation
                or self.orch._connection_contexts.get(id(self.websocket)) is not self.connection
                or self.connection.websocket is not self.websocket or self.connection.closing
                or not self.connection.registered):
            raise ReconciliationRefused("effect_current_authority_unavailable")

    async def _refresh(self):
        self._assert_placement()
        current = await asyncio.to_thread(self.orch.web_sessions.capture_execution_reference,
            owner_id=self.owner_id, session_id=self.reference.state.credential.session_id)
        original = self.reference.state.credential
        if (current.state.credential.incarnation_id != original.incarnation_id
                or current.state.credential.created_at != original.created_at):
            raise ReconciliationRefused("effect_session_replaced")
        candidate = await self.orch.web_sessions.refresh_for_execution(current,
            exchange=web_auth._exchange_session_refresh,
            bound_exchange=web_auth._exchange_bound_session_refresh)
        claims = await auth.verify_user(await auth.verify_production_token(candidate.access_token))
        expiry = _expiry(claims, self.owner_id)
        self._assert_placement()
        return SessionExecutionObservation(candidate.credential, candidate.started_at,
            min(candidate.started_at + timedelta(seconds=15), expiry,
                datetime.fromtimestamp(candidate.credential.hard_expires_at, timezone.utc)))

    def _observation(self):
        future = asyncio.run_coroutine_threadsafe(self._refresh(), self.loop)
        try:
            return future.result(timeout=15)
        except BaseException:
            future.cancel()
            raise

    def _guard(self, tx, observation, target):
        runtime = self.orch.runtime_composition.plane.runtime
        sessions = runtime.repositories.history.sessions
        self._assert_placement()
        sessions.bound_request_execution_waits(tx)
        sessions.assert_current_execution(tx, observation=observation)
        lock_owner(runtime, tx, self.owner_id)
        from orchestrator.emergency_stop import current_effect_epoch
        runtime.repositories.stop_epochs.assert_running(tx, owner_id=self.owner_id,
            expected_epoch=current_effect_epoch(self.owner_id))
        runtime.repositories.stop_epochs.assert_operation(tx, owner_id=self.owner_id,
            operation_id=self.fence.operation_id)
        record = self.orch.work_admission.assert_current_execution_lease(self.fence, transaction=tx)
        if record.owner_scope is not OwnerScope.USER or record.owner_user_id != self.owner_id:
            raise ReconciliationRefused("effect_owner_mismatch")
        if self.review is not None and self.review.remote_proposal is not None:
            from orchestrator.remote_confirmation import consumed_review
            if consumed_review(self.orch, self.review.remote_proposal, self.owner_id,
                    self.review.chat, self.review.agent, self.tool, json.loads(self.review.arguments),
                    transaction=tx) is None:
                raise ReconciliationRefused("effect_remote_approval_changed")
        if (not target.host_key_fingerprint or not target.authority_digest
                or authority_digest(runtime, tx, self.owner_id, target.machine_id) != target.authority_digest):
            raise ReconciliationRefused("effect_target_authority_changed")

    def reserve(self, target, argv):
        with self._lock:
            if self.attempt is not None:
                raise ReconciliationRefused("effect_selection_already_reserved")
            observation = self._observation()
            ledger = self.orch.effect_attempts
            if ledger._audit is None or ledger._store.source.plane_runtime is not self.orch.runtime_composition.plane.runtime:
                raise ReconciliationRefused("effect_audit_unavailable")
            with self.orch.runtime_composition.plane.runtime.transaction() as tx:
                self._guard(tx, observation, target)
                attempt = ledger._bind_in_transaction(tx, self.fence,
                    target_kind="remote_machine", target_id=target.machine_id,
                    argument_digest=_digest({"original": self.original_digest, "tool": self.tool,
                        "target": target.machine_id, "argv": argv}),
                    dispatch_id=self.dispatch_id, authority_digest=target.authority_digest)
                self._guard(tx, observation, target)
            self.attempt = attempt
            return attempt

    def before_send(self, target):
        observation = self._observation()
        with self.orch.runtime_composition.plane.runtime.transaction() as tx:
            self._guard(tx, observation, target)

    def settle(self, target, *, applied, result=None):
        if self.attempt is None:
            return
        observation = self._observation()
        with self.orch.runtime_composition.plane.runtime.transaction() as tx:
            self._guard(tx, observation, target)
            self.orch.effect_attempts._settle_in_transaction(tx, partition=self.owner_id,
                operation_id=self.fence.operation_id, nonce=self.attempt.attempt_nonce,
                outcome=AttemptOutcome.APPLIED if applied else AttemptOutcome.UNCERTAIN,
                outcome_digest=_digest(result) if applied else None)
            self._guard(tx, observation, target)


async def select_effect(orch, websocket, agent_id, tool_name, args, owner_id):
    if not qualified_tool(agent_id, tool_name):
        return None
    from persistent_agents.dispatch_context import current_dispatch
    context = _CONNECTION_OPERATION_CONTEXT.get()
    connection = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    if (current_dispatch() is not None or agent_id not in getattr(orch, "local_agents", {})
            or not isinstance(context, dict) or connection is None
            or connection.websocket is not websocket or connection.closing or not connection.registered):
        raise ReconciliationRefused("effect_placement_unsupported")
    fence, owner = context.get("execution_fence"), context.get("owner")
    if (not isinstance(fence, ExecutionFence) or owner is None
            or owner.owner_scope is not OwnerScope.USER or owner.owner_user_id != owner_id):
        raise ReconciliationRefused("effect_current_authority_unavailable")
    registration = getattr(orch, "ui_sessions", {}).get(websocket)
    if not isinstance(registration, dict) or registration.get("sub") != owner_id:
        raise ReconciliationRefused("effect_current_authority_unavailable")
    token = registration.get("_raw_token")
    claims = await auth.verify_user(await auth.verify_production_token(token))
    _expiry(claims, owner_id)
    transport = HTTPConnection({"type": "websocket", "headers": _transport_headers(websocket)})
    sid = _signed_selection(transport)
    if sid is None:
        raise ReconciliationRefused("effect_session_required")
    reference = await asyncio.to_thread(orch.web_sessions.capture_execution_reference,
                                       owner_id=owner_id, session_id=sid)
    arguments = {key: value for key, value in args.items()
                 if not key.startswith("_") and key not in {"user_id", "delegated_token"}}
    return RemoteEffectSelection(orch, websocket, connection, owner_id, fence, tool_name, arguments, reference)


@contextmanager
def selected_effect(selection):
    token = _SELECTION.set(selection)
    try:
        yield
    finally:
        if selection is not None:
            selection.closed = True
        _SELECTION.reset(token)


def selection_requires_target(owner_id):
    selection = _SELECTION.get()
    return type(selection) is RemoteEffectSelection and selection.owner_id == owner_id


def current_effect():
    value = _SELECTION.get()
    return value if type(value) is RemoteEffectSelection and _ACTION.get() == value.tool else None


def run_qualified(transport, target, argv, *, tool, owner_id, timeout):
    from orchestrator.remote_transport import ParamikoTransport, RemoteResult, Verdict
    selection = _SELECTION.get()
    if (type(selection) is not RemoteEffectSelection or selection.tool != tool
            or selection.owner_id != owner_id or not isinstance(transport, ParamikoTransport)):
        return RemoteResult(verdict=Verdict.UNATTENDED_REFUSED, machine=target.label,
                            next_action="effect_current_authority_unavailable", retryable=False)
    token = _ACTION.set(tool)
    try:
        return transport.run(target, argv, timeout=timeout, retryable=False)
    finally:
        _ACTION.reset(token)
