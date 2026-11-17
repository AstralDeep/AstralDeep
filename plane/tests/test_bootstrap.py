"""Tests for src/astralplane/database/bootstrap.py: the migration-then-reconciliation
boot lifecycle, concurrent-initializer coalescing, and failure/retry state
transitions.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from astralplane.contracts import ReconciliationHookIdentity
from astralplane.database.bootstrap import BootInitializer, BootStatus
from astralplane.database.migrations import MigrationReport
from astralplane.errors import InitializationError, SchemaRevisionError
from astralplane.reconciliation import (
    RECONCILIATION_ADVISORY_LOCK,
    ReconciliationHookReport,
    ReconciliationReport,
)


def _migration_report(*, already_current: bool = False, target: str = "066.001") -> MigrationReport:
    return MigrationReport(
        source_revision=target if already_current else "065.001",
        target_revision=target,
        applied_steps=() if already_current else ("065-to-066",),
        already_current=already_current,
        migration_digest="a" * 64,
    )


def _reconciliation_report(
    *,
    plan_digest: str,
    schema_revision: str = "066.001",
    durably_complete: bool = True,
    advisory_lock: tuple[int, int] = RECONCILIATION_ADVISORY_LOCK,
    include_hooks: bool = True,
) -> ReconciliationReport:
    hooks = (
        ReconciliationHookReport(
            hook=ReconciliationHookIdentity("required", "1"),
            attempt=1,
            already_complete=False,
            result_digest="b" * 64,
        ),
    )
    return ReconciliationReport(
        schema_revision=schema_revision,
        plan_digest=plan_digest,
        advisory_lock=advisory_lock,
        hooks=hooks if include_hooks else (),
        durably_complete=durably_complete,
    )


class Runner:
    def __init__(
        self,
        outcomes: list[Any] | None = None,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.calls = 0
        self.expected: list[str] = []
        self._outcomes = list(outcomes or [_migration_report()])
        self._entered = entered
        self._release = release
        self._lock = threading.Lock()

    def run(self, *, expected_revision: str) -> MigrationReport:
        with self._lock:
            self.calls += 1
            self.expected.append(expected_revision)
            outcome = self._outcomes.pop(0)
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            assert self._release.wait(timeout=5)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Reconciler:
    def __init__(
        self,
        *,
        plan_digest: str = "c" * 64,
        outcomes: list[Any] | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.digest = plan_digest
        self.calls = 0
        self.plans = 0
        self.contexts: list[Mapping[str, object]] = []
        self._outcomes = list(outcomes or ["auto"])
        self._entered = entered
        self._release = release
        self._lock = threading.Lock()

    def plan_digest(self, *, schema_revision: str) -> str:
        assert schema_revision in {"066.001", "067.001"}
        self.plans += 1
        return self.digest

    def run(
        self,
        *,
        schema_revision: str,
        context: Mapping[str, object],
    ) -> ReconciliationReport:
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
        if outcome == "auto":
            return _reconciliation_report(
                plan_digest=self.digest,
                schema_revision=schema_revision,
            )
        return outcome


def _identity() -> str:
    return f"test-{uuid.uuid4()}"


def _initializer(
    identity: str | None = None,
    *,
    runner: Runner | None = None,
    reconciler: Reconciler | None = None,
) -> tuple[BootInitializer, Runner, Reconciler]:
    selected_runner = runner or Runner()
    selected_reconciler = reconciler or Reconciler()
    return (
        BootInitializer(
            identity or _identity(),
            selected_runner,
            selected_reconciler,
            reconciliation_context={"owner": "system"},
        ),
        selected_runner,
        selected_reconciler,
    )


def test_construction_is_inert_until_explicit_initialize() -> None:
    initializer, runner, reconciler = _initializer()

    assert initializer.status is BootStatus.NEW
    assert runner.calls == 0
    assert reconciler.calls == 0

    result = initializer.initialize(expected_revision="066.001")

    assert result.migration.target_revision == "066.001"
    assert result.reconciliation.durably_complete
    assert initializer.status is BootStatus.READY
    assert runner.calls == 1
    assert reconciler.calls == 1
    assert dict(reconciler.contexts[0]) == {"owner": "system"}


def test_ready_is_not_published_while_required_reconciliation_is_running() -> None:
    entered = threading.Event()
    release = threading.Event()
    reconciler = Reconciler(entered=entered, release=release)
    initializer, runner, _ = _initializer(reconciler=reconciler)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(initializer.initialize, expected_revision="066.001")
        assert entered.wait(timeout=5)
        assert runner.calls == 1
        assert reconciler.calls == 1
        assert initializer.status is BootStatus.RUNNING
        release.set()
        report = future.result(timeout=5)

    assert report.reconciliation.durably_complete
    assert initializer.status is BootStatus.READY


def test_concurrent_initializers_publish_one_shared_ready_report() -> None:
    identity = _identity()
    entered = threading.Event()
    release = threading.Event()
    runner = Runner()
    reconciler = Reconciler(entered=entered, release=release)
    initializers = tuple(BootInitializer(identity, runner, reconciler) for _ in range(12))

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(item.initialize, expected_revision="066.001") for item in initializers
        ]
        assert entered.wait(timeout=5)
        assert runner.calls == 1
        assert reconciler.calls == 1
        release.set()
        reports = [future.result(timeout=5) for future in futures]

    assert len({id(report) for report in reports}) == 1
    assert runner.calls == 1
    assert reconciler.calls == 1
    assert all(item.status is BootStatus.READY for item in initializers)


def test_multiple_instances_ignore_unused_runners_after_ready() -> None:
    identity = _identity()
    first, first_runner, first_reconciler = _initializer(identity)
    unused_runner = Runner()
    unused_reconciler = Reconciler()
    second = BootInitializer(identity, unused_runner, unused_reconciler)

    first_report = first.initialize(expected_revision="066.001")
    second_report = second.initialize(expected_revision="066.001")

    assert second_report is first_report
    assert first_runner.calls == 1
    assert first_reconciler.calls == 1
    assert unused_runner.calls == 0
    assert unused_reconciler.calls == 0
    assert not first.reset_failed()


def test_migration_failure_never_runs_reconciliation_or_publishes_ready() -> None:
    runner = Runner([RuntimeError("database unavailable"), _migration_report()])
    reconciler = Reconciler()
    initializer, _, _ = _initializer(runner=runner, reconciler=reconciler)

    with pytest.raises(RuntimeError, match="database unavailable"):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.FAILED
    assert reconciler.calls == 0
    assert initializer.reset_failed()
    initializer.initialize(expected_revision="066.001")
    assert runner.calls == 2
    assert reconciler.calls == 1


def test_reconciliation_failure_requires_explicit_retry_before_ready() -> None:
    reconciler = Reconciler(
        outcomes=[RuntimeError("hook failed"), "auto"],
    )
    runner = Runner([_migration_report(), _migration_report(already_current=True)])
    initializer, _, _ = _initializer(runner=runner, reconciler=reconciler)

    with pytest.raises(RuntimeError, match="hook failed"):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.FAILED
    assert initializer.failure is not None
    assert initializer.failure.error_type == "RuntimeError"
    with pytest.raises(InitializationError, match="prior explicit"):
        initializer.initialize(expected_revision="066.001")
    assert initializer.reset_failed()

    report = initializer.initialize(expected_revision="066.001")

    assert report.migration.already_current
    assert report.reconciliation.durably_complete
    assert runner.calls == 2
    assert reconciler.calls == 2


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(9)])
def test_termination_propagates_but_boot_becomes_failed_and_retryable(
    termination: BaseException,
) -> None:
    reconciler = Reconciler(outcomes=[termination, "auto"])
    runner = Runner([_migration_report(), _migration_report(already_current=True)])
    initializer, _, _ = _initializer(runner=runner, reconciler=reconciler)

    with pytest.raises(type(termination)):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.FAILED
    assert initializer.failure is not None
    assert initializer.failure.error_type == "InterruptedInitialization"
    assert initializer.reset_failed()
    assert initializer.initialize(expected_revision="066.001").reconciliation.durably_complete


def test_shared_failure_state_does_not_implicitly_retry_from_another_instance() -> None:
    identity = _identity()
    runner = Runner([RuntimeError("failed"), _migration_report()])
    first, _, _ = _initializer(identity, runner=runner)
    second, unused_runner, unused_reconciler = _initializer(identity)

    with pytest.raises(RuntimeError, match="failed"):
        first.initialize(expected_revision="066.001")
    with pytest.raises(InitializationError) as error:
        second.initialize(expected_revision="066.001")

    assert error.value.metadata == (
        ("error_type", "RuntimeError"),
        ("identity", identity),
    )
    assert unused_runner.calls == 0
    assert unused_reconciler.calls == 0


def test_different_plan_is_rejected_while_attempt_is_running() -> None:
    identity = _identity()
    entered = threading.Event()
    release = threading.Event()
    first_reconciler = Reconciler(entered=entered, release=release)
    first = BootInitializer(identity, Runner(), first_reconciler)
    second = BootInitializer(identity, Runner(), Reconciler(plan_digest="d" * 64))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.initialize, expected_revision="066.001")
        assert entered.wait(timeout=5)
        with pytest.raises(SchemaRevisionError, match="different boot plan"):
            second.initialize(expected_revision="066.001")
        with pytest.raises(InitializationError, match="while it is running"):
            second.reset_failed()
        release.set()
        future.result(timeout=5)


def test_ready_state_rejects_later_revision_or_plan_change() -> None:
    identity = _identity()
    initializer, _, _ = _initializer(identity)
    initializer.initialize(expected_revision="066.001")

    different_plan = BootInitializer(
        identity,
        Runner(),
        Reconciler(plan_digest="d" * 64),
    )
    with pytest.raises(SchemaRevisionError, match="does not match"):
        different_plan.initialize(expected_revision="066.001")


@pytest.mark.parametrize(
    "report",
    [
        _reconciliation_report(plan_digest="c" * 64, schema_revision="067.001"),
        _reconciliation_report(plan_digest="d" * 64),
        _reconciliation_report(plan_digest="c" * 64, advisory_lock=(1, 2)),
        _reconciliation_report(plan_digest="c" * 64, durably_complete=False),
        _reconciliation_report(plan_digest="c" * 64, include_hooks=False),
    ],
)
def test_invalid_reconciliation_proof_never_publishes_ready(
    report: ReconciliationReport,
) -> None:
    reconciler = Reconciler(outcomes=[report])
    initializer, _, _ = _initializer(reconciler=reconciler)

    with pytest.raises(InitializationError, match="did not prove"):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.FAILED


def test_invalid_reconciliation_plan_digest_fails_before_running_migration() -> None:
    runner = Runner()
    initializer, _, _ = _initializer(
        runner=runner,
        reconciler=Reconciler(plan_digest="NOT-A-DIGEST"),
    )

    with pytest.raises(InitializationError, match="invalid plan digest"):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.NEW
    assert runner.calls == 0


def test_wrong_migration_target_marks_attempt_failed() -> None:
    initializer, _, reconciler = _initializer(runner=Runner([_migration_report(target="067.001")]))

    with pytest.raises(InitializationError, match="wrong target"):
        initializer.initialize(expected_revision="066.001")

    assert initializer.status is BootStatus.FAILED
    assert reconciler.calls == 0


@pytest.mark.parametrize(
    "identity",
    ["", "   ", "postgresql://example.invalid/astral", "x" * 129, 3, None],
)
def test_invalid_boot_identity_is_rejected(identity: object) -> None:
    with pytest.raises(InitializationError):
        BootInitializer(identity, Runner(), Reconciler())  # type: ignore[arg-type]
