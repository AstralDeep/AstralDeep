"""Real-PostgreSQL tests for astralplane.repositories.offline_grants and history: opaque
refresh replacement respects every durable fence, finite allowance charges exactly
once, and session rotation/delete return the final credential.
"""

import uuid
from dataclasses import replace

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.history import SessionRecord
from tests.integration.test_catalog_caller_rollback import catalog_database as _catalog_database

catalog_database = _catalog_database


def test_opaque_refresh_replacement_respects_every_durable_fence(catalog_database):
    database = catalog_database.database
    repo = catalog_database.catalog.offline_grants
    gid = str(uuid.uuid4())
    with database.transaction() as tx:
        repo.create_grant(tx, grant_id=gid, owner_id="cas-owner", agent_id=None,
                          encrypted_refresh_token=b"original", issued_at=100, expires_at=1000)

    def rotate(tx, **changes):
        args = dict(owner_id="cas-owner", grant_id=gid,
                    expected_encrypted_refresh_token=b"original",
                    encrypted_refresh_token=b"reference", as_of=500)
        args.update(changes)
        return repo.replace_refresh_token_if_current(tx, **args)

    with database.transaction() as tx:
        assert rotate(tx, owner_id="other-owner") is None
        assert rotate(tx, grant_id=str(uuid.uuid4())) is None
        assert rotate(tx, expected_encrypted_refresh_token=b"stale") is None
        assert rotate(tx, as_of=1000) is None
    with pytest.raises(RuntimeError, match="caller abort"), database.transaction() as tx:
        assert rotate(tx).encrypted_refresh_token == b"reference"
        raise RuntimeError("caller abort")
    with database.transaction() as tx:
        stored = repo.get_grant(tx, owner_id="cas-owner", grant_id=gid)
        assert stored.encrypted_refresh_token == b"original"
        assert rotate(tx).encrypted_refresh_token == b"reference"
    with database.transaction() as tx:
        assert rotate(tx) is None
        repo.revoke_grant(tx, owner_id="cas-owner", grant_id=gid, revoked_at=600)
    with database.transaction() as tx:
        assert rotate(tx, expected_encrypted_refresh_token=b"reference") is None
        assert not repo.get_grant(tx, owner_id="cas-owner", grant_id=gid).active


def test_finite_grant_allowance_charges_exactly_once_per_admission(catalog_database):
    database = catalog_database.database
    repo = catalog_database.catalog.offline_grants
    gid = str(uuid.uuid4())
    with database.transaction() as tx:
        created = repo.create_grant(
            tx,
            grant_id=gid,
            owner_id="allowance-owner",
            agent_id=None,
            encrypted_refresh_token=b"finite-grant-token",
            issued_at=100,
            expires_at=2_000_000_000,
            max_admissions=2,
        )
        assert created.max_admissions == 2
        assert created.consumed_admissions == 0
        assert created.admissions_remaining == 2
    with database.transaction() as tx:
        first = repo.consume_admission(tx, owner_id="allowance-owner", grant_id=gid, as_of=200)
        assert first.consumed_admissions == 1
        assert first.admissions_remaining == 1
    with database.transaction() as tx:
        second = repo.consume_admission(tx, owner_id="allowance-owner", grant_id=gid, as_of=300)
        assert second.consumed_admissions == 2
        assert second.admissions_remaining == 0
    with database.transaction() as tx, pytest.raises(
        RepositoryConflictError, match="offline grant allowance exhausted"
    ):
        repo.consume_admission(tx, owner_id="allowance-owner", grant_id=gid, as_of=400)
    unlimited_id = str(uuid.uuid4())
    with database.transaction() as tx:
        repo.create_grant(
            tx,
            grant_id=unlimited_id,
            owner_id="allowance-owner",
            agent_id=None,
            encrypted_refresh_token=b"unlimited-grant-token",
            issued_at=100,
            expires_at=2_000_000_000,
        )
    with database.transaction() as tx:
        for as_of in (150, 250, 350):
            record = repo.consume_admission(
                tx, owner_id="allowance-owner", grant_id=unlimited_id, as_of=as_of
            )
            assert record.max_admissions is None
            assert record.admissions_remaining is None


def test_session_rotation_and_deletion_return_the_final_credential(catalog_database):
    database = catalog_database.database
    repo = catalog_database.catalog.history.sessions
    sid = uuid.uuid4().hex
    initial = SessionRecord(sid, "owner", "access-old", "refresh-old", 1, 1000, 1, False, 1)
    with database.transaction() as tx:
        initial = repo.put(tx, initial)
    with database.transaction() as tx:
        latest = repo.compare_and_set_refresh(
            tx,
            replace(initial, refresh_token_ciphertext="refresh-new", last_refresh_at=2),
            expected_last_refresh_at=1,
        )
    with database.transaction() as tx:
        assert (
            repo.delete_and_return(
                tx, owner_id="other", session_id=sid, expected_incarnation_id=initial.incarnation_id
            )
            is None
        )
    with pytest.raises(RuntimeError, match="rollback"), database.transaction() as tx:
        assert (
            repo.delete_and_return(
                tx, owner_id="owner", session_id=sid, expected_incarnation_id=initial.incarnation_id
            )
            == latest
        )
        raise RuntimeError("rollback")
    with database.transaction() as tx:
        assert (
            repo.delete_and_return(
                tx, owner_id="owner", session_id=sid, expected_incarnation_id=initial.incarnation_id
            )
            == latest
        )
    with database.transaction() as tx:
        assert (
            repo.delete_and_return(
                tx, owner_id="owner", session_id=sid, expected_incarnation_id=initial.incarnation_id
            )
            is None
        )
