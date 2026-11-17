"""Real-PostgreSQL behavior contract for astralplane.repositories.mesh_enrollment:
owner scoping, replay idempotency, monotonic epochs, atomic single-use invitations,
fenced transitions, and caller-owned rollback recovery.
"""

from __future__ import annotations

import hashlib

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import parallel_transactions, standalone_database, uid

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.mesh_enrollment import (
    MeshChallengeExpiredError,
    MeshEnrollmentRepository,
    MeshInvitationDigestMismatchError,
    MeshInvitationExpiredError,
)

ISSUED = 1_000
EXPIRES = 2_000


def digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def bootstrap(transaction, repo=None, *, owner=None, mesh=None, member=None):
    repo = repo or MeshEnrollmentRepository()
    owner = owner or uid()
    mesh = mesh or uid()
    member = member or uid()
    record, member_record = repo.bootstrap_mesh(
        transaction,
        mesh_id=mesh,
        owner_id=owner,
        display_name="synthetic mesh",
        bootstrap_member_id=member,
        bootstrap_member_kind="device",
        bootstrap_label="owner device",
    )
    return repo, owner, mesh, record, member_record


def test_bootstrap_is_atomic_owner_scoped_and_replay_safe():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, record, member_record = bootstrap(tx)
            assert record.owner_id == owner
            assert record.membership_epoch == 1
            assert record.record_version == 1
            assert member_record.membership_epoch == 1
            assert member_record.member_status == "active"
            assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh).mesh_id == mesh
            replay, replay_member = repo.bootstrap_mesh(
                tx,
                mesh_id=mesh,
                owner_id=owner,
                display_name="synthetic mesh",
                bootstrap_member_id=member_record.member_id,
                bootstrap_member_kind="device",
                bootstrap_label="owner device",
            )
            assert replay.record_version == record.record_version
            assert replay_member.member_id == member_record.member_id
            assert replay_member.membership_epoch == 1
            with pytest.raises(RepositoryConflictError):
                repo.bootstrap_mesh(
                    tx,
                    mesh_id=mesh,
                    owner_id=uid(),
                    display_name="synthetic mesh",
                    bootstrap_member_id=uid(),
                    bootstrap_member_kind="device",
                )
            with pytest.raises(RepositoryConflictError):
                repo.bootstrap_mesh(
                    tx,
                    mesh_id=mesh,
                    owner_id=owner,
                    display_name="different name",
                    bootstrap_member_id=member_record.member_id,
                    bootstrap_member_kind="device",
                )
            foreign = uid()
            with pytest.raises(RepositoryNotFoundError):
                repo.get_mesh(tx, owner_id=foreign, mesh_id=mesh)
            assert repo.list_meshes(tx, owner_id=foreign, limit=10) == ()
            assert len(repo.list_meshes(tx, owner_id=owner, limit=10)) == 1
    finally:
        next(fixture, None)


def test_caller_rollback_leaves_no_mesh_state():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with pytest.raises(RuntimeError), db.transaction() as tx:
            _, owner, _, _, _ = bootstrap(tx)
            raise RuntimeError("caller aborts the enrollment composition")
        with db.transaction() as tx:
            assert MeshEnrollmentRepository().list_meshes(tx, owner_id=owner, limit=10) == ()
    finally:
        next(fixture, None)


def test_member_activation_allocates_monotonic_epochs_and_reactivates():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, member_record = bootstrap(tx)
            second = repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=uid(),
                member_kind="agent",
                display_label="synthetic agent",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=0,
            )
            assert second.membership_epoch == member_record.membership_epoch + 1
            retired = repo.retire_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=second.member_id,
                expected_record_version=second.record_version,
            )
            assert retired.member_status == "retired"
            with pytest.raises(RepositoryConflictError):
                repo.retire_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=second.member_id,
                    expected_record_version=second.record_version,
                )
            reactivated = repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=second.member_id,
                member_kind="agent",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=repo.get_member(
                    tx, owner_id=owner, mesh_id=mesh, member_id=second.member_id
                ).record_version,
            )
            assert reactivated.member_status == "active"
            assert reactivated.membership_epoch > second.membership_epoch
            with pytest.raises(RepositoryConflictError):
                repo.retire_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=second.member_id,
                    expected_record_version=retired.record_version,
                )
            members = repo.list_members(tx, owner_id=owner, mesh_id=mesh)
            assert [item.membership_epoch for item in members] == sorted(
                item.membership_epoch for item in members
            )
            with pytest.raises(RepositoryNotFoundError):
                repo.get_member(tx, owner_id=uid(), mesh_id=mesh, member_id=second.member_id)
            with pytest.raises(RepositoryNotFoundError):
                repo.activate_member(
                    tx,
                    owner_id=uid(),
                    mesh_id=mesh,
                    member_id=uid(),
                    member_kind="device",
                    expected_mesh_version=1,
                    expected_member_version=0,
                )
    finally:
        next(fixture, None)


def test_invitation_lifecycle_is_atomic_and_single_use():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, _ = bootstrap(tx)
            invitation_digest = digest("invitation-secret")
            invitation = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="device",
                invitation_digest=invitation_digest,
                member_label="new device",
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            assert invitation.invitation_state == "pending"
            replayed = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=invitation.invitation_id,
                member_kind="device",
                invitation_digest=invitation_digest,
                member_label="new device",
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            assert replayed.record_version == invitation.record_version
            with pytest.raises(RepositoryConflictError):
                repo.issue_invitation(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    invitation_id=invitation.invitation_id,
                    member_kind="agent",
                    invitation_digest=invitation_digest,
                    issued_at=ISSUED,
                    expires_at=EXPIRES,
                )
            with pytest.raises(MeshInvitationDigestMismatchError):
                repo.consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation.invitation_id,
                    invitation_digest=digest("wrong-secret"),
                    as_of=ISSUED + 1,
                )
            with pytest.raises(MeshInvitationExpiredError):
                repo.consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation.invitation_id,
                    invitation_digest=invitation_digest,
                    as_of=EXPIRES + 1,
                )
            consumed = repo.consume_invitation(
                tx,
                owner_id=owner,
                invitation_id=invitation.invitation_id,
                invitation_digest=invitation_digest,
                as_of=ISSUED + 5,
            )
            assert consumed.invitation_state == "consumed"
            assert consumed.consumed_at == ISSUED + 5
            with pytest.raises(RepositoryConflictError):
                repo.consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation.invitation_id,
                    invitation_digest=invitation_digest,
                    as_of=ISSUED + 6,
                )
            confirmed, member_record = repo.confirm_invitation(
                tx,
                owner_id=owner,
                invitation_id=invitation.invitation_id,
                member_id=uid(),
                expected_invitation_version=repo.get_invitation(
                    tx, owner_id=owner, invitation_id=invitation.invitation_id
                ).record_version,
                expected_mesh_version=repo.get_mesh(
                    tx,
                    owner_id=owner,
                    mesh_id=repo.get_invitation(
                        tx, owner_id=owner, invitation_id=invitation.invitation_id
                    ).mesh_id,
                ).record_version,
                expected_member_version=0,
                as_of=ISSUED + 10,
            )
            assert confirmed.invitation_state == "confirmed"
            assert member_record.member_kind == "device"
            assert member_record.member_status == "active"
            assert member_record.membership_epoch > 1
            with pytest.raises(RepositoryConflictError):
                repo.confirm_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation.invitation_id,
                    member_id=uid(),
                    expected_invitation_version=repo.get_invitation(
                        tx, owner_id=owner, invitation_id=invitation.invitation_id
                    ).record_version,
                    expected_mesh_version=repo.get_mesh(
                        tx,
                        owner_id=owner,
                        mesh_id=repo.get_invitation(
                            tx, owner_id=owner, invitation_id=invitation.invitation_id
                        ).mesh_id,
                    ).record_version,
                    expected_member_version=0,
                    as_of=ISSUED + 10,
                )
            assert (
                repo.get_invitation(
                    tx, owner_id=owner, invitation_id=invitation.invitation_id
                ).invitation_state
                == "confirmed"
            )
    finally:
        next(fixture, None)


def test_pending_invitations_can_expire_or_be_revoked_but_not_confirmed():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, _ = bootstrap(tx)
            expiring = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="agent",
                invitation_digest=digest("expire-me"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            expired = repo.expire_invitation(
                tx,
                owner_id=owner,
                invitation_id=expiring.invitation_id,
                as_of=EXPIRES,
            )
            assert expired.invitation_state == "expired"
            with pytest.raises(RepositoryConflictError):
                repo.expire_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=expiring.invitation_id,
                    as_of=EXPIRES,
                )
            with pytest.raises(RepositoryConflictError):
                repo.confirm_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=expiring.invitation_id,
                    member_id=uid(),
                    expected_invitation_version=repo.get_invitation(
                        tx, owner_id=owner, invitation_id=expiring.invitation_id
                    ).record_version,
                    expected_mesh_version=repo.get_mesh(
                        tx,
                        owner_id=owner,
                        mesh_id=repo.get_invitation(
                            tx, owner_id=owner, invitation_id=expiring.invitation_id
                        ).mesh_id,
                    ).record_version,
                    expected_member_version=0,
                    as_of=ISSUED + 10,
                )
            revoked = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="companion",
                invitation_digest=digest("revoke-me"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            cancelled = repo.revoke_invitation(
                tx,
                owner_id=owner,
                invitation_id=revoked.invitation_id,
            )
            assert cancelled.invitation_state == "revoked"
            with pytest.raises(RepositoryConflictError):
                repo.consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=revoked.invitation_id,
                    invitation_digest=digest("revoke-me"),
                    as_of=ISSUED + 1,
                )
            consumed = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="device",
                invitation_digest=digest("consume-then-revoke"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            repo.consume_invitation(
                tx,
                owner_id=owner,
                invitation_id=consumed.invitation_id,
                invitation_digest=digest("consume-then-revoke"),
                as_of=ISSUED + 1,
            )
            revoked_after = repo.revoke_invitation(
                tx,
                owner_id=owner,
                invitation_id=consumed.invitation_id,
            )
            assert revoked_after.invitation_state == "revoked"
            with pytest.raises(RepositoryNotFoundError):
                repo.get_invitation(tx, owner_id=uid(), invitation_id=consumed.invitation_id)
            invitations = repo.list_invitations(tx, owner_id=owner, mesh_id=mesh)
            assert len(invitations) == 3
    finally:
        next(fixture, None)


def test_concurrent_invitation_consumption_admits_exactly_one():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, _ = bootstrap(tx)
            invitation = repo.issue_invitation(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                invitation_id=uid(),
                member_kind="device",
                invitation_digest=digest("race-secret"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            invitation_id = invitation.invitation_id
        outcomes = parallel_transactions(
            db,
            (
                lambda tx: MeshEnrollmentRepository().consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation_id,
                    invitation_digest=digest("race-secret"),
                    as_of=ISSUED + 1,
                ),
                lambda tx: MeshEnrollmentRepository().consume_invitation(
                    tx,
                    owner_id=owner,
                    invitation_id=invitation_id,
                    invitation_digest=digest("race-secret"),
                    as_of=ISSUED + 1,
                ),
            ),
        )
        states = sorted(
            "won" if not isinstance(outcome, BaseException) else "lost" for outcome in outcomes
        )
        assert states == ["lost", "won"]
        with db.transaction() as tx:
            final = MeshEnrollmentRepository().get_invitation(
                tx, owner_id=owner, invitation_id=invitation_id
            )
            assert final.invitation_state == "consumed"
            members = MeshEnrollmentRepository().list_members(tx, owner_id=owner, mesh_id=mesh)
            assert len(members) == 1
    finally:
        next(fixture, None)


def test_concurrent_activation_rejects_stale_revision_and_refreshes_monotonic_epochs():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, initial, _ = bootstrap(tx)
        operations = tuple(
            lambda tx, index=index: repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=f"racer-{index}",
                member_kind="device",
                expected_mesh_version=initial.record_version,
                expected_member_version=0,
            )
            for index in range(3)
        )
        outcomes = parallel_transactions(db, operations)
        assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
        assert sum(isinstance(value, RepositoryConflictError) for value in outcomes) == 2
        with db.transaction() as tx:
            assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh).membership_epoch == 2
            for index, outcome in enumerate(outcomes):
                if isinstance(outcome, RepositoryConflictError):
                    current = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
                    repo.activate_member(
                        tx,
                        owner_id=owner,
                        mesh_id=mesh,
                        member_id=f"racer-{index}",
                        member_kind="device",
                        expected_mesh_version=current.record_version,
                        expected_member_version=0,
                    )
            members = repo.list_members(tx, owner_id=owner, mesh_id=mesh)
            assert sorted(value.membership_epoch for value in members) == [1, 2, 3, 4]
            assert repo.get_mesh(tx, owner_id=owner, mesh_id=mesh).record_version == 4
    finally:
        next(fixture, None)


def test_public_identity_binds_only_active_members_and_replays_exactly():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, member_record = bootstrap(tx)
            identity = repo.bind_public_identity(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                identity_id=uid(),
                algorithm="ed25519",
                public_key="synthetic-public-key-material",
                key_fingerprint=digest("synthetic-public-key-material"),
                activated_epoch=member_record.membership_epoch,
            )
            assert identity.identity_state == "active"
            replayed = repo.bind_public_identity(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                identity_id=identity.identity_id,
                algorithm="ed25519",
                public_key="synthetic-public-key-material",
                key_fingerprint=digest("synthetic-public-key-material"),
                activated_epoch=member_record.membership_epoch,
            )
            assert replayed.record_version == identity.record_version
            with pytest.raises(RepositoryConflictError):
                repo.bind_public_identity(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=member_record.member_id,
                    identity_id=identity.identity_id,
                    algorithm="ed25519",
                    public_key="rotated-key-material",
                    key_fingerprint=digest("rotated-key-material"),
                )
            with pytest.raises(RepositoryNotFoundError):
                repo.bind_public_identity(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=uid(),
                    identity_id=uid(),
                    algorithm="ed25519",
                    public_key="synthetic-public-key-material",
                    key_fingerprint=digest("synthetic-public-key-material"),
                )
            rotated = repo.rotate_public_identity(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                identity_id=identity.identity_id,
                expected_record_version=identity.record_version,
            )
            assert rotated.identity_state == "rotated"
            with pytest.raises(RepositoryConflictError):
                repo.rotate_public_identity(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=member_record.member_id,
                    identity_id=identity.identity_id,
                    expected_record_version=identity.record_version,
                )
            revoked = repo.revoke_public_identity(
                tx,
                owner_id=owner,
                identity_id=identity.identity_id,
                expected_record_version=rotated.record_version,
            )
            assert revoked.identity_state == "revoked"
            replacement = repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=uid(),
                member_kind="agent",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=0,
            )
            bound = repo.bind_public_identity(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=replacement.member_id,
                identity_id=uid(),
                algorithm="ed25519",
                public_key="synthetic-public-key-material",
                key_fingerprint=digest("synthetic-public-key-material"),
            )
            repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=replacement.member_id,
                revocation_id=uid(),
                reason="rotate off the mesh",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=repo.get_member(
                    tx, owner_id=owner, mesh_id=mesh, member_id=replacement.member_id
                ).record_version,
            )
            with pytest.raises(RepositoryConflictError):
                repo.bind_public_identity(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=replacement.member_id,
                    identity_id=uid(),
                    algorithm="ed25519",
                    public_key="late-key",
                    key_fingerprint=digest("late-key"),
                )
            identities = repo.list_public_identities(tx, owner_id=owner, mesh_id=mesh)
            assert len(identities) == 2
            by_member = repo.list_public_identities(
                tx, owner_id=owner, mesh_id=mesh, member_id=bound.member_id
            )
            assert len(by_member) == 1
            assert by_member[0].identity_state == "active"
    finally:
        next(fixture, None)


def test_revocation_allocates_monotonic_epochs_and_stays_visible():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, member_record = bootstrap(tx)
            first_member = repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=uid(),
                member_kind="device",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=0,
            )
            second_member = repo.activate_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=uid(),
                member_kind="device",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=0,
            )
            revoked, record = repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=first_member.member_id,
                revocation_id=uid(),
                reason="stolen device",
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=repo.get_member(
                    tx, owner_id=owner, mesh_id=mesh, member_id=first_member.member_id
                ).record_version,
            )
            assert revoked.member_status == "revoked"
            assert record.revocation_epoch == 1
            assert record.reason == "stolen device"
            with pytest.raises(RepositoryConflictError):
                repo.revoke_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=first_member.member_id,
                    revocation_id=uid(),
                    expected_mesh_version=repo.get_mesh(
                        tx, owner_id=owner, mesh_id=mesh
                    ).record_version,
                    expected_member_version=repo.get_member(
                        tx, owner_id=owner, mesh_id=mesh, member_id=first_member.member_id
                    ).record_version,
                )
            _, second_record = repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=second_member.member_id,
                revocation_id=uid(),
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=repo.get_member(
                    tx, owner_id=owner, mesh_id=mesh, member_id=second_member.member_id
                ).record_version,
            )
            assert second_record.revocation_epoch > record.revocation_epoch
            _, bootstrap_record = repo.revoke_member(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                revocation_id=uid(),
                expected_mesh_version=repo.get_mesh(
                    tx, owner_id=owner, mesh_id=mesh
                ).record_version,
                expected_member_version=repo.get_member(
                    tx, owner_id=owner, mesh_id=mesh, member_id=member_record.member_id
                ).record_version,
            )
            assert bootstrap_record.revocation_epoch > second_record.revocation_epoch
            records = repo.list_revocations(tx, owner_id=owner, mesh_id=mesh)
            assert [item.revocation_epoch for item in records] == sorted(
                item.revocation_epoch for item in records
            )
            mesh_record = repo.get_mesh(tx, owner_id=owner, mesh_id=mesh)
            assert mesh_record.revocation_epoch == bootstrap_record.revocation_epoch
            with pytest.raises(RepositoryNotFoundError):
                repo.revoke_member(
                    tx,
                    owner_id=uid(),
                    mesh_id=mesh,
                    member_id=member_record.member_id,
                    revocation_id=uid(),
                    expected_mesh_version=1,
                    expected_member_version=1,
                )
    finally:
        next(fixture, None)


def test_challenge_state_requires_digest_and_expiry():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, member_record = bootstrap(tx)
            challenge_digest = digest("possession-secret")
            challenge = repo.issue_enrollment_challenge(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                challenge_id=uid(),
                challenge_digest=challenge_digest,
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            assert challenge.challenge_state == "pending"
            replayed = repo.issue_enrollment_challenge(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                challenge_id=challenge.challenge_id,
                challenge_digest=challenge_digest,
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            assert replayed.record_version == challenge.record_version
            with pytest.raises(RepositoryConflictError):
                repo.issue_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=member_record.member_id,
                    challenge_id=challenge.challenge_id,
                    challenge_digest=digest("changed-secret"),
                    issued_at=ISSUED,
                    expires_at=EXPIRES,
                )
            with pytest.raises(MeshInvitationDigestMismatchError):
                repo.prove_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    challenge_id=challenge.challenge_id,
                    challenge_digest=digest("wrong-secret"),
                    as_of=ISSUED + 1,
                )
            with pytest.raises(MeshChallengeExpiredError):
                repo.prove_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    challenge_id=challenge.challenge_id,
                    challenge_digest=challenge_digest,
                    as_of=EXPIRES + 1,
                )
            proven = repo.prove_enrollment_challenge(
                tx,
                owner_id=owner,
                challenge_id=challenge.challenge_id,
                challenge_digest=challenge_digest,
                as_of=ISSUED + 2,
            )
            assert proven.challenge_state == "proven"
            assert proven.proven_at == ISSUED + 2
            with pytest.raises(RepositoryConflictError):
                repo.prove_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    challenge_id=challenge.challenge_id,
                    challenge_digest=challenge_digest,
                    as_of=ISSUED + 3,
                )
            pending = repo.issue_enrollment_challenge(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                member_id=member_record.member_id,
                challenge_id=uid(),
                challenge_digest=digest("cancel-me"),
                issued_at=ISSUED,
                expires_at=EXPIRES,
            )
            cancelled = repo.cancel_enrollment_challenge(
                tx,
                owner_id=owner,
                challenge_id=pending.challenge_id,
            )
            assert cancelled.challenge_state == "cancelled"
            with pytest.raises(RepositoryConflictError):
                repo.cancel_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    challenge_id=pending.challenge_id,
                )
            challenges = repo.list_enrollment_challenges(tx, owner_id=owner, mesh_id=mesh)
            assert len(challenges) == 2
    finally:
        next(fixture, None)


def test_rename_mesh_is_a_current_revision_fence():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, record, _ = bootstrap(tx)
            renamed = repo.rename_mesh(
                tx,
                owner_id=owner,
                mesh_id=mesh,
                display_name="renamed mesh",
                expected_record_version=record.record_version,
            )
            assert renamed.display_name == "renamed mesh"
            assert renamed.record_version == record.record_version + 1
            with pytest.raises(RepositoryConflictError):
                repo.rename_mesh(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    display_name="stale fence",
                    expected_record_version=record.record_version,
                )
            with pytest.raises(RepositoryNotFoundError):
                repo.rename_mesh(
                    tx,
                    owner_id=uid(),
                    mesh_id=mesh,
                    display_name="foreign fence",
                    expected_record_version=renamed.record_version,
                )
    finally:
        next(fixture, None)


def test_invalid_enrollment_inputs_fail_closed():
    fixture = standalone_database()
    db = next(fixture)
    try:
        with db.transaction() as tx:
            repo, owner, mesh, _, _ = bootstrap(tx)
            with pytest.raises(RepositoryValidationError):
                repo.activate_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=uid(),
                    member_kind="robot",
                    expected_mesh_version=repo.get_mesh(
                        tx, owner_id=owner, mesh_id=mesh
                    ).record_version,
                    expected_member_version=0,
                )
            with pytest.raises(RepositoryValidationError):
                repo.issue_invitation(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    invitation_id=uid(),
                    member_kind="device",
                    invitation_digest="not-a-digest",
                    issued_at=ISSUED,
                    expires_at=EXPIRES,
                )
            with pytest.raises(RepositoryValidationError):
                repo.issue_invitation(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    invitation_id=uid(),
                    member_kind="device",
                    invitation_digest=digest("window"),
                    issued_at=EXPIRES,
                    expires_at=ISSUED,
                )
            with pytest.raises(RepositoryValidationError):
                repo.issue_enrollment_challenge(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=uid(),
                    challenge_id=uid(),
                    challenge_digest=digest("window"),
                    issued_at=EXPIRES,
                    expires_at=ISSUED,
                )
            with pytest.raises(RepositoryValidationError):
                repo.rename_mesh(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    display_name="",
                    expected_record_version=1,
                )
            with pytest.raises(RepositoryValidationError):
                repo.list_members(tx, owner_id=owner, mesh_id=mesh, limit=0)
            with pytest.raises(RepositoryValidationError):
                repo.revoke_member(
                    tx,
                    owner_id=owner,
                    mesh_id=mesh,
                    member_id=uid(),
                    revocation_id=uid(),
                    reason="x" * 600,
                    expected_mesh_version=repo.get_mesh(
                        tx, owner_id=owner, mesh_id=mesh
                    ).record_version,
                    expected_member_version=0,
                )
    finally:
        next(fixture, None)
