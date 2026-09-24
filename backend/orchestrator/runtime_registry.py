"""Atomic process-local projections of runtime state, published as one coherent snapshot
so event-loop and worker-thread readers never see a partially mutated view; writers
serialize through a state-revision compare-and-set.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


_MAX_UINT64 = (1 << 64) - 1


class RegistryKind(str, Enum):
    RUNTIME = "runtime"
    HOST_SESSION = "host_session"
    LIFECYCLE = "lifecycle"
    CARD = "card"


class StaleRegistryRevisionError(RuntimeError):
    pass


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[Any, Any] = {}
        for key, item in value.items():
            frozen[_freeze_value(key)] = _freeze_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class RuntimeRegistryRecord:
    kind: RegistryKind
    identity: str
    state_revision: int
    value: object

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RegistryKind):
            raise TypeError("kind must be a RegistryKind")
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("identity must be a non-empty string")
        if (
            isinstance(self.state_revision, bool)
            or not isinstance(self.state_revision, int)
            or self.state_revision < 0
            or self.state_revision > _MAX_UINT64
        ):
            raise ValueError("state_revision must be an unsigned 64-bit integer")
        object.__setattr__(self, "value", _freeze_value(self.value))


@dataclass(frozen=True)
class RuntimeRegistrySnapshot:
    registry_version: int
    runtimes: tuple[RuntimeRegistryRecord, ...]
    host_sessions: tuple[RuntimeRegistryRecord, ...]
    lifecycles: tuple[RuntimeRegistryRecord, ...]
    cards: tuple[RuntimeRegistryRecord, ...]
    captured_at_monotonic: float

    def records(self, kind: RegistryKind) -> tuple[RuntimeRegistryRecord, ...]:
        if kind is RegistryKind.RUNTIME:
            return self.runtimes
        if kind is RegistryKind.HOST_SESSION:
            return self.host_sessions
        if kind is RegistryKind.LIFECYCLE:
            return self.lifecycles
        if kind is RegistryKind.CARD:
            return self.cards
        raise TypeError("kind must be a RegistryKind")


class RuntimeRegistry:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._writer_lock = threading.Lock()
        self._snapshot = RuntimeRegistrySnapshot(
            registry_version=0,
            runtimes=(),
            host_sessions=(),
            lifecycles=(),
            cards=(),
            captured_at_monotonic=self._capture_time(0.0),
        )

    def _capture_time(self, floor: float) -> float:
        value = float(self._monotonic())
        if value < 0:
            raise ValueError("monotonic clock returned a negative value")
        return max(floor, value)

    def snapshot(self) -> RuntimeRegistrySnapshot:
        return self._snapshot

    def list_records(
        self,
        kind: RegistryKind,
        *,
        snapshot: RuntimeRegistrySnapshot | None = None,
    ) -> tuple[RuntimeRegistryRecord, ...]:
        if not isinstance(kind, RegistryKind):
            raise TypeError("kind must be a RegistryKind")
        selected = self._snapshot if snapshot is None else snapshot
        if not isinstance(selected, RuntimeRegistrySnapshot):
            raise TypeError("snapshot must be a RuntimeRegistrySnapshot")
        return selected.records(kind)

    def register(
        self,
        record: RuntimeRegistryRecord,
        *,
        expected_state_revision: int | None,
    ) -> RuntimeRegistrySnapshot:
        if not isinstance(record, RuntimeRegistryRecord):
            raise TypeError("record must be a RuntimeRegistryRecord")
        self._validate_expected_revision(expected_state_revision)
        with self._writer_lock:
            current = self._snapshot
            records = {
                item.identity: item for item in current.records(record.kind)
            }
            existing = records.get(record.identity)
            if existing is None:
                if expected_state_revision is not None:
                    self._stale(record.kind, record.identity)
            elif (
                expected_state_revision is None
                or existing.state_revision != expected_state_revision
                or record.state_revision <= existing.state_revision
            ):
                self._stale(record.kind, record.identity)
            records[record.identity] = record
            return self._publish(current, record.kind, tuple(records.values()))

    def remove(
        self,
        kind: RegistryKind,
        identity: str,
        *,
        expected_state_revision: int | None,
    ) -> RuntimeRegistrySnapshot:
        if not isinstance(kind, RegistryKind):
            raise TypeError("kind must be a RegistryKind")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("identity must be a non-empty string")
        self._validate_expected_revision(expected_state_revision)
        with self._writer_lock:
            current = self._snapshot
            records = {item.identity: item for item in current.records(kind)}
            existing = records.get(identity)
            if (
                existing is None
                or expected_state_revision is None
                or existing.state_revision != expected_state_revision
            ):
                self._stale(kind, identity)
            del records[identity]
            return self._publish(current, kind, tuple(records.values()))

    @staticmethod
    def _validate_expected_revision(value: int | None) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_UINT64
        ):
            raise ValueError(
                "expected_state_revision must be an unsigned 64-bit integer or None"
            )

    @staticmethod
    def _stale(kind: RegistryKind, identity: str) -> None:
        raise StaleRegistryRevisionError(
            f"stale {kind.value} registry revision for {identity!r}"
        )

    def _publish(
        self,
        current: RuntimeRegistrySnapshot,
        kind: RegistryKind,
        records: tuple[RuntimeRegistryRecord, ...],
    ) -> RuntimeRegistrySnapshot:
        if current.registry_version >= _MAX_UINT64:
            raise OverflowError("runtime registry version exhausted")
        ordered = tuple(sorted(records, key=lambda item: item.identity))
        values = {
            "runtimes": current.runtimes,
            "host_sessions": current.host_sessions,
            "lifecycles": current.lifecycles,
            "cards": current.cards,
        }
        field_name = {
            RegistryKind.RUNTIME: "runtimes",
            RegistryKind.HOST_SESSION: "host_sessions",
            RegistryKind.LIFECYCLE: "lifecycles",
            RegistryKind.CARD: "cards",
        }[kind]
        values[field_name] = ordered
        published = RuntimeRegistrySnapshot(
            registry_version=current.registry_version + 1,
            captured_at_monotonic=self._capture_time(
                current.captured_at_monotonic
            ),
            **values,
        )
        self._snapshot = published
        return published


__all__ = [
    "RegistryKind",
    "RuntimeRegistry",
    "RuntimeRegistryRecord",
    "RuntimeRegistrySnapshot",
    "StaleRegistryRevisionError",
]
