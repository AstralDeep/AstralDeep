"""Owner-scoped persistence for neutral personal-mesh membership, enrollment, and
revocation state: monotonic epochs, fenced member activation, and atomic single-use
invitation expiry/consumption/confirmation. Possession proof, IAM, and admission
policy stay with the host; only public key material and opaque digests are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
)

_MAX_ID = 128
_MAX_OWNER = 512
_MEMBER_KINDS: Final = ("device", "agent", "companion")


class MeshMemberKind(StrEnum):
    DEVICE = "device"
    AGENT = "agent"
    COMPANION = "companion"


class MeshMemberStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    RETIRED = "retired"


class MeshIdentityState(StrEnum):
    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"


class MeshChallengeState(StrEnum):
    PENDING = "pending"
    PROVEN = "proven"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MeshInvitationState(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class MeshMembershipEpochConflictError(RepositoryConflictError):
    default_code = "mesh_membership_epoch_conflict"


class MeshRevocationEpochConflictError(RepositoryConflictError):
    default_code = "mesh_revocation_epoch_conflict"


class MeshInvitationExpiredError(RepositoryConflictError):
    default_code = "mesh_invitation_expired"


class MeshInvitationDigestMismatchError(RepositoryConflictError):
    default_code = "mesh_invitation_digest_mismatch"


class MeshChallengeExpiredError(RepositoryConflictError):
    default_code = "mesh_challenge_expired"


def _kind(value: object, field: str) -> str:
    kind = _bounded_text(value, field, maximum=32)
    if kind not in _MEMBER_KINDS:
        raise RepositoryValidationError(
            f"{field} is not a supported mesh member kind",
            metadata={"field": field, "allowed": ",".join(_MEMBER_KINDS)},
        )
    return kind


def _digest(value: object, field: str) -> str:
    digest = _bounded_text(value, field, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RepositoryValidationError(
            f"{field} must be a lowercase 64-character hex digest",
            metadata={"field": field},
        )
    return digest


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _fingerprint(value: object, field: str) -> str:
    return _digest(value, field)


def _version(value: object, field: str) -> int:
    version = _non_negative_int(value, field)
    if version < 1:
        raise RepositoryValidationError(
            f"{field} must be a positive record version",
            metadata={"field": field},
        )
    return version


@dataclass(frozen=True, slots=True)
class MeshRecord:
    mesh_id: str
    owner_id: str
    display_name: str
    membership_epoch: int
    revocation_epoch: int
    record_version: int
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class MeshMemberRecord:
    mesh_id: str
    member_id: str
    owner_id: str
    member_kind: str
    display_label: str | None
    membership_epoch: int
    member_status: str
    record_version: int
    joined_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class MeshPublicIdentityRecord:
    identity_id: str
    mesh_id: str
    owner_id: str
    member_id: str
    algorithm: str
    public_key: str
    key_fingerprint: str
    identity_state: str
    activated_epoch: int | None
    record_version: int
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class MeshEnrollmentChallengeRecord:
    challenge_id: str
    mesh_id: str
    owner_id: str
    member_id: str
    challenge_digest: str
    challenge_state: str
    issued_at: Any
    expires_at: Any
    proven_at: Any
    record_version: int
    updated_at: Any


@dataclass(frozen=True, slots=True)
class MeshEnrollmentInvitationRecord:
    invitation_id: str
    mesh_id: str
    owner_id: str
    member_kind: str
    member_label: str | None
    invitation_digest: str
    invitation_state: str
    issued_at: Any
    expires_at: Any
    consumed_at: Any
    confirmed_at: Any
    record_version: int
    updated_at: Any


@dataclass(frozen=True, slots=True)
class MeshMemberRevocationRecord:
    revocation_id: str
    mesh_id: str
    owner_id: str
    member_id: str
    revocation_epoch: int
    reason: str | None
    revoked_at: Any


_MESH_FIELDS = (
    "mesh_id, owner_id, display_name, membership_epoch, revocation_epoch, "
    "record_version, created_at, updated_at"
)
_MEMBER_FIELDS = (
    "mesh_id, member_id, owner_id, member_kind, display_label, membership_epoch, "
    "member_status, record_version, joined_at, updated_at"
)
_IDENTITY_FIELDS = (
    "identity_id, mesh_id, owner_id, member_id, algorithm, public_key, "
    "key_fingerprint, identity_state, activated_epoch, record_version, "
    "created_at, updated_at"
)
_CHALLENGE_FIELDS = (
    "challenge_id, mesh_id, owner_id, member_id, challenge_digest, challenge_state, "
    "issued_at, expires_at, proven_at, record_version, updated_at"
)
_INVITATION_FIELDS = (
    "invitation_id, mesh_id, owner_id, member_kind, member_label, invitation_digest, "
    "invitation_state, issued_at, expires_at, consumed_at, confirmed_at, "
    "record_version, updated_at"
)
_REVOCATION_FIELDS = (
    "revocation_id, mesh_id, owner_id, member_id, revocation_epoch, reason, revoked_at"
)


def _mesh(row: Any) -> MeshRecord:
    return MeshRecord(
        mesh_id=_row_value(row, "mesh_id"),
        owner_id=_row_value(row, "owner_id"),
        display_name=_row_value(row, "display_name"),
        membership_epoch=_row_value(row, "membership_epoch"),
        revocation_epoch=_row_value(row, "revocation_epoch"),
        record_version=_row_value(row, "record_version"),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def _member(row: Any) -> MeshMemberRecord:
    return MeshMemberRecord(
        mesh_id=_row_value(row, "mesh_id"),
        member_id=_row_value(row, "member_id"),
        owner_id=_row_value(row, "owner_id"),
        member_kind=_row_value(row, "member_kind"),
        display_label=_row_value(row, "display_label"),
        membership_epoch=_row_value(row, "membership_epoch"),
        member_status=_row_value(row, "member_status"),
        record_version=_row_value(row, "record_version"),
        joined_at=_row_value(row, "joined_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def _identity(row: Any) -> MeshPublicIdentityRecord:
    return MeshPublicIdentityRecord(
        identity_id=_row_value(row, "identity_id"),
        mesh_id=_row_value(row, "mesh_id"),
        owner_id=_row_value(row, "owner_id"),
        member_id=_row_value(row, "member_id"),
        algorithm=_row_value(row, "algorithm"),
        public_key=_row_value(row, "public_key"),
        key_fingerprint=_row_value(row, "key_fingerprint"),
        identity_state=_row_value(row, "identity_state"),
        activated_epoch=_row_value(row, "activated_epoch"),
        record_version=_row_value(row, "record_version"),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def _challenge(row: Any) -> MeshEnrollmentChallengeRecord:
    return MeshEnrollmentChallengeRecord(
        challenge_id=_row_value(row, "challenge_id"),
        mesh_id=_row_value(row, "mesh_id"),
        owner_id=_row_value(row, "owner_id"),
        member_id=_row_value(row, "member_id"),
        challenge_digest=_row_value(row, "challenge_digest"),
        challenge_state=_row_value(row, "challenge_state"),
        issued_at=_row_value(row, "issued_at"),
        expires_at=_row_value(row, "expires_at"),
        proven_at=_row_value(row, "proven_at"),
        record_version=_row_value(row, "record_version"),
        updated_at=_row_value(row, "updated_at"),
    )


def _invitation(row: Any) -> MeshEnrollmentInvitationRecord:
    return MeshEnrollmentInvitationRecord(
        invitation_id=_row_value(row, "invitation_id"),
        mesh_id=_row_value(row, "mesh_id"),
        owner_id=_row_value(row, "owner_id"),
        member_kind=_row_value(row, "member_kind"),
        member_label=_row_value(row, "member_label"),
        invitation_digest=_row_value(row, "invitation_digest"),
        invitation_state=_row_value(row, "invitation_state"),
        issued_at=_row_value(row, "issued_at"),
        expires_at=_row_value(row, "expires_at"),
        consumed_at=_row_value(row, "consumed_at"),
        confirmed_at=_row_value(row, "confirmed_at"),
        record_version=_row_value(row, "record_version"),
        updated_at=_row_value(row, "updated_at"),
    )


def _revocation(row: Any) -> MeshMemberRevocationRecord:
    return MeshMemberRevocationRecord(
        revocation_id=_row_value(row, "revocation_id"),
        mesh_id=_row_value(row, "mesh_id"),
        owner_id=_row_value(row, "owner_id"),
        member_id=_row_value(row, "member_id"),
        revocation_epoch=_row_value(row, "revocation_epoch"),
        reason=_row_value(row, "reason"),
        revoked_at=_row_value(row, "revoked_at"),
    )


class MeshEnrollmentRepository:
    def bootstrap_mesh(
        self,
        transaction: Transaction,
        *,
        mesh_id: str,
        owner_id: str,
        display_name: str,
        bootstrap_member_id: str,
        bootstrap_member_kind: str,
        bootstrap_label: str | None = None,
    ) -> tuple[MeshRecord, MeshMemberRecord]:
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        name = _bounded_text(display_name, "display_name", maximum=256)
        member = _required_id(bootstrap_member_id, "bootstrap_member_id", maximum=_MAX_ID)
        kind = _kind(bootstrap_member_kind, "bootstrap_member_kind")
        label = _optional_text(bootstrap_label, "bootstrap_label", 256)
        with transaction.savepoint("mesh_bootstrap"):
            row = transaction.fetch_one(
                f"""
                INSERT INTO mesh_record (mesh_id, owner_id, display_name, membership_epoch)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (mesh_id) DO NOTHING
                RETURNING {_MESH_FIELDS}
                """,
                (mesh, owner, name),
            )
            if row is None:
                existing = transaction.fetch_one(
                    f"SELECT {_MESH_FIELDS} FROM mesh_record "
                    "WHERE mesh_id = %s AND owner_id = %s FOR UPDATE",
                    (mesh, owner),
                )
                if existing is None:
                    raise RepositoryConflictError("mesh identity is unavailable for this owner")
                if _row_value(existing, "display_name") != name:
                    raise RepositoryConflictError("mesh replay changed immutable semantics")
                existing_member = transaction.fetch_one(
                    f"SELECT {_MEMBER_FIELDS} FROM mesh_member "
                    "WHERE mesh_id = %s AND member_id = %s AND owner_id = %s",
                    (mesh, member, owner),
                )
                if (
                    existing_member is None
                    or _row_value(existing_member, "membership_epoch") != 1
                    or _row_value(existing_member, "member_kind") != kind
                    or _row_value(existing_member, "display_label") != label
                ):
                    raise RepositoryConflictError("bootstrap member replay changed semantics")
                return _mesh(existing), _member(existing_member)
            member_row = transaction.fetch_one(
                f"""
                INSERT INTO mesh_member (
                    mesh_id, member_id, owner_id, member_kind, display_label, membership_epoch
                )
                SELECT %s, %s, %s, %s, %s, 1
                WHERE EXISTS (
                    SELECT 1 FROM mesh_record
                    WHERE mesh_id = %s AND owner_id = %s AND record_version = 1
                )
                ON CONFLICT (mesh_id, member_id) DO NOTHING
                RETURNING {_MEMBER_FIELDS}
                """,
                (mesh, member, owner, kind, label, mesh, owner),
            )
            if member_row is None:
                raise RepositoryConflictError("bootstrap member identity is unavailable")
            return _mesh(row), _member(member_row)

    def get_mesh(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
    ) -> MeshRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            SELECT {_MESH_FIELDS} FROM mesh_record
            WHERE mesh_id = %s AND owner_id = %s
            """,
            (mesh, owner),
        )
        if row is None:
            raise RepositoryNotFoundError("mesh not found for this owner")
        return _mesh(row)

    def list_meshes(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 50,
    ) -> tuple[MeshRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_MESH_FIELDS} FROM mesh_record
            WHERE owner_id = %s
            ORDER BY created_at, mesh_id
            LIMIT %s
            """,
            (owner, bounded),
        )
        return tuple(_mesh(row) for row in rows)

    def rename_mesh(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        display_name: str,
        expected_record_version: int,
    ) -> MeshRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        name = _bounded_text(display_name, "display_name", maximum=256)
        version = _version(expected_record_version, "expected_record_version")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_record
               SET display_name = %s,
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE mesh_id = %s AND owner_id = %s AND record_version = %s
            RETURNING {_MESH_FIELDS}
            """,
            (name, mesh, owner, version),
        )
        if row is None:
            raise self._mesh_write_miss(transaction, owner, mesh)
        return _mesh(row)

    def _mesh_write_miss(
        self,
        transaction: Transaction,
        owner: str,
        mesh: str,
    ) -> RepositoryNotFoundError:
        row = transaction.fetch_one(
            "SELECT owner_id FROM mesh_record WHERE mesh_id = %s AND owner_id = %s",
            (mesh, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("mesh not found for this owner")
        return RepositoryConflictError("mesh record version fence rejected the write")

    def activate_member(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        member_kind: str,
        expected_mesh_version: int,
        expected_member_version: int,
        display_label: str | None = None,
    ) -> MeshMemberRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        kind = _kind(member_kind, "member_kind")
        label = _optional_text(display_label, "display_label", 256)
        mesh_version = _version(expected_mesh_version, "expected_mesh_version")
        member_version = _non_negative_int(expected_member_version, "expected_member_version")
        with transaction.savepoint("mesh_activation"):
            self._lock_mesh(transaction, owner, mesh, mesh_version)
            existing = transaction.fetch_one(
                f"SELECT {_MEMBER_FIELDS} FROM mesh_member "
                "WHERE mesh_id = %s AND member_id = %s AND owner_id = %s FOR UPDATE",
                (mesh, member, owner),
            )
            if existing is None and member_version != 0:
                raise RepositoryConflictError("member record version fence rejected activation")
            if existing is not None and _row_value(existing, "record_version") != member_version:
                raise RepositoryConflictError("member record version fence rejected activation")
            epoch_row = transaction.fetch_one(
                """
                UPDATE mesh_record
                   SET membership_epoch = membership_epoch + 1,
                       record_version = record_version + 1,
                       updated_at = now()
                 WHERE mesh_id = %s AND owner_id = %s AND record_version = %s
                RETURNING membership_epoch
                """,
                (mesh, owner, mesh_version),
            )
            if epoch_row is None:
                raise RepositoryConflictError("mesh record version fence rejected activation")
            epoch = _row_value(epoch_row, "membership_epoch")
            if existing is None:
                row = transaction.fetch_one(
                    f"""
                    INSERT INTO mesh_member (
                        mesh_id, member_id, owner_id, member_kind, display_label, membership_epoch
                    )
                    SELECT %s, %s, %s, %s, %s, %s
                    WHERE %s = 0 AND EXISTS (
                        SELECT 1 FROM mesh_record
                        WHERE mesh_id = %s AND owner_id = %s AND record_version = %s
                    )
                    ON CONFLICT (mesh_id, member_id) DO NOTHING
                    RETURNING {_MEMBER_FIELDS}
                    """,
                    (
                        mesh,
                        member,
                        owner,
                        kind,
                        label,
                        epoch,
                        member_version,
                        mesh,
                        owner,
                        mesh_version + 1,
                    ),
                )
            else:
                row = transaction.fetch_one(
                    f"""
                    UPDATE mesh_member
                       SET member_kind = %s, display_label = %s, membership_epoch = %s,
                           member_status = 'active', record_version = record_version + 1,
                           updated_at = now()
                     WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
                       AND record_version = %s
                    RETURNING {_MEMBER_FIELDS}
                    """,
                    (kind, label, epoch, mesh, member, owner, member_version),
                )
            if row is None:
                raise RepositoryConflictError("member record version fence rejected activation")
            return _member(row)

    def _lock_mesh(
        self, transaction: Transaction, owner: str, mesh: str, version: int
    ) -> MeshRecord:
        row = transaction.fetch_one(
            f"SELECT {_MESH_FIELDS} FROM mesh_record "
            "WHERE mesh_id = %s AND owner_id = %s AND record_version = %s FOR UPDATE",
            (mesh, owner, version),
        )
        if row is None:
            raise self._mesh_write_miss(transaction, owner, mesh)
        return _mesh(row)

    def get_member(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
    ) -> MeshMemberRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            SELECT {_MEMBER_FIELDS} FROM mesh_member
            WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
            """,
            (mesh, member, owner),
        )
        if row is None:
            raise RepositoryNotFoundError("member not found for this owner")
        return _member(row)

    def list_members(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        limit: int = 200,
    ) -> tuple[MeshMemberRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_MEMBER_FIELDS} FROM mesh_member
            WHERE mesh_id = %s AND owner_id = %s
            ORDER BY membership_epoch, member_id
            LIMIT %s
            """,
            (mesh, owner, bounded),
        )
        return tuple(_member(row) for row in rows)

    def assert_current_member(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        expected_membership_epoch: int | None = None,
        expected_revocation_epoch: int | None = None,
        expected_member_version: int | None = None,
    ) -> tuple[MeshRecord, MeshMemberRecord]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        expected = (expected_membership_epoch, expected_revocation_epoch, expected_member_version)
        if any(value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1)
               for value in expected):
            raise RepositoryValidationError("current member fences must be bounded integers")
        mesh_row = transaction.fetch_one(
            f"SELECT {_MESH_FIELDS} FROM mesh_record WHERE owner_id = %s "
            "AND mesh_id = %s FOR UPDATE", (owner, mesh),
        )
        if mesh_row is None:
            raise RepositoryNotFoundError("mesh not found for this owner")
        member_row = transaction.fetch_one(
            f"SELECT {_MEMBER_FIELDS} FROM mesh_member WHERE owner_id = %s "
            "AND mesh_id = %s AND member_id = %s FOR UPDATE", (owner, mesh, member),
        )
        if member_row is None:
            raise RepositoryNotFoundError("member not found for this owner")
        if member_row["member_status"] != "active":
            raise RepositoryConflictError("member is not active")
        if expected_membership_epoch is not None and (
            mesh_row["membership_epoch"] != expected_membership_epoch
        ):
            raise MeshMembershipEpochConflictError("mesh membership epoch changed")
        if expected_revocation_epoch is not None and (
            mesh_row["revocation_epoch"] != expected_revocation_epoch
        ):
            raise MeshRevocationEpochConflictError("mesh revocation epoch changed")
        if expected_member_version is not None and (
            member_row["record_version"] != expected_member_version
        ):
            raise RepositoryConflictError("member record version changed")
        return _mesh(mesh_row), _member(member_row)

    def retire_member(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        expected_record_version: int,
    ) -> MeshMemberRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        version = _version(expected_record_version, "expected_record_version")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_member
               SET member_status = 'retired',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
               AND record_version = %s AND member_status <> 'retired'
            RETURNING {_MEMBER_FIELDS}
            """,
            (mesh, member, owner, version),
        )
        if row is None:
            raise self._member_write_miss(transaction, owner, mesh, member)
        return _member(row)

    def _member_write_miss(
        self,
        transaction: Transaction,
        owner: str,
        mesh: str,
        member: str,
    ) -> RepositoryNotFoundError | RepositoryConflictError:
        row = transaction.fetch_one(
            "SELECT owner_id, member_status FROM mesh_member "
            "WHERE mesh_id = %s AND member_id = %s AND owner_id = %s",
            (mesh, member, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("member not found for this owner")
        if _row_value(row, "member_status") == "retired":
            return RepositoryConflictError("member is already retired")
        return RepositoryConflictError("member record version fence rejected the write")

    def revoke_member(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        revocation_id: str,
        expected_mesh_version: int,
        expected_member_version: int,
        reason: str | None = None,
    ) -> tuple[MeshMemberRecord, MeshMemberRevocationRecord]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        revocation = _required_id(revocation_id, "revocation_id", maximum=_MAX_ID)
        bounded_reason = _optional_text(reason, "reason", 512)
        mesh_version = _version(expected_mesh_version, "expected_mesh_version")
        member_version = _version(expected_member_version, "expected_member_version")
        with transaction.savepoint("mesh_revocation"):
            self._lock_mesh(transaction, owner, mesh, mesh_version)
            locked = transaction.fetch_one(
                "SELECT member_status, record_version FROM mesh_member "
                "WHERE mesh_id = %s AND member_id = %s AND owner_id = %s FOR UPDATE",
                (mesh, member, owner),
            )
            if locked is None:
                raise RepositoryNotFoundError("member not found for this owner")
            if (
                _row_value(locked, "member_status") == "revoked"
                or _row_value(locked, "record_version") != member_version
            ):
                raise RepositoryConflictError("member state or version fence rejected revocation")
            epoch_row = transaction.fetch_one(
                """
                UPDATE mesh_record
                   SET revocation_epoch = revocation_epoch + 1,
                       record_version = record_version + 1,
                       updated_at = now()
                 WHERE mesh_id = %s AND owner_id = %s AND record_version = %s
                RETURNING revocation_epoch
                """,
                (mesh, owner, mesh_version),
            )
            if epoch_row is None:
                raise RepositoryConflictError("mesh record version fence rejected revocation")
            epoch = _row_value(epoch_row, "revocation_epoch")
            member_row = transaction.fetch_one(
                f"""
                UPDATE mesh_member
                   SET member_status = 'revoked', record_version = record_version + 1,
                       updated_at = now()
                 WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
                   AND record_version = %s AND member_status <> 'revoked'
                RETURNING {_MEMBER_FIELDS}
                """,
                (mesh, member, owner, member_version),
            )
            if member_row is None:
                raise RepositoryConflictError("member state or version fence rejected revocation")
            revocation_row = transaction.fetch_one(
                f"""
                INSERT INTO mesh_member_revocation (
                    revocation_id, mesh_id, owner_id, member_id, revocation_epoch, reason
                )
                SELECT %s, %s, %s, %s, %s, %s
                WHERE EXISTS (
                    SELECT 1 FROM mesh_member
                    WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
                      AND record_version = %s AND member_status = 'revoked'
                )
                ON CONFLICT (revocation_id) DO NOTHING
                RETURNING {_REVOCATION_FIELDS}
                """,
                (
                    revocation,
                    mesh,
                    owner,
                    member,
                    epoch,
                    bounded_reason,
                    mesh,
                    member,
                    owner,
                    member_version + 1,
                ),
            )
            if revocation_row is None:
                raise RepositoryConflictError("revocation identity is unavailable")
            return _member(member_row), _revocation(revocation_row)

    def list_revocations(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        limit: int = 200,
    ) -> tuple[MeshMemberRevocationRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_REVOCATION_FIELDS} FROM mesh_member_revocation
            WHERE mesh_id = %s AND owner_id = %s
            ORDER BY revocation_epoch, revocation_id
            LIMIT %s
            """,
            (mesh, owner, bounded),
        )
        return tuple(_revocation(row) for row in rows)

    def bind_public_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        identity_id: str,
        algorithm: str,
        public_key: str,
        key_fingerprint: str,
        activated_epoch: int | None = None,
    ) -> MeshPublicIdentityRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        identity = _required_id(identity_id, "identity_id", maximum=_MAX_ID)
        bounded_algorithm = _bounded_text(algorithm, "algorithm", maximum=64)
        bounded_key = _bounded_text(public_key, "public_key", maximum=8192)
        fingerprint = _fingerprint(key_fingerprint, "key_fingerprint")
        epoch = (
            None
            if activated_epoch is None
            else _non_negative_int(activated_epoch, "activated_epoch")
        )
        locked = transaction.fetch_one(
            f"""
            SELECT {_MEMBER_FIELDS} FROM mesh_member
            WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
            FOR UPDATE
            """,
            (mesh, member, owner),
        )
        if locked is None:
            raise RepositoryNotFoundError("member not found for this owner")
        if _row_value(locked, "member_status") != "active":
            raise RepositoryConflictError("public identity requires an active member")
        row = transaction.fetch_one(
            f"""
            INSERT INTO mesh_public_identity (
                identity_id, mesh_id, owner_id, member_id, algorithm, public_key,
                key_fingerprint, activated_epoch
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s
            WHERE EXISTS (
                SELECT 1 FROM mesh_member
                WHERE mesh_id = %s AND member_id = %s AND owner_id = %s
                  AND member_status = 'active' AND record_version = %s
            )
            ON CONFLICT (identity_id) DO NOTHING
            RETURNING {_IDENTITY_FIELDS}
            """,
            (
                identity,
                mesh,
                owner,
                member,
                bounded_algorithm,
                bounded_key,
                fingerprint,
                epoch,
                mesh,
                member,
                owner,
                _row_value(locked, "record_version"),
            ),
        )
        if row is None:
            existing = transaction.fetch_one(
                f"SELECT {_IDENTITY_FIELDS} FROM mesh_public_identity "
                "WHERE identity_id = %s AND owner_id = %s",
                (identity, owner),
            )
            if existing is None or _row_value(existing, "owner_id") != owner:
                raise RepositoryConflictError("identity is bound to another owner")
            immutable = (
                _row_value(existing, "mesh_id"),
                _row_value(existing, "member_id"),
                _row_value(existing, "algorithm"),
                _row_value(existing, "public_key"),
                _row_value(existing, "key_fingerprint"),
                _row_value(existing, "activated_epoch"),
            )
            if immutable != (
                mesh,
                member,
                bounded_algorithm,
                bounded_key,
                fingerprint,
                epoch,
            ):
                raise RepositoryConflictError("identity replay changed immutable semantics")
            row = existing
        return _identity(row)

    def list_public_identities(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str | None = None,
        limit: int = 200,
    ) -> tuple[MeshPublicIdentityRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        bounded = _bounded_limit(limit)
        if member_id is None:
            rows = transaction.fetch_all(
                f"""
                SELECT {_IDENTITY_FIELDS} FROM mesh_public_identity
                WHERE mesh_id = %s AND owner_id = %s
                ORDER BY created_at, identity_id
                LIMIT %s
                """,
                (mesh, owner, bounded),
            )
        else:
            member = _required_id(member_id, "member_id", maximum=_MAX_ID)
            rows = transaction.fetch_all(
                f"""
                SELECT {_IDENTITY_FIELDS} FROM mesh_public_identity
                WHERE mesh_id = %s AND owner_id = %s AND member_id = %s
                ORDER BY created_at, identity_id
                LIMIT %s
                """,
                (mesh, owner, member, bounded),
            )
        return tuple(_identity(row) for row in rows)

    def rotate_public_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        identity_id: str,
        expected_record_version: int,
    ) -> MeshPublicIdentityRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        identity = _required_id(identity_id, "identity_id", maximum=_MAX_ID)
        version = _version(expected_record_version, "expected_record_version")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_public_identity
               SET identity_state = 'rotated',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE identity_id = %s AND mesh_id = %s AND member_id = %s
               AND owner_id = %s AND record_version = %s
               AND identity_state = 'active'
            RETURNING {_IDENTITY_FIELDS}
            """,
            (identity, mesh, member, owner, version),
        )
        if row is None:
            raise self._identity_write_miss(transaction, owner, identity)
        return _identity(row)

    def revoke_public_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        identity_id: str,
        expected_record_version: int,
    ) -> MeshPublicIdentityRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        identity = _required_id(identity_id, "identity_id", maximum=_MAX_ID)
        version = _version(expected_record_version, "expected_record_version")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_public_identity
               SET identity_state = 'revoked',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE identity_id = %s AND owner_id = %s AND record_version = %s
               AND identity_state <> 'revoked'
            RETURNING {_IDENTITY_FIELDS}
            """,
            (identity, owner, version),
        )
        if row is None:
            raise self._identity_write_miss(transaction, owner, identity)
        return _identity(row)

    def _identity_write_miss(
        self,
        transaction: Transaction,
        owner: str,
        identity: str,
    ) -> RepositoryNotFoundError | RepositoryConflictError:
        row = transaction.fetch_one(
            "SELECT owner_id, identity_state FROM mesh_public_identity "
            "WHERE identity_id = %s AND owner_id = %s",
            (identity, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("identity not found for this owner")
        return RepositoryConflictError("identity state or version fence rejected the write")

    def issue_enrollment_challenge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        member_id: str,
        challenge_id: str,
        challenge_digest: str,
        issued_at,
        expires_at,
    ) -> MeshEnrollmentChallengeRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        challenge = _required_id(challenge_id, "challenge_id", maximum=_MAX_ID)
        digest = _digest(challenge_digest, "challenge_digest")
        issued = _non_negative_int(issued_at, "issued_at")
        expires = _non_negative_int(expires_at, "expires_at")
        if expires <= issued:
            raise RepositoryValidationError("expires_at must be later than issued_at")
        row = transaction.fetch_one(
            f"""
            INSERT INTO mesh_enrollment_challenge (
                challenge_id, mesh_id, owner_id, member_id, challenge_digest,
                issued_at, expires_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s
            WHERE EXISTS (
                SELECT 1 FROM mesh_record WHERE mesh_id = %s AND owner_id = %s
            )
            ON CONFLICT (challenge_id) DO NOTHING
            RETURNING {_CHALLENGE_FIELDS}
            """,
            (challenge, mesh, owner, member, digest, issued, expires, mesh, owner),
        )
        if row is None:
            self.get_mesh(transaction, owner_id=owner, mesh_id=mesh)
            existing = transaction.fetch_one(
                f"SELECT {_CHALLENGE_FIELDS} FROM mesh_enrollment_challenge "
                "WHERE challenge_id = %s AND owner_id = %s",
                (challenge, owner),
            )
            if existing is None or _row_value(existing, "owner_id") != owner:
                raise RepositoryConflictError("challenge identity is bound to another owner")
            immutable = (
                _row_value(existing, "mesh_id"),
                _row_value(existing, "member_id"),
                _row_value(existing, "challenge_digest"),
                _row_value(existing, "issued_at"),
                _row_value(existing, "expires_at"),
            )
            if immutable != (mesh, member, digest, issued, expires):
                raise RepositoryConflictError("challenge replay changed immutable semantics")
            row = existing
        return _challenge(row)

    def prove_enrollment_challenge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        challenge_id: str,
        challenge_digest: str,
        as_of,
    ) -> MeshEnrollmentChallengeRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        challenge = _required_id(challenge_id, "challenge_id", maximum=_MAX_ID)
        digest = _digest(challenge_digest, "challenge_digest")
        observed = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_enrollment_challenge
               SET challenge_state = 'proven',
                   proven_at = %s,
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE challenge_id = %s AND owner_id = %s AND challenge_digest = %s
               AND challenge_state = 'pending' AND issued_at <= %s AND expires_at > %s
            RETURNING {_CHALLENGE_FIELDS}
            """,
            (observed, challenge, owner, digest, observed, observed),
        )
        if row is None:
            raise self._challenge_miss(transaction, owner, challenge, digest)
        return _challenge(row)

    def _challenge_miss(
        self,
        transaction: Transaction,
        owner: str,
        challenge: str,
        digest: str | None,
    ) -> RepositoryConflictError | RepositoryNotFoundError:
        row = transaction.fetch_one(
            "SELECT owner_id, challenge_digest, challenge_state, expires_at "
            "FROM mesh_enrollment_challenge WHERE challenge_id = %s AND owner_id = %s",
            (challenge, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("challenge not found for this owner")
        if digest is not None and _row_value(row, "challenge_digest") != digest:
            return MeshInvitationDigestMismatchError("challenge digest does not match")
        if _row_value(row, "challenge_state") == "pending":
            return MeshChallengeExpiredError("challenge is outside its validity window")
        return RepositoryConflictError(f"challenge is already {_row_value(row, 'challenge_state')}")

    def cancel_enrollment_challenge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        challenge_id: str,
    ) -> MeshEnrollmentChallengeRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        challenge = _required_id(challenge_id, "challenge_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_enrollment_challenge
               SET challenge_state = 'cancelled',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE challenge_id = %s AND owner_id = %s AND challenge_state = 'pending'
            RETURNING {_CHALLENGE_FIELDS}
            """,
            (challenge, owner),
        )
        if row is None:
            raise self._challenge_miss(transaction, owner, challenge, None)
        return _challenge(row)

    def list_enrollment_challenges(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        limit: int = 200,
    ) -> tuple[MeshEnrollmentChallengeRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_CHALLENGE_FIELDS} FROM mesh_enrollment_challenge
            WHERE mesh_id = %s AND owner_id = %s
            ORDER BY issued_at, challenge_id
            LIMIT %s
            """,
            (mesh, owner, bounded),
        )
        return tuple(_challenge(row) for row in rows)

    def issue_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        invitation_id: str,
        member_kind: str,
        invitation_digest: str,
        member_label: str | None = None,
        issued_at=None,
        expires_at=None,
    ) -> MeshEnrollmentInvitationRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        kind = _kind(member_kind, "member_kind")
        label = _optional_text(member_label, "member_label", 256)
        digest = _digest(invitation_digest, "invitation_digest")
        issued = _non_negative_int(issued_at, "issued_at")
        expires = _non_negative_int(expires_at, "expires_at")
        if expires <= issued:
            raise RepositoryValidationError("expires_at must be later than issued_at")
        row = transaction.fetch_one(
            f"""
            INSERT INTO mesh_enrollment_invitation (
                invitation_id, mesh_id, owner_id, member_kind, member_label,
                invitation_digest, issued_at, expires_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s
            WHERE EXISTS (
                SELECT 1 FROM mesh_record WHERE mesh_id = %s AND owner_id = %s
            )
            ON CONFLICT (invitation_id) DO NOTHING
            RETURNING {_INVITATION_FIELDS}
            """,
            (invitation, mesh, owner, kind, label, digest, issued, expires, mesh, owner),
        )
        if row is None:
            self.get_mesh(transaction, owner_id=owner, mesh_id=mesh)
            existing = transaction.fetch_one(
                f"SELECT {_INVITATION_FIELDS} FROM mesh_enrollment_invitation "
                "WHERE invitation_id = %s AND owner_id = %s",
                (invitation, owner),
            )
            if existing is None or _row_value(existing, "owner_id") != owner:
                raise RepositoryConflictError("invitation identity is bound to another owner")
            immutable = (
                _row_value(existing, "mesh_id"),
                _row_value(existing, "member_kind"),
                _row_value(existing, "member_label"),
                _row_value(existing, "invitation_digest"),
                _row_value(existing, "issued_at"),
                _row_value(existing, "expires_at"),
            )
            if immutable != (mesh, kind, label, digest, issued, expires):
                raise RepositoryConflictError("invitation replay changed immutable semantics")
            row = existing
        return _invitation(row)

    def get_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        invitation_id: str,
    ) -> MeshEnrollmentInvitationRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            SELECT {_INVITATION_FIELDS} FROM mesh_enrollment_invitation
            WHERE invitation_id = %s AND owner_id = %s
            """,
            (invitation, owner),
        )
        if row is None:
            raise RepositoryNotFoundError("invitation not found for this owner")
        return _invitation(row)

    def list_invitations(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        limit: int = 200,
    ) -> tuple[MeshEnrollmentInvitationRecord, ...]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        mesh = _required_id(mesh_id, "mesh_id", maximum=_MAX_ID)
        bounded = _bounded_limit(limit)
        rows = transaction.fetch_all(
            f"""
            SELECT {_INVITATION_FIELDS} FROM mesh_enrollment_invitation
            WHERE mesh_id = %s AND owner_id = %s
            ORDER BY issued_at, invitation_id
            LIMIT %s
            """,
            (mesh, owner, bounded),
        )
        return tuple(_invitation(row) for row in rows)

    def consume_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        invitation_id: str,
        invitation_digest: str,
        as_of,
    ) -> MeshEnrollmentInvitationRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        digest = _digest(invitation_digest, "invitation_digest")
        observed = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_enrollment_invitation
               SET invitation_state = 'consumed',
                   consumed_at = %s,
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE invitation_id = %s AND owner_id = %s AND invitation_digest = %s
               AND invitation_state = 'pending' AND issued_at <= %s AND expires_at > %s
            RETURNING {_INVITATION_FIELDS}
            """,
            (observed, invitation, owner, digest, observed, observed),
        )
        if row is None:
            raise self._invitation_miss(transaction, owner, invitation, digest)
        return _invitation(row)

    def _invitation_miss(
        self,
        transaction: Transaction,
        owner: str,
        invitation: str,
        digest: str | None,
    ) -> RepositoryConflictError | RepositoryNotFoundError:
        row = transaction.fetch_one(
            "SELECT owner_id, invitation_digest, invitation_state, expires_at "
            "FROM mesh_enrollment_invitation WHERE invitation_id = %s AND owner_id = %s",
            (invitation, owner),
        )
        if row is None or _row_value(row, "owner_id") != owner:
            return RepositoryNotFoundError("invitation not found for this owner")
        if digest is not None and _row_value(row, "invitation_digest") != digest:
            return MeshInvitationDigestMismatchError("invitation digest does not match")
        if _row_value(row, "invitation_state") == "pending":
            return MeshInvitationExpiredError("invitation is outside its validity window")
        return RepositoryConflictError(
            f"invitation is already {_row_value(row, 'invitation_state')}"
        )

    def expire_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        invitation_id: str,
        as_of,
    ) -> MeshEnrollmentInvitationRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        observed = _non_negative_int(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_enrollment_invitation
               SET invitation_state = 'expired',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE invitation_id = %s AND owner_id = %s AND invitation_state = 'pending'
               AND expires_at <= %s
            RETURNING {_INVITATION_FIELDS}
            """,
            (invitation, owner, observed),
        )
        if row is None:
            raise self._invitation_miss(transaction, owner, invitation, None)
        return _invitation(row)

    def revoke_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        invitation_id: str,
    ) -> MeshEnrollmentInvitationRecord:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        row = transaction.fetch_one(
            f"""
            UPDATE mesh_enrollment_invitation
               SET invitation_state = 'revoked',
                   record_version = record_version + 1,
                   updated_at = now()
             WHERE invitation_id = %s AND owner_id = %s
               AND invitation_state IN ('pending', 'consumed')
            RETURNING {_INVITATION_FIELDS}
            """,
            (invitation, owner),
        )
        if row is None:
            raise self._invitation_miss(transaction, owner, invitation, None)
        return _invitation(row)

    def confirm_invitation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        invitation_id: str,
        member_id: str,
        expected_invitation_version: int,
        expected_mesh_version: int,
        expected_member_version: int,
        as_of,
        display_label: str | None = None,
    ) -> tuple[MeshEnrollmentInvitationRecord, MeshMemberRecord]:
        owner = _required_id(owner_id, "owner_id", maximum=_MAX_OWNER)
        invitation = _required_id(invitation_id, "invitation_id", maximum=_MAX_ID)
        member = _required_id(member_id, "member_id", maximum=_MAX_ID)
        label = _optional_text(display_label, "display_label", 256)
        invitation_version = _version(expected_invitation_version, "expected_invitation_version")
        mesh_version = _version(expected_mesh_version, "expected_mesh_version")
        member_version = _non_negative_int(expected_member_version, "expected_member_version")
        observed = _non_negative_int(as_of, "as_of")
        current = self.get_invitation(transaction, owner_id=owner, invitation_id=invitation)
        with transaction.savepoint("mesh_confirmation"):
            self._lock_mesh(transaction, owner, current.mesh_id, mesh_version)
            row = transaction.fetch_one(
                f"""
                UPDATE mesh_enrollment_invitation
                   SET invitation_state = 'confirmed', confirmed_at = %s,
                       record_version = record_version + 1, updated_at = now()
                 WHERE invitation_id = %s AND owner_id = %s AND mesh_id = %s
                   AND record_version = %s AND invitation_state = 'consumed'
                   AND issued_at <= %s AND consumed_at <= %s AND expires_at > %s
                RETURNING {_INVITATION_FIELDS}
                """,
                (
                    observed,
                    invitation,
                    owner,
                    current.mesh_id,
                    invitation_version,
                    observed,
                    observed,
                    observed,
                ),
            )
            if row is None:
                raise RepositoryConflictError(
                    "invitation state, window or version fence rejected confirmation"
                )
            confirmed = _invitation(row)
            member_record = self.activate_member(
                transaction,
                owner_id=owner,
                mesh_id=confirmed.mesh_id,
                member_id=member,
                member_kind=confirmed.member_kind,
                expected_mesh_version=mesh_version,
                expected_member_version=member_version,
                display_label=label if label is not None else confirmed.member_label,
            )
            return confirmed, member_record
