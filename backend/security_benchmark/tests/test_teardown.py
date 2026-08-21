"""Plane-owned benchmark cleanup boundary (spec 047 FR-008)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
import security_benchmark.isolation as isolation_module
from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

from security_benchmark.isolation import assert_namespaced, teardown


class _TransactionScope:
    def __init__(self) -> None:
        self.transaction = object()
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self.transaction

    def __exit__(self, exc_type, _exc, _tb):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False


class _Runtime:
    def __init__(self) -> None:
        self.scope = _TransactionScope()
        self.transaction_calls = 0

    def transaction(self):
        self.transaction_calls += 1
        return self.scope


class _CleanupRepository:
    def __init__(self, *, deleted: object = 7, error: Exception | None = None) -> None:
        self.deleted = deleted
        self.error = error
        self.calls = []

    def purge_run(self, transaction, *, profile, run_id):
        self.calls.append((transaction, profile, run_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(total_deleted=self.deleted)


def _catalog(repository: object) -> SimpleNamespace:
    return SimpleNamespace(harness_cleanup=repository)


def test_teardown_uses_one_application_plane_transaction() -> None:
    runtime = _Runtime()
    cleanup = _CleanupRepository(deleted=23)

    deleted = teardown(
        plane_runtime=runtime,
        plane_repositories=_catalog(cleanup),
        run_id="__bench__run-1",
    )

    assert deleted == 23
    assert runtime.transaction_calls == 1
    assert runtime.scope.committed and not runtime.scope.rolled_back
    assert cleanup.calls == [
        (
            runtime.scope.transaction,
            HarnessCleanupProfile.SECURITY_BENCHMARK,
            "__bench__run-1",
        )
    ]


def test_teardown_failure_rolls_back_and_propagates() -> None:
    runtime = _Runtime()
    cleanup = _CleanupRepository(error=RuntimeError("schema drift"))

    with pytest.raises(RuntimeError, match="schema drift"):
        teardown(
            plane_runtime=runtime,
            plane_repositories=_catalog(cleanup),
            run_id="run-2",
        )

    assert runtime.scope.rolled_back and not runtime.scope.committed


@pytest.mark.parametrize(
    ("runtime", "repositories", "message"),
    [
        (object(), SimpleNamespace(harness_cleanup=object()), "application Plane runtime"),
        (_Runtime(), SimpleNamespace(), "harness_cleanup repository"),
    ],
)
def test_teardown_fails_closed_without_application_plane(
    runtime: object,
    repositories: object,
    message: str,
) -> None:
    with pytest.raises(TypeError, match=message):
        teardown(
            plane_runtime=runtime,
            plane_repositories=repositories,
            run_id="run-3",
        )


def test_invalid_plane_report_rolls_back() -> None:
    runtime = _Runtime()

    with pytest.raises(RuntimeError, match="invalid deletion count"):
        teardown(
            plane_runtime=runtime,
            plane_repositories=_catalog(_CleanupRepository(deleted=-1)),
            run_id="run-4",
        )

    assert runtime.scope.rolled_back and not runtime.scope.committed


def test_assert_namespaced_guards_real_principals() -> None:
    assert_namespaced("__bench__run__agentdojo__primary")
    with pytest.raises(ValueError):
        assert_namespaced("real-user-42")


def test_benchmark_isolation_source_has_no_sql_or_driver_boundary() -> None:
    source = inspect.getsource(isolation_module)
    for forbidden in ("shared.database", "psycopg", "DELETE FROM", ".cursor(", ".execute("):
        assert forbidden not in source
