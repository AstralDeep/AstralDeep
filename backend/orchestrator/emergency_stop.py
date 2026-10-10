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
from dataclasses import dataclass, field, replace
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


class EmergencyStopRefused(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


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
    ) -> None:
        self._clock = clock or time.time
        self._audit = audit
        self._remote_responders = remote_responders
        self._probe_responder = probe_responder
        self._rearm_loader = rearm_loader
        self._lock = threading.RLock()
        self._states: dict[str, _StopState] = {}
        self._revisions: dict[str, int] = {}
        self._transitions: dict[str, asyncio.Lock] = {}
        self._rearm_checked: set[str] = set()
        self._sweeps: set[str] = set()

    async def _record(self, action: str, outcome: str, *, owner_id: str,
                      claims: dict | None = None, detail: dict[str, Any] | None = None,
                      committed: Callable[[], None] | None = None) -> None:
        if self._audit is None:
            if committed is not None:
                committed()
            return
        task = asyncio.create_task(self._audit(
            action, outcome, owner_id=owner_id, claims=claims, detail=detail or {}))
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
                task.result()
            except (asyncio.CancelledError, Exception):
                logger.warning("emergency_stop cancelled audit failed", exc_info=True)
            else:
                if committed is not None:
                    committed()
            raise
        except Exception:
            logger.warning("emergency_stop audit transition failed", exc_info=True)
            raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
        if committed is not None:
            committed()

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
            if owner_id in self._states or owner_id in self._rearm_checked:
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
            if owner_id in self._states:
                return self._states[owner_id]
            if durable is None:
                self._rearm_checked.add(owner_id)
                return None
            if (not isinstance(durable, dict) or not isinstance(durable.get("engaged"), bool)
                    or isinstance(durable.get("revision"), bool)
                    or not isinstance(durable.get("revision"), int)
                    or not 1 <= durable["revision"] <= 2**63 - 1):
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            self._revisions[owner_id] = durable["revision"]
            if not durable["engaged"]:
                self._rearm_checked.add(owner_id)
                return None
            engaged_at = durable.get("engaged_at")
            if (isinstance(engaged_at, bool) or not isinstance(engaged_at, (int, float))
                    or not 0 <= engaged_at <= 2**63 - 1 or not math.isfinite(engaged_at)
                    or durable.get("engaged_by") != owner_id
                    or not isinstance(durable.get("reason"), str)):
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            now = self._clock()
            state = _StopState(
                engaged_at=float(engaged_at), engaged_by=owner_id,
                reason=_clean_reason(durable["reason"]), revision=durable["revision"],
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
                     sweep: bool = True) -> dict[str, Any]:
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
                    self._states[owner_id] = state
                    self._revisions[owner_id] = revision
                    self._register_responders(state, owner_id, now)
                self._rearm_checked.add(owner_id)
            def persisted() -> None:
                with self._lock:
                    state.audit_durable = True

            await self._record("emergency_stop.engage", "success", owner_id=owner_id, claims=claims,
                               detail={"revision": state.revision, "engaged_at": state.engaged_at,
                                       "reason": state.reason}, committed=persisted)
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
                     claims: dict | None = None) -> dict[str, Any]:
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
            def persisted() -> None:
                with self._lock:
                    del self._states[owner_id]

            await self._record("emergency_stop.resume",
                               "success" if denial is None else "failure",
                               owner_id=owner_id, claims=claims,
                               detail={"expected_revision": expected_revision,
                                       "current_revision": revision, "denial": denial},
                               committed=persisted if denial is None else None)
            if denial is not None:
                raise EmergencyStopRefused(
                    denial, 403 if denial == "emergency_stop_resume_denied" else 409)
            with self._lock:
                return self._status_locked(owner_id)

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
