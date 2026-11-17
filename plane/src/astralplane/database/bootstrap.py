"""One explicit migration-and-reconciliation boot lifecycle: BootInitializer publishes
ready only after the migration commits and every required cross-process
reconciliation hook durably completes.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from astralplane.database.migrations import MigrationReport
from astralplane.database.revision import validate_revision
from astralplane.errors import InitializationError, SchemaRevisionError
from astralplane.reconciliation import (
    RECONCILIATION_ADVISORY_LOCK,
    ReconciliationReport,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_BOOT_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BootMigrationRunner(Protocol):
    def run(self, *, expected_revision: str) -> MigrationReport: ...


class BootReconciliationRunner(Protocol):
    def plan_digest(self, *, schema_revision: str) -> str: ...

    def run(
        self,
        *,
        schema_revision: str,
        context: Mapping[str, object],
    ) -> ReconciliationReport: ...


class BootStatus(StrEnum):
    NEW = "new"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InitializationFailure:
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class InitializationReport:
    identity: str
    expected_revision: str
    migration: MigrationReport
    reconciliation: ReconciliationReport


class _SharedBootState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.status = BootStatus.NEW
        self.expected_revision: str | None = None
        self.plan_digest: str | None = None
        self.report: InitializationReport | None = None
        self.failure: InitializationFailure | None = None


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, _SharedBootState] = {}


def _state_for(identity: str) -> _SharedBootState:
    with _REGISTRY_LOCK:
        state = _REGISTRY.get(identity)
        if state is None:
            state = _SharedBootState()
            _REGISTRY[identity] = state
        return state


class BootInitializer:
    def __init__(
        self,
        identity: str,
        runner: BootMigrationRunner,
        reconciler: BootReconciliationRunner,
        *,
        reconciliation_context: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(identity, str) or _SAFE_BOOT_IDENTITY.fullmatch(identity) is None:
            raise InitializationError("boot identity must be a bounded non-sensitive identifier")
        self.identity = identity
        self._runner = runner
        self._reconciler = reconciler
        self._reconciliation_context = MappingProxyType(dict(reconciliation_context or {}))
        self._state = _state_for(identity)

    @property
    def status(self) -> BootStatus:
        with self._state.condition:
            return self._state.status

    @property
    def failure(self) -> InitializationFailure | None:
        with self._state.condition:
            return self._state.failure

    def _raise_failed(self) -> None:
        failure = self._state.failure
        raise InitializationError(
            "a prior explicit boot initialization failed",
            metadata={
                "identity": self.identity,
                "error_type": "unknown" if failure is None else failure.error_type,
            },
        )

    def _publish_failure(self, *, error_type: str) -> None:
        failure = InitializationFailure(
            error_type=error_type,
            message="initialization attempt failed",
        )
        with self._state.condition:
            self._state.status = BootStatus.FAILED
            self._state.failure = failure
            self._state.report = None
            self._state.condition.notify_all()

    def initialize(self, *, expected_revision: str) -> InitializationReport:
        expected = validate_revision(expected_revision, field="expected revision")
        plan_digest = self._reconciler.plan_digest(schema_revision=expected)
        if not isinstance(plan_digest, str) or _SHA256.fullmatch(plan_digest) is None:
            raise InitializationError("reconciliation runner returned an invalid plan digest")

        with self._state.condition:
            while self._state.status is BootStatus.RUNNING:
                if (
                    self._state.expected_revision != expected
                    or self._state.plan_digest != plan_digest
                ):
                    raise SchemaRevisionError(
                        "concurrent initializer requested a different boot plan"
                    )
                self._state.condition.wait()

            if self._state.status is BootStatus.READY:
                if (
                    self._state.expected_revision != expected
                    or self._state.plan_digest != plan_digest
                ):
                    raise SchemaRevisionError(
                        "initialized data plane does not match the requested boot plan"
                    )
                assert self._state.report is not None
                return self._state.report
            if self._state.status is BootStatus.FAILED:
                self._raise_failed()

            self._state.status = BootStatus.RUNNING
            self._state.expected_revision = expected
            self._state.plan_digest = plan_digest
            self._state.failure = None

        attempt_complete = False
        try:
            migration_report = self._runner.run(expected_revision=expected)
            if migration_report.target_revision != expected:
                raise InitializationError(
                    "migration runner returned the wrong target revision",
                    metadata={
                        "expected": expected,
                        "observed": migration_report.target_revision,
                    },
                )
            reconciliation_report = self._reconciler.run(
                schema_revision=expected,
                context=self._reconciliation_context,
            )
            if (
                reconciliation_report.schema_revision != expected
                or reconciliation_report.plan_digest != plan_digest
                or reconciliation_report.advisory_lock != RECONCILIATION_ADVISORY_LOCK
                or not reconciliation_report.durably_complete
                or not reconciliation_report.hooks
            ):
                raise InitializationError(
                    "reconciliation runner did not prove the required durable plan complete"
                )
            report = InitializationReport(
                identity=self.identity,
                expected_revision=expected,
                migration=migration_report,
                reconciliation=reconciliation_report,
            )
            with self._state.condition:
                self._state.status = BootStatus.READY
                self._state.report = report
                self._state.failure = None
                self._state.condition.notify_all()
                attempt_complete = True
                return report
        except Exception as exc:
            self._publish_failure(error_type=type(exc).__name__)
            raise
        finally:
            if not attempt_complete:
                with self._state.condition:
                    if self._state.status is BootStatus.RUNNING:
                        self._state.status = BootStatus.FAILED
                        self._state.failure = InitializationFailure(
                            error_type="InterruptedInitialization",
                            message="initialization attempt failed",
                        )
                        self._state.report = None
                        self._state.condition.notify_all()

    def reset_failed(self) -> bool:
        with self._state.condition:
            if self._state.status is BootStatus.RUNNING:
                raise InitializationError("cannot reset an initializer while it is running")
            if self._state.status is not BootStatus.FAILED:
                return False
            self._state.status = BootStatus.NEW
            self._state.expected_revision = None
            self._state.plan_digest = None
            self._state.failure = None
            self._state.report = None
            return True


__all__ = (
    "BootInitializer",
    "BootMigrationRunner",
    "BootReconciliationRunner",
    "BootStatus",
    "InitializationFailure",
    "InitializationReport",
)
