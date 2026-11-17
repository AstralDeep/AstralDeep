"""Scripted failure-path coverage for the verification credential repository: validation,
capacity, replay, and fence denials fail closed with typed errors before any caller
transaction state is touched.
"""

from __future__ import annotations

import pytest

from astralplane import create_verification_credential_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.verification_credentials import (
    VerificationChallengeAlreadyConsumedError,
    VerificationCredentialLimitError,
    VerificationCredentialRevokedError,
)
from tests.repositories._support import ScriptedTransaction

CREDENTIAL = "10000000-0000-4000-8000-000000000001"
OWNER = "verification-owner"


def credential_row(**changes):
    values = dict(
        credential_id=CREDENTIAL,
        owner_id=OWNER,
        device_id="synthetic-mac-device",
        algorithm="es256",
        key_format="spki",
        public_key="0" * 64,
        credential_state="active",
        record_version=1,
        enrolled_at=10,
        revoked_at=None,
        updated_at=10,
    )
    values.update(changes)
    return values


def consumption_row(**changes):
    values = dict(
        consumption_id="consumption-1",
        owner_id=OWNER,
        challenge_digest="a" * 64,
        consumed_at=10,
    )
    values.update(changes)
    return values


def enroll(repo, transaction, **changes):
    return repo.enroll_credential(
        transaction,
        owner_id=changes.pop("owner_id", OWNER),
        credential_id=changes.pop("credential_id", CREDENTIAL),
        device_id=changes.pop("device_id", "synthetic-mac-device"),
        algorithm=changes.pop("algorithm", "es256"),
        key_format=changes.pop("key_format", "spki"),
        public_key=changes.pop("public_key", "0" * 64),
    )


def test_enroll_validation_fails_before_any_statement():
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        enroll(create_verification_credential_repository(), transaction, owner_id="")
    with pytest.raises(RepositoryValidationError):
        enroll(create_verification_credential_repository(), transaction, key_format="pem")
    with pytest.raises(RepositoryValidationError):
        enroll(create_verification_credential_repository(), transaction, credential_id="")
    assert transaction.calls == []


def test_enroll_at_capacity_conflicts_before_insert():
    transaction = ScriptedTransaction(one=[{"pg_advisory_xact_lock": None}, {"total": 32}, None])
    with pytest.raises(VerificationCredentialLimitError, match="capacity"):
        enroll(create_verification_credential_repository(), transaction)
    assert not transaction.calls[-1][1].startswith("INSERT")


def test_enroll_replay_paths_stay_atomic():
    repo = create_verification_credential_repository()
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}, {"total": 0}, credential_row(), None]
    )
    record = enroll(repo, transaction)
    assert record.credential_id == CREDENTIAL
    transaction = ScriptedTransaction(
        one=[
            {"pg_advisory_xact_lock": None},
            {"total": 0},
            credential_row(public_key="1" * 64),
            None,
        ]
    )
    with pytest.raises(RepositoryConflictError, match="replay changed immutable semantics"):
        enroll(repo, transaction)
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}, {"total": 0}, None, None]
    )
    with pytest.raises(RepositoryConflictError, match="bound to another owner"):
        enroll(repo, transaction)


def test_revocation_and_require_active_fences_fail_closed():
    repo = create_verification_credential_repository()
    transaction = ScriptedTransaction(one=[None, None])
    with pytest.raises(RepositoryNotFoundError):
        repo.revoke_credential(
            transaction, owner_id=OWNER, credential_id=CREDENTIAL, expected_record_version=1
        )
    transaction = ScriptedTransaction(
        one=[None, credential_row(owner_id=OWNER, credential_state="active")]
    )
    with pytest.raises(RepositoryConflictError, match="fence rejected the revocation"):
        repo.revoke_credential(
            transaction, owner_id=OWNER, credential_id=CREDENTIAL, expected_record_version=1
        )
    transaction = ScriptedTransaction(one=[credential_row(credential_state="revoked")])
    with pytest.raises(VerificationCredentialRevokedError):
        repo.require_active_credential(transaction, owner_id=OWNER, credential_id=CREDENTIAL)
    transaction = ScriptedTransaction(
        one=[credential_row(record_version=2, credential_state="revoked", revoked_at=10)]
    )
    with pytest.raises(VerificationCredentialRevokedError):
        repo.require_active_credential(
            transaction, owner_id=OWNER, credential_id=CREDENTIAL, expected_record_version=1
        )
    transaction = ScriptedTransaction(one=[credential_row(record_version=2)])
    with pytest.raises(RepositoryConflictError, match="version fence rejected the read"):
        repo.require_active_credential(
            transaction, owner_id=OWNER, credential_id=CREDENTIAL, expected_record_version=1
        )


def test_consume_challenge_replay_and_exactly_once_denials():
    repo = create_verification_credential_repository()
    transaction = ScriptedTransaction(one=[consumption_row()])
    record = repo.consume_challenge(
        transaction, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="consumption-1"
    )
    assert record.challenge_digest == "a" * 64
    transaction = ScriptedTransaction(one=[None, consumption_row()])
    replay = repo.consume_challenge(
        transaction, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="consumption-1"
    )
    assert replay == record
    transaction = ScriptedTransaction(one=[None, consumption_row(challenge_digest="b" * 64)])
    with pytest.raises(RepositoryConflictError, match="replay changed immutable semantics"):
        repo.consume_challenge(
            transaction, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="consumption-1"
        )
    transaction = ScriptedTransaction(one=[None, None, consumption_row()])
    with pytest.raises(VerificationChallengeAlreadyConsumedError):
        repo.consume_challenge(
            transaction, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="consumption-2"
        )
    transaction = ScriptedTransaction(one=[None, None, None])
    with pytest.raises(RepositoryConflictError, match="bound to another owner"):
        repo.consume_challenge(
            transaction, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="consumption-2"
        )
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repo.consume_challenge(
            transaction, owner_id=OWNER, challenge_digest="A" * 64, consumption_id="consumption-2"
        )
    assert transaction.calls == []


def test_prune_validates_cutoff_and_limit():
    repo = create_verification_credential_repository()
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repo.prune_consumed_challenges(transaction, owner_id=OWNER, before_epoch=-1)
    with pytest.raises(RepositoryValidationError):
        repo.prune_consumed_challenges(transaction, owner_id=OWNER, before_epoch=10, limit=501)
    assert transaction.calls == []
