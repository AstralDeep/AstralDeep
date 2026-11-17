"""Tests for astralplane.repositories.preferences and secrets: TypeSafe credential
storage and data-sharing acknowledgment, covering owner isolation,
fingerprint-conditioned outcomes, and first-consent-preserving upsert.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_assignments_postgres import database as database

from astralplane.repositories import (
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.preferences import (
    DataSharingAcknowledgmentRepository,
)
from astralplane.repositories.secrets import (
    TYPESAFE_OUTCOMES,
    EncryptedTypeSafeCredentialRecord,
    EncryptedTypeSafeCredentialRepository,
)
from tests.repositories._support import Result, ScriptedTransaction

NOW = datetime(2026, 9, 17, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
FINGERPRINT = "0123456789ab"
OTHER_FINGERPRINT = "ba9876543210"
NOTICE = "2026-09-17.1"


def _credential_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user_id": "owner-1",
        "api_key_enc": b"opaque-fernet-token",
        "key_fingerprint": FINGERPRINT,
        "last_verified_at": NOW,
        "last_verification_outcome": "valid",
        "last_outcome_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def test_read_is_owner_scoped_and_ciphertext_is_redacted() -> None:
    transaction = ScriptedTransaction(one=[_credential_row()])

    record = EncryptedTypeSafeCredentialRepository().get_user(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
    )

    assert record is not None
    assert record.owner_id == "owner-1"
    assert record.api_key_ciphertext == "opaque-fernet-token"
    assert "opaque-fernet-token" not in repr(record)
    assert "WHERE user_id = %s" in transaction.fetch_sql()
    assert transaction.calls[0][2] == ("owner-1",)


def test_missing_row_reads_as_none() -> None:
    transaction = ScriptedTransaction(one=[None])
    assert (
        EncryptedTypeSafeCredentialRepository().get_user(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
        )
        is None
    )


def test_locked_read_uses_for_update() -> None:
    transaction = ScriptedTransaction(one=[_credential_row()])
    EncryptedTypeSafeCredentialRepository().get_user_for_update(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
    )
    assert "FOR UPDATE" in transaction.fetch_sql()


def test_null_ciphertext_is_a_data_error_not_a_none_key() -> None:
    transaction = ScriptedTransaction(one=[_credential_row(api_key_enc=None)])
    with pytest.raises(RepositoryDataError):
        EncryptedTypeSafeCredentialRepository().get_user(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
        )


@pytest.mark.parametrize("bad", ["", "not-hex-here", "0123456789AB", "0123456789abc"])
def test_malformed_fingerprints_are_rejected(bad: str) -> None:
    transaction = ScriptedTransaction(one=[_credential_row(key_fingerprint=bad)])
    with pytest.raises((RepositoryValidationError, RepositoryDataError)):
        EncryptedTypeSafeCredentialRepository().get_user(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
        )


def test_unknown_outcome_in_a_row_is_rejected() -> None:
    transaction = ScriptedTransaction(
        one=[_credential_row(last_verification_outcome="probably-fine")]
    )
    with pytest.raises(RepositoryValidationError):
        EncryptedTypeSafeCredentialRepository().get_user(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
        )


def test_upsert_lands_valid_and_binds_the_fingerprint() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_credential_row(),))]
    )
    record = EncryptedTypeSafeCredentialRepository().upsert_user(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        api_key_ciphertext="opaque-fernet-token",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    statement = transaction.fetch_sql()
    assert "INSERT INTO user_typesafe_credential" in statement
    assert "ON CONFLICT (user_id) DO UPDATE SET" in statement
    assert "last_verification_outcome = 'valid'" in statement
    assert record.last_verification_outcome == "valid"


def test_upsert_requires_a_timezone_aware_verification_time() -> None:
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        EncryptedTypeSafeCredentialRepository().upsert_user(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            api_key_ciphertext="opaque",
            key_fingerprint=FINGERPRINT,
            verified_at=datetime(2026, 9, 17),
        )


def test_record_outcome_conditions_on_the_fingerprint() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=1)])
    applied = EncryptedTypeSafeCredentialRepository().record_outcome(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        outcome="rejected",
        at=NOW,
        expected_fingerprint=FINGERPRINT,
    )
    statement = transaction.fetch_sql()
    assert applied is True
    assert "WHERE user_id = %s AND key_fingerprint = %s" in statement
    assert transaction.calls[0][2][-2:] == ("owner-1", FINGERPRINT)


def test_record_outcome_refuses_the_initial_state() -> None:
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        EncryptedTypeSafeCredentialRepository().record_outcome(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            outcome="unverified",
            at=NOW,
            expected_fingerprint=FINGERPRINT,
        )


def test_record_outcome_refuses_an_undeclared_outcome() -> None:
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        EncryptedTypeSafeCredentialRepository().record_outcome(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            outcome="expired",
            at=NOW,
            expected_fingerprint=FINGERPRINT,
        )


def test_delete_reports_whether_a_row_existed() -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    present = ScriptedTransaction(execute=[Result(rowcount=1)])
    absent = ScriptedTransaction(execute=[Result(rowcount=0)])
    assert repository.delete_user(present, owner_id="owner-1") is True  # type: ignore[arg-type]
    assert repository.delete_user(absent, owner_id="owner-1") is False  # type: ignore[arg-type]


def test_multi_row_writes_are_refused_as_data_errors() -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    with pytest.raises(RepositoryDataError):
        repository.delete_user(
            ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
            owner_id="owner-1",
        )
    with pytest.raises(RepositoryDataError):
        repository.record_outcome(
            ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            outcome="valid",
            at=NOW,
            expected_fingerprint=FINGERPRINT,
        )


def test_declared_outcomes_match_the_column_check() -> None:
    assert TYPESAFE_OUTCOMES == ("unverified", "valid", "rejected", "unavailable")


def test_record_repr_never_carries_the_ciphertext() -> None:
    record = EncryptedTypeSafeCredentialRecord(
        owner_id="owner-1",
        api_key_ciphertext="gAAAAA-secret",
        key_fingerprint=FINGERPRINT,
    )
    assert "gAAAAA-secret" not in repr(record)


def test_acknowledgment_upsert_preserves_the_first_time_in_sql() -> None:
    transaction = ScriptedTransaction(
        execute=[
            Result(
                returned_records=(
                    {
                        "user_id": "owner-1",
                        "notice_version": NOTICE,
                        "acknowledged_at": LATER,
                        "first_acknowledged_at": NOW,
                    },
                )
            )
        ]
    )
    record = DataSharingAcknowledgmentRepository().acknowledge(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        notice_version=NOTICE,
        at=LATER,
    )
    statement = transaction.fetch_sql()
    assert "INSERT INTO user_data_sharing_acknowledgment" in statement
    assert (
        "first_acknowledged_at =\n"
        "                    user_data_sharing_acknowledgment.first_acknowledged_at"
    ) in statement
    assert record.first_acknowledged_at == NOW
    assert record.acknowledged_at == LATER


def test_acknowledgment_requires_a_timezone_aware_time() -> None:
    with pytest.raises(RepositoryValidationError):
        DataSharingAcknowledgmentRepository().acknowledge(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            notice_version=NOTICE,
            at=datetime(2026, 9, 17),
        )


@pytest.fixture
def credentials(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM user_typesafe_credential")
        transaction.execute("DELETE FROM user_data_sharing_acknowledgment")
        yield transaction


def _owner() -> str:
    return "owner-" + uuid.uuid4().hex[:8]


@pytest.mark.postgresql_schema_catalog
def test_table_exists_at_089_001(credentials) -> None:
    row = credentials.fetch_one(
        "SELECT to_regclass('user_typesafe_credential') AS credential, "
        "to_regclass('user_data_sharing_acknowledgment') AS acknowledgment"
    )
    assert row["credential"] is not None
    assert row["acknowledgment"] is not None


@pytest.mark.postgresql_schema_catalog
def test_no_system_scope_table_exists(credentials) -> None:
    row = credentials.fetch_one(
        "SELECT to_regclass('system_typesafe_credential') AS system_table"
    )
    assert row["system_table"] is None


def test_round_trip_and_owner_isolation(credentials) -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    mine, theirs = _owner(), _owner()
    repository.upsert_user(
        credentials,
        owner_id=mine,
        api_key_ciphertext="gAAAAA-mine",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    repository.upsert_user(
        credentials,
        owner_id=theirs,
        api_key_ciphertext="gAAAAA-theirs",
        key_fingerprint=OTHER_FINGERPRINT,
        verified_at=NOW,
    )

    read = repository.get_user(credentials, owner_id=mine)
    assert read is not None
    assert read.api_key_ciphertext == "gAAAAA-mine"
    assert read.last_verification_outcome == "valid"
    assert read.last_verified_at == NOW

    assert repository.delete_user(credentials, owner_id=mine) is True
    assert repository.get_user(credentials, owner_id=mine) is None
    other = repository.get_user(credentials, owner_id=theirs)
    assert other is not None and other.api_key_ciphertext == "gAAAAA-theirs"


def test_resaving_replaces_the_key_and_clears_a_rejected_status(credentials) -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    owner = _owner()
    repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-first",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    repository.record_outcome(
        credentials,
        owner_id=owner,
        outcome="rejected",
        at=NOW,
        expected_fingerprint=FINGERPRINT,
    )
    assert repository.get_user(
        credentials, owner_id=owner
    ).last_verification_outcome == "rejected"

    repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-second",
        key_fingerprint=OTHER_FINGERPRINT,
        verified_at=LATER,
    )
    record = repository.get_user(credentials, owner_id=owner)
    assert record.api_key_ciphertext == "gAAAAA-second"
    assert record.key_fingerprint == OTHER_FINGERPRINT
    assert record.last_verification_outcome == "valid"


def test_a_stale_outcome_cannot_mark_a_newly_saved_key(credentials) -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    owner = _owner()
    repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-old",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-new",
        key_fingerprint=OTHER_FINGERPRINT,
        verified_at=LATER,
    )

    applied = repository.record_outcome(
        credentials,
        owner_id=owner,
        outcome="rejected",
        at=LATER,
        expected_fingerprint=FINGERPRINT,
    )

    assert applied is False
    record = repository.get_user(credentials, owner_id=owner)
    assert record.last_verification_outcome == "valid"


def test_outcome_updates_only_the_matching_owner(credentials) -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    mine, theirs = _owner(), _owner()
    for owner in (mine, theirs):
        repository.upsert_user(
            credentials,
            owner_id=owner,
            api_key_ciphertext="gAAAAA-" + owner,
            key_fingerprint=FINGERPRINT,
            verified_at=NOW,
        )
    repository.record_outcome(
        credentials,
        owner_id=mine,
        outcome="unavailable",
        at=LATER,
        expected_fingerprint=FINGERPRINT,
    )
    assert repository.get_user(
        credentials, owner_id=mine
    ).last_verification_outcome == "unavailable"
    assert repository.get_user(
        credentials, owner_id=theirs
    ).last_verification_outcome == "valid"


def test_a_valid_outcome_advances_last_verified_at(credentials) -> None:
    repository = EncryptedTypeSafeCredentialRepository()
    owner = _owner()
    repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-key",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    repository.record_outcome(
        credentials,
        owner_id=owner,
        outcome="unavailable",
        at=LATER,
        expected_fingerprint=FINGERPRINT,
    )
    assert repository.get_user(credentials, owner_id=owner).last_verified_at == NOW

    repository.record_outcome(
        credentials,
        owner_id=owner,
        outcome="valid",
        at=LATER,
        expected_fingerprint=FINGERPRINT,
    )
    assert repository.get_user(credentials, owner_id=owner).last_verified_at == LATER


@pytest.mark.postgresql_schema_catalog
def test_the_live_catalog_carries_the_declared_constraints(credentials) -> None:
    rows = credentials.fetch_all(
        "SELECT pg_get_constraintdef(constraint_record.oid) AS definition "
        "FROM pg_constraint AS constraint_record "
        "WHERE constraint_record.conrelid = 'user_typesafe_credential'::regclass "
        "AND constraint_record.contype = 'c'"
    )
    definitions = " ".join(str(row["definition"]) for row in rows)
    for outcome in TYPESAFE_OUTCOMES:
        assert "'" + outcome + "'" in definitions
    assert "key_fingerprint" in definitions


def test_acknowledgment_preserves_the_first_time_across_versions(credentials) -> None:
    repository = DataSharingAcknowledgmentRepository()
    owner = _owner()
    first = repository.acknowledge(
        credentials, owner_id=owner, notice_version=NOTICE, at=NOW
    )
    assert first.first_acknowledged_at == NOW

    repeated = repository.acknowledge(
        credentials, owner_id=owner, notice_version=NOTICE, at=LATER
    )
    assert repeated.first_acknowledged_at == NOW
    assert repeated.acknowledged_at == LATER

    bumped = repository.acknowledge(
        credentials, owner_id=owner, notice_version="2026-10-01.1", at=LATER
    )
    assert bumped.first_acknowledged_at == NOW
    assert bumped.notice_version == "2026-10-01.1"


def test_acknowledgment_is_owner_scoped(credentials) -> None:
    repository = DataSharingAcknowledgmentRepository()
    mine, theirs = _owner(), _owner()
    repository.acknowledge(credentials, owner_id=mine, notice_version=NOTICE, at=NOW)

    assert repository.has_acknowledged(
        credentials, owner_id=mine, notice_version=NOTICE
    )
    assert not repository.has_acknowledged(
        credentials, owner_id=theirs, notice_version=NOTICE
    )
    assert repository.get_user(credentials, owner_id=theirs) is None


def test_a_version_bump_requires_a_new_acknowledgment(credentials) -> None:
    repository = DataSharingAcknowledgmentRepository()
    owner = _owner()
    repository.acknowledge(credentials, owner_id=owner, notice_version=NOTICE, at=NOW)

    assert repository.has_acknowledged(
        credentials, owner_id=owner, notice_version=NOTICE
    )
    assert not repository.has_acknowledged(
        credentials, owner_id=owner, notice_version="2026-10-01.1"
    )


def test_clearing_a_credential_leaves_the_acknowledgment(credentials) -> None:
    credential_repository = EncryptedTypeSafeCredentialRepository()
    acknowledgments = DataSharingAcknowledgmentRepository()
    owner = _owner()
    acknowledgments.acknowledge(
        credentials, owner_id=owner, notice_version=NOTICE, at=NOW
    )
    credential_repository.upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-key",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )

    credential_repository.delete_user(credentials, owner_id=owner)

    assert credential_repository.get_user(credentials, owner_id=owner) is None
    assert acknowledgments.get_user(credentials, owner_id=owner) is not None


@pytest.mark.postgresql_schema_catalog
def test_rollback_drops_both_tables_and_leaves_the_rest(credentials) -> None:
    owner = _owner()
    EncryptedTypeSafeCredentialRepository().upsert_user(
        credentials,
        owner_id=owner,
        api_key_ciphertext="gAAAAA-key",
        key_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    credentials.execute("DROP TABLE user_typesafe_credential")
    credentials.execute("DROP TABLE user_data_sharing_acknowledgment")
    credentials.execute(
        "UPDATE schema_meta SET value = %s WHERE key = 'revision'", ("088.008",)
    )

    row = credentials.fetch_one(
        "SELECT to_regclass('user_typesafe_credential') AS credential, "
        "to_regclass('user_llm_config') AS llm_config, "
        "(SELECT value FROM schema_meta WHERE key = 'revision') AS revision"
    )
    assert row["credential"] is None
    assert row["llm_config"] is not None
    assert row["revision"] == "088.008"
