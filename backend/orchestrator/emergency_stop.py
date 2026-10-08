"""Owner-scoped emergency stop state machine for the orchestrator: engages one
labeled stop across work admission, chat dispatch, scheduled turns, and tool
effects, tracks per-responder acknowledgments (local orchestrator plus enrolled
remote machines), and releases only through an explicit, revision-checked,
owner-matched resume. Pure stdlib; host wiring (audit, remote probes, durable
re-arm) is injected by emergency_stop_binding.py.
"""

from __future__ import annotations

import asyncio
import logging
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
    responders: dict[str, ResponderRow] = field(default_factory=dict)

    def overall(self) -> str:
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


def _safe_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


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
        self._rearm_checked: set[str] = set()
        self._sweeps: set[str] = set()

    async def _record(self, action: str, outcome: str, *, owner_id: str,
                      claims: dict | None = None, detail: dict[str, Any] | None = None) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(action, outcome, owner_id=owner_id, claims=claims, detail=detail or {})
        except Exception:
            logger.debug("emergency_stop audit %s failed", action, exc_info=True)

    def _remote_ids(self, owner_id: str) -> list[str]:
        if self._remote_responders is None:
            return []
        try:
            machines = self._remote_responders(owner_id)
        except Exception:
            logger.debug("emergency_stop responder discovery failed", exc_info=True)
            return []
        if not isinstance(machines, list):
            return []
        identifiers: list[str] = []
        for machine in machines[:_MAX_RESPONDERS]:
            if isinstance(machine, str) and machine and f"{REMOTE_PREFIX}{machine}" not in identifiers:
                identifiers.append(f"{REMOTE_PREFIX}{machine}")
        return identifiers

    def _register_responders(self, state: _StopState, owner_id: str, now: float) -> None:
        rows = {LOCAL_RESPONDER: ResponderRow(LOCAL_RESPONDER, "local", RESPONDER_ACKNOWLEDGED, now)}
        for responder in self._remote_ids(owner_id):
            rows[responder] = ResponderRow(responder, "remote", RESPONDER_PENDING, now)
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
            self._rearm_checked.add(owner_id)
            if owner_id in self._states:
                return self._states[owner_id]
            if not isinstance(durable, dict) or not durable.get("engaged"):
                return None
            now = self._clock()
            state = _StopState(
                engaged_at=float(durable.get("engaged_at") or now),
                engaged_by=str(durable.get("engaged_by") or owner_id),
                reason=str(durable.get("reason") or ""),
                revision=_safe_int(durable.get("revision"), 1),
            )
            state.responders = {LOCAL_RESPONDER: ResponderRow(
                LOCAL_RESPONDER, "local", RESPONDER_ACKNOWLEDGED, now)}
            for responder in self._remote_ids(owner_id):
                state.responders[responder] = ResponderRow(
                    responder, "remote", RESPONDER_PENDING, now)
            self._states[owner_id] = state
            return state

    def _status_locked(self, owner_id: str) -> dict[str, Any]:
        state = self._states.get(owner_id)
        if state is None:
            return {"engaged": False, "state": STATE_RUNNING, "revision": 0,
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
        now = self._clock()
        with self._lock:
            existing = self._states.get(owner_id)
            if existing is not None:
                return existing.status()
            self._rearm_checked.add(owner_id)
            state = _StopState(engaged_at=now, engaged_by=owner_id, reason=reason,
                               revision=self._next_revision_locked(owner_id))
            self._register_responders(state, owner_id, now)
            self._states[owner_id] = state
            status = state.status()
        await self._record("emergency_stop.engage", "success", owner_id=owner_id, claims=claims,
                           detail={"revision": status["revision"], "engaged_at": status["engaged_at"],
                                   "reason": reason})
        if sweep:
            self._start_sweep(owner_id)
        return status

    def _next_revision_locked(self, owner_id: str) -> int:
        state = self._states.get(owner_id)
        base = state.revision if state is not None else self._durable_revision(owner_id)
        return base + 1

    def _durable_revision(self, owner_id: str) -> int:
        if self._rearm_loader is None:
            return 0
        try:
            durable = self._rearm_loader(owner_id)
        except Exception:
            return 0
        if isinstance(durable, dict):
            return _safe_int(durable.get("revision"), 0)
        return 0

    async def resume(self, owner_id: str, *, expected_revision: Any, actor_id: str | None = None,
                     claims: dict | None = None) -> dict[str, Any]:
        expected_revision = _revision_value(expected_revision)
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                raise EmergencyStopRefused("emergency_stop_not_engaged", 409)
            revision = state.revision
            if state.revision != expected_revision:
                denial = "emergency_stop_stale_revision"
            elif actor_id is not None and actor_id != state.engaged_by:
                denial = "emergency_stop_resume_denied"
            else:
                denial = None
                del self._states[owner_id]
        await self._record("emergency_stop.resume",
                           "success" if denial is None else "failure",
                           owner_id=owner_id, claims=claims,
                           detail={"expected_revision": expected_revision,
                                   "current_revision": revision, "denial": denial})
        if denial is not None:
            raise EmergencyStopRefused(denial, 403 if denial == "emergency_stop_resume_denied" else 409)
        with self._lock:
            return self._status_locked(owner_id)

    def _set_responder(self, owner_id: str, responder: str, state_name: str) -> bool:
        if state_name not in _RESPONDER_STATES:
            raise EmergencyStopRefused("emergency_stop_invalid", 422)
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
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
            pending = [row.responder for row in state.responders.values()
                       if row.kind == "remote" and row.state != RESPONDER_ACKNOWLEDGED]
        for responder in pending:
            try:
                observed = await self._probe_responder(owner_id, responder)
            except Exception:
                observed = RESPONDER_UNREACHABLE
            if observed not in _RESPONDER_STATES:
                observed = RESPONDER_UNREACHABLE
            if observed == RESPONDER_ACKNOWLEDGED:
                self.acknowledge(owner_id, responder)
            else:
                self._set_responder(owner_id, responder, observed)

    def responder_snapshot(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                return []
            return [row.to_dict() for row in
                    sorted(state.responders.values(), key=lambda row: row.responder)]


def new_submission_id() -> str:
    return str(uuid4())
