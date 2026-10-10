"""Owner-scoped stop admission and responder state for the orchestrator.
emergency_stop_binding.py supplies durable audit transitions and remote observations;
resume retains the local fence until its current transition is durably recorded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable
from uuid import uuid4

logger = logging.getLogger("Orchestrator.EmergencyStop")

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_PARTIAL = "partial"
STATE_UNREACHABLE = "unreachable"

RESPONDER_PENDING = "pending"
RESPONDER_ACKNOWLEDGED = "acknowledged"
RESPONDER_UNREACHABLE = "unreachable"
_RESPONDER_STATES = frozenset({RESPONDER_PENDING, RESPONDER_ACKNOWLEDGED, RESPONDER_UNREACHABLE})

LOCAL_RESPONDER = "local.orchestrator"
DISCOVERY_RESPONDER = "remote.discovery"
REMOTE_PREFIX = "remote:"
_MAX_RESPONDERS = 64
_MAX_REASON = 280
_MAX_METAS = 32
_INTERRUPTION_TIMEOUT_SECONDS = 6.0
_effect_epochs = ContextVar("emergency_stop_effect_epochs", default=None)


def current_effect_epoch(owner_id: str) -> int | None:
    return (_effect_epochs.get() or {}).get(owner_id)


class EmergencyStopRefused(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


async def await_interruption(tasks) -> None:
    pending = {task for task in tasks if task is not asyncio.current_task() and not task.done()}
    if pending:
        _, unfinished = await asyncio.wait(pending, timeout=_INTERRUPTION_TIMEOUT_SECONDS)
        if unfinished:
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)


@dataclass(frozen=True)
class ResponderRow:
    responder: str
    kind: str
    state: str
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "responder": self.responder,
            "kind": self.kind,
            "state": self.state,
            "updated_at": self.updated_at,
        }


@dataclass
class _StopState:
    engaged_at: float
    engaged_by: str
    reason: str
    revision: int
    audit_durable: bool = True
    epoch: int = 1
    responders: dict[str, ResponderRow] = field(default_factory=dict)

    def overall(self) -> str:
        if not self.audit_durable:
            return STATE_UNREACHABLE
        rows = list(self.responders.values())
        if rows and all(row.state == RESPONDER_ACKNOWLEDGED for row in rows):
            return STATE_STOPPED
        if any(row.state == RESPONDER_UNREACHABLE for row in rows):
            return STATE_UNREACHABLE
        return STATE_PARTIAL

    def status(self, *, engaged: bool = True) -> dict[str, Any]:
        if not engaged:
            return {
                "engaged": False,
                "state": STATE_RUNNING,
                "revision": self.revision,
                "engaged_at": None,
                "engaged_by": None,
                "reason": None,
                "responders": [],
            }
        return {
            "engaged": True,
            "state": self.overall(),
            "revision": self.revision,
            "epoch": self.epoch,
            "engaged_at": self.engaged_at,
            "engaged_by": self.engaged_by,
            "reason": self.reason,
            "responders": [row.to_dict() for row in
                           sorted(self.responders.values(), key=lambda row: row.responder)],
        }


def _clean_reason(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EmergencyStopRefused("emergency_stop_invalid", 422)
    reason = " ".join(value.split())
    if len(reason) > _MAX_REASON:
        raise EmergencyStopRefused("emergency_stop_invalid", 422)
    return reason


def _revision_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise EmergencyStopRefused("emergency_stop_invalid", 422)
    return value


class EmergencyStopCoordinator:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        audit: Callable[..., Awaitable[None]] | None = None,
        remote_responders: Callable[[str], list[str]] | None = None,
        probe_responder: Callable[[str, str], Awaitable[str]] | None = None,
        rearm_loader: Callable[[str], dict[str, Any] | None] | None = None,
        fresh_rearm: bool = False,
        interrupt: Callable[[str, asyncio.Task], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._audit = audit
        self._remote_responders = remote_responders
        self._probe_responder = probe_responder
        self._rearm_loader = rearm_loader
        self._fresh_rearm = fresh_rearm
        self._interrupt = interrupt
        self._effects: dict[str, dict[asyncio.Task, int]] = {}
        self._epochs: dict[str, int] = {}
        self._lock = threading.RLock()
        self._states: dict[str, _StopState] = {}
        self._revisions: dict[str, int] = {}
        self._transitions: dict[str, asyncio.Lock] = {}
        self._rearm_checked: set[str] = set()
        self._sweeps: set[str] = set()

    async def _record(self, action: str, outcome: str, *, owner_id: str,
                      claims: dict | None = None, detail: dict[str, Any] | None = None,
                      committed: Callable[[Any], None] | None = None, caller=None) -> None:
        if self._audit is None:
            if committed is not None:
                committed(None)
            return
        kwargs = {"owner_id": owner_id, "claims": claims, "detail": detail or {}}
        if caller is not None:
            kwargs["caller"] = caller
        task = asyncio.create_task(self._audit(action, outcome, **kwargs))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                receipt = task.result()
            except (asyncio.CancelledError, Exception):
                logger.warning("emergency_stop cancelled audit failed", exc_info=True)
            else:
                if committed is not None:
                    committed(receipt)
            raise
        except EmergencyStopRefused:
            raise
        except Exception:
            logger.warning("emergency_stop audit transition failed", exc_info=True)
            raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
        if committed is not None:
            committed(task.result())

    def _transition_lock(self, owner_id: str) -> asyncio.Lock:
        with self._lock:
            return self._transitions.setdefault(owner_id, asyncio.Lock())

    def _remote_ids(self, owner_id: str) -> list[str]:
        if self._remote_responders is None:
            return []
        try:
            machines = self._remote_responders(owner_id)
        except Exception:
            logger.debug("emergency_stop responder discovery failed", exc_info=True)
            raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
        if (not isinstance(machines, list) or len(machines) > _MAX_RESPONDERS
                or any(not isinstance(machine, str) or not machine for machine in machines)):
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)
        identifiers: list[str] = []
        for machine in machines:
            if f"{REMOTE_PREFIX}{machine}" not in identifiers:
                identifiers.append(f"{REMOTE_PREFIX}{machine}")
        return identifiers

    def _register_responders(self, state: _StopState, owner_id: str, now: float) -> None:
        rows = {LOCAL_RESPONDER: ResponderRow(LOCAL_RESPONDER, "local", RESPONDER_ACKNOWLEDGED, now)}
        try:
            for responder in self._remote_ids(owner_id):
                rows[responder] = ResponderRow(responder, "remote", RESPONDER_PENDING, now)
        except EmergencyStopRefused:
            rows[DISCOVERY_RESPONDER] = ResponderRow(
                DISCOVERY_RESPONDER, "discovery", RESPONDER_UNREACHABLE, now)
        state.responders = rows

    def _load_rearm(self, owner_id: str) -> _StopState | None:
        with self._lock:
            existing = self._states.get(owner_id)
            if existing is not None and not existing.audit_durable:
                return existing
            if not self._fresh_rearm and (owner_id in self._states or owner_id in self._rearm_checked):
                return self._states.get(owner_id)
        if self._rearm_loader is None:
            with self._lock:
                self._rearm_checked.add(owner_id)
            return None
        try:
            durable = self._rearm_loader(owner_id)
        except Exception:
            logger.warning("emergency_stop durable state unavailable for %s; failing closed",
                           owner_id, exc_info=True)
            raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
        with self._lock:
            if not self._fresh_rearm and owner_id in self._states:
                return self._states[owner_id]
            if durable is None:
                self._states.pop(owner_id, None)
                self._revisions[owner_id] = 0
                self._epochs[owner_id] = 0
                self._rearm_checked.add(owner_id)
                return None
            if (not isinstance(durable, dict) or not isinstance(durable.get("engaged"), bool)
                    or isinstance(durable.get("revision"), bool)
                    or not isinstance(durable.get("revision"), int)
                    or not 1 <= durable["revision"] <= 2**63 - 1):
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            self._revisions[owner_id] = durable["revision"]
            epoch = durable.get("epoch", durable["revision"])
            if type(epoch) is not int or not 0 <= epoch <= 2**63 - 1:
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            self._epochs[owner_id] = epoch
            if not durable["engaged"]:
                self._states.pop(owner_id, None)
                self._rearm_checked.add(owner_id)
                return None
            engaged_at = durable.get("engaged_at")
            if (isinstance(engaged_at, bool) or not isinstance(engaged_at, (int, float))
                    or not 0 <= engaged_at <= 2**63 - 1 or not math.isfinite(engaged_at)
                    or durable.get("engaged_by") != owner_id
                    or not isinstance(durable.get("reason"), str)):
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            existing = self._states.get(owner_id)
            if (self._fresh_rearm and existing is not None and existing.revision == durable["revision"]
                    and existing.epoch == epoch):
                return existing
            now = self._clock()
            state = _StopState(
                engaged_at=float(engaged_at), engaged_by=owner_id,
                reason=_clean_reason(durable["reason"]), revision=durable["revision"],
                epoch=self._epochs[owner_id],
            )
            self._register_responders(state, owner_id, now)
            self._states[owner_id] = state
            self._rearm_checked.add(owner_id)
            return state

    def _status_locked(self, owner_id: str) -> dict[str, Any]:
        state = self._states.get(owner_id)
        if state is None:
            return {"engaged": False, "state": STATE_RUNNING,
                    "revision": self._revisions.get(owner_id, 0),
                    "epoch": self._epochs.get(owner_id, 0),
                    "engaged_at": None, "engaged_by": None, "reason": None, "responders": []}
        return state.status()

    def status(self, owner_id: str) -> dict[str, Any]:
        try:
            self._load_rearm(owner_id)
        except EmergencyStopRefused:
            with self._lock:
                return {"engaged": True, "state": STATE_UNREACHABLE, "revision": 0,
                        "engaged_at": None, "engaged_by": None,
                        "reason": "durable stop state unavailable",
                        "responders": []}
        with self._lock:
            return self._status_locked(owner_id)

    def admission_allowed(self, owner_id: str) -> bool:
        if not isinstance(owner_id, str) or not owner_id:
            return True
        try:
            state = self._load_rearm(owner_id)
        except EmergencyStopRefused:
            return False
        return state is None

    async def engage(self, owner_id: str, *, reason: Any = None, claims: dict | None = None,
                     sweep: bool = True, caller=None) -> dict[str, Any]:
        reason = _clean_reason(reason)
        async with self._transition_lock(owner_id):
            self._load_rearm(owner_id)
            now = self._clock()
            with self._lock:
                state = self._states.get(owner_id)
                if state is not None and state.audit_durable:
                    return state.status()
                if state is None:
                    revision = self._next_revision_locked(owner_id)
                    state = _StopState(now, owner_id, reason, revision, audit_durable=False)
                    state.epoch = self._epochs.get(owner_id, 0) + 1
                    self._states[owner_id] = state
                    self._revisions[owner_id] = revision
                    self._register_responders(state, owner_id, now)
                self._rearm_checked.add(owner_id)
            def persisted(receipt) -> None:
                with self._lock:
                    if isinstance(receipt, dict):
                        state.revision = receipt["revision"]
                        state.epoch = receipt["epoch"]
                        state.engaged_at = receipt["engaged_at"]
                        state.engaged_by = receipt["engaged_by"]
                        state.reason = receipt["reason"]
                        self._revisions[owner_id] = state.revision
                    self._epochs[owner_id] = state.epoch
                    state.audit_durable = True

            with self._lock:
                control_task = asyncio.current_task()
                tasks = tuple(task for task in self._effects.get(owner_id, ())
                              if task is not control_task and not task.done())
                if self._interrupt is not None or tasks:
                    state.responders[LOCAL_RESPONDER] = ResponderRow(
                        LOCAL_RESPONDER, "local", RESPONDER_PENDING, now)
            for task in tasks:
                task.cancel()

            async def interrupt() -> None:
                if self._interrupt is not None:
                    await self._interrupt(owner_id, control_task)
                await await_interruption(tasks)

            interruption = (asyncio.create_task(interrupt())
                            if self._interrupt is not None or tasks else None)
            try:
                await self._record("emergency_stop.engage", "success", owner_id=owner_id, claims=claims,
                                   detail={"revision": state.revision, "engaged_at": state.engaged_at,
                                           "epoch": state.epoch, "reason": state.reason},
                                   committed=persisted, caller=caller)
            finally:
                if interruption is not None:
                    cancelled = False
                    while not interruption.done():
                        try:
                            await asyncio.shield(interruption)
                        except asyncio.CancelledError:
                            cancelled = True
                            continue
                        except Exception:
                            break
                    try:
                        interruption.result()
                    except (asyncio.CancelledError, Exception):
                        self._set_responder(owner_id, LOCAL_RESPONDER, RESPONDER_UNREACHABLE,
                                            expected_revision=state.revision)
                        raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
                    self._set_responder(owner_id, LOCAL_RESPONDER, RESPONDER_ACKNOWLEDGED,
                                        expected_revision=state.revision)
                    if cancelled:
                        raise asyncio.CancelledError
            with self._lock:
                status = state.status()
            if sweep:
                self._start_sweep(owner_id)
            return status

    def _next_revision_locked(self, owner_id: str) -> int:
        base = self._revisions.get(owner_id, 0)
        if base >= 2**63 - 1:
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)
        return base + 1

    async def resume(self, owner_id: str, *, expected_revision: Any, actor_id: str | None = None,
                     claims: dict | None = None, caller=None) -> dict[str, Any]:
        expected_revision = _revision_value(expected_revision)
        async with self._transition_lock(owner_id):
            self._load_rearm(owner_id)
            with self._lock:
                state = self._states.get(owner_id)
                if state is None:
                    raise EmergencyStopRefused("emergency_stop_not_engaged", 409)
                if not state.audit_durable:
                    raise EmergencyStopRefused("emergency_stop_unavailable", 503)
                revision = state.revision
                if revision != expected_revision:
                    denial = "emergency_stop_stale_revision"
                elif actor_id is not None and actor_id != state.engaged_by:
                    denial = "emergency_stop_resume_denied"
                else:
                    denial = None
            def persisted(receipt) -> None:
                with self._lock:
                    if self._states.get(owner_id) is state:
                        self._states.pop(owner_id, None)
                    if isinstance(receipt, dict):
                        self._revisions[owner_id] = max(self._revisions.get(owner_id, 0), receipt["revision"])

            await self._record("emergency_stop.resume",
                               "success" if denial is None else "failure",
                               owner_id=owner_id, claims=claims,
                               detail={"expected_revision": expected_revision,
                                       "current_revision": revision, "denial": denial},
                               committed=persisted if denial is None else None, caller=caller)
            if denial is not None:
                raise EmergencyStopRefused(
                    denial, 403 if denial == "emergency_stop_resume_denied" else 409)
            with self._lock:
                return self._status_locked(owner_id)

    @asynccontextmanager
    async def effect(self, owner_id: str):
        if not self.admission_allowed(owner_id):
            raise EmergencyStopRefused("emergency_stop_active", 423)
        task = asyncio.current_task()
        with self._lock:
            if owner_id in self._states:
                raise EmergencyStopRefused("emergency_stop_active", 423)
            tasks = self._effects.setdefault(owner_id, {})
            tasks[task] = tasks.get(task, 0) + 1
            epoch = self._epochs.get(owner_id, 0)
        previous = _effect_epochs.get() or {}
        if owner_id in previous and previous[owner_id] != epoch:
            with self._lock:
                tasks[task] -= 1
                if not tasks[task]:
                    tasks.pop(task)
                if not tasks:
                    self._effects.pop(owner_id, None)
            raise EmergencyStopRefused("emergency_stop_stale_epoch", 423)
        token = _effect_epochs.set({**previous, owner_id: epoch})
        try:
            yield
        finally:
            _effect_epochs.reset(token)
            with self._lock:
                tasks = self._effects.get(owner_id)
                if tasks is not None:
                    count = tasks.get(task, 0)
                    if count > 1:
                        tasks[task] = count - 1
                    else:
                        tasks.pop(task, None)
                    if not tasks:
                        self._effects.pop(owner_id, None)

    def _set_responder(self, owner_id: str, responder: str, state_name: str,
                       *, expected_revision: int | None = None) -> bool:
        if state_name not in _RESPONDER_STATES:
            raise EmergencyStopRefused("emergency_stop_invalid", 422)
        with self._lock:
            state = self._states.get(owner_id)
            if state is None or (expected_revision is not None and state.revision != expected_revision):
                return False
            row = state.responders.get(responder)
            if row is None:
                return False
            if row.state == RESPONDER_ACKNOWLEDGED and state_name != RESPONDER_ACKNOWLEDGED:
                return False
            state.responders[responder] = replace(row, state=state_name,
                                                  updated_at=self._clock())
            return True

    def acknowledge(self, owner_id: str, responder: str) -> bool:
        return self._set_responder(owner_id, responder, RESPONDER_ACKNOWLEDGED)

    def mark_unreachable(self, owner_id: str, responder: str) -> bool:
        return self._set_responder(owner_id, responder, RESPONDER_UNREACHABLE)

    async def verify(self, owner_id: str) -> dict[str, Any]:
        self._load_rearm(owner_id)
        await self._sweep_once(owner_id)
        with self._lock:
            return self._status_locked(owner_id)

    def _start_sweep(self, owner_id: str) -> None:
        with self._lock:
            if owner_id in self._sweeps:
                return
            self._sweeps.add(owner_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with self._lock:
                self._sweeps.discard(owner_id)
            return
        task = loop.create_task(self._sweep_task(owner_id))
        task.add_done_callback(lambda _task: self._sweeps.discard(owner_id))

    async def _sweep_task(self, owner_id: str) -> None:
        try:
            await self._sweep_once(owner_id)
        except Exception:
            logger.debug("emergency_stop sweep failed for %s", owner_id, exc_info=True)

    async def _sweep_once(self, owner_id: str) -> None:
        if self._probe_responder is None:
            return
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                return
            revision = state.revision
            pending = [row.responder for row in state.responders.values()
                       if row.kind == "remote" and row.state != RESPONDER_ACKNOWLEDGED]
        for responder in pending:
            try:
                observed = await self._probe_responder(owner_id, responder)
            except Exception:
                observed = RESPONDER_UNREACHABLE
            if observed not in _RESPONDER_STATES:
                observed = RESPONDER_UNREACHABLE
            self._set_responder(owner_id, responder, observed, expected_revision=revision)

    def responder_snapshot(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                return []
            return [row.to_dict() for row in
                    sorted(state.responders.values(), key=lambda row: row.responder)]


def new_submission_id() -> str:
    return str(uuid4())
