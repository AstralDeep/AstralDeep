"""Carries read-only turn authority into inherited context and evidence callbacks.
The original guidance binding and public Plane assertions preserve current consent and execution leases without granting writes.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import time

from astralplane.repositories.history import SessionConsentObservation
from orchestrator import auth
from orchestrator.turn_guidance_authority import TurnGuidanceBinding, current_turn_guidance
from orchestrator.work_admission import ExecutionFence, OperationRecord
from orchestrator.work_submit_authority import _expiry
from persistent_agents.models import AssignmentError

_CURRENT = ContextVar("private_context_read_authority", default=None)
_CAPTURE_TOKEN = object()
_VERIFY_SECONDS = 15


def _refuse():
    raise AssignmentError("context_authority_unavailable", 503) from None


@dataclass(frozen=True, slots=True, repr=False)
class ContextAuthorityProof:
    owner_id: str
    chat_id: str
    operation_records: tuple[OperationRecord, ...]
    operation_fences: tuple[ExecutionFence, ...]

    @property
    def stable_identity(self):
        return self.owner_id, self.chat_id, tuple(
            (
                record.operation_id, record.operation_kind, record.admission_class, record.owner_scope,
                record.owner_user_id, record.connection_scope_id, record.idempotency_namespace,
                record.idempotency_key, record.normalized_input_digest, record.chat_id,
                record.parent_operation_id, record.connection_generation, record.request_generation,
                record.state, record.phase_code, record.terminal_code, record.execution_generation,
                record.execution_lease_token, record.state_revision, fence,
            )
            for record, fence in zip(self.operation_records, self.operation_fences, strict=True)
        )

    def __repr__(self):
        return "<ContextAuthorityProof private>"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ContextAuthorityLease:
    _turn: TurnGuidanceBinding
    _orch: object
    _websocket: object
    _chat_id: str
    _owner_id: str
    _task: asyncio.Task
    _closed: bool

    def __init__(self, binding, *, orchestrator, _capture_token=None):
        if (_capture_token is not _CAPTURE_TOKEN or type(binding) is not TurnGuidanceBinding
                or binding.task is not asyncio.current_task()):
            _refuse()
        object.__setattr__(self, "_turn", binding)
        object.__setattr__(self, "_orch", orchestrator)
        object.__setattr__(self, "_websocket", binding.websocket)
        object.__setattr__(self, "_chat_id", binding.chat_id)
        object.__setattr__(self, "_owner_id", binding.origin.owner_id)
        object.__setattr__(self, "_task", binding.task)
        object.__setattr__(self, "_closed", False)
        self._local(orchestrator, binding.websocket, binding.chat_id)

    def __repr__(self):
        return "<ContextAuthorityLease private>"

    @property
    def owner_id(self):
        return self._owner_id

    @property
    def chat_id(self):
        return self._chat_id

    def close(self):
        object.__setattr__(self, "_closed", True)

    def require_write(self):
        raise AssignmentError("human_write_required", 403)

    def assert_current(self, *, orchestrator, websocket, chat_id):
        self._local(orchestrator, websocket, chat_id)

    def _local(self, orchestrator, websocket, chat_id):
        try:
            if (self._closed or orchestrator is not self._orch or websocket is not self._websocket
                    or chat_id != self._chat_id or self._turn.task is not self._task
                    or self._turn.websocket is not self._websocket or self._turn.chat_id != self._chat_id
                    or self._turn.origin.owner_id != self._owner_id):
                _refuse()
            self._turn.local(self._orch)
        except Exception:
            _refuse()

    def _consent(self, tx, valid_until):
        origin = self._turn.origin
        if origin.credential is None:
            return
        sessions = origin.binding.session_repository
        state = sessions.get_execution_state(tx, owner_id=self._owner_id,
                                             session_id=origin.credential.session_id)
        if state is None or state.credential != origin.credential:
            _refuse()
        until = min(valid_until, state.observed_at + timedelta(seconds=15),
                    datetime.fromtimestamp(origin.credential.hard_expires_at, UTC))
        sessions.assert_current_consent(tx, observation=SessionConsentObservation(
            origin.credential, state.observed_at, until))

    def _proof(self, tx):
        lineage, current = [], self._turn
        while current is not None:
            if (type(current) is not TurnGuidanceBinding or len(lineage) >= 32
                    or any(binding is current for binding in lineage)):
                _refuse()
            lineage.append(current)
            current = current.parent
        records, fences = [], []
        for binding in reversed(lineage):
            for operation in binding.operations:
                record = operation.coordinator.assert_current_execution_lease(operation.fence, transaction=tx)
                if (type(record) is not OperationRecord or type(operation.fence) is not ExecutionFence
                        or record.operation_id != operation.fence.operation_id
                        or record.execution_generation != operation.fence.execution_generation
                        or record.execution_lease_token != operation.fence.execution_lease_token):
                    _refuse()
                records.append(record)
                fences.append(operation.fence)
        return ContextAuthorityProof(self._owner_id, self._chat_id, tuple(records), tuple(fences))

    async def verify(self, *, orchestrator, websocket, chat_id):
        try:
            self._local(orchestrator, websocket, chat_id)
            if asyncio.get_running_loop() is not self._task.get_loop():
                _refuse()
            deadline = time.monotonic() + _VERIFY_SECONDS
            origin = self._turn.origin
            valid_until = min(datetime.now(UTC) + timedelta(seconds=_VERIFY_SECONDS), origin.expires_at)
            async with asyncio.timeout_at(deadline):
                claims = await auth.verify_user(await auth.verify_production_token(origin.token))
                if (json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False) != origin.claims_json
                        or _expiry(claims, self._owner_id) != origin.expires_at):
                    _refuse()
                self._local(orchestrator, websocket, chat_id)
                def current(tx):
                    origin.binding.session_repository.bound_request_execution_waits(tx)
                    self._local(orchestrator, websocket, chat_id)
                    origin.binding.repositories.preferences.skills.lock_owner(tx, owner_id=self._owner_id)
                    self._consent(tx, valid_until)
                    self._turn.current(tx, self._orch)
                    proof = self._proof(tx)
                    self._turn.current(tx, self._orch)
                    self._consent(tx, valid_until)
                    self._local(orchestrator, websocket, chat_id)
                    return proof
                proof = await origin.binding.adapter.run_in_transaction(current)
                self._local(orchestrator, websocket, chat_id)
                if time.monotonic() >= deadline or datetime.now(UTC) >= valid_until:
                    _refuse()
                return proof
        except Exception:
            _refuse()


async def capture_context_authority(*, orchestrator, websocket, chat_id):
    try:
        binding = current_turn_guidance(expected_orchestrator=orchestrator, websocket=websocket, chat_id=chat_id)
        lease = ContextAuthorityLease(binding, orchestrator=orchestrator, _capture_token=_CAPTURE_TOKEN)
    except Exception:
        _refuse()
    try:
        await lease.verify(orchestrator=orchestrator, websocket=websocket, chat_id=chat_id)
        return lease
    except BaseException:
        lease.close()
        raise


@contextmanager
def use_context_authority(lease):
    if type(lease) is not ContextAuthorityLease:
        _refuse()
    lease._local(lease._orch, lease._websocket, lease._chat_id)
    if (asyncio.current_task() is not lease._task or current_turn_guidance(
            expected_orchestrator=lease._orch, websocket=lease._websocket, chat_id=lease._chat_id) is not lease._turn):
        _refuse()
    token = _CURRENT.set(lease)
    try:
        yield lease
    finally:
        _CURRENT.reset(token)
        lease.close()


def current_context_authority(*, orchestrator, websocket, chat_id):
    value = _CURRENT.get()
    if type(value) is not ContextAuthorityLease:
        _refuse()
    value._local(orchestrator, websocket, chat_id)
    return value
