"""Real-PostgreSQL tests for operation harness-profile bindings: legacy absence, owner
isolation, execution-fence replay, immutable storage, restart persistence, and purge cascade.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.harness_bindings import (
    HarnessProfileIdentity,
    OperationHarnessBinding,
)
from astralplane.repositories.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleWorkExecutionFenceError,
    WorkAdmissionNotFoundError,
    WorkAdmissionRepository,
)

_DIGEST = "b" * 64
_OTHER_DIGEST = "c" * 64


def _owner(owner_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(OwnerScope.USER, owner_id, None)


def _configs() -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 4, 0, 0, "harness-binding-tests"),
        AdmissionClassConfig(
            AdmissionClass.BACKGROUND,
            AdmissionClass.GLOBAL,
            4,
            0,
            0,
            "harness-binding-tests",
        ),
    )


def _admitted(
    tx,
    *,
    owner_id: str = "owner-a",
) -> tuple[WorkAdmissionRepository, OperationOwner, ExecutionFence, uuid.UUID]:
    work = WorkAdmissionRepository()
    work.configure(tx, _configs())
    work.bind_configs(_configs())
    owner = _owner(owner_id)
    accepted = work.submit(
        tx,
        OperationRequest(
            operation_kind="harness_turn",
            admission_class=AdmissionClass.BACKGROUND,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        ),
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=5),
    )
    claim = work.claim_operation(
        tx,
        AdmissionClass.BACKGROUND,
        accepted.operation_id,
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=5),
    )
    assert claim is not None
    return work, owner, claim.fence, accepted.operation_id


def _identity(**changes: str) -> HarnessProfileIdentity:
    values = {
        "profile": "standard",
        "version": "1.0.0",
        "artifact_digest": _DIGEST,
    }
    values.update(changes)
    return HarnessProfileIdentity(**values)


@pytest.fixture
def tx(migrated_clone):
    with migrated_clone.database.transaction() as transaction:
        yield transaction


def test_legacy_operation_has_no_binding_and_bind_is_owner_scoped_and_replay_safe(tx):
    work, owner, fence, operation_id = _admitted(tx)
    assert work.get_harness_profile_binding(tx, owner, operation_id) is None

    identity = _identity()
    binding = work.bind_harness_profile(tx, owner, fence, identity)

    assert isinstance(binding, OperationHarnessBinding)
    assert binding.operation_id == operation_id
    assert binding.identity == identity
    assert binding.execution_generation == fence.execution_generation
    assert binding.bound_at.tzinfo is not None
    assert work.get_harness_profile_binding(tx, owner, operation_id) == binding
    assert work.get_harness_profile_binding(tx, _owner("owner-b"), operation_id) is None
    assert work.bind_harness_profile(tx, owner, fence, identity) == binding
    with pytest.raises(RepositoryConflictError, match="different harness profile identity"):
        work.bind_harness_profile(
            tx, owner, fence, _identity(profile="typesafe_jev_latest")
        )
    row = tx.fetch_one("SELECT * FROM operation_harness_binding")
    assert row["profile"] == "standard"
    assert row["profile_version"] == "1.0.0"
    assert row["artifact_digest"] == _DIGEST


def test_binding_rejects_foreign_or_stale_fences_and_never_overwrites(tx):
    work, owner, fence, operation_id = _admitted(tx)
    binding = work.bind_harness_profile(tx, owner, fence, _identity())

    with pytest.raises(WorkAdmissionNotFoundError):
        work.bind_harness_profile(tx, _owner("owner-b"), fence, _identity())
    stale_owner = _owner("owner-a")
    stale_fence = ExecutionFence(operation_id, fence.execution_generation, uuid.uuid4())
    with pytest.raises(StaleWorkExecutionFenceError):
        work.bind_harness_profile(tx, stale_owner, stale_fence, _identity())

    renewed = work.reselect_execution(
        tx,
        fence,
        now=None,
        slot_lease=timedelta(minutes=5),
    )
    with pytest.raises(StaleWorkExecutionFenceError):
        work.bind_harness_profile(tx, owner, fence, _identity())
    assert work.bind_harness_profile(tx, owner, renewed, _identity()) == binding
    with pytest.raises(RepositoryConflictError):
        work.bind_harness_profile(
            tx, owner, renewed, _identity(artifact_digest=_OTHER_DIGEST)
        )
    assert work.get_harness_profile_binding(tx, owner, operation_id) == binding


def test_binding_rows_are_immutable_and_refuse_direct_mutation(tx):
    work, owner, fence, operation_id = _admitted(tx)
    work.bind_harness_profile(tx, owner, fence, _identity())

    for statement in (
        "UPDATE operation_harness_binding SET profile = 'changed'",
        "UPDATE operation_harness_binding SET artifact_digest = repeat('d', 64)",
        f"DELETE FROM operation_harness_binding WHERE operation_id = '{operation_id}'",
    ):
        with (
            pytest.raises(
                (
                    psycopg2.errors.CheckViolation,
                    psycopg2.errors.RaiseException,
                    sqlite3.IntegrityError,
                ),
                match="immutable",
            ),
            tx.savepoint("binding_mutation"),
        ):
            tx.execute(statement)
    assert tx.fetch_one("SELECT count(*) AS count FROM operation_harness_binding")["count"] == 1


def test_binding_survives_restart_and_operation_purge_cascades(migrated_clone):
    db = migrated_clone.database
    with db.transaction() as tx:
        work, owner, fence, operation_id = _admitted(tx)
        binding = work.bind_harness_profile(tx, owner, fence, _identity())
        terminal = work.terminalize(
            tx,
            fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Synthetic harness completion",
            retry_after_ms=None,
            now=None,
            retention=timedelta(seconds=1),
        )
        assert terminal.state is OperationState.COMPLETED

    with db.transaction() as tx:
        restarted = WorkAdmissionRepository()
        assert restarted.get_harness_profile_binding(tx, owner, operation_id) == binding
        purged = restarted.purge_expired(
            tx,
            now=datetime.now(UTC) + timedelta(days=1),
            limit=10,
        )
        assert purged.operations == 1
        assert restarted.get_harness_profile_binding(tx, owner, operation_id) is None
        assert tx.fetch_one("SELECT count(*) AS count FROM operation_harness_binding")["count"] == 0


def test_unavailable_operation_identity_is_refused_without_writes(tx):
    work = WorkAdmissionRepository()
    work.configure(tx, _configs())
    work.bind_configs(_configs())
    owner = _owner()
    unknown_operation = uuid.uuid4()
    unknown_fence = ExecutionFence(unknown_operation, 1, uuid.uuid4())

    with pytest.raises(WorkAdmissionNotFoundError):
        work.bind_harness_profile(tx, owner, unknown_fence, _identity())
    assert tx.fetch_one("SELECT count(*) AS count FROM operation_harness_binding")["count"] == 0
    assert work.get_harness_profile_binding(tx, owner, unknown_operation) is None
