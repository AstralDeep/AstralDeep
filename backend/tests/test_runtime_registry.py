"""Tests for the process-local runtime registry (orchestrator/runtime_registry.py):
copy-on-write snapshot publication, compare-and-set revision fencing, and coherent
concurrent register/remove/list.
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.runtime_registry import (
    RegistryKind,
    RuntimeRegistry,
    RuntimeRegistryRecord,
    RuntimeRegistrySnapshot,
    StaleRegistryRevisionError,
)


_SNAPSHOT_FIELDS = {
    RegistryKind.RUNTIME: "runtimes",
    RegistryKind.HOST_SESSION: "host_sessions",
    RegistryKind.LIFECYCLE: "lifecycles",
    RegistryKind.CARD: "cards",
}


def _record(
    kind: RegistryKind,
    identity: str,
    state_revision: int,
    value: object | None = None,
) -> RuntimeRegistryRecord:
    return RuntimeRegistryRecord(
        kind=kind,
        identity=identity,
        state_revision=state_revision,
        value=identity if value is None else value,
    )


def _assert_coherent_snapshot(snapshot: RuntimeRegistrySnapshot) -> None:
    seen: set[tuple[RegistryKind, str]] = set()
    for kind, field_name in _SNAPSHOT_FIELDS.items():
        records = getattr(snapshot, field_name)
        assert isinstance(records, tuple)
        assert [record.identity for record in records] == sorted(
            record.identity for record in records
        )
        for record in records:
            assert record.kind is kind
            assert record.state_revision >= 0
            key = (record.kind, record.identity)
            assert key not in seen
            seen.add(key)


def test_initial_snapshot_is_frozen_empty_and_reused_by_readers() -> None:
    registry = RuntimeRegistry()

    snapshot = registry.snapshot()

    assert registry.snapshot() is snapshot
    assert snapshot.registry_version == 0
    assert snapshot.captured_at_monotonic >= 0
    assert snapshot.runtimes == ()
    assert snapshot.host_sessions == ()
    assert snapshot.lifecycles == ()
    assert snapshot.cards == ()
    _assert_coherent_snapshot(snapshot)

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.registry_version = 1  # type: ignore[misc]


def test_register_uses_copy_on_write_and_old_snapshots_remain_stable() -> None:
    registry = RuntimeRegistry()
    empty = registry.snapshot()
    original = _record(RegistryKind.RUNTIME, "runtime-1", 0, "socket-1")

    registered = registry.register(original, expected_state_revision=None)
    updated = registry.register(
        _record(RegistryKind.RUNTIME, "runtime-1", 1, "socket-2"),
        expected_state_revision=0,
    )
    removed = registry.remove(
        RegistryKind.RUNTIME,
        "runtime-1",
        expected_state_revision=1,
    )

    assert registry.snapshot() is removed
    assert [
        empty.registry_version,
        registered.registry_version,
        updated.registry_version,
        removed.registry_version,
    ] == [0, 1, 2, 3]
    assert empty.runtimes == ()
    assert registered.runtimes == (original,)
    assert registered.runtimes[0].value == "socket-1"
    assert updated.runtimes[0].state_revision == 1
    assert updated.runtimes[0].value == "socket-2"
    assert removed.runtimes == ()

    with pytest.raises(dataclasses.FrozenInstanceError):
        original.state_revision = 99  # type: ignore[misc]


def test_one_snapshot_partitions_all_kinds_in_deterministic_order() -> None:
    registry = RuntimeRegistry()
    records = (
        _record(RegistryKind.CARD, "card-z", 4),
        _record(RegistryKind.RUNTIME, "runtime-a", 2),
        _record(RegistryKind.HOST_SESSION, "host-a", 7),
        _record(RegistryKind.CARD, "card-a", 1),
        _record(RegistryKind.LIFECYCLE, "agent-a", 9),
    )
    snapshots = [registry.snapshot()]

    for record in records:
        snapshots.append(
            registry.register(record, expected_state_revision=None)
        )

    current = registry.snapshot()
    assert current.registry_version == len(records)
    assert tuple(record.identity for record in current.runtimes) == ("runtime-a",)
    assert tuple(record.identity for record in current.host_sessions) == ("host-a",)
    assert tuple(record.identity for record in current.lifecycles) == ("agent-a",)
    assert tuple(record.identity for record in current.cards) == ("card-a", "card-z")
    for kind, field_name in _SNAPSHOT_FIELDS.items():
        assert registry.list_records(kind, snapshot=current) == getattr(
            current, field_name
        )
    assert [item.registry_version for item in snapshots] == list(
        range(len(records) + 1)
    )
    assert [item.captured_at_monotonic for item in snapshots] == sorted(
        item.captured_at_monotonic for item in snapshots
    )
    _assert_coherent_snapshot(current)


def test_stale_register_and_remove_are_refused_without_publication() -> None:
    registry = RuntimeRegistry()
    registry.register(
        _record(RegistryKind.LIFECYCLE, "agent-1", 7, "online"),
        expected_state_revision=None,
    )
    current = registry.snapshot()

    stale_attempts = (
        lambda: registry.register(
            _record(RegistryKind.LIFECYCLE, "agent-1", 8, "offline"),
            expected_state_revision=6,
        ),
        lambda: registry.register(
            _record(RegistryKind.LIFECYCLE, "agent-1", 7, "offline"),
            expected_state_revision=7,
        ),
        lambda: registry.register(
            _record(RegistryKind.LIFECYCLE, "agent-1", 8, "offline"),
            expected_state_revision=None,
        ),
        lambda: registry.remove(
            RegistryKind.LIFECYCLE,
            "agent-1",
            expected_state_revision=6,
        ),
    )

    for attempt in stale_attempts:
        with pytest.raises(StaleRegistryRevisionError):
            attempt()
        assert registry.snapshot() is current

    published = registry.register(
        _record(RegistryKind.LIFECYCLE, "agent-1", 8, "offline"),
        expected_state_revision=7,
    )
    assert published.registry_version == current.registry_version + 1
    assert published.lifecycles[0].state_revision == 8
    assert current.lifecycles[0].state_revision == 7
    assert current.lifecycles[0].value == "online"


def test_two_same_revision_writers_have_exactly_one_winner() -> None:
    registry = RuntimeRegistry()
    registry.register(
        _record(RegistryKind.CARD, "agent-1", 0, "base"),
        expected_state_revision=None,
    )
    before = registry.snapshot()
    start = threading.Barrier(3)

    def replace(value: str) -> str:
        start.wait()
        try:
            registry.register(
                _record(RegistryKind.CARD, "agent-1", 1, value),
                expected_state_revision=0,
            )
        except StaleRegistryRevisionError:
            return "stale"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(replace, value) for value in ("left", "right")]
        start.wait()
        outcomes = [future.result() for future in futures]

    assert sorted(outcomes) == ["published", "stale"]
    assert registry.snapshot().registry_version == 2
    assert registry.snapshot().cards[0].state_revision == 1
    assert registry.snapshot().cards[0].value in {"left", "right"}
    assert before.registry_version == 1
    assert before.cards[0].value == "base"


def test_concurrent_register_remove_and_list_stays_coherent() -> None:
    registry = RuntimeRegistry()
    writer_count = 4
    reader_count = 2
    keys_per_writer = 96
    start = threading.Barrier(writer_count + reader_count)
    finished = threading.Event()
    finish_lock = threading.Lock()
    writers_remaining = writer_count

    def writer(writer_number: int) -> None:
        nonlocal writers_remaining
        start.wait()
        try:
            for index in range(keys_per_writer):
                identity = f"runtime-{writer_number:02d}-{index:04d}"
                registry.register(
                    _record(RegistryKind.RUNTIME, identity, 0),
                    expected_state_revision=None,
                )
                registry.register(
                    _record(RegistryKind.RUNTIME, identity, 1),
                    expected_state_revision=0,
                )
                if index % 2 == 0:
                    registry.remove(
                        RegistryKind.RUNTIME,
                        identity,
                        expected_state_revision=1,
                    )
                else:
                    registry.register(
                        _record(RegistryKind.RUNTIME, identity, 2),
                        expected_state_revision=1,
                    )
        finally:
            with finish_lock:
                writers_remaining -= 1
                if writers_remaining == 0:
                    finished.set()

    def reader() -> int:
        start.wait()
        prior_version = -1
        observations = 0
        while not finished.is_set() or observations < keys_per_writer:
            snapshot = registry.snapshot()
            assert snapshot.registry_version >= prior_version
            prior_version = snapshot.registry_version
            _assert_coherent_snapshot(snapshot)
            listed = registry.list_records(RegistryKind.RUNTIME)
            assert tuple(record.identity for record in listed) == tuple(
                sorted(record.identity for record in listed)
            )
            observations += 1
        return observations

    with ThreadPoolExecutor(max_workers=writer_count + reader_count) as executor:
        writer_futures = [executor.submit(writer, number) for number in range(writer_count)]
        reader_futures = [executor.submit(reader) for _ in range(reader_count)]
        for future in writer_futures:
            future.result()
        observation_counts = [future.result() for future in reader_futures]

    final = registry.snapshot()
    expected_mutations = writer_count * keys_per_writer * 3
    expected_survivors = writer_count * (keys_per_writer // 2)
    assert final.registry_version == expected_mutations
    assert len(final.runtimes) == expected_survivors
    assert all(record.state_revision == 2 for record in final.runtimes)
    assert all(count >= keys_per_writer for count in observation_counts)
    _assert_coherent_snapshot(final)
