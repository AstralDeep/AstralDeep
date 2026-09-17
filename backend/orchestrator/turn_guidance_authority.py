"""Private, read-only guidance authority for already admitted ordinary turns.

An original human or consent-derived token is retained only in memory. Reading
guidance neither refreshes that token nor grants model, tool, publication, or
authored-metadata authority. A lookup has a new bounded observation window, but
can never outlive or replace the original credential, grant, or admitted turn.
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
import time
from starlette.websockets import WebSocketState

from astralplane.repositories.history import SessionConsentObservation
from orchestrator import auth
from orchestrator.human_request_authority import (
    CurrentHumanCaller, _Composition, _expiry, _socket_policy,
)
from orchestrator.work_admission import (
    ExecutionFence, OperationRecord, OwnerScope, PlaneWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)
from persistent_agents.models import AssignmentError

_TURN = contextvars.ContextVar("private_turn_guidance", default=None)


def _refuse():
    raise AssignmentError("guidance_read_unavailable", 503)


def _claims_json(claims):
    try:
        return json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _refuse()


@dataclass(frozen=True, slots=True, repr=False)
class _GuidanceOrigin:
    binding: _Composition
    owner_id: str
    token: str = field(repr=False)
    claims_json: str = field(repr=False)
    credential: object = field(repr=False)
    expires_at: datetime
    policy: tuple = field(repr=False)
    connection_scope_id: object = None
    connection_generation: object = None
    closed: bool = False

    def current(self, orch):
        self.binding.assert_host_current(orch)
        if (self.closed or not self.token or datetime.now(timezone.utc) >= self.expires_at
                or self.policy != _socket_policy()):
            _refuse()

    def close(self):
        object.__setattr__(self, "closed", True)
        object.__setattr__(self, "token", "")
        object.__setattr__(self, "claims_json", "")


async def capture_turn_guidance_from_human(caller, *, expected_orchestrator):
    """Detach only a verified original human read origin for a private handoff.

    The resulting object is not a caller and cannot enter metadata mutations.
    The receiving turn must bind its actual execution before a lookup can use it.
    """
    if type(caller) is not CurrentHumanCaller:
        _refuse()
    caller._assert_local(expected_orchestrator)
    await caller.verify_delivery()
    caller._assert_local(expected_orchestrator)
    pending = caller._binding.socket_request
    return _GuidanceOrigin(caller._binding, caller.owner_id, caller._token,
        _claims_json(caller.claims), None if caller.caller is None else caller.caller.credential,
        caller.context.principal_expires_at, _socket_policy(),
        getattr(getattr(pending, "connection", None), "connection_scope_id", None),
        getattr(pending, "connection_generation", None))


@dataclass(frozen=True, slots=True, repr=False)
class _OperationBinding:
    coordinator: WorkAdmissionCoordinator
    adapter: PlaneWorkAdmissionRepository
    repository: object
    record: OperationRecord
    fence: ExecutionFence

    @classmethod
    def capture(cls, orch, origin, operation, fence):
        coordinator = getattr(orch, "work_admission", None)
        adapter = getattr(coordinator, "_repository", None)
        if (type(coordinator) is not WorkAdmissionCoordinator
                or type(adapter) is not PlaneWorkAdmissionRepository
                or type(operation) is not OperationRecord or type(fence) is not ExecutionFence
                or operation.operation_id != fence.operation_id):
            _refuse()
        if operation.owner_scope is OwnerScope.CONNECTION:
            if (origin.connection_scope_id is None
                    or operation.connection_scope_id != origin.connection_scope_id
                    or str(operation.connection_generation) != origin.connection_generation):
                _refuse()
        elif (operation.owner_scope not in {OwnerScope.USER, OwnerScope.SCHEDULE}
                or operation.owner_user_id != origin.owner_id):
            _refuse()
        value = cls(coordinator, adapter, origin.binding.repositories.work_admission,
                    operation, fence)
        value.local(orch, origin)
        return value

    def local(self, orch, origin):
        if (getattr(orch, "work_admission", None) is not self.coordinator
                or self.coordinator._repository is not self.adapter
                or self.adapter._runtime is not origin.binding.runtime
                or self.adapter._plane_repository is not self.repository
                or origin.binding.repositories.work_admission is not self.repository):
            _refuse()

    def current(self, tx, orch, origin):
        self.local(orch, origin)
        current = self.coordinator.assert_current_execution_lease(self.fence, transaction=tx)
        # Phase/revision and lease duration may advance. Logical and execution
        # identities may not be silently adopted after admission.
        for name in ("operation_id", "owner_scope", "owner_user_id", "connection_scope_id",
                     "operation_kind", "chat_id", "connection_generation", "request_generation"):
            if getattr(current, name) != getattr(self.record, name):
                _refuse()


@dataclass(frozen=True, slots=True, repr=False)
class _ForegroundSocketBinding:
    """A connected foreground turn retains its original transport identity.

    The short metadata capture may retire after handoff. Its original socket
    and registration still constrain this connected operation and its children;
    background/USER voice custody uses their separate execution bindings.
    """
    websocket: object
    context: object
    registration: object
    snapshot: dict
    generation: str

    def current(self, orch):
        context, socket = self.context, self.websocket
        if (getattr(orch, "ui_sessions", {}).get(socket) is not self.registration
                or self.registration != self.snapshot
                or getattr(orch, "_connection_contexts", {}).get(id(socket)) is not context
                or getattr(context, "websocket", None) is not socket
                or not getattr(context, "registered", False) or getattr(context, "closing", True)
                or getattr(context, "work_registrations_pending", None) != 0
                or str(getattr(context, "connection_generation", None)) != self.generation
                or getattr(socket, "closed", False)
                or getattr(socket, "client_state", None) == WebSocketState.DISCONNECTED
                or getattr(socket, "application_state", None) == WebSocketState.DISCONNECTED):
            _refuse()


@dataclass(frozen=True, slots=True, repr=False)
class TurnGuidanceBinding:
    """One private turn handoff; children retain every parent execution fence."""
    origin: _GuidanceOrigin
    websocket: object
    chat_id: str
    operations: tuple[_OperationBinding, ...] = ()
    background: object = field(default=None, repr=False)
    machine: object = field(default=None, repr=False)
    voice: object = field(default=None, repr=False)
    parent: object = field(default=None, repr=False)
    budget: object = field(default=None, repr=False)
    budget_limits: tuple = field(default=(), repr=False)
    task: object = field(default_factory=asyncio.current_task, repr=False)
    closed: bool = False
    foreground: object = field(default=None, repr=False)
    # 088 T011/T037 — the composer's exact selected agent/skill/note heads for
    # this turn, existence-checked once at capture time (``bind_turn_selection``).
    # ``None`` (the overwhelming default: no client submits one yet) means this
    # binding behaves exactly as it always has — nothing downstream reads this
    # field today, so its absence changes nothing (FR-023 byte-identical pin).
    selection: object = field(default=None, repr=False)

    def close(self):
        object.__setattr__(self, "closed", True)

    def local(self, orch):
        if (type(self.origin) is not _GuidanceOrigin or self.closed
                or not isinstance(self.task, asyncio.Task) or self.task.done() or self.task.cancelling()):
            _refuse()
        self.origin.current(orch)
        if self.foreground is not None:
            self.foreground.current(orch)
        if getattr(self.websocket, "_closed", False):
            _refuse()
        if self.parent is not None:
            self.parent.local(orch)
        if self.budget is not None:
            if getattr(orch, "_chain_budgets", {}).get(self.chat_id) is not self.budget:
                _refuse()
            if (self.parent is None or getattr(orch, "_chain_budgets", {}).get(
                    self.parent.chat_id) is not self.budget.parent):
                _refuse()
            for budget, maximum, depth, wall, started, parent in self.budget_limits:
                if ((budget.max_hops, budget.max_depth, budget.wall_clock_s, budget.started_at,
                     budget.parent) != (maximum, depth, wall, started, parent)
                        or budget.spent_hops > maximum or time.monotonic() - started >= wall):
                    _refuse()
        for operation in self.operations:
            operation.local(orch, self.origin)
        if self.background is not None:
            manager, task, fence = self.background
            if (getattr(orch, "async_task_manager", None) is not manager
                    or manager._tasks.get(task.task_id) is not task
                    or task._virtual_websocket is not self.websocket
                    or self.websocket.task is not task or task._execution_fence != fence
                    or getattr(task, "_guidance_origin", None) is not self.origin
                    or task.asyncio_task is not self.task):
                _refuse()

    def current(self, tx, orch):
        self.local(orch)
        if self.parent is not None:
            self.parent.current(tx, orch)
        if self.machine is not None:
            self.machine.current(tx, orch, self.origin)
        for operation in self.operations:
            operation.current(tx, orch, self.origin)
        if self.machine is not None:
            self.machine.current_grant(tx, orch, self.origin)
        if self.voice is not None:
            self.voice.current(tx, orch, self.origin)
        # Refresh clocks only on rows already held after every downstream wait.
        # A SELECT clock expression may otherwise have preceded its row wait.
        if self.machine is not None:
            self.machine.current(tx, orch, self.origin)
        for operation in self.operations:
            operation.current(tx, orch, self.origin)
        self.local(orch)


@dataclass(frozen=True, slots=True, repr=False)
class _MachineBinding:
    websocket: object
    authority: object
    registration: dict = field(repr=False)
    snapshot: dict = field(repr=False)
    grant: object = field(repr=False)
    store: object = field(repr=False)
    adapter: object = field(repr=False)
    repository: object = field(repr=False)
    scheduled: object = field(default=None, repr=False)

    def current(self, tx, orch, origin):
        if (getattr(self.websocket, "_guidance_machine_authority", None) is not self.authority
                or getattr(orch, "ui_sessions", {}).get(self.websocket) is not self.registration
                or self.registration != self.snapshot
                or getattr(orch, "offline_grants", None) is not self.store
                or self.store._grants is not self.adapter
                or self.adapter.plane_runtime is not origin.binding.runtime
                or self.adapter.repository is not self.repository
                or origin.binding.repositories.offline_grants is not self.repository):
            _refuse()
        if self.scheduled is not None:
            store, adapter, repository, attempt, job = self.scheduled
            if (store._plane is not adapter or adapter.plane_runtime is not origin.binding.runtime
                    or adapter.repository is not repository
                    or origin.binding.repositories.scheduler is not repository or attempt.job != job):
                _refuse()
            _scheduled_current(tx, self.scheduled, origin.owner_id)

    def current_grant(self, tx, orch, origin):
        # Original owner79 is already held. Grant locking follows every
        # occurrence/operation/slot lock, never the reverse.
        grant = self.repository.assert_current_grant(tx, owner_id=origin.owner_id,
                                                     grant_id=self.grant.grant_id)
        if grant != self.grant:
            _refuse()


def _scheduled_current(tx, scheduled, owner):
    from astralplane.repositories.scheduler import OccurrenceState
    _, _, repository, attempt, job = scheduled
    claim = attempt.claim
    current = repository.assert_current_claim(tx, owner_id=owner,
        occurrence_id=str(claim.occurrence_id), claim_generation=claim.claim_generation,
        lease_token=str(claim.lease_token), lease_owner=claim.lease_owner,
        states=(OccurrenceState.RUNNING,))
    if (current.operation_id != str(attempt.operation_id)
            or current.operation_execution_generation != attempt.execution_fence.execution_generation
            or current.job_id != str(job["id"])):
        _refuse()


async def bind_machine_guidance(*, expected_orchestrator, websocket, authority,
                                chat_id, scheduled_attempt=None, scheduled_store=None):
    """Use the exact previously derived machine token and selected grant.

    This performs normal token verification and current grant/attempt reads,
    never a new mint, refresh, or latest-grant selection. Missing machine
    authority remains an explicit refusal when owner guidance is required.
    """
    from orchestrator.async_tasks import VirtualWebSocket
    from orchestrator.chain_authority import MachineAuthority, machine_session_binding
    from orchestrator.offline_grant import OfflineGrantStore
    from scheduler.store import ScheduledAttempt, ScheduledJobStore
    if (type(authority) is not MachineAuthority or type(websocket) is not VirtualWebSocket
            or getattr(websocket, "_guidance_machine_authority", None) is not authority):
        _refuse()
    orch = expected_orchestrator
    binding = _Composition.capture_host(getattr(orch, "human_request_boundary", None))
    registration = getattr(orch, "ui_sessions", {}).get(websocket)
    snapshot = machine_session_binding(authority)
    store = getattr(orch, "offline_grants", None)
    if type(store) is not OfflineGrantStore or registration != snapshot:
        _refuse()
    adapter = store._grants
    repository = binding.repositories.offline_grants
    if adapter.plane_runtime is not binding.runtime or adapter.repository is not repository:
        _refuse()
    scheduled = None
    if authority.turn_class == "scheduled_job":
        if (type(scheduled_attempt) is not ScheduledAttempt or type(scheduled_store) is not ScheduledJobStore
                or scheduled_attempt.execution_fence is None
                or scheduled_attempt.job.get("user_id") != authority.user_id
                or scheduled_attempt.job.get("offline_grant_id") != authority.consent_ref):
            _refuse()
        scheduled = (scheduled_store, scheduled_store._plane, binding.repositories.scheduler,
                     scheduled_attempt, deepcopy(scheduled_attempt.job))
    elif scheduled_attempt is not None or scheduled_store is not None:
        _refuse()
    elif authority.turn_class not in {"parser_replay", "draft_self_test"}:
        _refuse()

    def capture(tx):
        binding.session_repository.bound_request_execution_waits(tx)
        binding.assert_host_current(orch)
        binding.repositories.preferences.skills.lock_owner(tx, owner_id=authority.user_id)
        if scheduled is not None:
            _scheduled_current(tx, scheduled, authority.user_id)
        operation = (orch.work_admission.assert_current_execution_lease(
            scheduled_attempt.execution_fence, transaction=tx) if scheduled is not None else None)
        grant = repository.assert_current_grant(tx, owner_id=authority.user_id, grant_id=authority.consent_ref)
        if scheduled is not None:
            _scheduled_current(tx, scheduled, authority.user_id)
            orch.work_admission.assert_current_execution_lease(scheduled_attempt.execution_fence, transaction=tx)
        return grant, operation
    origin = None
    returned = False
    try:
        async with asyncio.timeout(15):
            grant, operation = await binding.adapter.run_in_transaction(capture)
            claims = await auth.verify_user(await auth.verify_production_token(authority.access_token))
            expiry = _expiry(claims, authority.user_id)
            origin = _GuidanceOrigin(binding, authority.user_id, authority.access_token,
                _claims_json(claims), None, expiry, _socket_policy())
            machine = _MachineBinding(websocket, authority, registration, snapshot,
                                      grant, store, adapter, repository, scheduled)
            operations = (() if operation is None else (_OperationBinding.capture(
                orch, origin, operation, scheduled_attempt.execution_fence),))
            result = TurnGuidanceBinding(origin, websocket, chat_id, operations, machine=machine)
            reader = TurnGuidanceReader(result, expected_orchestrator=orch)
            try:
                await reader.verify_delivery()
            finally:
                reader.close()
            returned = True
            return result
    except AssignmentError:
        raise
    except Exception:
        _refuse()
    finally:
        if origin is not None and not returned:
            origin.close()


def bind_background_guidance(origin, *, expected_orchestrator, websocket):
    """Bind an original human handoff to the actual manager-owned running task."""
    from orchestrator.async_tasks import BackgroundTask, BackgroundTaskManager, VirtualWebSocket
    if type(origin) is not _GuidanceOrigin or type(websocket) is not VirtualWebSocket:
        _refuse()
    manager = getattr(expected_orchestrator, "async_task_manager", None)
    task = websocket.task
    if type(manager) is not BackgroundTaskManager or type(task) is not BackgroundTask:
        _refuse()
    operation = _OperationBinding.capture(expected_orchestrator, origin,
                                         task._operation, task._execution_fence)
    value = TurnGuidanceBinding(origin, websocket, task.chat_id, (operation,),
                               (manager, task, task._execution_fence))
    value.local(expected_orchestrator)
    return value


@dataclass(frozen=True, slots=True, repr=False)
class _VoiceBinding:
    services: object
    store: object
    adapter: object
    repository: object
    turn: object
    operation_id: str
    connection_generation: str

    def current(self, tx, orch, origin):
        if (getattr(orch, "voice_services", None) is not self.services
                or self.services.repository is not self.store
                or self.store._plane is not self.adapter
                or self.adapter.plane_runtime is not origin.binding.runtime
                or self.adapter.repository is not self.repository
                or self.store._voice is not self.repository
                or origin.binding.repositories.voice is not self.repository):
            _refuse()
        # Operation/slot locks are already held. This exact read uses NOWAIT
        # session/turn locks and samples DB time after both, never a host clock.
        observation = self.repository.assert_current_guidance_turn(tx,
            owner_id=origin.owner_id, session_id=self.turn.session_id, turn_id=self.turn.turn_id,
            expected_session_generation=self.turn.session_generation,
            expected_media_grant_revision=self.turn.media_grant_revision,
            operation_id=self.operation_id)
        session, current = observation.session, observation.turn
        if session["owner_connection_generation"] != self.connection_generation:
            _refuse()
        for name in ("turn_id", "client_turn_id", "session_id", "session_generation",
                     "media_grant_revision", "user_id", "chat_id", "chat_context_revision",
                     "execution_base_render_revision", "submission_id", "request_generation"):
            if str(current.get(name)) != str(getattr(self.turn, name)):
                _refuse()


def bind_voice_guidance(origin, *, expected_orchestrator, websocket, voice_dispatch,
                        operation_context, chat_id):
    """Bind original human selection only to a real proof-admitted voice turn."""
    from orchestrator.orchestrator import _VoiceDispatchContext
    from orchestrator.voice_sessions import TranscriptAdmission, VoiceSessionRepository
    if (type(origin) is not _GuidanceOrigin or type(voice_dispatch) is not _VoiceDispatchContext
            or type(voice_dispatch.admission) is not TranscriptAdmission
            or type(operation_context) is not dict
            or operation_context.get("operation_kind") != "voice_chat_message"):
        _refuse()
    turn = voice_dispatch.admission.turn
    operation = _OperationBinding.capture(expected_orchestrator, origin,
        operation_context.get("operation"), operation_context.get("execution_fence"))
    if (str(operation.record.chat_id) != chat_id or turn.chat_id != chat_id
            or turn.user_id != origin.owner_id
            or str(operation.record.request_generation) != turn.request_generation
            or str(operation.record.connection_generation) != voice_dispatch.connection_generation
            or voice_dispatch.connection_generation != origin.connection_generation):
        _refuse()
    services = getattr(expected_orchestrator, "voice_services", None)
    store = getattr(services, "repository", None)
    if type(store) is not VoiceSessionRepository:
        _refuse()
    voice = _VoiceBinding(services, store, store._plane, origin.binding.repositories.voice,
                           turn, str(operation.record.operation_id), voice_dispatch.connection_generation)
    return TurnGuidanceBinding(origin, websocket, chat_id, (operation,), voice=voice)


def bind_foreground_guidance(origin, *, expected_orchestrator, websocket, operation_context, chat_id):
    """Retain a parent read origin under its actual foreground operation fence."""
    if type(origin) is not _GuidanceOrigin or type(operation_context) is not dict:
        _refuse()
    operation = _OperationBinding.capture(expected_orchestrator, origin,
        operation_context.get("operation"), operation_context.get("execution_fence"))
    if str(operation.record.chat_id) != chat_id:
        _refuse()
    pending = origin.binding.socket_request
    if pending is None or pending.websocket is not websocket:
        _refuse()
    pending.assert_socket()
    foreground = _ForegroundSocketBinding(websocket, pending.connection,
        pending.registration, deepcopy(pending.captured), pending.connection_generation)
    foreground.current(expected_orchestrator)
    return TurnGuidanceBinding(origin, websocket, chat_id, (operation,), foreground=foreground)


def bind_http_guidance(origin, *, expected_orchestrator, websocket, chat_id):
    """Carry the original HTTP request's guidance read into its owned task.

    This supplies no durable execution authority to the legacy REST chat path.
    A socket selected only for delivery cannot replace the HTTP principal.
    """
    if type(origin) is not _GuidanceOrigin or not isinstance(chat_id, str) or not chat_id:
        _refuse()
    origin.current(expected_orchestrator)
    return TurnGuidanceBinding(origin, websocket, chat_id)


def inherit_turn_guidance(parent, *, expected_orchestrator, websocket, chat_id, budget):
    """Narrow a parent turn's read origin into one actual internal child turn."""
    from orchestrator.chain_authority import ChainBudget
    if (type(parent) is not TurnGuidanceBinding or type(budget) is not ChainBudget
            or budget.parent is None or not isinstance(chat_id, str) or not chat_id):
        _refuse()
    parent.local(expected_orchestrator)
    if getattr(expected_orchestrator, "_chain_budgets", {}).get(parent.chat_id) is not budget.parent:
        _refuse()
    limits, current = [], budget
    while current is not None:
        if type(current) is not ChainBudget or len(limits) >= 32 or any(row[0] is current for row in limits):
            _refuse()
        limits.append((current, current.max_hops, current.max_depth, current.wall_clock_s,
                       current.started_at, current.parent))
        current = current.parent
    # The caller already charged the child hop. Reading its instructions is not
    # another hop: spent==maximum remains valid until the admitted wall bound.
    return TurnGuidanceBinding(parent.origin, websocket, chat_id, parent=parent,
                               budget=budget, budget_limits=tuple(limits))


# ---------------------------------------------------------------------------
# 088 T011/T037 — the composer's selection (agent revision + skills + notes)
#
# This binds the SAME closed, existence-checked identifiers HTTP Work already
# requires (``orchestrator.work_submit._selected_ids``) into an ordinary chat
# turn. It is a read-only, current-heads check at capture time — never a grant,
# never a value read (note plaintext is never opened here), and never a
# durable commitment: nothing downstream consumes ``TurnGuidanceBinding.
# selection`` yet, so an absent selection changes nothing (FR-023).
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class TurnSelectionBinding:
    """Bounded, existence-checked identifiers only; never an opened note value."""
    agent: object = None
    skills: tuple = ()
    notes: tuple = ()

    def recheck(self, tx, repositories, owner_id):
        """Re-assert every reference is still exactly the current head."""
        _selection_current(tx, repositories, owner_id, self.agent, self.skills, self.notes)


def _selection_current(tx, repositories, owner_id, agent, skills, notes):
    from astralplane.repositories.agents import AgentRevisionRecord, UserAgentRecord
    from astralplane.repositories.guidance_models import ExplicitNoteRecord, SkillHead
    if agent is not None:
        agent_id, revision_id = agent
        head = repositories.agents.get_agent(tx, owner_id=owner_id, agent_id=agent_id)
        if (type(head) is not UserAgentRecord or head.owner_id != owner_id
                or head.agent_kind != "declarative" or head.status != "active"
                or head.deleted_at is not None or head.selected_definition_revision_id != revision_id):
            _refuse()
        revision = repositories.agents.get_revision(tx, owner_id=owner_id, agent_id=agent_id,
                                                    revision_id=revision_id)
        if (type(revision) is not AgentRevisionRecord or revision.owner_id != owner_id
                or revision.agent_id != agent_id or revision.revision_id != revision_id):
            _refuse()
    for skill_id, revision in skills:
        head = repositories.preferences.skills.get(tx, owner_id=owner_id, skill_id=skill_id)
        if (type(head) is not SkillHead or head.owner_id != owner_id or head.revision != revision
                or not head.enabled or head.deleted_at is not None):
            _refuse()
    for note_id, revision in notes:
        note = repositories.preferences.personalization.get_explicit_note(
            tx, owner_id=owner_id, note_id=note_id, include_disabled=False)
        if (type(note) is not ExplicitNoteRecord or note.owner_id != owner_id
                or note.note_id != note_id or note.revision != revision):
            _refuse()


def _selection_identifiers(value):
    """Validate the closed version-1 shape and return hashable identifiers.

    Reuses the exact same validator HTTP Work applies to its own
    ``selection`` body, so a chat turn and a Work admission accept and refuse
    identically shaped input.
    """
    from orchestrator.work_submit import _selected_ids
    try:
        _selected_ids(value)
    except AssignmentError:
        _refuse()
    agent = None if value["agent"] is None else (value["agent"]["agent_id"], value["agent"]["revision_id"])
    skills = tuple(sorted((entry["skill_id"], entry["revision"]) for entry in value["skills"]))
    notes = tuple(sorted((entry["note_id"], entry["revision"]) for entry in value["notes"]))
    return agent, skills, notes


async def bind_turn_selection(binding, *, expected_orchestrator, selection):
    """Existence-check every selected head now, exactly as HTTP Work does at capture.

    A stale, forgotten or malformed selection refuses (``AssignmentError``);
    it is the caller's choice whether to drop it or fail the whole turn — this
    never mutates or re-derives ``selection`` itself.
    """
    from dataclasses import replace
    if type(binding) is not TurnGuidanceBinding:
        _refuse()
    binding.local(expected_orchestrator)
    agent, skills, notes = _selection_identifiers(selection)
    reader = TurnGuidanceReader(binding, expected_orchestrator=expected_orchestrator)
    try:
        def check(tx, repositories):
            _selection_current(tx, repositories, binding.origin.owner_id, agent, skills, notes)
            return TurnSelectionBinding(agent, skills, notes)
        resolved = await reader.transaction(check, expected_orchestrator=expected_orchestrator)
        await reader.verify_delivery()
    finally:
        reader.close()
    return replace(binding, selection=resolved)


@contextmanager
def use_turn_guidance(binding, *, expected_orchestrator):
    """Scope a server-owned handoff to its exact turn, restoring on every exit."""
    if type(binding) is not TurnGuidanceBinding:
        _refuse()
    binding.local(expected_orchestrator)
    if binding.task is not asyncio.current_task():
        _refuse()
    token = _TURN.set(binding)
    try:
        yield binding
    finally:
        _TURN.reset(token)
        binding.close()


def current_turn_guidance(*, expected_orchestrator, websocket, chat_id):
    """Return an explicit parent handoff; never reconstruct it from an owner."""
    value = _TURN.get()
    if value is None:
        return None
    if (type(value) is not TurnGuidanceBinding or value.websocket is not websocket
            or value.chat_id != chat_id or value.task is not asyncio.current_task()):
        _refuse()
    value.local(expected_orchestrator)
    return value


class TurnGuidanceReader:
    """Exact read-only facade capability for one short guidance observation.

    Only the fixed skill list may accept this type. Its legacy materialization
    is deterministic migration; authored save/toggle/delete still require an
    actual CurrentHumanCaller. The callback is server-owned synchronous code.
    """

    def __init__(self, binding, *, expected_orchestrator):
        if type(binding) is not TurnGuidanceBinding:
            _refuse()
        binding.local(expected_orchestrator)
        self._turn = binding
        self._orch = expected_orchestrator
        self._deadline = time.monotonic() + 15
        self._until = min(datetime.now(timezone.utc) + timedelta(seconds=15),
                          binding.origin.expires_at)
        self._closed = False

    def __repr__(self):
        return "<TurnGuidanceReader private>"

    @property
    def owner_id(self):
        return self._turn.origin.owner_id

    @property
    def runtime(self):
        return self._turn.origin.binding.runtime

    @property
    def plane_runtime(self):
        return self.runtime

    @property
    def repositories(self):
        return self._turn.origin.binding.repositories

    @property
    def audit_repo(self):
        return self._turn.origin.binding.audit

    def require_write(self):
        raise AssignmentError("human_write_required", 403)

    def close(self):
        self._closed = True

    def _local(self, orch):
        if (orch is not self._orch or self._closed or time.monotonic() >= self._deadline
                or datetime.now(timezone.utc) >= self._until):
            _refuse()
        self._turn.local(orch)
        try:
            task = asyncio.current_task()
        except RuntimeError:  # The bounded Plane callback runs in its owned worker.
            task = None
        if task is not None and task is not self._turn.task:
            _refuse()

    def _current(self, tx):
        self._local(self._orch)
        origin = self._turn.origin
        sessions = origin.binding.session_repository
        # Same owner79 fence as guidance writers, acquired before session and
        # operation rows. A callback may only reacquire this already-held lock.
        self.repositories.preferences.skills.lock_owner(tx, owner_id=self.owner_id)
        if origin.credential is not None:
            state = sessions.get_execution_state(tx, owner_id=self.owner_id,
                                                 session_id=origin.credential.session_id)
            if state is None or state.credential != origin.credential:
                _refuse()
            until = min(self._until, state.observed_at + timedelta(seconds=15), datetime.fromtimestamp(
                origin.credential.hard_expires_at, timezone.utc))
            sessions.assert_current_consent(tx, observation=SessionConsentObservation(
                origin.credential, state.observed_at, until))
        self._turn.current(tx, self._orch)
        self._local(self._orch)

    async def transaction(self, callback, *, expected_orchestrator):
        self._local(expected_orchestrator)
        if not callable(callback):
            _refuse()
        def invoke(tx):
            self._turn.origin.binding.session_repository.bound_request_execution_waits(tx)
            self._current(tx)
            value = callback(tx, self.repositories)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                _refuse()
            self._current(tx)
            return value
        try:
            async with asyncio.timeout_at(self._deadline):
                result = await self._turn.origin.binding.adapter.run_in_transaction(invoke)
                self._local(expected_orchestrator)
                return result
        except AssignmentError:
            raise
        except Exception:
            _refuse()

    async def verify_delivery(self):
        try:
            async with asyncio.timeout_at(self._deadline):
                self._local(self._orch)
                origin = self._turn.origin
                claims = await auth.verify_user(await auth.verify_production_token(origin.token))
                if (_claims_json(claims) != origin.claims_json
                        or _expiry(claims, origin.owner_id) != origin.expires_at):
                    _refuse()
                await self.transaction(lambda tx, repositories: None, expected_orchestrator=self._orch)
                self._local(self._orch)
        except AssignmentError:
            raise
        except Exception:
            _refuse()


async def acquire_turn_guidance_reader(*, expected_orchestrator, websocket, chat_id):
    """Verify the original admitted turn immediately before its fixed read."""
    binding = current_turn_guidance(expected_orchestrator=expected_orchestrator,
                                    websocket=websocket, chat_id=chat_id)
    reader = TurnGuidanceReader(binding, expected_orchestrator=expected_orchestrator)
    try:
        await reader.verify_delivery()
        return reader
    except BaseException:
        reader.close()
        raise
