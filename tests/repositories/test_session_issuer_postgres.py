"""Real-PostgreSQL tests for astralplane.repositories.history and revocations: issuing
identity survives refresh and exact replay, metadata can't be spoofed or relabeled,
and deferred revocation keeps its exact identity through later reads.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import tx as tx
from test_session_incarnation_postgres import input_record

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.history import (
    SessionConsentObservation,
    SessionExecutionObservation,
    SessionRepository,
)
from tests.fixtures.database_errors import check_violation

ISSUER = "https://iam.example.test/realms/Astral"
CLIENT = "astral-mobile"


@pytest.mark.parametrize("bound", [False, True])
def test_issuance_rotation_resume_and_exact_replay_preserve_metadata(tx, bound):
    repo = SessionRepository()
    submitted = input_record(
        tx, issuing_issuer=ISSUER if bound else None, issuing_client_id=CLIENT if bound else None
    )
    original = repo.put(tx, submitted)
    assert repo.put(tx, submitted) == original
    assert repo.put(tx, original) == original
    state = repo.get_execution_state(tx, owner_id="owner", session_id=original.session_id)
    assert state.credential.issuing_issuer == original.issuing_issuer
    assert state.credential.issuing_client_id == original.issuing_client_id
    proposed = replace(
        original,
        last_refresh_at=original.last_refresh_at + 1,
        access_token_ciphertext="rotated-a",
        refresh_token_ciphertext="rotated-r",
    )
    rotated = repo.compare_and_set_refresh(
        tx,
        proposed,
        expected_last_refresh_at=original.last_refresh_at,
        expected_credential=state.credential,
    )
    assert rotated == proposed
    assert repo.mark_resumed(
        tx,
        owner_id="owner",
        session_id=original.session_id,
        expected_incarnation_id=original.incarnation_id,
        expected_resumed=False,
        resumed=True,
    )
    assert repo.get_by_incarnation(
        tx, owner_id="owner", incarnation_id=original.incarnation_id
    ) == replace(rotated, resumed=True)
    removed = repo.delete_and_return(
        tx,
        owner_id="owner",
        session_id=original.session_id,
        expected_incarnation_id=original.incarnation_id,
    )
    assert removed == replace(rotated, resumed=True)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("use_fence", [False, True])
def test_metadata_cannot_be_changed_or_added_or_removed_by_refresh(tx, bound, use_fence):
    repo = SessionRepository()
    original = repo.put(
        tx,
        input_record(
            tx,
            issuing_issuer=ISSUER if bound else None,
            issuing_client_id=CLIENT if bound else None,
        ),
    )
    before = repo.execution_fence(original)
    candidates = [
        (ISSUER + "/different", CLIENT),
        (ISSUER, "astral-desktop"),
        (None, None) if bound else (ISSUER, CLIENT),
    ]
    for issuer, client in candidates:
        proposed = replace(
            original,
            last_refresh_at=original.last_refresh_at + 1,
            issuing_issuer=issuer,
            issuing_client_id=client,
        )
        with pytest.raises(RepositoryValidationError if use_fence else RepositoryConflictError):
            repo.compare_and_set_refresh(
                tx,
                proposed,
                expected_last_refresh_at=original.last_refresh_at,
                expected_credential=before if use_fence else None,
            )
        assert repo.get(tx, owner_id="owner", session_id=original.session_id) == original


@pytest.mark.parametrize("explicit", [False, True])
def test_put_cannot_relabel_existing_incarnation_even_with_identical_ciphertext(tx, explicit):
    repo = SessionRepository()
    original = repo.put(tx, input_record(tx, issuing_issuer=ISSUER, issuing_client_id=CLIENT))
    for changes in (
        {"issuing_issuer": ISSUER + "/other"},
        {"issuing_client_id": "other"},
        {"issuing_issuer": None, "issuing_client_id": None},
    ):
        with pytest.raises(RepositoryConflictError):
            repo.put(
                tx,
                replace(
                    original,
                    incarnation_id=original.incarnation_id if explicit else None,
                    **changes,
                ),
            )
    assert repo.get(tx, owner_id="owner", session_id=original.session_id) == original


@pytest.mark.parametrize(
    "observation_type", [SessionExecutionObservation, SessionConsentObservation]
)
def test_bound_and_legacy_observations_are_current_but_metadata_spoof_is_refused(
    tx, observation_type
):
    repo = SessionRepository()
    for bound in (False, True):
        original = repo.put(
            tx,
            input_record(
                tx,
                issuing_issuer=ISSUER if bound else None,
                issuing_client_id=CLIENT if bound else None,
            ),
        )
        state = repo.get_execution_state(tx, owner_id="owner", session_id=original.session_id)
        observation = observation_type(
            state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
        )
        check = (
            repo.assert_current_execution
            if observation_type is SessionExecutionObservation
            else repo.assert_current_consent
        )
        assert check(tx, observation=observation).credential == state.credential
        tampered = replace(
            state.credential, issuing_issuer=ISSUER + "/other", issuing_client_id=CLIENT
        )
        with pytest.raises(RepositoryConflictError):
            check(tx, observation=replace(observation, credential=tampered))
        coherent = repo.execution_fence(
            replace(original, issuing_issuer=ISSUER + "/other", issuing_client_id=CLIENT)
        )
        with pytest.raises(RepositoryConflictError):
            check(tx, observation=replace(observation, credential=coherent))


def test_new_database_incarnation_can_have_new_issuer_but_old_fence_never_adopts_it(tx):
    repo = SessionRepository()
    original = repo.put(tx, input_record(tx, issuing_issuer=ISSUER, issuing_client_id=CLIENT))
    state = repo.get_execution_state(tx, owner_id="owner", session_id=original.session_id)
    assert repo.delete(
        tx,
        owner_id="owner",
        session_id=original.session_id,
        expected_incarnation_id=original.incarnation_id,
    )
    newer = repo.put(
        tx,
        replace(
            original,
            incarnation_id=None,
            issuing_issuer=ISSUER + "/new",
            issuing_client_id="astral-desktop",
        ),
    )
    assert newer.incarnation_id != original.incarnation_id
    with pytest.raises(RepositoryConflictError):
        repo.compare_and_set_refresh(
            tx,
            replace(original, last_refresh_at=original.last_refresh_at + 1),
            expected_last_refresh_at=original.last_refresh_at,
        )
    with pytest.raises(RepositoryConflictError):
        repo.assert_current_execution(
            tx,
            observation=SessionExecutionObservation(
                state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
            ),
        )
    assert repo.get(tx, owner_id="owner", session_id=original.session_id) == newer


@pytest.mark.parametrize(
    "issuer,client",
    [
        (None, CLIENT),
        (ISSUER, None),
        ("", CLIENT),
        (ISSUER, ""),
        ("x" * 2049, CLIENT),
        (ISSUER, "x" * 257),
        ("\u2003leading", CLIENT),
        (ISSUER, "control\u0085inside"),
    ],
)
def test_database_constraints_refuse_malformed_pair(tx, issuer, client):

    repo = SessionRepository()
    original = repo.put(tx, input_record(tx))
    with check_violation(), tx.savepoint("malformed_issuing_pair"):
        tx.execute(
            "UPDATE web_session SET issuing_issuer=%s, issuing_client_id=%s WHERE sid=%s",
            (issuer, client, original.session_id),
        )
    assert repo.get(tx, owner_id="owner", session_id=original.session_id) == original


@pytest.mark.parametrize("issuer,client", [(None, None), (None, "old-client"), (ISSUER, CLIENT)])
def test_deferred_revocation_keeps_exact_identity_through_reads_and_attempts(tx, issuer, client):
    from astralplane.repositories import RepositoryNotFoundError
    from astralplane.repositories.revocations import RevocationQueueRepository

    queue = RevocationQueueRepository()
    item = queue.enqueue(
        tx,
        owner_id="owner",
        refresh_token_ciphertext="synthetic-ciphertext",
        enqueued_at=100,
        client_id=client,
        issuing_issuer=issuer,
    )
    assert queue.pending_for_owner(tx, owner_id="owner") == (item,)
    assert queue.pending_for_owner(tx, owner_id="other") == ()
    assert queue.pending_for_administration(tx) == (item,)
    with pytest.raises(RepositoryNotFoundError):
        queue.bump_attempt(tx, owner_id="other", queue_id=item.queue_id, expected_attempts=0)
    bumped = queue.bump_attempt(tx, owner_id="owner", queue_id=item.queue_id, expected_attempts=0)
    assert bumped == replace(item, attempts=1)
    with pytest.raises(RepositoryNotFoundError):
        queue.bump_attempt(tx, owner_id="owner", queue_id=item.queue_id, expected_attempts=0)
    assert not queue.resolve(tx, owner_id="other", queue_id=item.queue_id)
    assert queue.pending_for_administration(tx) == (bumped,)
    assert queue.resolve(tx, owner_id="owner", queue_id=item.queue_id)
    assert queue.pending_for_administration(tx) == ()


@pytest.mark.parametrize(
    "issuer,client",
    [
        (ISSUER, None),
        ("", CLIENT),
        (ISSUER, ""),
        ("x" * 2049, CLIENT),
        (ISSUER, "x" * 257),
        ("\u2003leading", CLIENT),
        (ISSUER, "control\u0085inside"),
    ],
)
def test_queue_database_constraints_refuse_malformed_bound_identity(tx, issuer, client):

    from astralplane.repositories.revocations import RevocationQueueRepository

    queue = RevocationQueueRepository()
    item = queue.enqueue(tx, owner_id="owner", refresh_token_ciphertext="cipher", enqueued_at=100)
    with check_violation(), tx.savepoint("malformed_queue_issuer"):
        tx.execute(
            "UPDATE auth_revocation_queue SET issuing_issuer=%s, client_id=%s WHERE id=%s",
            (issuer, client, item.queue_id),
        )
    assert queue.pending_for_administration(tx) == (item,)
    assert queue.resolve(tx, owner_id="owner", queue_id=item.queue_id)


def test_bound_restored_session_retirement_preserves_queue_and_denies_old_observation(database):
    import os

    from psycopg2.extensions import make_dsn

    from astralplane import retire_restored_sessions
    from astralplane.database.migrations import MIGRATION_DIGEST
    from astralplane.database.revision import SCHEMA_REVISION
    from astralplane.repositories.revocations import RevocationQueueRepository

    repo, queue = SessionRepository(), RevocationQueueRepository()
    with database.transaction() as tx:
        tx.execute("DELETE FROM web_session")
        original = repo.put(tx, input_record(tx, issuing_issuer=ISSUER, issuing_client_id=CLIENT))
        state = repo.get_execution_state(tx, owner_id="owner", session_id=original.session_id)
        observation = SessionExecutionObservation(
            state.credential, state.observed_at, state.observed_at + timedelta(seconds=15)
        )
        item = queue.enqueue(
            tx,
            owner_id="owner",
            refresh_token_ciphertext="cipher",
            enqueued_at=100,
            client_id=CLIENT,
            issuing_issuer=ISSUER,
        )
        if tx.dialect.is_sqlite:
            from astralplane.database.sqlite import sqlite_path_from_url
            from astralplane.database.sqlite_migrations import SQLITE_MIGRATION_DIGEST

            database_url = os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"]
            selected = dict(
                database_url=database_url,
                expected_database=str(sqlite_path_from_url(database_url).resolve()),
                expected_schema="main",
                expected_schema_revision=SCHEMA_REVISION,
                expected_migration_digest=SQLITE_MIGRATION_DIGEST,
            )
        else:
            location = tx.fetch_one("SELECT current_database() AS db, current_schema() AS schema")
            selected = dict(
                database_url=make_dsn(
                    os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"],
                    options=f"-csearch_path={location['schema']},pg_catalog",
                ),
                expected_database=location["db"],
                expected_schema=location["schema"],
                expected_schema_revision=SCHEMA_REVISION,
                expected_migration_digest=MIGRATION_DIGEST,
            )
        snapshot = dict(
            tx.fetch_one("SELECT * FROM web_session WHERE sid=%s", (original.session_id,))
        )
    assert retire_restored_sessions(**selected).retired_sessions == 1
    with database.transaction() as tx:
        assert queue.pending_for_administration(tx) == (item,)
        with pytest.raises(RepositoryConflictError):
            repo.assert_current_execution(tx, observation=observation)
        columns = tuple(snapshot)
        tx.execute(
            "INSERT INTO web_session ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join(["%s"] * len(columns))
            + ")",
            tuple(snapshot.values()),
        )
    assert retire_restored_sessions(**selected).retired_sessions == 1
    assert retire_restored_sessions(**selected).retired_sessions == 0
    with database.transaction() as tx:
        assert queue.pending_for_administration(tx) == (item,)
        assert queue.resolve(tx, owner_id="owner", queue_id=item.queue_id)
