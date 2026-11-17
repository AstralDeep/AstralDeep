"""Real PostgreSQL admission capture preserves queued identities through replay, restart and
retention.
Selection races, owner isolation, exact binding fences and database immutability fail closed.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.preferences import PreferencesRepository
from astralplane.repositories.work_admission import (
    AdmissionClass,
    ExecutionFence,
    OperationRequest,
    OperationState,
    WorkAdmissionRepository,
)
from tests.fixtures.database_errors import check_violation
from tests.repositories.test_harness_bindings_postgres import _configs, _owner
from tests.repositories.test_harness_capture_models import capture


def prepare(tx):
    repository = WorkAdmissionRepository()
    repository.configure(tx, _configs())
    repository.bind_configs(_configs())
    selection = PreferencesRepository().harness.compare_and_set(
        tx,
        owner_id="owner-a",
        expected_revision=0,
        identity=capture().identity,
        qualification_digest="b" * 64,
    )
    request = OperationRequest(
        "harness_turn",
        AdmissionClass.BACKGROUND,
        _owner(),
        uuid.uuid4(),
        "capture",
        "same",
        "d" * 64,
        None,
        None,
        None,
        None,
        harness_selection=selection,
        harness_capture=capture(),
    )
    return repository, request, selection


def submit(repository, tx, request):
    return repository.submit(
        tx, request, now=None, retention=timedelta(days=1), slot_lease=timedelta(minutes=5)
    )


def test_admission_capture_survives_rollback_replay_restart_and_exact_binding(migrated_clone):
    db = migrated_clone.database
    with db.transaction() as tx:
        repository, request, selection = prepare(tx)
        admitted = submit(repository, tx, request)
        assert repository.get_harness_capture(tx, request.owner, admitted.operation_id) == capture()
        PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner-a",
            expected_revision=selection.revision,
            identity=None,
            qualification_digest=None,
        )
        replay = submit(
            repository, tx, replace(request, harness_capture=None, harness_selection=None)
        )
        assert replay.operation_id == admitted.operation_id
        replay = submit(repository, tx, replace(request, submission_id=uuid.uuid4()))
        assert replay.operation_id == admitted.operation_id
        claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            admitted.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        assert (
            repository.bind_harness_profile(
                tx, request.owner, claim.fence, capture().identity
            ).identity
            == capture().identity
        )
        with pytest.raises(RepositoryConflictError):
            repository.bind_harness_profile(
                tx, request.owner, claim.fence, HarnessProfileIdentity("other", "1.0.0", "f" * 64)
            )
        with pytest.raises(RepositoryConflictError):
            repository.bind_harness_profile(
                tx,
                request.owner,
                ExecutionFence(admitted.operation_id, 99, uuid.uuid4()),
                capture().identity,
            )
    with db.transaction() as tx:
        assert (
            WorkAdmissionRepository().get_harness_capture(tx, request.owner, admitted.operation_id)
            == capture()
        )
        assert repository.get_harness_capture(tx, _owner("other"), admitted.operation_id) is None


def test_new_admission_denies_changed_selection_and_mismatched_owner(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, selection = prepare(tx)
        PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner-a",
            expected_revision=selection.revision,
            identity=None,
            qualification_digest=None,
        )
        with pytest.raises(RepositoryConflictError):
            submit(repository, tx, request)
        with pytest.raises(RepositoryValidationError):
            replace(request, owner=_owner("other"))
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_harness_capture")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0


def test_capture_cannot_be_added_or_mutated_after_admission(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
        admitted = submit(repository, tx, request)
        for statement in (
            "UPDATE operation_harness_capture SET capture=capture||'{\"extra\":1}'::jsonb",
            "DELETE FROM operation_harness_capture",
        ):
            with check_violation(), tx.savepoint("immutable"):
                tx.execute(statement)
        claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            admitted.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        repository.terminalize(
            tx,
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Completed",
            retry_after_ms=None,
            now=None,
            retention=timedelta(seconds=1),
        )
        repository.purge_expired(tx, now=datetime.now(UTC) + timedelta(days=1), limit=10)
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_harness_capture")["n"] == 0


def test_queued_capture_survives_current_preference_reset_before_claim(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, selection = prepare(tx)
        configs = tuple(
            replace(config, active_limit=1, queue_limit=2, max_wait_ms=60000)
            for config in _configs()
        )
        repository.configure(tx, configs)
        repository.bind_configs(configs)
        blocker = submit(
            repository,
            tx,
            replace(
                request,
                harness_selection=None,
                harness_capture=None,
                submission_id=uuid.uuid4(),
                idempotency_key="blocker",
            ),
        )
        queued = submit(repository, tx, request)
        current = repository.query_operation(tx, request.owner, queued.operation_id)
        assert current.state is OperationState.QUEUED
        PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner-a",
            expected_revision=selection.revision,
            identity=None,
            qualification_digest=None,
        )
        claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            blocker.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        repository.terminalize(
            tx,
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Completed",
            retry_after_ms=None,
            now=None,
            retention=timedelta(days=1),
        )
        next_claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            queued.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        assert next_claim is not None
        assert repository.get_harness_capture(tx, request.owner, queued.operation_id) == capture()


@pytest.mark.parametrize(
    "changes",
    [
        {"capture": None},
        {"capture": capture(selection_revision=2)},
        {"capture": capture(qualification_digest="d" * 64)},
        {"selection": None},
        {"selection": "standard_local"},
        {"capture": capture(owner_id="other")},
        {"capture": {}},
        {"assignment_id": str(uuid.uuid4())},
    ],
)
def test_admission_guard_rejects_missing_ambiguous_or_wrong_owner_capture(migrated_clone, changes):
    from astralplane.repositories.harness_preferences import assert_harness_admission

    with migrated_clone.database.transaction() as tx:
        _, _, selected = prepare(tx)
        values = dict(owner_id="owner-a", selection=selected, capture=capture())
        values.update(changes)
        with pytest.raises(RepositoryValidationError):
            assert_harness_admission(tx, **values)


def test_default_absence_is_guarded_and_inherited_missing_capture_is_denied(migrated_clone):
    from astralplane.repositories.harness_capture import HarnessSelectionRecord
    from astralplane.repositories.harness_preferences import assert_harness_admission

    with migrated_clone.database.transaction() as tx:
        default = HarnessSelectionRecord("owner-a", 0, None, None)
        assert_harness_admission(tx, owner_id="owner-a", selection=default, capture=None)
        with pytest.raises(RepositoryValidationError):
            assert_harness_admission(tx, owner_id="owner-a", selection=default, capture=capture())
        with pytest.raises(RepositoryConflictError):
            assert_harness_admission(
                tx,
                owner_id="owner-a",
                selection=None,
                capture=capture(),
                assignment_id=str(uuid.uuid4()),
            )


def test_persisted_capture_owner_corruption_cannot_be_read_as_owned_or_legacy(migrated_clone):
    import json

    from astralplane.repositories import RepositoryDataError

    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
        admitted = submit(
            repository, tx, replace(request, harness_capture=None, harness_selection=None)
        )
        wrong = capture(owner_id="other")
        tx.execute(
            "INSERT INTO operation_harness_capture(operation_id,owner_id,capture,created_at) "
            "VALUES(%s,%s,%s,clock_timestamp())",
            (str(admitted.operation_id), "other", json.dumps(wrong.to_dict())),
        )
        with pytest.raises(RepositoryDataError):
            repository.get_harness_capture(tx, request.owner, admitted.operation_id)


def test_only_private_server_retry_admission_inherits_exact_parent_capture(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        repository, request, selection = prepare(tx)
        parent = submit(repository, tx, request)
        claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            parent.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        terminal = repository.terminalize(
            tx,
            claim.fence,
            state=OperationState.RETRYABLE,
            terminal_code="provider_unavailable",
            safe_summary="Unavailable",
            retry_after_ms=1000,
            now=None,
            retention=timedelta(days=1),
        )
        expected = repository.get_operation_for_administration(tx, operation_id=parent.operation_id)
        assert expected.state_revision == terminal.state_revision
        PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner-a",
            expected_revision=selection.revision,
            identity=None,
            qualification_digest=None,
        )
        child = replace(
            request,
            submission_id=uuid.uuid4(),
            idempotency_key="retry:" + str(parent.operation_id),
            parent_operation_id=parent.operation_id,
            harness_selection=None,
            harness_capture=None,
        )
        inherited = repository.submit_retry(
            tx,
            child,
            expected_parent=expected,
            expected_capture=capture(),
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        assert (
            repository.get_harness_capture(tx, request.owner, inherited.operation_id) == capture()
        )
        replay = repository.submit_retry(
            tx,
            replace(child, submission_id=uuid.uuid4()),
            expected_parent=replace(expected, state_revision=expected.state_revision + 1),
            expected_capture=capture(),
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        assert replay.operation_id == inherited.operation_id
        generic = submit(
            repository, tx, replace(child, submission_id=uuid.uuid4(), idempotency_key="group")
        )
        assert repository.get_harness_capture(tx, request.owner, generic.operation_id) is None


@pytest.mark.parametrize(
    "changed", ["owner", "parent", "revision", "state", "kind", "digest", "key", "capture"]
)
def test_private_retry_proof_denies_changed_parent_or_child_identity(migrated_clone, changed):
    with migrated_clone.database.transaction() as tx:
        repository, request, _ = prepare(tx)
        parent = submit(repository, tx, request)
        claim = repository.claim_operation(
            tx,
            AdmissionClass.BACKGROUND,
            parent.operation_id,
            now=None,
            retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5),
        )
        repository.terminalize(
            tx,
            claim.fence,
            state=OperationState.RETRYABLE,
            terminal_code="provider_unavailable",
            safe_summary="Unavailable",
            retry_after_ms=1000,
            now=None,
            retention=timedelta(days=1),
        )
        expected = repository.get_operation_for_administration(tx, operation_id=parent.operation_id)
        child = replace(
            request,
            submission_id=uuid.uuid4(),
            harness_selection=None,
            harness_capture=None,
            parent_operation_id=parent.operation_id,
            idempotency_key="retry:" + str(parent.operation_id),
        )
        if changed == "owner":
            child = replace(child, owner=_owner("other"))
        elif changed == "parent":
            tx.execute(
                "DELETE FROM operation_record WHERE operation_id=%s", (str(parent.operation_id),)
            )
        elif changed == "revision":
            expected = replace(expected, state_revision=expected.state_revision + 1)
        elif changed == "state":
            expected = replace(expected, state=OperationState.COMPLETED)
        elif changed == "kind":
            child = replace(child, operation_kind="different")
        elif changed == "digest":
            child = replace(child, normalized_input_digest="e" * 64)
        elif changed == "key":
            child = replace(child, idempotency_key="unrelated")
        expected_capture = capture(config_revision="f" * 64) if changed == "capture" else capture()
        with pytest.raises(RepositoryConflictError):
            repository.submit_retry(
                tx,
                child,
                expected_parent=expected,
                expected_capture=expected_capture,
                now=None,
                retention=timedelta(days=1),
                slot_lease=timedelta(minutes=5),
            )
