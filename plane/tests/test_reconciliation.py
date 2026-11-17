"""Tests for src/astralplane/reconciliation.py: golden-plan hook execution,
concurrent-runner serialization, durable failure marking, and plan/digest mismatch
refusal.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import pytest

from astralplane.contracts import (
    ReconciliationHookIdentity,
    ReconciliationMarker,
    ReconciliationMarkerState,
)
from astralplane.errors import ReconciliationError
from astralplane.reconciliation import (
    RECONCILIATION_ADVISORY_LOCK,
    ReconciliationRunner,
    _validate_marker,
)


class FakeSession:
    def __init__(
        self,
        coordinator: FakeCoordinator,
        *,
        schema_revision: str,
        plan_digest: str,
    ) -> None:
        self.coordinator = coordinator
        self.schema_revision = schema_revision
        self.plan_digest = plan_digest

    def _key(self, hook: ReconciliationHookIdentity) -> tuple[str, str, str, str]:
        return self.schema_revision, self.plan_digest, hook.name, hook.version

    def get_marker(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker | None:
        marker = self.coordinator.markers.get(self._key(hook))
        if marker is not None and self.coordinator.corrupt_reads:
            return ReconciliationMarker(
                schema_revision="999.999",
                plan_digest=marker.plan_digest,
                hook=marker.hook,
                state=marker.state,
                attempt=marker.attempt,
                result_digest=marker.result_digest,
                error_type=marker.error_type,
            )
        return marker

    def mark_started(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker:
        existing = self.coordinator.markers.get(self._key(hook))
        marker = ReconciliationMarker(
            schema_revision=self.schema_revision,
            plan_digest=self.plan_digest,
            hook=hook,
            state=ReconciliationMarkerState.STARTED,
            attempt=(1 if existing is None else existing.attempt + 1)
            + self.coordinator.start_attempt_delta,
        )
        self.coordinator.markers[self._key(hook)] = marker
        self.coordinator.transitions.append((hook.name, marker.state, marker.attempt))
        return marker

    def mark_completed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        result_digest: str,
    ) -> ReconciliationMarker:
        existing = self.coordinator.markers[self._key(hook)]
        marker = ReconciliationMarker(
            schema_revision=self.schema_revision,
            plan_digest=self.plan_digest,
            hook=hook,
            state=ReconciliationMarkerState.COMPLETED,
            attempt=existing.attempt + self.coordinator.completion_attempt_delta,
            result_digest=self.coordinator.completion_result_digest or result_digest,
        )
        if self.coordinator.erase_completions:
            self.coordinator.markers.pop(self._key(hook), None)
        elif not self.coordinator.drop_completions:
            self.coordinator.markers[self._key(hook)] = marker
        self.coordinator.transitions.append((hook.name, marker.state, marker.attempt))
        return marker

    def mark_failed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        error_type: str,
    ) -> ReconciliationMarker:
        if self.coordinator.fail_failure_markers:
            raise RuntimeError("marker store unavailable")
        existing = self.coordinator.markers[self._key(hook)]
        marker = ReconciliationMarker(
            schema_revision=self.schema_revision,
            plan_digest=self.plan_digest,
            hook=hook,
            state=ReconciliationMarkerState.FAILED,
            attempt=existing.attempt + self.coordinator.failure_attempt_delta,
            error_type=error_type,
        )
        self.coordinator.markers[self._key(hook)] = marker
        self.coordinator.transitions.append((hook.name, marker.state, marker.attempt))
        return marker


class FakeCoordinator:
    def __init__(self) -> None:
        self.markers: dict[tuple[str, str, str, str], ReconciliationMarker] = {}
        self.transitions: list[tuple[str, ReconciliationMarkerState, int]] = []
        self.coordinates: list[tuple[tuple[int, int], str, str]] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.RLock()
        self.corrupt_reads = False
        self.drop_completions = False
        self.fail_failure_markers = False
        self.erase_completions = False
        self.completion_attempt_delta = 0
        self.failure_attempt_delta = 0
        self.start_attempt_delta = 0
        self.completion_result_digest: str | None = None

    @contextmanager
    def coordinate(
        self,
        *,
        advisory_lock: tuple[int, int],
        schema_revision: str,
        plan_digest: str,
    ) -> Iterator[FakeSession]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.coordinates.append((advisory_lock, schema_revision, plan_digest))
            try:
                yield FakeSession(
                    self,
                    schema_revision=schema_revision,
                    plan_digest=plan_digest,
                )
            finally:
                self._active -= 1


class Hook:
    def __init__(
        self,
        name: str,
        version: str,
        outcomes: list[object] | None = None,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.calls = 0
        self.contexts: list[Mapping[str, object]] = []
        self._outcomes = list(outcomes or [{"changed": 1}])
        self._entered = entered
        self._release = release
        self._lock = threading.Lock()

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object] | None:
        with self._lock:
            self.calls += 1
            self.contexts.append(context)
            outcome = self._outcomes.pop(0)
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            assert self._release.wait(timeout=5)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def test_golden_plan_runs_required_hooks_and_persists_exact_markers() -> None:
    coordinator = FakeCoordinator()
    first = Hook("deep-seeds", "1.0.0", [{"changed": 2}])
    second = Hook("projection-seeds", "3", [None])
    runner = ReconciliationRunner(coordinator, (first, second))

    report = runner.run(schema_revision="066.001", context={"owner": "system"})

    assert report.durably_complete
    assert report.advisory_lock == (1095980114, 60002)
    assert report.advisory_lock == RECONCILIATION_ADVISORY_LOCK
    assert report.plan_digest == runner.plan_digest(schema_revision="066.001")
    assert tuple(item.hook.name for item in report.hooks) == (
        "deep-seeds",
        "projection-seeds",
    )
    assert all(not item.already_complete for item in report.hooks)
    assert coordinator.max_active == 1
    assert coordinator.coordinates == [
        (RECONCILIATION_ADVISORY_LOCK, "066.001", report.plan_digest)
    ]
    assert [transition[1] for transition in coordinator.transitions] == [
        ReconciliationMarkerState.STARTED,
        ReconciliationMarkerState.COMPLETED,
        ReconciliationMarkerState.STARTED,
        ReconciliationMarkerState.COMPLETED,
    ]
    with pytest.raises(TypeError):
        first.contexts[0]["owner"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        report.durably_complete = False  # type: ignore[misc]


def test_plan_digest_binds_schema_order_name_and_version() -> None:
    coordinator = FakeCoordinator()
    base = ReconciliationRunner(
        coordinator,
        (Hook("first", "1"), Hook("second", "1")),
    )
    reordered = ReconciliationRunner(
        coordinator,
        (Hook("second", "1"), Hook("first", "1")),
    )
    bumped = ReconciliationRunner(
        coordinator,
        (Hook("first", "2"), Hook("second", "1")),
    )

    digest = base.plan_digest(schema_revision="066.001")
    assert digest != base.plan_digest(schema_revision="067.001")
    assert digest != reordered.plan_digest(schema_revision="066.001")
    assert digest != bumped.plan_digest(schema_revision="066.001")
    assert base.plan(schema_revision="066.001").hooks == (
        ReconciliationHookIdentity("first", "1"),
        ReconciliationHookIdentity("second", "1"),
    )


def test_already_complete_plan_skips_hook_execution() -> None:
    coordinator = FakeCoordinator()
    hook = Hook("required", "1")
    runner = ReconciliationRunner(coordinator, (hook,))
    first = runner.run(schema_revision="066.001", context={})

    second = runner.run(schema_revision="066.001", context={})

    assert hook.calls == 1
    assert first.hooks[0].attempt == 1
    assert not first.hooks[0].already_complete
    assert second.hooks[0].attempt == 1
    assert second.hooks[0].already_complete


def test_concurrent_runners_serialize_and_execute_hook_once() -> None:
    coordinator = FakeCoordinator()
    entered = threading.Event()
    release = threading.Event()
    hook = Hook("required", "1", entered=entered, release=release)
    runner = ReconciliationRunner(coordinator, (hook,))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(runner.run, schema_revision="066.001", context={}) for _ in range(8)
        ]
        assert entered.wait(timeout=5)
        release.set()
        reports = [future.result(timeout=5) for future in futures]

    assert hook.calls == 1
    assert coordinator.max_active == 1
    assert sum(not report.hooks[0].already_complete for report in reports) == 1


def test_failed_hook_is_durably_marked_and_explicit_retry_completes() -> None:
    coordinator = FakeCoordinator()
    hook = Hook("required", "1", [RuntimeError("product failed"), {"changed": 1}])
    runner = ReconciliationRunner(coordinator, (hook,))

    with pytest.raises(ReconciliationError, match="required reconciliation") as error:
        runner.run(schema_revision="066.001", context={})

    assert error.value.metadata == (
        ("attempt", "1"),
        ("hook", "required"),
        ("version", "1"),
    )
    marker = next(iter(coordinator.markers.values()))
    assert marker.state is ReconciliationMarkerState.FAILED
    assert marker.error_type == "RuntimeError"

    report = runner.run(schema_revision="066.001", context={})

    assert hook.calls == 2
    assert report.hooks[0].attempt == 2
    assert not report.hooks[0].already_complete
    assert next(iter(coordinator.markers.values())).state is ReconciliationMarkerState.COMPLETED


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(17)])
def test_termination_is_not_wrapped_and_retry_resumes_idempotently(
    termination: BaseException,
) -> None:
    coordinator = FakeCoordinator()
    hook = Hook("required", "1", [termination, {"changed": 1}])
    runner = ReconciliationRunner(coordinator, (hook,))

    with pytest.raises(type(termination)):
        runner.run(schema_revision="066.001", context={})

    marker = next(iter(coordinator.markers.values()))
    assert marker.state is ReconciliationMarkerState.FAILED
    assert marker.error_type == "Interrupted"

    report = runner.run(schema_revision="066.001", context={})
    assert report.hooks[0].attempt == 2
    assert report.durably_complete


def test_failure_marker_write_failure_is_visible_and_never_reports_ready() -> None:
    coordinator = FakeCoordinator()
    coordinator.fail_failure_markers = True
    runner = ReconciliationRunner(
        coordinator,
        (Hook("required", "1", [RuntimeError("hook failure")]),),
    )

    with pytest.raises(ReconciliationError, match="could not be persisted"):
        runner.run(schema_revision="066.001", context={})

    assert next(iter(coordinator.markers.values())).state is ReconciliationMarkerState.STARTED


def test_missing_final_durable_marker_blocks_completion_report() -> None:
    coordinator = FakeCoordinator()
    coordinator.drop_completions = True
    runner = ReconciliationRunner(coordinator, (Hook("required", "1"),))

    with pytest.raises(ReconciliationError, match="completion is not durable"):
        runner.run(schema_revision="066.001", context={})

    assert next(iter(coordinator.markers.values())).state is ReconciliationMarkerState.FAILED


def test_missing_completion_and_failure_storage_is_attributed() -> None:
    coordinator = FakeCoordinator()
    coordinator.erase_completions = True
    runner = ReconciliationRunner(coordinator, (Hook("required", "1"),))

    with pytest.raises(ReconciliationError, match="completion and failure markers"):
        runner.run(schema_revision="066.001", context={})

    assert coordinator.markers == {}


@pytest.mark.parametrize("marker_kind", ["completion", "failure"])
def test_marker_store_cannot_change_attempt_identity(marker_kind: str) -> None:
    coordinator = FakeCoordinator()
    if marker_kind == "completion":
        coordinator.completion_attempt_delta = 1
        outcomes: list[object] = [{"changed": 1}]
    else:
        coordinator.failure_attempt_delta = 1
        outcomes = [RuntimeError("hook failed")]
    runner = ReconciliationRunner(coordinator, (Hook("required", "1", outcomes),))

    with pytest.raises(ReconciliationError, match="failure marker could not be persisted"):
        runner.run(schema_revision="066.001", context={})


@pytest.mark.parametrize("fault", ["start-attempt", "result-digest"])
def test_marker_store_cannot_rebind_attempt_or_result(fault: str) -> None:
    coordinator = FakeCoordinator()
    if fault == "start-attempt":
        coordinator.start_attempt_delta = 1
    else:
        coordinator.completion_result_digest = "d" * 64
    runner = ReconciliationRunner(coordinator, (Hook("required", "1"),))

    with pytest.raises(ReconciliationError, match="required reconciliation"):
        runner.run(schema_revision="066.001", context={})

    assert next(iter(coordinator.markers.values())).state is ReconciliationMarkerState.FAILED


def test_corrupt_store_marker_is_rejected_before_hook_execution() -> None:
    coordinator = FakeCoordinator()
    hook = Hook("required", "1")
    runner = ReconciliationRunner(coordinator, (hook,))
    runner.run(schema_revision="066.001", context={})
    coordinator.corrupt_reads = True

    with pytest.raises(ReconciliationError, match="does not match"):
        runner.run(schema_revision="066.001", context={})

    assert hook.calls == 1


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        (object(), "invalid marker"),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.STARTED,
                0,
            ),
            "attempt must be positive",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.COMPLETED,
                1,
                result_digest="not-a-digest",
            ),
            "lacks a canonical result digest",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.COMPLETED,
                1,
                result_digest="a" * 64,
                error_type="Wrong",
            ),
            "must not carry a failure",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.FAILED,
                1,
            ),
            "failed reconciliation marker is malformed",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.FAILED,
                1,
                error_type="bad/error",
            ),
            "failed reconciliation marker is malformed",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                ReconciliationMarkerState.STARTED,
                1,
                error_type="Wrong",
            ),
            "started reconciliation marker is malformed",
        ),
        (
            ReconciliationMarker(
                "066.001",
                "c" * 64,
                ReconciliationHookIdentity("required", "1"),
                "unknown",  # type: ignore[arg-type]
                1,
            ),
            "marker state is invalid",
        ),
    ],
)
def test_invalid_marker_shapes_fail_closed(marker: object, message: str) -> None:
    runner = ReconciliationRunner(FakeCoordinator(), (Hook("required", "1"),))
    plan = runner.plan(schema_revision="066.001")
    if isinstance(marker, ReconciliationMarker):
        marker = replace(marker, plan_digest=plan.plan_digest, hook=plan.hooks[0])

    with pytest.raises(ReconciliationError, match=message):
        _validate_marker(
            marker,  # type: ignore[arg-type]
            plan=plan,
            hook=plan.hooks[0],
        )


@pytest.mark.parametrize(
    "hooks",
    [
        (),
        (Hook("", "1"),),
        (Hook("valid", ""),),
        (Hook("bad/name", "1"),),
        (Hook("same", "1"), Hook("same", "2")),
    ],
)
def test_invalid_required_hook_sets_fail_closed(hooks: tuple[Hook, ...]) -> None:
    with pytest.raises(ReconciliationError):
        ReconciliationRunner(FakeCoordinator(), hooks)


@pytest.mark.parametrize(
    "result",
    [
        ["not", "a", "mapping"],
        {"": "empty key"},
        {1: "non-string key"},
        {"not_json": object()},
        {"nan": float("nan")},
    ],
)
def test_invalid_hook_result_is_durably_failed(result: object) -> None:
    coordinator = FakeCoordinator()
    runner = ReconciliationRunner(coordinator, (Hook("required", "1", [result]),))

    with pytest.raises(ReconciliationError, match="required reconciliation"):
        runner.run(schema_revision="066.001", context={})

    marker = next(iter(coordinator.markers.values()))
    assert marker.state is ReconciliationMarkerState.FAILED
    assert marker.error_type == "ReconciliationError"
