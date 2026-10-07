"""Emergency stop core — owner-visible, spans admission/agents/schedules/shells/remote."""
from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple


class EmergencyState(enum.Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PARTIAL_ACK = "partial_acknowledgment"
    UNREACHABLE = "unreachable"
    RESUMED = "resumed"


class ResumeDisposition(enum.Enum):
    ALLOW = "allow"
    DENIED = "denied"
    STALE = "stale"
    ACCOUNT_SWITCHED = "account_switched"


@dataclass
class StopRecord:
    stop_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    owner: str = ""
    initiated_at: float = field(default_factory=time.monotonic)
    acknowledged_by: Set[str] = field(default_factory=set)
    state: EmergencyState = EmergencyState.RUNNING
    local_only: bool = False
    last_remote_ack: Optional[float] = None
    failure_reason: Optional[str] = None


class EmergencyStopCore:
    """Qualified stop core with local fallback and remote acknowledgment tracking."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, StopRecord] = {}
        self._current: Optional[StopRecord] = None
        self._remote_available = True

    def set_remote_availability(self, available: bool) -> None:
        with self._lock:
            self._remote_available = available

    def emergency_stop(self, owner: str, local_only: bool = False) -> StopRecord:
        with self._lock:
            record = StopRecord(owner=owner, local_only=local_only)
            if local_only or not self._remote_available:
                record.state = EmergencyState.STOPPED
                record.local_only = True
            else:
                record.state = EmergencyState.PARTIAL_ACK
            self._current = record
            self._records[record.stop_id] = record
            return record

    def acknowledge_remote(self, stop_id: str, node_id: str) -> bool:
        with self._lock:
            record = self._records.get(stop_id)
            if record is None:
                return False
            record.acknowledged_by.add(node_id)
            if not record.local_only and len(record.acknowledged_by) > 0:
                record.last_remote_ack = time.monotonic()
                if record.state == EmergencyState.PARTIAL_ACK:
                    record.state = EmergencyState.STOPPED
            return True

    def mark_unreachable(self, stop_id: str) -> bool:
        with self._lock:
            record = self._records.get(stop_id)
            if record is None or record.local_only:
                return False
            record.state = EmergencyState.UNREACHABLE
            return True

    def resume(self, stop_id: str, owner: str, account_id: Optional[str] = None,
               expected_account: Optional[str] = None) -> Tuple[ResumeDisposition, Optional[str]]:
        with self._lock:
            record = self._records.get(stop_id)
            if record is None:
                return ResumeDisposition.DENIED, "stop_not_found"
            if record.owner != owner:
                return ResumeDisposition.DENIED, "not_owner"
            if expected_account and account_id != expected_account:
                return ResumeDisposition.ACCOUNT_SWITCHED, account_id
            if record.state == EmergencyState.UNREACHABLE:
                return ResumeDisposition.STALE, "remote_state_unknown"
            record.state = EmergencyState.RESUMED
            return ResumeDisposition.ALLOW, record.stop_id

    def current_state(self) -> Optional[EmergencyState]:
        with self._lock:
            return self._current.state if self._current else None

    def current_record(self) -> Optional[StopRecord]:
        with self._lock:
            return self._current

    def is_stopped(self) -> bool:
        with self._lock:
            if self._current is None:
                return False
            return self._current.state in (EmergencyState.STOPPED, EmergencyState.PARTIAL_ACK,
                                           EmergencyState.UNREACHABLE)


# Module-level singleton used across the system
emergency_core = EmergencyStopCore()
