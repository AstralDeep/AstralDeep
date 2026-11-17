"""Framework operation discovery and private credential observations use the existing
assignment and credential fences on real PostgreSQL, independently of web sessions.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import (
    create_operation,
    framework_credential_observation,
    uid,
)
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import mutate

from astralplane.errors import PlaneError
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignments import (
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    plain,
)
from astralplane.repositories.framework_credentials import FrameworkCredentialRepository
from astralplane.repositories.history import FrameworkCredentialObservation


def operation(repo, tx, *, seconds=300):
    tx.execute("DELETE FROM framework_credential")
    credential, observation = framework_credential_observation(tx)
    now = observation.started_at
    from test_operation_incarnation_postgres import arguments

    definition = replace(arguments(tx)["definition"], consented_scopes=())
    record = create_operation(
        repo,
        tx,
        caller_key=uid(),
        authority=observation,
        definition=definition,
        credential_id=credential.credential_id,
        operation=AssignmentOperationSpec(
            "chat",
            AssignmentOperationAuthority(
                "owner",
                "framework",
                "credential",
                credential.credential_id,
                now + timedelta(seconds=seconds),
            ),
            now + timedelta(seconds=seconds),
            "operation",
        ),
    )
    return record, credential


def capture(repo, tx, record, **changes):
    return repo.capture_framework_operation_execution_observation(
        tx,
        **{
            "owner_id": record.owner_id,
            "assignment_id": record.assignment_id,
            "expected_record": record,
            **changes,
        },
    )


def test_framework_discovery_observation_claim_and_refresh_do_not_charge_admission(tx, repo):
    record, credential = operation(repo, tx)
    assert record in repo.discover_due_operations_for_administration(tx)
    observation = capture(repo, tx, record)
    assert type(observation) is FrameworkCredentialObservation
    assert observation.credential.credential_id == credential.credential_id
    assert observation.credential.scopes == ("operations.submit",)
    assert "a" * 64 not in repr(observation)
    claim = repo.claim_operation_for_administration(
        tx,
        owner_id="owner",
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        worker_id="framework-worker",
        authority=observation,
    )
    refreshed = capture(repo, tx, claim.assignment)
    assert refreshed.credential.consumed_admissions == 0
    assert repo.discover_due_operations_for_administration(tx) == ()
    stored = repo.get_operation(tx, owner_id="owner", assignment_id=record.assignment_id).assignment
    assert "a" * 64 not in str(plain(stored))


def test_observation_is_capped_by_operation_deadline(tx, repo):
    record, _ = operation(repo, tx, seconds=5)
    observation = capture(repo, tx, record)
    assert observation.valid_until.isoformat() == record.operation["deadline_at"]
    assert timedelta(0) < observation.valid_until - observation.started_at <= timedelta(seconds=15)


@pytest.mark.parametrize(
    "loss",
    [
        "owner",
        "id",
        "untyped",
        "version",
        "control",
        "instruction",
        "operation",
        "definition",
        "credential",
        "revoked",
        "expired",
        "scope",
        "retired",
        "deadline",
        "kind",
        "tools",
        "offline",
        "selection",
        "interactive",
    ],
)
def test_capture_refuses_changed_or_unsupported_authority(tx, repo, loss):
    record, credential = operation(repo, tx)
    changes = {}
    if loss == "owner":
        changes["owner_id"] = "foreign"
    elif loss == "id":
        changes["assignment_id"] = uid()
    elif loss == "untyped":
        changes["expected_record"] = plain(record)
    elif loss in {"version", "control", "instruction"}:
        field = {
            "version": "state_version",
            "control": "control_epoch",
            "instruction": "instruction_revision",
        }[loss]
        changes["expected_record"] = replace(record, **{field: getattr(record, field) + 1})
    elif loss in {"revoked", "expired", "scope"}:
        column = {
            "revoked": "revoked_at=clock_timestamp()",
            "expired": (
                "created_at=clock_timestamp()-interval '2 hours', "
                "expires_at=clock_timestamp()-interval '1 second'"
            ),
            "scope": "scopes='[\"operations.read\"]'::jsonb",
        }[loss]
        tx.execute(
            "UPDATE framework_credential SET " + column + " WHERE id=%s",
            (credential.credential_id,),
        )
    elif loss == "retired":
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
            "VALUES('owner','retired',clock_timestamp())"
        )
    else:

        def change(data):
            if loss == "operation":
                data["operation"]["source_retention"] = "none"
            elif loss == "definition":
                data["definition"]["instructions"] += " changed"
            elif loss == "credential":
                data["operation"]["authority"]["reference_id"] = uid()
            elif loss == "deadline":
                data["operation"]["deadline_at"] = "2000-01-01T00:00:00+00:00"
            elif loss == "kind":
                data["operation"]["kind"] = "research"
            elif loss == "tools":
                data["definition"]["allowed_tools"] = ["web-research-1:fetch_page"]
            elif loss == "offline":
                data["definition"]["offline_grant_id"] = uid()
            elif loss == "selection":
                data["operation"]["selection"] = {"version": 1}
            else:
                data["operation"]["authority"].update(
                    origin="interactive", reference_kind="session_incarnation"
                )

        mutate(tx, record, change)
    with pytest.raises(PlaneError):
        capture(repo, tx, record, **changes)


def test_old_observation_cannot_keep_submit_scope_after_live_mutation(tx, repo):
    record, credential = operation(repo, tx)
    observation = capture(repo, tx, record)
    tx.execute(
        "UPDATE framework_credential SET scopes='[\"operations.read\"]'::jsonb WHERE id=%s",
        (credential.credential_id,),
    )
    with pytest.raises(RepositoryConflictError):
        repo.claim_operation_for_administration(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_state_version=record.state_version,
            worker_id="framework-worker",
            authority=observation,
        )


def test_fresh_observation_survives_issuer_logout_but_not_credential_revocation(tx, repo):
    record, credential = operation(repo, tx)
    tx.execute("DELETE FROM web_session WHERE user_id='owner'")
    observation = capture(repo, tx, record)
    FrameworkCredentialRepository().revoke(
        tx, owner_id="owner", credential_id=credential.credential_id
    )
    with pytest.raises(RepositoryConflictError):
        repo.claim_operation_for_administration(
            tx,
            owner_id="owner",
            assignment_id=record.assignment_id,
            expected_state_version=record.state_version,
            worker_id="framework-worker",
            authority=observation,
        )


@pytest.mark.parametrize("method", ["consume", "assert", "capture"])
def test_credential_entrypoints_lock_owner_before_credential(tx, repo, method):
    record, credential = operation(repo, tx)
    observation = capture(repo, tx, record)
    statements = []

    class Tracking:
        def __getattr__(self, name):
            return getattr(tx, name)

        def fetch_one(self, statement, *args, **kwargs):
            statements.append(statement)
            return tx.fetch_one(statement, *args, **kwargs)

    repository = FrameworkCredentialRepository()
    wrapped = Tracking()
    if method == "consume":
        repository.consume_admission(
            wrapped, owner_id="owner", credential_id=credential.credential_id
        )
    elif method == "assert":
        repository.assert_current_execution(wrapped, observation=observation)
    else:
        repository.capture_execution_observation(
            wrapped,
            owner_id="owner",
            credential_id=credential.credential_id,
            token_hash=observation.credential.token_hash,
        )
    owner_lock = next(
        (i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql), None
    )
    credential_lock = next(i for i, sql in enumerate(statements) if "framework_credential" in sql)
    assert owner_lock is not None and owner_lock < credential_lock


def test_corrupted_credential_reference_fails_with_a_typed_error(tx, repo):
    record, _ = operation(repo, tx)
    mutate(tx, record, lambda data: data["operation"]["authority"].update(reference_id="opaque"))
    current = repo.get_operation(
        tx, owner_id="owner", assignment_id=record.assignment_id
    ).assignment
    with pytest.raises(PlaneError):
        capture(repo, tx, current)


def test_framework_execution_refuses_even_an_empty_selected_input_header(tx, repo):
    record, _ = operation(repo, tx)
    selected = repo.bind_guidance_references(
        tx,
        owner_id=record.owner_id,
        assignment_id=record.assignment_id,
        expected_state_version=record.state_version,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch,
        references=(),
    )
    with pytest.raises(RepositoryConflictError):
        capture(repo, tx, selected)


def test_framework_capture_and_revoke_serialize_without_reusing_observation(database, repo):
    from test_assignments_postgres import parallel_transactions

    with database.transaction() as transaction:
        transaction.execute("DELETE FROM assignment_operation_receipt")
        transaction.execute("DELETE FROM persistent_assignment")
        transaction.execute("DELETE FROM framework_credential")
        transaction.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        record, credential = operation(repo, transaction)
    results = parallel_transactions(
        database,
        (
            lambda transaction: capture(repo, transaction, record),
            lambda transaction: FrameworkCredentialRepository().revoke(
                transaction, owner_id="owner", credential_id=credential.credential_id
            ),
        ),
    )
    assert results[1].revoked_at is not None
    with database.transaction() as transaction:
        with pytest.raises(RepositoryConflictError):
            capture(repo, transaction, record)
        if isinstance(results[0], FrameworkCredentialObservation):
            with pytest.raises(RepositoryConflictError):
                repo.claim_operation_for_administration(
                    transaction,
                    owner_id="owner",
                    assignment_id=record.assignment_id,
                    expected_state_version=record.state_version,
                    worker_id="late-worker",
                    authority=results[0],
                )
        else:
            assert isinstance(results[0], RepositoryConflictError)


def fixed_credential_clock(tx, *, timestamp):
    class Clock:
        def __getattr__(self, name):
            return getattr(tx, name)

        def fetch_one(self, statement, *args, **kwargs):
            statement = statement.replace("clock_timestamp()", "TIMESTAMPTZ '" + timestamp + "'")
            return tx.fetch_one(statement, *args, **kwargs)

    return Clock()


def test_execution_caps_at_exact_credential_expiry_and_rejects_rounded_old_observation(tx, repo):
    from datetime import UTC, datetime

    record, credential = operation(repo, tx)
    tx.execute(
        "UPDATE framework_credential SET created_at='2025-01-01T00:00:00Z', "
        "expires_at='2026-01-01T00:00:01.75Z' WHERE id=%s",
        (credential.credential_id,),
    )
    before = fixed_credential_clock(tx, timestamp="2026-01-01T00:00:01.5Z")
    observation = capture(repo, before, record)
    assert observation.valid_until == datetime(2026, 1, 1, 0, 0, 1, 750000, UTC)
    legacy = replace(observation, valid_until=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC))
    after = fixed_credential_clock(tx, timestamp="2026-01-01T00:00:01.8Z")
    with pytest.raises(RepositoryConflictError):
        FrameworkCredentialRepository().assert_current_execution(after, observation=legacy)
