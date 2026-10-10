"""Per-owner local emergency stop state machine: one labeled, revision-fenced stop per
owner that gates new effect admission, persists through an injected durable writer
before success is acknowledged, and interrupts supported local work through an injected
hook; host wiring (audit persistence, dispatch gates, task cancellation) is provided by
emergency_stop_binding.py.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

logger = logging.getLogger("Orchestrator.EmergencyStop")

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_PERSISTENCE_PENDING = "persistence_pending"
STATE_DURABLE_UNAVAILABLE = "durable_unavailable"

ENGAGE_ACTION = "emergency_stop.engage"
RESUME_ACTION = "emergency_stop.resume"

_MAX_REASON = 280
_MAX_REVISION = 2**63 - 1


class EmergencyStopRefused(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass
class _StopState:
    engaged_at: float
    engaged_by: str
    reason: str
    revision: int
    persisted: bool = False
    interrupted: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "engaged": True,
            "state": STATE_STOPPED if self.persisted else STATE_PERSISTENCE_PENDING,
            "revision": self.revision,
            "engaged_at": self.engaged_at,
            "engaged_by": self.engaged_by,
            "reason": self.reason,
            "persisted": self.persisted,
            "interrupted": self.interrupted,
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
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_REVISION:
        raise EmergencyStopRefused("emergency_stop_invalid", 422)
    return value


def _safe_revision(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if 1 <= value <= _MAX_REVISION else default


class EmergencyStopCoordinator:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        persist: Callable[[str, str, dict[str, Any]], Any] | None = None,
        load_durable: Callable[[str], dict[str, Any] | None] | None = None,
        interrupt: Callable[[str], Any] | None = None,
        audit_denial: Callable[..., Any] | None = None,
    ) -> None:
        self._clock = clock
        self._persist = persist
        self._load_durable = load_durable
        self._interrupt = interrupt
        self._audit_denial = audit_denial
        self._lock = threading.RLock()
        self._states: dict[str, _StopState] = {}
        self._durable_checked: set[str] = set()
        self._revision_floor: dict[str, int] = {}

    def _durable_state(self, owner_id: str) -> dict[str, Any] | None:
        with self._lock:
            if owner_id in self._durable_checked:
                return None
        if self._load_durable is None:
            with self._lock:
                self._durable_checked.add(owner_id)
            return None
        try:
            durable = self._load_durable(owner_id)
        except Exception:
            logger.warning(
                "emergency stop durable state unavailable for %s; failing closed",
                owner_id, exc_info=True)
            raise EmergencyStopRefused("emergency_stop_unavailable", 503) from None
        with self._lock:
            self._durable_checked.add(owner_id)
            if not isinstance(durable, dict):
                return None
            self._revision_floor[owner_id] = max(
                self._revision_floor.get(owner_id, 0),
                _safe_revision(durable.get("revision"), 0))
            if not durable.get("engaged"):
                return None
            now = self._clock()
            state = _StopState(
                engaged_at=float(durable.get("engaged_at") or now),
                engaged_by=str(durable.get("engaged_by") or owner_id),
                reason=str(durable.get("reason") or ""),
                revision=_safe_revision(durable.get("revision"), 1),
                persisted=True,
            )
            existing = self._states.get(owner_id)
            if existing is not None and existing.revision >= state.revision:
                if existing.revision == state.revision and not existing.persisted:
                    existing.persisted = True
                return existing
            self._states[owner_id] = state
            return state

    def _known_revision_locked(self, owner_id: str) -> int:
        state = self._states.get(owner_id)
        revision = state.revision if state is not None else 0
        return max(revision, self._revision_floor.get(owner_id, 0))

    def _durable_write(self, owner_id: str, action: str, detail: dict[str, Any]) -> None:
        if self._persist is None:
            raise EmergencyStopRefused("emergency_stop_unavailable", 503)
        try:
            self._persist(owner_id, action, detail)
        except EmergencyStopRefused:
            raise
        except Exception:
            logger.warning(
                "emergency stop durable %s write failed for %s",
                action, owner_id, exc_info=True)
            raise EmergencyStopRefused("emergency_stop_persistence_failed", 503) from None

    async def _record_denial(self, action: str, outcome: str, *, owner_id: str,
                             detail: dict[str, Any] | None = None) -> None:
        if self._audit_denial is None:
            return
        try:
            outcome_recorded = self._audit_denial(
                action, outcome, owner_id=owner_id, detail=dict(detail or {}))
            if asyncio.iscoroutine(outcome_recorded):
                await outcome_recorded
        except Exception:
            logger.debug("emergency stop denial audit failed", exc_info=True)

    async def engage(self, owner_id: str, *, reason: Any = None,
                     claims: dict | None = None) -> dict[str, Any]:
        if not isinstance(owner_id, str) or not owner_id:
            raise EmergencyStopRefused("emergency_stop_invalid", 422)
        clean_reason = _clean_reason(reason)
        self._durable_state(owner_id)
        with self._lock:
            state = self._states.get(owner_id)
            if state is not None and state.persisted:
                return state.status()
            if state is None:
                state = _StopState(
                    engaged_at=self._clock(),
                    engaged_by=owner_id,
                    reason=clean_reason,
                    revision=self._known_revision_locked(owner_id) + 1,
                )
                self._states[owner_id] = state
            detail = {
                "revision": state.revision,
                "engaged_at": _iso(state.engaged_at),
                "engaged_by": state.engaged_by,
                "reason": state.reason,
            }
        try:
            await asyncio.to_thread(
                self._durable_write, owner_id, ENGAGE_ACTION, detail)
        except EmergencyStopRefused as exc:
            await self._record_denial(
                ENGAGE_ACTION, "failure", owner_id=owner_id,
                detail={"revision": detail["revision"], "denial": exc.code})
            raise
        interrupted: dict[str, Any] | None = None
        if self._interrupt is not None:
            try:
                outcome = self._interrupt(owner_id)
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                interrupted = outcome if isinstance(outcome, dict) else None
            except Exception:
                logger.warning(
                    "emergency stop interruption hook failed for %s", owner_id,
                    exc_info=True)
                interrupted = {"error": "interrupt_failed"}
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                raise EmergencyStopRefused("emergency_stop_unavailable", 503)
            state.persisted = True
            state.interrupted = interrupted
            status = state.status()
        self._revision_floor[owner_id] = max(
            self._revision_floor.get(owner_id, 0), status["revision"])
        return status

    async def resume(self, owner_id: str, *, expected_revision: Any,
                     actor_id: str | None = None,
                     claims: dict | None = None) -> dict[str, Any]:
        revision = _revision_value(expected_revision)
        self._durable_state(owner_id)
        denial: str | None = None
        with self._lock:
            state = self._states.get(owner_id)
            if state is None:
                denial = "emergency_stop_not_engaged"
            elif state.revision != revision:
                denial = "emergency_stop_stale_revision"
            elif actor_id is not None and actor_id != state.engaged_by:
                denial = "emergency_stop_resume_denied"
            else:
                detail = {
                    "revision": state.revision,
                    "resumed_by": actor_id or state.engaged_by,
                    "engaged_at": _iso(state.engaged_at),
                }
        if denial is not None:
            await self._record_denial(
                RESUME_ACTION, "failure", owner_id=owner_id, detail={"denial": denial})
            raise EmergencyStopRefused(
                denial,
                403 if denial == "emergency_stop_resume_denied" else 409)
        try:
            await asyncio.to_thread(
                self._durable_write, owner_id, RESUME_ACTION, detail)
        except EmergencyStopRefused as exc:
            await self._record_denial(
                RESUME_ACTION, "failure", owner_id=owner_id,
                detail={"revision": detail["revision"], "denial": exc.code})
            raise
        with self._lock:
            self._states.pop(owner_id, None)
            self._revision_floor[owner_id] = max(
                self._revision_floor.get(owner_id, 0), detail["revision"])
            return self._status_locked(owner_id)

    def _status_locked(self, owner_id: str) -> dict[str, Any]:
        state = self._states.get(owner_id)
        if state is not None:
            return state.status()
        return {
            "engaged": False,
            "state": STATE_RUNNING,
            "revision": self._revision_floor.get(owner_id, 0),
            "engaged_at": None,
            "engaged_by": None,
            "reason": None,
            "persisted": False,
            "interrupted": None,
        }

    def status(self, owner_id: str) -> dict[str, Any]:
        try:
            self._durable_state(owner_id)
        except EmergencyStopRefused:
            with self._lock:
                return {
                    "engaged": True,
                    "state": STATE_DURABLE_UNAVAILABLE,
                    "revision": 0,
                    "engaged_at": None,
                    "engaged_by": None,
                    "reason": "durable stop state unavailable",
                    "persisted": False,
                    "interrupted": None,
                }
        with self._lock:
            return self._status_locked(owner_id)

    def refresh(self, owner_id: str) -> dict[str, Any]:
        with self._lock:
            self._durable_checked.discard(owner_id)
        return self.status(owner_id)

    def admission_allowed(self, owner_id: Any) -> bool:
        if not isinstance(owner_id, str) or not owner_id or owner_id == "legacy":
            return True
        try:
            self._durable_state(owner_id)
        except EmergencyStopRefused:
            return False
        with self._lock:
            return owner_id not in self._states


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat()
