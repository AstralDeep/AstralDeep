"""Current-member assertions retain mesh then member locks without mutation.
Real PostgreSQL tests reject stale observations, foreign owners and revocation
races before host credential publication."""

from __future__ import annotations

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid
from test_mesh_enrollment_postgres import bootstrap

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)


@pytest.mark.parametrize("fence", [None, "membership", "revocation", "member"])
def test_current_member_assertion_never_mutates_records(database, fence):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        fields = dict(
            expected_membership_epoch=initial.membership_epoch,
            expected_revocation_epoch=initial.revocation_epoch,
            expected_member_version=member.record_version,
        )
        assert repo.assert_current_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id, **fields
        ) == (initial, member)
        assert repo.assert_current_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id
        ) == (initial, member)
        if fence is not None:
            field = {
                "membership": "expected_membership_epoch",
                "revocation": "expected_revocation_epoch",
                "member": "expected_member_version",
            }[fence]
            with pytest.raises(RepositoryConflictError):
                repo.assert_current_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=member.member_id,
                    **(fields | {field: 999}),
                )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == initial
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id) == member
        )


@pytest.mark.parametrize(
    "field", ["expected_membership_epoch", "expected_revocation_epoch", "expected_member_version"]
)
@pytest.mark.parametrize("bad", [True, -1, 2**63, "1", 1.5])
def test_current_member_rejects_noninteger_fences(database, field, bad):
    with database.transaction() as tx:
        repo, owner, mesh, _, member = bootstrap(tx)
        with pytest.raises(RepositoryValidationError):
            repo.assert_current_member(
                tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id, **{field: bad}
            )


def test_current_member_owner_and_identity_are_scoped(database):
    with database.transaction() as tx:
        repo, owner, mesh, _, member = bootstrap(tx)
        for values in (
            dict(owner_id=uid(), mesh_id=mesh, member_id=member.member_id),
            dict(owner_id=owner, mesh_id=uid(), member_id=member.member_id),
            dict(owner_id=owner, mesh_id=mesh, member_id=uid()),
        ):
            with pytest.raises(RepositoryNotFoundError):
                repo.assert_current_member(tx, **values)


def test_current_member_holds_lock_until_commit_and_then_refuses_revoked_observation(database):
    from tests.fixtures.database_errors import LOCK_ERRORS

    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    with database.transaction() as tx:
        observed = repo.assert_current_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id
        )
        with (
            independent_database(schema) as second,
            pytest.raises(LOCK_ERRORS),
            second.transaction() as other,
        ):
            other.execute("SET LOCAL lock_timeout = '50ms'")
            repo.revoke_member(
                other,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                revocation_id=uid(),
                expected_mesh_version=initial.record_version,
                expected_member_version=member.record_version,
            )
        assert observed == (initial, member)
    with database.transaction() as tx:
        repo.revoke_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=member.member_id,
            revocation_id=uid(),
            expected_mesh_version=initial.record_version,
            expected_member_version=member.record_version,
        )
    with database.transaction() as tx, pytest.raises(RepositoryConflictError):
        repo.assert_current_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=member.member_id,
            expected_member_version=observed[1].record_version,
        )
