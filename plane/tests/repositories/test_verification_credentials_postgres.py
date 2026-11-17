"""Real-PostgreSQL behavior contract for astralplane.repositories.verification_credentials:
owner isolation, bounded enrollment, revision-fenced revocation, exactly-once challenge
consumption, bounded retention pruning, and the revocation-lookup race against a live schema.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from time import monotonic

import pytest
from test_assignments_postgres import Pool, standalone_database

from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.verification_credentials import (
    VerificationChallengeAlreadyConsumedError,
    VerificationCredentialLimitError,
    VerificationCredentialRepository,
    VerificationCredentialRevokedError,
)

ALGORITHM = "es256"


def uid() -> str:
    return str(uuid.uuid4())


def synthetic_key() -> str:
    return hashlib.sha256(uid().encode()).hexdigest()


def challenge_digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def enroll(tx, repo=None, *, owner=None, credential=None, public_key_text=None):
    repo = repo or VerificationCredentialRepository()
    return repo.enroll_credential(
        tx,
        owner_id=owner or uid(),
        credential_id=credential or uid(),
        device_id="synthetic-mac-device",
        algorithm=ALGORITHM,
        key_format="spki",
        public_key=public_key_text or synthetic_key(),
    )


def wait_for_blocked_statement(observer_database, waiting_pid, blocking_pid):
    deadline = monotonic() + 30
    with observer_database.transaction() as observer:
        while monotonic() < deadline:
            observer.fetch_one("SELECT pg_stat_clear_snapshot()")
            row = observer.fetch_one(
                "SELECT wait_event_type, pg_blocking_pids(pid) AS blockers "
                "FROM pg_stat_activity WHERE pid=%s",
                (waiting_pid,),
            )
            if row is not None and row["wait_event_type"] == "Lock" and blocking_pid in (
                row["blockers"] or ()
            ):
                return
    pytest.fail("verification lookup did not block on the exact held revocation transaction")


def independent_connection(schema):
    import psycopg2

    if os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"].startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        return create_sqlite_driver_pool(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"]).getconn()
    connection = psycopg2.connect(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"])
    with connection.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{schema}",pg_catalog')
    connection.commit()
    return connection


def test_owner_isolation_replay_and_limit_are_atomic():
    fixture = standalone_database()
    database = next(fixture)
    try:
        repo = VerificationCredentialRepository()
        owner, other = uid(), uid()
        credential = uid()
        with database.transaction() as tx:
            original = enroll(tx, repo, owner=owner, credential=credential)
            assert original.credential_state == "active"
            assert original.record_version == 1
            assert original.revoked_at is None
            assert original.enrolled_at is not None
            replay = enroll(
                tx, repo, owner=owner, credential=credential, public_key_text=original.public_key
            )
            assert replay == original
            with pytest.raises(RepositoryConflictError):
                enroll(
                    tx,
                    repo,
                    owner=owner,
                    credential=credential,
                    public_key_text=synthetic_key(),
                )
            with pytest.raises(RepositoryConflictError):
                enroll(tx, repo, owner=other, credential=credential)
            with pytest.raises(RepositoryNotFoundError):
                repo.get_credential(tx, owner_id=other, credential_id=credential)
            assert repo.list_credentials(tx, owner_id=other) == ()
            assert [row.credential_id for row in repo.list_credentials(tx, owner_id=owner)] == [
                credential
            ]
        with database.transaction() as tx:
            revoked = repo.revoke_credential(
                tx, owner_id=owner, credential_id=credential, expected_record_version=1
            )
            assert revoked.credential_state == "revoked"
            assert revoked.record_version == 2
            assert revoked.revoked_at is not None
        with database.transaction() as tx:
            for _ in range(32):
                enroll(tx, repo, owner=owner)
            with pytest.raises(VerificationCredentialLimitError):
                enroll(tx, repo, owner=owner)
            replacement = enroll(tx, repo, owner=other)
            assert replacement.credential_state == "active"
            assert len(repo.list_credentials(tx, owner_id=other)) == 1
    finally:
        next(fixture, None)


def test_revocation_cas_replay_and_challenge_single_use():
    fixture = standalone_database()
    database = next(fixture)
    try:
        repo = VerificationCredentialRepository()
        owner, credential = uid(), uid()
        digest = challenge_digest("synthetic-approval-challenge")
        with database.transaction() as tx:
            enroll(tx, repo, owner=owner, credential=credential)
            with pytest.raises(RepositoryConflictError):
                repo.revoke_credential(
                    tx, owner_id=owner, credential_id=credential, expected_record_version=2
                )
            revoked = repo.revoke_credential(
                tx, owner_id=owner, credential_id=credential, expected_record_version=1
            )
            assert revoked.credential_state == "revoked"
            assert revoked.record_version == 2
            assert revoked.revoked_at is not None
            with pytest.raises(RepositoryConflictError):
                repo.revoke_credential(
                    tx, owner_id=owner, credential_id=credential, expected_record_version=2
                )
            with pytest.raises(VerificationCredentialRevokedError):
                repo.require_active_credential(tx, owner_id=owner, credential_id=credential)
            stored = repo.get_credential(tx, owner_id=owner, credential_id=credential)
            assert stored.credential_state == "revoked"
            first = repo.consume_challenge(
                tx, owner_id=owner, challenge_digest=digest, consumption_id=uid()
            )
            assert first.challenge_digest == digest
            with pytest.raises(VerificationChallengeAlreadyConsumedError):
                repo.consume_challenge(
                    tx, owner_id=owner, challenge_digest=digest, consumption_id=uid()
                )
            replay = repo.consume_challenge(
                tx, owner_id=owner, challenge_digest=digest, consumption_id=first.consumption_id
            )
            assert replay == first
            second_digest = challenge_digest("synthetic-approval-challenge-2")
            second = repo.consume_challenge(
                tx, owner_id=owner, challenge_digest=second_digest, consumption_id=uid()
            )
            with pytest.raises(RepositoryConflictError):
                repo.consume_challenge(
                    tx,
                    owner_id=owner,
                    challenge_digest=challenge_digest("other"),
                    consumption_id=second.consumption_id,
                )
            assert repo.get_consumed_challenge(tx, owner_id=owner, challenge_digest=digest) == first
            assert (
                repo.get_consumed_challenge(tx, owner_id=owner, challenge_digest="f" * 64)
                is None
            )
    finally:
        next(fixture, None)


def test_concurrent_revocation_never_returns_active():
    fixture = standalone_database()
    database = next(fixture)
    try:
        repo = VerificationCredentialRepository()
        owner, credential = uid(), uid()
        with database.transaction() as tx:
            enroll(tx, repo, owner=owner, credential=credential)
            schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
        outcomes: dict[str, object] = {}
        revoked_ready = threading.Event()
        release = threading.Event()
        verifier_started = threading.Event()
        sqlite = os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"].startswith("sqlite:")

        def revoker():
            connection = independent_connection(schema)
            pool = ConnectionPool(Pool(connection))
            try:
                with PlaneDatabase(pool).transaction() as tx:
                    if not sqlite:
                        observed = tx.fetch_one("SELECT pg_backend_pid() AS pid")
                        outcomes["revoker_pid"] = observed["pid"]
                    repo.revoke_credential(
                        tx, owner_id=owner, credential_id=credential, expected_record_version=1
                    )
                    revoked_ready.set()
                    assert release.wait(30)
                outcomes["revocation"] = "committed"
            finally:
                pool.close()
                connection.close()

        def verifier():
            connection = independent_connection(schema)
            pool = ConnectionPool(Pool(connection))
            try:
                with PlaneDatabase(pool).transaction() as tx:
                    if not sqlite:
                        observed = tx.fetch_one("SELECT pg_backend_pid() AS pid")
                        outcomes["verifier_pid"] = observed["pid"]
                    verifier_started.set()
                    repo.require_active_credential(tx, owner_id=owner, credential_id=credential)
                outcomes["verifier"] = "active"
            except VerificationCredentialRevokedError:
                outcomes["verifier"] = "revoked"
            finally:
                pool.close()
                connection.close()

        revocation_thread = threading.Thread(target=revoker)
        revocation_thread.start()
        assert revoked_ready.wait(30)
        verification_thread = threading.Thread(target=verifier)
        verification_thread.start()
        assert verifier_started.wait(30)
        if not sqlite:
            wait_for_blocked_statement(database, outcomes["verifier_pid"], outcomes["revoker_pid"])
        release.set()
        revocation_thread.join(30)
        verification_thread.join(30)
        assert outcomes["revocation"] == "committed"
        assert outcomes["verifier"] == "revoked"
        with database.transaction() as tx:
            final = repo.get_credential(tx, owner_id=owner, credential_id=credential)
            assert final.credential_state == "revoked"
            assert final.record_version == 2
    finally:
        next(fixture, None)


def test_invalid_inputs_and_foreign_rows_fail_closed():
    fixture = standalone_database()
    database = next(fixture)
    try:
        repo = VerificationCredentialRepository()
        owner, credential = uid(), uid()
        with database.transaction() as tx:
            enroll(tx, repo, owner=owner, credential=credential)
            for changes in (
                {"owner_id": ""},
                {"credential_id": ""},
                {"device_id": ""},
                {"algorithm": ""},
                {"key_format": "pem"},
                {"key_format": ""},
                {"public_key": ""},
            ):
                with pytest.raises(RepositoryValidationError):
                    repo.enroll_credential(
                        tx,
                        owner_id=changes.get("owner_id", owner),
                        credential_id=changes.get("credential_id", credential),
                        device_id=changes.get("device_id", "synthetic-mac-device"),
                        algorithm=changes.get("algorithm", ALGORITHM),
                        key_format=changes.get("key_format", "spki"),
                        public_key=changes.get("public_key", synthetic_key()),
                    )
            with pytest.raises(RepositoryValidationError):
                repo.enroll_credential(
                    tx,
                    owner_id=owner,
                    credential_id=credential,
                    device_id="synthetic-mac-device",
                    algorithm=ALGORITHM,
                    key_format="spki",
                    public_key="x" * 8193,
                )
            with pytest.raises(RepositoryValidationError):
                repo.consume_challenge(
                    tx, owner_id=owner, challenge_digest="UPPER" + "0" * 58, consumption_id=uid()
                )
            with pytest.raises(RepositoryValidationError):
                repo.consume_challenge(
                    tx, owner_id=owner, challenge_digest="z" * 64, consumption_id=uid()
                )
            with pytest.raises(RepositoryValidationError):
                repo.revoke_credential(
                    tx, owner_id=owner, credential_id=credential, expected_record_version=0
                )
            foreign = uid()
            with pytest.raises(RepositoryNotFoundError):
                repo.get_credential(tx, owner_id=foreign, credential_id=credential)
            with pytest.raises(RepositoryNotFoundError):
                repo.require_active_credential(tx, owner_id=foreign, credential_id=credential)
            with pytest.raises(RepositoryNotFoundError):
                repo.revoke_credential(
                    tx, owner_id=foreign, credential_id=credential, expected_record_version=1
                )
            repo.consume_challenge(
                tx,
                owner_id=owner,
                challenge_digest=challenge_digest("contract-digest"),
                consumption_id="contract-consumption",
            )
            with pytest.raises(RepositoryConflictError):
                repo.consume_challenge(
                    tx,
                    owner_id=foreign,
                    challenge_digest=challenge_digest("foreign-digest"),
                    consumption_id="contract-consumption",
                )
        with database.transaction() as tx:
            assert repo.get_credential(tx, owner_id=owner, credential_id=credential) is not None
    finally:
        next(fixture, None)


def test_retention_prune_is_bounded_and_owner_scoped():
    fixture = standalone_database()
    database = next(fixture)
    try:
        repo = VerificationCredentialRepository()
        owner, other = uid(), uid()
        with database.transaction() as tx:
            for index in range(3):
                repo.consume_challenge(
                    tx,
                    owner_id=owner,
                    challenge_digest=challenge_digest(f"prune-{index}"),
                    consumption_id=uid(),
                )
            repo.consume_challenge(
                tx,
                owner_id=other,
                challenge_digest=challenge_digest("prune-foreign"),
                consumption_id=uid(),
            )
            assert repo.prune_consumed_challenges(tx, owner_id=owner, before_epoch=0) == 0
            with pytest.raises(RepositoryValidationError):
                repo.prune_consumed_challenges(
                    tx, owner_id=owner, before_epoch=4_102_444_800, limit=501
                )
            assert (
                repo.prune_consumed_challenges(
                    tx, owner_id=owner, before_epoch=4_102_444_800, limit=2
                )
                == 2
            )
            assert (
                repo.get_consumed_challenge(
                    tx, owner_id=other, challenge_digest=challenge_digest("prune-foreign")
                )
                is not None
            )
        with database.transaction() as tx:
            assert repo.prune_consumed_challenges(
                tx, owner_id=owner, before_epoch=4_102_444_800
            ) == 1
            assert (
                repo.get_consumed_challenge(
                    tx, owner_id=other, challenge_digest=challenge_digest("prune-foreign")
                )
                is not None
            )
            assert repo.prune_consumed_challenges(
                tx, owner_id=other, before_epoch=4_102_444_800
            ) == 1
    finally:
        next(fixture, None)
