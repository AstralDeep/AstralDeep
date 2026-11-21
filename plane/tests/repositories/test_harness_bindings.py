"""Tests for astralplane.repositories.harness_bindings and the work-admission binding
contract: strict immutable identities, owner/fence predicates, and bounded reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from test_work_admission import _Result, _running_row, _Transaction

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.harness_bindings import (
    HarnessProfileIdentity,
    OperationHarnessBinding,
    operation_harness_binding_from_row,
)
from astralplane.repositories.work_admission import (
    ExecutionFence,
    OperationOwner,
    OwnerScope,
    WorkAdmissionIntegrityError,
    WorkAdmissionRepository,
)

_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
_DIGEST = "a" * 64


def _identity(**changes: object) -> HarnessProfileIdentity:
    values: dict[str, object] = {
        "profile": "standard",
        "version": "1.0.0",
        "artifact_digest": _DIGEST,
    }
    values.update(changes)
    return HarnessProfileIdentity(**values)  # type: ignore[arg-type]


def _binding_row(**changes: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "operation_id": uuid.uuid4(),
        "profile": "standard",
        "profile_version": "1.0.0",
        "artifact_digest": _DIGEST,
        "execution_generation": 2,
        "bound_at": _NOW,
    }
    values.update(changes)
    return values


def test_identity_is_typed_immutable_and_exposes_bounded_aliases() -> None:
    identity = _identity()

    assert identity.profile == identity.profile_id == "standard"
    assert identity.version == identity.profile_version == "1.0.0"
    assert identity.artifact_digest == _DIGEST
    with pytest.raises(AttributeError):
        identity.profile = "changed"


@pytest.mark.parametrize(
    "changes",
    [
        {"profile": "Standard"},
        {"profile": "standard profile"},
        {"profile": ""},
        {"profile": "a" * 65},
        {"profile": 1},
        {"version": "1.0"},
        {"version": "1.0.0-rc1"},
        {"version": "1.0.0 "},
        {"version": 100},
        {"version": "1" * 65 + ".0.0"},
        {"artifact_digest": "A" * 64},
        {"artifact_digest": "a" * 63},
        {"artifact_digest": None},
    ],
)
def test_identity_rejects_unavailable_or_malformed_profiles(changes: dict[str, object]) -> None:
    with pytest.raises(RepositoryValidationError):
        _identity(**changes)


def test_binding_record_requires_a_positive_generation_and_aware_timestamp() -> None:
    operation_id = uuid.uuid4()
    identity = _identity()
    binding = OperationHarnessBinding(operation_id, identity, 3, _NOW)
    assert binding.profile == identity.profile
    assert binding.version == identity.version
    assert binding.artifact_digest == identity.artifact_digest

    for generation, bound_at in ((0, _NOW), (True, _NOW), (1, datetime(2026, 10, 21))):
        with pytest.raises(RepositoryValidationError):
            OperationHarnessBinding(operation_id, identity, generation, bound_at)
    with pytest.raises(RepositoryValidationError):
        OperationHarnessBinding("not-a-uuid", identity, 1, _NOW)  # type: ignore[arg-type]
    with pytest.raises(RepositoryValidationError):
        OperationHarnessBinding(operation_id, "standard", 1, _NOW)  # type: ignore[arg-type]


def test_binding_rows_are_detached_and_corruption_fails_closed() -> None:
    row = _binding_row()
    binding = operation_harness_binding_from_row(row)
    assert binding.operation_id == row["operation_id"]
    assert binding.bound_at == _NOW

    for corruption in (
        {"operation_id": "not-a-uuid"},
        {"profile": "Standard"},
        {"profile_version": "1"},
        {"artifact_digest": "not-a-digest"},
        {"execution_generation": 0},
        {"bound_at": "2026-10-21T12:00:00Z"},
        {"bound_at": datetime(2026, 10, 21, 12, 0)},
    ):
        with pytest.raises(RepositoryDataError):
            operation_harness_binding_from_row({**row, **corruption})
    with pytest.raises(RepositoryDataError):
        operation_harness_binding_from_row({})


def test_bind_uses_the_owner_and_execution_fence_in_the_write_statement() -> None:
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    operation = _running_row(operation_id=operation_id, lease_token=lease_token)
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": _NOW},)),
            _Result(returned_records=(operation,)),
            _Result(),
            _Result(),
            _Result(returned_records=(_binding_row(operation_id=operation_id),)),
        ]
    )
    owner = OperationOwner(OwnerScope.USER, "owner-a", None)
    fence = ExecutionFence(operation_id, 1, lease_token)

    binding = WorkAdmissionRepository().bind_harness_profile(
        transaction, owner, fence, _identity()
    )

    assert binding.operation_id == operation_id
    lock_sql, lock_parameters = transaction.calls[1]
    assert "owner_scope = %s" in lock_sql and "owner_user_id = %s" in lock_sql
    assert "FOR UPDATE" in lock_sql
    assert lock_parameters == (str(operation_id), "user", "owner-a")
    insert_sql, insert_parameters = transaction.calls[4]
    assert "INSERT INTO operation_harness_binding" in insert_sql
    assert "operation.owner_scope = %s" in insert_sql
    assert "operation.owner_user_id = %s" in insert_sql
    assert "operation.state = 'running'" in insert_sql
    assert "operation.execution_generation = %s" in insert_sql
    assert "operation.execution_lease_token = %s" in insert_sql
    assert "ON CONFLICT (operation_id) DO NOTHING" in insert_sql
    assert "->" not in insert_sql and "json" not in insert_sql.lower()
    assert insert_parameters == (
        "standard",
        "1.0.0",
        _DIGEST,
        _NOW,
        str(operation_id),
        "user",
        "owner-a",
        1,
        str(lease_token),
    )


def test_bind_validates_its_typed_contract_before_sql() -> None:
    operation_id = uuid.uuid4()
    owner = OperationOwner(OwnerScope.USER, "owner-a", None)
    fence = ExecutionFence(operation_id, 1, uuid.uuid4())
    repository = WorkAdmissionRepository()
    for invalid_owner, invalid_fence, invalid_identity in (
        (None, fence, _identity()),
        (owner, None, _identity()),
        (owner, fence, None),
    ):
        transaction = _Transaction()
        with pytest.raises(RepositoryValidationError):
            repository.bind_harness_profile(
                transaction,
                invalid_owner,  # type: ignore[arg-type]
                invalid_fence,  # type: ignore[arg-type]
                invalid_identity,  # type: ignore[arg-type]
            )
        assert transaction.calls == []


def test_binding_read_is_owner_scoped_bounded_and_absent_for_legacy_operations() -> None:
    operation_id = uuid.uuid4()
    transaction = _Transaction(
        [_Result(returned_records=(_binding_row(operation_id=operation_id),))]
    )
    owner = OperationOwner(OwnerScope.USER, "owner-a", None)

    binding = WorkAdmissionRepository().get_harness_profile_binding(
        transaction, owner, operation_id
    )

    assert binding is not None and binding.operation_id == operation_id
    statement, parameters = transaction.calls[0]
    assert "JOIN operation_record AS operation" in statement
    assert "operation.owner_scope = %s" in statement
    assert "operation.owner_user_id = %s" in statement
    assert "LIMIT 1" in statement
    assert parameters == (str(operation_id), "user", "owner-a")

    missing = _Transaction([_Result()])
    assert (
        WorkAdmissionRepository().get_harness_profile_binding(
            missing, owner, operation_id
        )
        is None
    )


def test_binding_read_rejects_invalid_identity_before_sql() -> None:
    transaction = _Transaction()
    with pytest.raises(RepositoryValidationError):
        WorkAdmissionRepository().get_harness_profile_binding(
            transaction,
            OperationOwner(OwnerScope.USER, "owner-a", None),
            "not-a-uuid",  # type: ignore[arg-type]
        )
    assert transaction.calls == []
    with pytest.raises(RepositoryValidationError):
        WorkAdmissionRepository().get_harness_profile_binding(
            transaction,
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    assert transaction.calls == []

def test_bind_returns_a_concurrently_committed_same_identity() -> None:
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    operation = _running_row(operation_id=operation_id, lease_token=lease_token)
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": _NOW},)),
            _Result(returned_records=(operation,)),
            _Result(),
            _Result(),
            _Result(),
            _Result(returned_records=(_binding_row(operation_id=operation_id),)),
        ]
    )

    binding = WorkAdmissionRepository().bind_harness_profile(
        transaction,
        OperationOwner(OwnerScope.USER, "owner-a", None),
        ExecutionFence(operation_id, 1, lease_token),
        _identity(),
    )

    assert binding.identity == _identity()
    assert "operation.owner_scope = %s" in transaction.calls[5][0]


def test_bind_refuses_a_concurrently_committed_different_identity() -> None:
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    operation = _running_row(operation_id=operation_id, lease_token=lease_token)
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": _NOW},)),
            _Result(returned_records=(operation,)),
            _Result(),
            _Result(),
            _Result(),
            _Result(returned_records=(_binding_row(operation_id=operation_id),)),
        ]
    )

    with pytest.raises(RepositoryConflictError, match="different harness profile identity"):
        WorkAdmissionRepository().bind_harness_profile(
            transaction,
            OperationOwner(OwnerScope.USER, "owner-a", None),
            ExecutionFence(operation_id, 1, lease_token),
            _identity(profile="typesafe_jev_latest"),
        )


def test_bind_reports_a_lost_insert_as_an_integrity_error() -> None:
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    operation = _running_row(operation_id=operation_id, lease_token=lease_token)
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": _NOW},)),
            _Result(returned_records=(operation,)),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
        ]
    )

    with pytest.raises(WorkAdmissionIntegrityError, match="lost atomicity"):
        WorkAdmissionRepository().bind_harness_profile(
            transaction,
            OperationOwner(OwnerScope.USER, "owner-a", None),
            ExecutionFence(operation_id, 1, lease_token),
            _identity(),
        )
