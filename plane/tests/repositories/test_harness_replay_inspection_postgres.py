"""Neutral replay inspection retains ordinary owner and request locks without granting authority.
Hosts inspect original replay before checking new selection or config in the same transaction.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.preferences import PreferencesRepository
from astralplane.repositories.work_admission import OperationOwner, OwnerScope
from tests.integration.test_lifecycle_acceptance import concurrent_database
from tests.repositories.test_harness_bindings_postgres import _owner
from tests.repositories.test_harness_capture_postgres import prepare, submit


def test_submission_only_and_idempotency_replay_preserve_normal_conflict_outcome(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
        PreferencesRepository().harness.lock_owner(tx, owner_id="owner-a")
        ordinary = replace(
            request,
            owner=OperationOwner(OwnerScope.CONNECTION, None, uuid.uuid4()),
            harness_selection=None,
            harness_capture=None,
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
        )
        assert repository.has_admission_replay(tx, ordinary) is False
        original = submit(repository, tx, ordinary)
        assert repository.has_admission_replay(tx, ordinary) is True
        assert submit(repository, tx, ordinary).operation_id == original.operation_id
        assert (
            repository.has_admission_replay(tx, replace(ordinary, submission_id=uuid.uuid4()))
            is False
        )
        assert (
            repository.has_admission_replay(
                tx,
                replace(ordinary, owner=OperationOwner(OwnerScope.CONNECTION, None, uuid.uuid4())),
            )
            is False
        )
        selected = submit(repository, tx, request)
        conflicting = replace(request, submission_id=uuid.uuid4(), normalized_input_digest="e" * 64)
        assert repository.has_admission_replay(tx, conflicting) is True
        refusal = submit(repository, tx, conflicting)
        assert refusal.accepted is False and refusal.code == "idempotency_conflict"
        assert (
            repository.has_admission_replay(
                tx,
                replace(
                    request, owner=_owner("other"), harness_selection=None, harness_capture=None
                ),
            )
            is False
        )
        assert repository.get_harness_capture(tx, request.owner, selected.operation_id) is not None


def test_rejected_submission_replays_without_becoming_new_work(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
        config = tuple(replace(c, active_limit=1) for c in repository.load_existing_configs(tx))
        repository.configure(tx, config)
        repository.bind_configs(config)
        submit(
            repository, tx, replace(request, submission_id=uuid.uuid4(), idempotency_key="blocker")
        )
        refused = submit(repository, tx, request)
        assert refused.accepted is False
        assert repository.has_admission_replay(tx, request) is True
        assert submit(repository, tx, request) == refused


def test_same_owner_inspection_and_admission_serialize_without_duplicates(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
    barrier = Barrier(2)

    def admit(database):
        with database.transaction() as tx:
            barrier.wait(timeout=30)
            PreferencesRepository().harness.lock_owner(tx, owner_id="owner-a")
            replay = repository.has_admission_replay(tx, request)
            return replay, submit(repository, tx, request).operation_id

    with concurrent_database(migrated_clone) as database, ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: admit(database), range(2)))
    assert sorted(value[0] for value in values) == [False, True]
    assert values[0][1] == values[1][1]
    with migrated_clone.database.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 1


def test_retired_owner_lock_never_becomes_replay_authority(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
            "VALUES('owner-a','retired',clock_timestamp())"
        )
        with pytest.raises(RepositoryConflictError):
            PreferencesRepository().harness.lock_owner(tx, owner_id="owner-a")
