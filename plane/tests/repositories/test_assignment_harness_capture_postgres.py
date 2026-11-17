"""Assignments retain immutable typed harness captures independently of definitions and guidance.
Replay and later episode admission reuse the initial capture after owner rollback.
"""

import uuid
from dataclasses import replace

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignments import AssignmentRepository, digest
from astralplane.repositories.preferences import PreferencesRepository
from astralplane.repositories.work_admission import (
    AdmissionClass,
    OperationRequest,
    WorkAdmissionRepository,
)
from tests.fixtures.database_errors import check_violation
from tests.repositories.test_assignments_postgres import definition
from tests.repositories.test_harness_bindings_postgres import _configs, _owner
from tests.repositories.test_harness_capture_models import capture
from tests.repositories.test_harness_capture_postgres import submit


def test_assignment_capture_is_typed_immutable_and_replayed_before_current_selection(
    migrated_clone,
):
    with migrated_clone.database.transaction() as tx:
        repository = AssignmentRepository()
        selected = capture(owner_id="owner")
        preference = PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner",
            expected_revision=0,
            identity=selected.identity,
            qualification_digest="b" * 64,
        )
        plan = definition(tx)
        arguments = dict(
            owner_id="owner",
            assignment_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            submission_digest=digest("create"),
            definition=plan,
            harness_selection=preference,
            harness_capture=selected,
        )
        record = repository.create_assignment(tx, **arguments)
        assert record.harness_capture == selected
        assert "active-key" not in repr(record)
        PreferencesRepository().harness.compare_and_set(
            tx, owner_id="owner", expected_revision=1, identity=None, qualification_digest=None
        )
        assert repository.create_assignment(tx, **arguments) == record
        assert (
            repository.create_assignment(
                tx, **dict(arguments, harness_capture=None, harness_selection=None)
            )
            == record
        )
        with pytest.raises(RepositoryConflictError):
            repository.create_assignment(
                tx,
                **dict(arguments, assignment_id=str(uuid.uuid4()), submission_id=str(uuid.uuid4())),
            )
        for document in ("null", '{"unexpected":true}'):
            with check_violation(), tx.savepoint("capture_immutable"):
                tx.execute(
                    "UPDATE persistent_assignment SET data=jsonb_set(data,'{harness_capture}',%s) "
                    "WHERE owner_user_id=%s AND id=%s",
                    (document, "owner", record.assignment_id),
                )
        work = WorkAdmissionRepository()
        work.configure(tx, _configs())
        work.bind_configs(_configs())
        request = OperationRequest(
            "assignment_episode",
            AdmissionClass.BACKGROUND,
            _owner("owner"),
            uuid.uuid4(),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            harness_capture=selected,
            harness_assignment_id=uuid.UUID(record.assignment_id),
        )
        admitted = submit(work, tx, request)
        assert work.get_harness_capture(tx, request.owner, admitted.operation_id) == selected
        with pytest.raises(RepositoryConflictError):
            submit(
                work,
                tx,
                replace(
                    request,
                    submission_id=uuid.uuid4(),
                    harness_capture=replace(selected, config_revision="d" * 64),
                ),
            )


def test_one_shot_capture_replay_survives_reset_and_missing_new_authority(migrated_clone):
    from tests.repositories.test_assignments_postgres import create_operation

    with migrated_clone.database.transaction() as tx:
        repository = AssignmentRepository()
        selected = capture(owner_id="owner")
        preference = PreferencesRepository().harness.compare_and_set(
            tx,
            owner_id="owner",
            expected_revision=0,
            identity=selected.identity,
            qualification_digest="b" * 64,
        )
        record = create_operation(
            repository, tx, harness_selection=preference, harness_capture=selected
        )
        assert record.harness_capture == selected
        PreferencesRepository().harness.compare_and_set(
            tx, owner_id="owner", expected_revision=1, identity=None, qualification_digest=None
        )
        replay = create_operation(
            repository,
            tx,
            harness_selection=preference,
            harness_capture=selected,
            authority=None,
            definition=None,
            operation=None,
        )
        assert replay == record
        assert (
            AssignmentRepository()
            .get_assignment(tx, owner_id="owner", assignment_id=record.assignment_id)
            .harness_capture
            == selected
        )
