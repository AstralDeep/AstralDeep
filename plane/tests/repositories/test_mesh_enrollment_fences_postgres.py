"""Real PostgreSQL owner, current-revision and rollback regressions for mesh enrollment."""

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import parallel_transactions, uid
from test_mesh_enrollment_postgres import EXPIRES, ISSUED, bootstrap, digest

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.mesh_enrollment import MeshEnrollmentRepository


def consumed_invitation(tx, repo, owner, mesh):
    invitation = repo.issue_invitation(
        tx,
        owner_id=owner,
        mesh_id=mesh,
        invitation_id=uid(),
        member_kind="device",
        invitation_digest=digest("synthetic-fence"),
        issued_at=ISSUED,
        expires_at=EXPIRES,
    )
    return repo.consume_invitation(
        tx,
        owner_id=owner,
        invitation_id=invitation.invitation_id,
        invitation_digest=invitation.invitation_digest,
        as_of=ISSUED + 1,
    )


@pytest.mark.parametrize("kind", ["invitation", "challenge"])
def test_issuance_denies_foreign_and_missing_mesh_without_state(database, kind):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        foreign = uid()
        for requested_mesh in (mesh, uid()):
            common = dict(
                owner_id=foreign, mesh_id=requested_mesh, issued_at=ISSUED, expires_at=EXPIRES
            )
            with pytest.raises(RepositoryNotFoundError):
                if kind == "invitation":
                    repo.issue_invitation(
                        tx,
                        **common,
                        invitation_id=uid(),
                        member_kind="device",
                        invitation_digest=digest("foreign"),
                    )
                else:
                    repo.issue_enrollment_challenge(
                        tx,
                        **common,
                        member_id=member.member_id,
                        challenge_id=uid(),
                        challenge_digest=digest("foreign"),
                    )
        assert repo.list_invitations(tx, owner_id=foreign, mesh_id=mesh) == ()
        assert repo.list_enrollment_challenges(tx, owner_id=foreign, mesh_id=mesh) == ()
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == initial


def test_bootstrap_cannot_append_member_and_concurrent_exact_replays_are_idempotent(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        with pytest.raises(RepositoryConflictError):
            bootstrap(tx, repo, owner=owner, mesh=mesh)
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == initial
    operations = tuple(
        lambda tx: bootstrap(tx, repo, owner=owner, mesh=mesh, member=member.member_id)
        for _ in range(3)
    )
    results = parallel_transactions(database, operations)
    assert all(value[3].record_version == 1 for value in results)
    assert all(value[4].membership_epoch == 1 for value in results)
    with database.transaction() as tx:
        assert len(repo.list_members(tx, owner_id=owner, mesh_id=mesh)) == 1


@pytest.mark.parametrize("transition", ["retire", "revoke"])
def test_stale_activation_after_member_transition_does_not_reactivate(database, transition):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        if transition == "retire":
            current_member = repo.retire_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                expected_record_version=member.record_version,
            )
        else:
            current_member, _ = repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                revocation_id=uid(),
                expected_mesh_version=initial.record_version,
                expected_member_version=member.record_version,
            )
        current_mesh = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        with pytest.raises(RepositoryConflictError):
            repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                member_kind="device",
                expected_mesh_version=current_mesh.record_version,
                expected_member_version=member.record_version,
            )
        with pytest.raises(RepositoryConflictError):
            repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                member_kind="device",
                expected_mesh_version=current_mesh.record_version,
                expected_member_version=0,
            )
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id)
            == current_member
        )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == current_mesh


def test_stale_mesh_activation_preserves_outer_caller_write(database):
    repo = MeshEnrollmentRepository()
    with database.transaction() as tx:
        _, owner, mesh, initial, _ = bootstrap(tx, repo)
        changed = repo.rename_mesh(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            display_name="caller-owned update",
            expected_record_version=initial.record_version,
        )
        with pytest.raises(RepositoryConflictError):
            repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=uid(),
                member_kind="device",
                expected_mesh_version=initial.record_version,
                expected_member_version=0,
            )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == changed
    with database.transaction() as tx:
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh).display_name == "caller-owned update"
        assert len(repo.list_members(tx, owner_id=owner, mesh_id=mesh)) == 1


@pytest.mark.parametrize("denial", ["mesh", "member", "invitation", "future", "expiry", "missing"])
def test_confirmation_denials_restore_invitation_epoch_and_member(database, denial):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        invitation = consumed_invitation(tx, repo, owner, mesh)
        expected_mesh = initial.record_version
        expected_member = member.record_version
        target_member = member.member_id
        observed = ISSUED + 2
        if denial == "mesh":
            repo.rename_mesh(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                display_name="changed",
                expected_record_version=initial.record_version,
            )
        elif denial == "member":
            repo.retire_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                expected_record_version=member.record_version,
            )
        elif denial == "invitation":
            repo.revoke_invitation(tx, owner_id=owner, invitation_id=invitation.invitation_id)
        elif denial == "future":
            observed = ISSUED
        elif denial == "expiry":
            observed = EXPIRES
        elif denial == "missing":
            target_member = uid()
        before_mesh = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        before_member = repo.get_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id
        )
        before_invitation = repo.get_invitation(
            tx, owner_id=owner, invitation_id=invitation.invitation_id
        )
        with pytest.raises(RepositoryConflictError):
            repo.confirm_invitation(
                tx,
                owner_id=owner,
                invitation_id=invitation.invitation_id,
                member_id=target_member,
                expected_mesh_version=expected_mesh,
                expected_member_version=expected_member,
                expected_invitation_version=invitation.record_version,
                as_of=observed,
            )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == before_mesh
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id)
            == before_member
        )
        assert (
            repo.get_invitation(tx, owner_id=owner, invitation_id=invitation.invitation_id)
            == before_invitation
        )


def test_consumed_invitation_cannot_reactivate_after_revocation(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        invitation = consumed_invitation(tx, repo, owner, mesh)
        revoked, _ = repo.revoke_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=member.member_id,
            revocation_id=uid(),
            expected_mesh_version=initial.record_version,
            expected_member_version=member.record_version,
        )
        current_mesh = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        for expected_mesh in (initial.record_version, current_mesh.record_version):
            with pytest.raises(RepositoryConflictError):
                repo.confirm_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation.invitation_id,
                    member_id=member.member_id,
                    expected_mesh_version=expected_mesh,
                    expected_member_version=member.record_version,
                    expected_invitation_version=invitation.record_version,
                    as_of=ISSUED + 2,
                )
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id) == revoked
        )
        assert (
            repo.get_invitation(tx, owner_id=owner, invitation_id=invitation.invitation_id)
            == invitation
        )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == current_mesh


def test_concurrent_confirmations_admit_one_and_leave_no_partial_loser(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, _ = bootstrap(tx)
        invitation = consumed_invitation(tx, repo, owner, mesh)
    results = parallel_transactions(
        database,
        tuple(
            lambda tx, index=index: repo.confirm_invitation(
                tx,
                owner_id=owner,
                invitation_id=invitation.invitation_id,
                member_id=f"confirm-{index}",
                expected_mesh_version=initial.record_version,
                expected_member_version=0,
                expected_invitation_version=invitation.record_version,
                as_of=ISSUED + 2,
            )
            for index in range(2)
        ),
    )
    assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1
    with database.transaction() as tx:
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh).membership_epoch == 2
        assert len(repo.list_members(tx, owner_id=owner, mesh_id=mesh)) == 2
        assert (
            repo.get_invitation(
                tx, owner_id=owner, invitation_id=invitation.invitation_id
            ).invitation_state
            == "confirmed"
        )


def test_activation_and_revocation_share_mesh_first_order_and_same_revision_winner(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
    results = parallel_transactions(
        database,
        (
            lambda tx: repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                member_kind="device",
                expected_mesh_version=initial.record_version,
                expected_member_version=member.record_version,
            ),
            lambda tx: repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                revocation_id=uid(),
                expected_mesh_version=initial.record_version,
                expected_member_version=member.record_version,
            ),
        ),
    )
    assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1
    with database.transaction() as tx:
        current_mesh = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        current_member = repo.get_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id
        )
        assert current_mesh.record_version == 2
        assert current_member.record_version == 2
        if current_member.member_status == "revoked":
            assert current_mesh.revocation_epoch == 1
            assert current_mesh.membership_epoch == 1
        else:
            assert current_mesh.revocation_epoch == 0
            assert current_mesh.membership_epoch == 2


def test_duplicate_revocation_id_rolls_back_member_and_epoch(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, first = bootstrap(tx)
        second = repo.activate_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=uid(),
            member_kind="device",
            expected_mesh_version=initial.record_version,
            expected_member_version=0,
        )
        current = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        revocation_id = uid()
        repo.revoke_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=first.member_id,
            revocation_id=revocation_id,
            expected_mesh_version=current.record_version,
            expected_member_version=first.record_version,
        )
        current = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        with pytest.raises(RepositoryConflictError):
            repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=second.member_id,
                revocation_id=revocation_id,
                expected_mesh_version=current.record_version,
                expected_member_version=second.record_version,
            )
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == current
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=second.member_id) == second
        )


@pytest.mark.parametrize("kind", ["invitation", "challenge"])
def test_proof_and_consumption_reject_before_issue_and_allow_issue_boundary(database, kind):
    with database.transaction() as tx:
        repo, owner, mesh, _, member = bootstrap(tx)
        if kind == "invitation":
            record = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="device",
                invitation_digest=digest("window"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )

            def transition(observed):
                return repo.consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=record.invitation_id,
                    invitation_digest=record.invitation_digest,
                    as_of=observed,
                )
        else:
            record = repo.issue_enrollment_challenge(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member.member_id,
                challenge_id=uid(),
                challenge_digest=digest("window"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )

            def transition(observed):
                return repo.prove_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    challenge_id=record.challenge_id,
                    challenge_digest=record.challenge_digest,
                    as_of=observed,
                )

        with pytest.raises(RepositoryConflictError):
            transition(ISSUED - 1)
        assert transition(ISSUED).record_version == 2


@pytest.mark.parametrize("target", ["member", "invitation"])
def test_confirmation_and_revocation_race_has_one_complete_winner(database, target):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        invitation = consumed_invitation(tx, repo, owner, mesh)

    def confirmation(tx):
        return repo.confirm_invitation(
            tx,
            owner_id=owner,
            invitation_id=invitation.invitation_id,
            member_id=member.member_id,
            expected_mesh_version=initial.record_version,
            expected_member_version=member.record_version,
            expected_invitation_version=invitation.record_version,
            as_of=ISSUED + 2,
        )

    def revocation(tx):
        if target == "invitation":
            return repo.revoke_invitation(
                tx, owner_id=owner, invitation_id=invitation.invitation_id
            )
        return repo.revoke_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=member.member_id,
            revocation_id=uid(),
            expected_mesh_version=initial.record_version,
            expected_member_version=member.record_version,
        )

    results = parallel_transactions(database, (confirmation, revocation))
    assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1
    with database.transaction() as tx:
        current_invitation = repo.get_invitation(
            tx, owner_id=owner, invitation_id=invitation.invitation_id
        )
        current_mesh = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        current_member = repo.get_member(
            tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id
        )
        if current_invitation.invitation_state == "confirmed":
            assert current_mesh.membership_epoch == 2
            assert current_mesh.revocation_epoch == 0
            assert current_member.member_status == "active"
            assert current_member.record_version == 2
        else:
            assert current_mesh.membership_epoch == 1
            if target == "member":
                assert current_invitation == invitation
                assert current_member.member_status == "revoked"
                assert current_mesh.revocation_epoch == 1
            else:
                assert current_invitation.invitation_state == "revoked"
                assert current_member == member
                assert current_mesh == initial


def test_successful_confirmation_and_revocation_remain_caller_rollback_owned(database):
    with database.transaction() as tx:
        repo, owner, mesh, initial, member = bootstrap(tx)
        invitation = consumed_invitation(tx, repo, owner, mesh)
    with pytest.raises(RuntimeError), database.transaction() as tx:
        _, activated = repo.confirm_invitation(
            tx,
            owner_id=owner,
            invitation_id=invitation.invitation_id,
            member_id=member.member_id,
            expected_mesh_version=initial.record_version,
            expected_member_version=member.record_version,
            expected_invitation_version=invitation.record_version,
            as_of=ISSUED + 2,
        )
        current = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
        repo.revoke_member(
            tx,
            owner_id=owner,
            mesh_id=mesh,
            member_id=member.member_id,
            revocation_id=uid(),
            expected_mesh_version=current.record_version,
            expected_member_version=activated.record_version,
        )
        raise RuntimeError("caller abort")
    with database.transaction() as tx:
        assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh) == initial
        assert (
            repo.get_member(tx, owner_id=owner, mesh_id=mesh, member_id=member.member_id) == member
        )
        assert (
            repo.get_invitation(tx, owner_id=owner, invitation_id=invitation.invitation_id)
            == invitation
        )
        assert repo.list_revocations(tx, owner_id=owner, mesh_id=mesh) == ()
