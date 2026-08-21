"""Isolation + Plane-owned cleanup (T030 / SC-013, FR-031)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
import verification.isolation as isolation_module
from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

from verification.isolation import (
    NAMESPACE_PREFIX,
    is_harness_principal,
    make_principal,
    principal_id,
    teardown,
)


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
    def __init__(self, *, deleted: object = 9, error: Exception | None = None) -> None:
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


def test_principal_namespacing() -> None:
    pid = principal_id("__verif__abc123", "everyday", "primary")
    assert pid.startswith(NAMESPACE_PREFIX)
    assert "everyday" in pid and "primary" in pid
    assert is_harness_principal(pid)
    assert not is_harness_principal("real-user-42")


def test_principal_roles_and_claims() -> None:
    principal = make_principal("__verif__abc", "gov", "admin", roles=["admin", "user"])
    assert principal.is_admin
    claims = principal.claims()
    assert claims["sub"] == principal.user_id
    assert "admin" in claims["realm_access"]["roles"]


def test_teardown_uses_one_application_plane_transaction() -> None:
    runtime = _Runtime()
    cleanup = _CleanupRepository(deleted=31)

    deleted = teardown(
        plane_runtime=runtime,
        plane_repositories=_catalog(cleanup),
        run_id="__verif__run9",
    )

    assert deleted == 31
    assert runtime.transaction_calls == 1
    assert runtime.scope.committed and not runtime.scope.rolled_back
    assert cleanup.calls == [
        (
            runtime.scope.transaction,
            HarnessCleanupProfile.VERIFICATION,
            "__verif__run9",
        )
    ]


def test_teardown_failure_rolls_back_and_propagates() -> None:
    runtime = _Runtime()

    with pytest.raises(RuntimeError, match="schema drift"):
        teardown(
            plane_runtime=runtime,
            plane_repositories=_catalog(
                _CleanupRepository(error=RuntimeError("schema drift"))
            ),
            run_id="__verif__x",
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
            run_id="run10",
        )


def test_invalid_plane_report_rolls_back() -> None:
    runtime = _Runtime()

    with pytest.raises(RuntimeError, match="invalid deletion count"):
        teardown(
            plane_runtime=runtime,
            plane_repositories=_catalog(_CleanupRepository(deleted=True)),
            run_id="run11",
        )

    assert runtime.scope.rolled_back and not runtime.scope.committed


def test_verification_isolation_source_has_no_sql_or_driver_boundary() -> None:
    source = inspect.getsource(isolation_module)
    for forbidden in ("shared.database", "psycopg", "DELETE FROM", ".cursor(", ".execute("):
        assert forbidden not in source
