"""Confirms possession-bound invitations and attenuated membership through AstralPlane.
Encrypted product scope records share membership transitions and durable audit in one
transaction; mesh_api.py exposes the host's IAM and device boundaries.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.errors import PlaneError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from audit.schemas import AuditEventCreate, AuditEventDTO

from orchestrator.delegation import attenuate_scopes
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.tool_permissions import VALID_SCOPES

INVITATION_NAMESPACE = "astral-mesh-invitation"
MEMBER_NAMESPACE = "astral-mesh-member"
CONFIRM_SCOPE = "mesh:confirm"
GRANTABLE_SCOPES = (*VALID_SCOPES, CONFIRM_SCOPE)

RECORD_VERSION = 1
MEMBER_ACTIVE_STATUS = "active"
MEMBER_REVOKED_STATUS = "revoked"

DEFAULT_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_LABEL_CHARS = 64
MAX_SCOPES = len(GRANTABLE_SCOPES)
_CHALLENGE_BYTES = 32
_TOKEN_BYTES = 32
_PAYLOAD_MAX_CHARS = 2048
_FINGERPRINT_PREFIX = "sha256-"

_MESHCREDENTIAL_KEY_SOURCE = "CREDENTIAL_ENCRYPTION_KEY"


class MeshEnrollmentError(Exception):
    status = 500
    code = "mesh_error"


class InvitationNotFound(MeshEnrollmentError):
    status = 404
    code = "invitation_not_found"


class InvitationStateInvalid(MeshEnrollmentError):
    status = 409
    code = "invitation_state_invalid"


class InvitationExpired(MeshEnrollmentError):
    status = 410
    code = "invitation_expired"


class MemberNotFound(MeshEnrollmentError):
    status = 404
    code = "member_not_found"


class MemberUnauthorized(MeshEnrollmentError):
    status = 401
    code = "member_unauthorized"


class IdentityAmbiguous(MeshEnrollmentError):
    status = 409
    code = "mesh_identity_ambiguous"


class ConfirmForbidden(MeshEnrollmentError):
    status = 403
    code = "confirm_forbidden"


class RedeemInvalid(MeshEnrollmentError):
    status = 400
    code = "redeem_invalid"


class AuditUnavailable(MeshEnrollmentError):
    status = 503
    code = "mesh_audit_unavailable"


def mesh_id_for_owner(owner_id: str) -> str:
    digest = hashlib.sha256(f"astral-mesh/v1:{owner_id}".encode()).hexdigest()[:20]
    return f"mesh-{digest}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url value") from exc


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_file_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", ".credential_key")


def load_or_create_key_file(path: str) -> bytes:
    def read_key():
        with open(path, "rb") as file:
            key = file.read().strip()
        Fernet(key)
        return key

    if os.path.exists(path):
        return read_key()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".credential-key-", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(Fernet.generate_key())
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
        return read_key()
    finally:
        os.unlink(temporary_path)


def _fernet() -> Fernet:
    env_key = os.getenv(_MESHCREDENTIAL_KEY_SOURCE)
    if env_key:
        return Fernet(env_key.encode())
    return Fernet(load_or_create_key_file(_key_file_path()))


def _encode_record(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return _fernet().encrypt(payload.encode()).decode("ascii")


def _decode_record(ciphertext: str) -> dict[str, Any]:
    try:
        plaintext = _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise MeshEnrollmentError("mesh record cannot be decrypted") from exc
    try:
        record = json.loads(plaintext)
    except (ValueError, TypeError):
        raise MeshEnrollmentError("mesh record is malformed") from None
    if not isinstance(record, dict):
        raise MeshEnrollmentError("mesh record is malformed")
    return record


def normalize_device_key(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("device_key must be an Ed25519 public JWK object")
    if raw.get("kty") != "OKP" or raw.get("crv") != "Ed25519":
        raise ValueError("device_key must be an Ed25519 OKP JWK")
    x = raw.get("x")
    if not isinstance(x, str) or len(x) > 128:
        raise ValueError("device_key.x is missing")
    try:
        public_bytes = _b64url_decode(x)
    except ValueError as exc:
        raise ValueError("device_key.x is not valid base64url") from exc
    if len(public_bytes) != 32:
        raise ValueError("device_key.x must decode to 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes)
    except Exception as exc:
        raise ValueError("device_key.x is not a valid Ed25519 public key") from exc
    return {"kty": "OKP", "crv": "Ed25519", "x": x}


def device_key_fingerprint(jwk: dict[str, str]) -> str:
    public_bytes = _b64url_decode(jwk["x"])
    return f"{_FINGERPRINT_PREFIX}{_sha256_hex(public_bytes)}"


def normalize_scopes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SCOPES:
        raise ValueError("scopes must be an array of grantable scope names")
    scopes: list[str] = []
    for scope in raw:
        if not isinstance(scope, str) or scope not in GRANTABLE_SCOPES:
            raise ValueError(f"scope is not grantable: {scope!r}")
        if scope not in scopes:
            scopes.append(scope)
    return sorted(scopes)


def _normalize_label(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("label is required")
    label = raw.strip()
    if not label or len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"label must be 1-{MAX_LABEL_CHARS} characters")
    return label


def public_invitation(record: dict[str, Any]) -> dict[str, Any]:
    confirmer = record.get("confirmed_by")
    return {
        "invite_id": record["invite_id"],
        "mesh_id": record["mesh_id"],
        "label": record["label"],
        "device_key_fingerprint": record["device_key_fingerprint"],
        "requested_scopes": record["requested_scopes"],
        "confirmed_scopes": record.get("confirmed_scopes"),
        "status": record["status"],
        "created_by": record.get("created_by"),
        "confirmed_by": (
            {key: value for key, value in confirmer.items() if key != "custody"}
            if isinstance(confirmer, dict)
            else None
        ),
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "redeemed_at": record.get("redeemed_at"),
        "member_id": record.get("member_id"),
    }


def public_member(record: dict[str, Any]) -> dict[str, Any]:
    confirmer = record.get("confirmed_by")
    return {
        "member_id": record["member_id"],
        "owner_id": record["owner_id"],
        "mesh_id": record["mesh_id"],
        "label": record["label"],
        "device_key_fingerprint": record["device_key_fingerprint"],
        "device_key": record["device_key"],
        "membership_epoch": record["membership_epoch"],
        "status": record["status"],
        "scopes": record["scopes"],
        "enrolled_at": record["enrolled_at"],
        "confirmed_by": (
            {key: value for key, value in confirmer.items() if key != "custody"}
            if isinstance(confirmer, dict)
            else None
        ),
        "revoked_at": record.get("revoked_at"),
    }


def _authority_digest(member: dict[str, Any]) -> str:
    authority = {
        name: member.get(name)
        for name in (
            "owner_id",
            "mesh_id",
            "member_id",
            "device_key",
            "device_key_fingerprint",
            "status",
            "scopes",
            "membership_epoch",
            "custody",
        )
    }
    return _sha256_hex(
        json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
    )


class MeshEnrollmentStore:
    def __init__(
        self,
        db: Any = None,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        audit_repository: Any = None,
    ) -> None:
        repository, runtime = repository_from(
            "credentials",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._credentials = PlaneRepositoryContext(
            repository=repository,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._audit = audit_repository
        self._membership = runtime.repositories.mesh_enrollment

    @contextmanager
    def _transaction(self, transaction=None):
        if transaction is not None:
            with transaction.savepoint("mesh_authority"):
                yield transaction
            return
        with self._credentials.transaction() as current:
            with current.savepoint("mesh_authority"):
                yield current

    def _audit_transition(
        self,
        transaction,
        owner_id,
        action,
        *,
        actor_kind="owner",
        actor_id=None,
        meta=None,
        outcome="success",
    ) -> None:
        event = AuditEventCreate(
            event_id=str(uuid.uuid4()),
            actor_user_id=owner_id,
            auth_principal=(
                {
                    "owner": owner_id,
                    "member": f"mesh-member:{actor_id}",
                    "device": f"mesh-device:{actor_id}",
                    "anonymous": "mesh-enrollment:unverified",
                }[actor_kind]
            ),
            event_class="agent_lifecycle",
            action_type=action,
            description=action,
            correlation_id=str(uuid.uuid4()),
            inputs_meta=meta or {},
            outcome=outcome,
            started_at=datetime.now(timezone.utc),
        )
        try:
            if self._audit is None:
                raise AuditUnavailable()
            receipt = self._audit.insert_in_transaction(
                event,
                transaction=transaction,
                plane_runtime=self._credentials.plane_runtime,
            )
            if (
                not isinstance(receipt, AuditEventDTO)
                or receipt.event_id != event.event_id
                or receipt.event_class != event.event_class
                or receipt.action_type != action
                or receipt.correlation_id != event.correlation_id
                or receipt.outcome != outcome
                or receipt.inputs_meta != event.inputs_meta
            ):
                raise AuditUnavailable()
        except Exception:
            raise AuditUnavailable("durable mesh audit unavailable") from None

    def record_redemption_denial(
        self, owner_id: str, invite_id: str, reason: str
    ) -> None:
        with self._transaction() as transaction:
            self._audit_transition(
                transaction,
                owner_id,
                "mesh.member.redeem",
                actor_kind="anonymous",
                meta={"invite_id": invite_id, "reason": reason},
                outcome="failure",
            )

    def _get_in_transaction(self, transaction, owner_id, namespace, key):
        row = self._credentials.repository.get_credential(
            transaction, owner_id=owner_id, agent_id=namespace, credential_key=key
        )
        if row is None:
            return None
        record = _decode_record(row.encrypted_value)
        if (
            type(record.get("updated_at")) is not int
            or record["updated_at"] != row.updated_at
        ):
            raise MemberUnauthorized("credential revision is invalid")
        return record

    def _list_in_transaction(self, transaction, owner_id, namespace):
        rows = self._credentials.repository.list_credentials(
            transaction, owner_id=owner_id, agent_id=namespace, limit=1000
        )
        return [_decode_record(row.encrypted_value) for row in rows]

    def _active_key_holder(
        self, transaction, owner_id, fingerprint, *, exclude_member_id=None
    ):
        for record in self._list_in_transaction(
            transaction, owner_id, MEMBER_NAMESPACE
        ):
            if (
                record.get("status") == MEMBER_ACTIVE_STATUS
                and record.get("device_key_fingerprint") == fingerprint
                and record.get("member_id") != exclude_member_id
            ):
                return record
        return None

    def _ensure_mesh(self, transaction, owner_id):
        try:
            return self._membership.get_mesh(
                transaction, owner_id=owner_id, mesh_id=mesh_id_for_owner(owner_id)
            )
        except RepositoryNotFoundError:
            mesh, _ = self._membership.bootstrap_mesh(
                transaction,
                owner_id=owner_id,
                mesh_id=mesh_id_for_owner(owner_id),
                display_name="Personal mesh",
                bootstrap_member_id="owner-control",
                bootstrap_member_kind="companion",
                bootstrap_label="Human owner",
            )
            return mesh

    def _lock_mesh(self, transaction, owner_id):
        try:
            mesh, _ = self._membership.assert_current_member(
                transaction,
                owner_id=owner_id,
                mesh_id=mesh_id_for_owner(owner_id),
                member_id="owner-control",
            )
            return mesh
        except (RepositoryConflictError, RepositoryNotFoundError):
            raise MemberUnauthorized("mesh authority is unavailable") from None

    def _current_member(
        self,
        transaction,
        owner_id,
        member_id,
        *,
        record=None,
        membership_epoch=None,
        revocation_epoch=None,
        member_version=None,
    ):
        try:
            mesh, member = self._membership.assert_current_member(
                transaction,
                owner_id=owner_id,
                mesh_id=mesh_id_for_owner(owner_id),
                member_id=member_id,
                expected_membership_epoch=membership_epoch,
                expected_revocation_epoch=revocation_epoch,
                expected_member_version=member_version,
            )
            record = (
                record
                if record is not None
                else self._get_in_transaction(
                    transaction, owner_id, MEMBER_NAMESPACE, member_id
                )
            )
            if (
                record is None
                or record.get("owner_id") != owner_id
                or record.get("member_id") != member_id
                or record.get("mesh_id") != mesh.mesh_id
                or record.get("status") != member.member_status
                or record.get("membership_epoch") != member.membership_epoch
            ):
                raise MemberUnauthorized("membership authority is unavailable")
            identities = self._membership.list_public_identities(
                transaction,
                owner_id=owner_id,
                mesh_id=mesh.mesh_id,
                member_id=member_id,
            )
            active = [
                identity
                for identity in identities
                if identity.identity_state == "active"
            ]
            key = normalize_device_key(record.get("device_key"))
            if (
                len(active) != 1
                or active[0].algorithm != "Ed25519"
                or active[0].activated_epoch != member.membership_epoch
                or active[0].key_fingerprint != _sha256_hex(_b64url_decode(key["x"]))
                or json.loads(active[0].public_key) != key
                or device_key_fingerprint(key) != record.get("device_key_fingerprint")
            ):
                raise MemberUnauthorized("membership public identity is unavailable")
            return mesh, member, record
        except (PlaneError, ValueError, TypeError, KeyError):
            raise MemberUnauthorized(
                "membership authority is stale or revoked"
            ) from None

    def _fence_confirmer(
        self,
        transaction,
        owner_id,
        member_id,
        *,
        revision=None,
        fingerprint=None,
        scopes=(),
        authority_revision=None,
        authority_digest=None,
    ):
        member = self._get_in_transaction(
            transaction, owner_id, MEMBER_NAMESPACE, member_id
        )
        if member is None or member.get("status") != MEMBER_ACTIVE_STATUS:
            raise MemberNotFound(member_id)
        try:
            self._current_member(transaction, owner_id, member_id, record=member)
        except MemberUnauthorized:
            raise ConfirmForbidden("confirmation membership is unavailable") from None
        if (
            member.get("owner_id") != owner_id
            or member.get("member_id") != member_id
            or member.get("mesh_id") != mesh_id_for_owner(owner_id)
            or type(member.get("authority_revision")) is not int
            or (
                authority_revision is not None
                and (
                    type(authority_revision) is not int
                    or member.get("authority_revision") != authority_revision
                )
            )
            or (
                authority_digest is not None
                and _authority_digest(member) != authority_digest
            )
            or (
                revision is not None
                and (type(revision) is not int or member["updated_at"] != revision)
            )
        ):
            raise ConfirmForbidden("confirmation authority is stale or foreign")
        try:
            current_fingerprint = device_key_fingerprint(
                normalize_device_key(member.get("device_key"))
            )
            current_scopes = normalize_scopes(member.get("scopes"))
        except ValueError:
            raise ConfirmForbidden("confirmation identity is malformed") from None
        if (
            current_fingerprint != member.get("device_key_fingerprint")
            or (fingerprint is not None and fingerprint != current_fingerprint)
            or CONFIRM_SCOPE not in current_scopes
            or not set(scopes).issubset(current_scopes)
        ):
            raise ConfirmForbidden("confirmation authority is unavailable")
        self._transition(
            owner_id,
            MEMBER_NAMESPACE,
            member_id,
            expected_updated_at=member["updated_at"],
            record=member,
            transaction=transaction,
        )
        return member

    def _get(
        self, owner_id: str, namespace: str, key: str, *, transaction=None
    ) -> dict[str, Any] | None:
        if transaction is not None:
            return self._get_in_transaction(transaction, owner_id, namespace, key)
        record = self._credentials.call(
            self._credentials.repository.get_credential,
            owner_id=owner_id,
            agent_id=namespace,
            credential_key=key,
        )
        if record is None:
            return None
        return _decode_record(record.encrypted_value)

    def _list(self, owner_id: str, namespace: str) -> list[dict[str, Any]]:
        rows = self._credentials.call(
            self._credentials.repository.list_credentials,
            owner_id=owner_id,
            agent_id=namespace,
            limit=1000,
        )
        return [_decode_record(row.encrypted_value) for row in rows]

    def _put(
        self, owner_id: str, namespace: str, key: str, record: dict[str, Any]
    ) -> int:
        row = self._credentials.call(
            self._credentials.repository.upsert_credential,
            owner_id=owner_id,
            agent_id=namespace,
            credential_key=key,
            encrypted_value=_encode_record(record),
            updated_at=record["updated_at"],
        )
        return row.updated_at

    def _transition(
        self,
        owner_id: str,
        namespace: str,
        key: str,
        *,
        expected_updated_at: int,
        record: dict[str, Any],
        transaction: Any = None,
    ) -> None:
        updated_at = max(_now_ms(), expected_updated_at + 1)
        record["updated_at"] = updated_at

        def advance(current):
            if namespace == MEMBER_NAMESPACE:
                previous = self._get_in_transaction(current, owner_id, namespace, key)
                if previous is None:
                    raise MemberNotFound(key)
                record["authority_revision"] = (
                    updated_at
                    if _authority_digest(previous) != _authority_digest(record)
                    else previous.get("authority_revision", previous["updated_at"])
                )
            self._credentials.repository.compare_and_set_ciphertext(
                current,
                owner_id=owner_id,
                agent_id=namespace,
                credential_key=key,
                expected_updated_at=expected_updated_at,
                encrypted_value=_encode_record(record),
                updated_at=updated_at,
            )

        try:
            if transaction is None:
                with self._credentials.transaction() as current:
                    advance(current)
            else:
                advance(transaction)
        except RepositoryConflictError as exc:
            raise InvitationStateInvalid("credential changed concurrently") from exc

    def create_invitation(
        self,
        owner_id: str,
        *,
        label: str,
        device_key: dict[str, str],
        scopes: list[str],
        creator_kind: str,
        creator_id: str,
        ttl_seconds: int | None = None,
        creator_revision: int | None = None,
        transaction: Any = None,
    ) -> dict[str, Any]:
        normalized_label = _normalize_label(label)
        normalized_key = normalize_device_key(device_key)
        normalized_scopes = normalize_scopes(scopes)
        ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        if type(ttl) is not int or ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be {MIN_TTL_SECONDS}-{MAX_TTL_SECONDS}")
        if creator_kind not in ("owner", "member"):
            raise ValueError("creator_kind must be owner or member")

        invite_id = uuid.uuid4().hex
        now = _now_ms()
        token = secrets.token_bytes(_TOKEN_BYTES)
        challenge = secrets.token_bytes(_CHALLENGE_BYTES)
        record: dict[str, Any] = {
            "version": RECORD_VERSION,
            "invite_id": invite_id,
            "mesh_id": mesh_id_for_owner(owner_id),
            "owner_id": owner_id,
            "label": normalized_label,
            "device_key": normalized_key,
            "device_key_fingerprint": device_key_fingerprint(normalized_key),
            "requested_scopes": normalized_scopes,
            "confirmed_scopes": None,
            "status": "pending",
            "created_by": {"kind": creator_kind, "id": creator_id},
            "confirmed_by": None,
            "challenge_sha256": _sha256_hex(challenge),
            "token_sha256": _sha256_hex(token),
            "created_at": now,
            "updated_at": now,
            "expires_at": now + ttl * 1000,
            "decided_at": None,
            "redeemed_at": None,
            "member_id": None,
        }
        denial: dict[str, Any] | None = None
        with self._transaction(transaction) as current:
            if creator_kind == "member":
                self._fence_confirmer(
                    current, owner_id, creator_id, revision=creator_revision
                )
            elif creator_id != owner_id:
                raise ConfirmForbidden("owner identity does not match")
            else:
                self._ensure_mesh(current, owner_id)
                self._lock_mesh(current, owner_id)
            holder = self._active_key_holder(
                current, owner_id, record["device_key_fingerprint"]
            )
            if holder is not None:
                denial = {
                    "invite_id": invite_id,
                    "reason": "device_key_already_active",
                    "fingerprint": record["device_key_fingerprint"],
                    "holder": holder["member_id"],
                }
                self._audit_transition(
                    current,
                    owner_id,
                    "mesh.invitation.create",
                    actor_kind=creator_kind,
                    actor_id=creator_id,
                    meta=denial,
                    outcome="failure",
                )
            elif _now_ms() >= record["expires_at"]:
                raise InvitationExpired(invite_id)
            else:
                self._membership.issue_invitation(
                    current,
                    owner_id=owner_id,
                    mesh_id=record["mesh_id"],
                    invitation_id=invite_id,
                    member_kind="device",
                    member_label=normalized_label,
                    invitation_digest=record["token_sha256"],
                    issued_at=now,
                    expires_at=record["expires_at"],
                )
                self._membership.issue_enrollment_challenge(
                    current,
                    owner_id=owner_id,
                    mesh_id=record["mesh_id"],
                    member_id=invite_id,
                    challenge_id=invite_id,
                    challenge_digest=record["challenge_sha256"],
                    issued_at=now,
                    expires_at=record["expires_at"],
                )
                self._credentials.repository.upsert_credential(
                    current,
                    owner_id=owner_id,
                    agent_id=INVITATION_NAMESPACE,
                    credential_key=invite_id,
                    encrypted_value=_encode_record(record),
                    updated_at=now,
                )
                self._audit_transition(
                    current,
                    owner_id,
                    "mesh.invitation.create",
                    actor_kind=creator_kind,
                    actor_id=creator_id,
                    meta={
                        "invite_id": invite_id,
                        "fingerprint": record["device_key_fingerprint"],
                    },
                )
                if _now_ms() >= record["expires_at"]:
                    raise InvitationExpired(invite_id)
        if denial is not None:
            raise IdentityAmbiguous(denial["holder"])
        payload = {
            "v": RECORD_VERSION,
            "o": owner_id,
            "i": invite_id,
            "t": _b64url_encode(token),
            "c": _b64url_encode(challenge),
        }
        return {
            "invitation": public_invitation(record),
            "payload": _b64url_encode(
                json.dumps(payload, separators=(",", ":")).encode()
            ),
        }

    def list_invitations(self, owner_id: str) -> list[dict[str, Any]]:
        records = self._list(owner_id, INVITATION_NAMESPACE)
        records.sort(key=lambda item: item["created_at"], reverse=True)
        return [public_invitation(record) for record in records]

    def get_invitation(
        self, owner_id: str, invite_id: str, *, transaction=None
    ) -> dict[str, Any]:
        record = self._get(
            owner_id, INVITATION_NAMESPACE, invite_id, transaction=transaction
        )
        if record is None or record.get("owner_id") != owner_id:
            raise InvitationNotFound(invite_id)
        return record

    def decide_invitation(
        self,
        owner_id: str,
        invite_id: str,
        *,
        decision: str,
        decider_kind: str,
        decider_id: str,
        decider_revision: int | None = None,
        transaction: Any = None,
        custody: dict[str, Any] | None = None,
        consent_observation: Any = None,
    ) -> dict[str, Any]:
        if decision not in ("confirmed", "rejected"):
            raise ValueError("decision must be confirmed or rejected")
        if decider_kind not in ("owner", "member"):
            raise ConfirmForbidden("confirmation identity is invalid")
        record = self.get_invitation(owner_id, invite_id, transaction=transaction)
        if record["status"] != "pending":
            raise InvitationStateInvalid("invitation is not pending")
        if _now_ms() >= record["expires_at"]:
            raise InvitationExpired(invite_id)
        creator = record.get("created_by") or {}
        if (
            decider_kind == "member"
            and creator.get("kind") == "member"
            and creator.get("id") == decider_id
        ):
            raise ConfirmForbidden("a member cannot confirm its own invitation")
        with self._transaction(transaction) as current:
            self._lock_mesh(current, owner_id)
            if consent_observation is not None:
                self._credentials.plane_runtime.repositories.history.sessions.assert_current_consent(
                    current, observation=consent_observation
                )
            if decider_kind == "owner":
                if decider_id != owner_id:
                    raise ConfirmForbidden("owner identity does not match")
                available = GRANTABLE_SCOPES
                confirmed_by = {
                    "kind": "owner",
                    "id": owner_id,
                    **({"custody": custody} if custody is not None else {}),
                }
            else:
                member = self._fence_confirmer(
                    current, owner_id, decider_id, revision=decider_revision
                )
                available = member["scopes"]
                confirmed_by = {
                    "kind": "member",
                    "id": decider_id,
                    "revision": member["updated_at"],
                    "authority_revision": member["authority_revision"],
                    "authority_digest": _authority_digest(member),
                    "fingerprint": member["device_key_fingerprint"],
                    "custody": member.get("custody"),
                }
            if _now_ms() >= record["expires_at"]:
                raise InvitationExpired(invite_id)
            record["confirmed_scopes"] = (
                attenuate_scopes(available, record["requested_scopes"])
                if decision == "confirmed"
                else None
            )
            record["confirmed_by"] = confirmed_by if decision == "confirmed" else None
            record["status"] = decision
            record["decided_at"] = _now_ms()
            if decision == "rejected":
                self._membership.revoke_invitation(
                    current, owner_id=owner_id, invitation_id=invite_id
                )
                self._membership.cancel_enrollment_challenge(
                    current, owner_id=owner_id, challenge_id=invite_id
                )
            self._transition(
                owner_id,
                INVITATION_NAMESPACE,
                invite_id,
                expected_updated_at=record["updated_at"],
                record=record,
                transaction=current,
            )
            if _now_ms() >= record["expires_at"]:
                raise InvitationExpired(invite_id)
            action = (
                "mesh.invitation.confirm"
                if decision == "confirmed"
                else "mesh.invitation.reject"
            )
            self._audit_transition(
                current,
                owner_id,
                action,
                actor_kind=decider_kind,
                actor_id=decider_id,
                meta={"invite_id": invite_id},
            )
            if consent_observation is not None:
                self._credentials.plane_runtime.repositories.history.sessions.assert_current_consent(
                    current, observation=consent_observation
                )
            if _now_ms() >= record["expires_at"]:
                raise InvitationExpired(invite_id)
        return record

    def list_members(self, owner_id: str) -> list[dict[str, Any]]:
        records = self._list(owner_id, MEMBER_NAMESPACE)
        records.sort(key=lambda item: item["enrolled_at"], reverse=True)
        return [public_member(record) for record in records]

    def get_member(
        self, owner_id: str, member_id: str, *, transaction=None
    ) -> dict[str, Any]:
        record = self._get(
            owner_id, MEMBER_NAMESPACE, member_id, transaction=transaction
        )
        if record is None or record.get("owner_id") != owner_id:
            raise MemberNotFound(member_id)
        return record

    def revoke_member(
        self, owner_id: str, member_id: str, *, transaction: Any = None
    ) -> dict[str, Any]:
        record = self.get_member(owner_id, member_id, transaction=transaction)
        if record["status"] == MEMBER_REVOKED_STATUS:
            return record
        record["status"] = MEMBER_REVOKED_STATUS
        record["revoked_at"] = _now_ms()
        with self._transaction(transaction) as current:
            mesh, member, _ = self._current_member(current, owner_id, member_id)
            self._membership.revoke_member(
                current,
                owner_id=owner_id,
                mesh_id=mesh.mesh_id,
                member_id=member_id,
                revocation_id=uuid.uuid4().hex,
                expected_mesh_version=mesh.record_version,
                expected_member_version=member.record_version,
            )
            self._transition(
                owner_id,
                MEMBER_NAMESPACE,
                member_id,
                expected_updated_at=record["updated_at"],
                record=record,
                transaction=current,
            )
            self._audit_transition(
                current, owner_id, "mesh.member.revoke", meta={"member_id": member_id}
            )
        return record

    def prepare_redemption(
        self,
        *,
        owner_id: str,
        invite_id: str,
        token: str,
        challenge: str,
        signature: str,
        transaction: Any = None,
    ) -> dict[str, Any]:
        record = (
            self._get(owner_id, INVITATION_NAMESPACE, invite_id)
            if transaction is None
            else self._get(
                owner_id, INVITATION_NAMESPACE, invite_id, transaction=transaction
            )
        )
        if (
            record is None
            or record.get("invite_id") != invite_id
            or record.get("owner_id") != owner_id
            or record.get("mesh_id") != mesh_id_for_owner(owner_id)
        ):
            raise InvitationNotFound(invite_id)
        try:
            token_bytes = _b64url_decode(token)
            challenge_bytes = _b64url_decode(challenge)
            signature_bytes = _b64url_decode(signature)
        except ValueError as exc:
            raise RedeemInvalid("redemption payload is malformed") from exc

        if not hmac.compare_digest(
            record.get("token_sha256", ""), _sha256_hex(token_bytes)
        ):
            raise RedeemInvalid("invitation token does not match")
        if record["status"] == "redeemed":
            raise InvitationStateInvalid("invitation has already been redeemed")
        if record["status"] == "rejected":
            raise InvitationStateInvalid("invitation was rejected")
        if record["status"] != "confirmed":
            raise InvitationStateInvalid("invitation is not confirmed")
        if _now_ms() >= record["expires_at"]:
            raise InvitationExpired(invite_id)
        if not hmac.compare_digest(
            record.get("challenge_sha256", ""), _sha256_hex(challenge_bytes)
        ):
            raise RedeemInvalid("challenge does not match")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                _b64url_decode(record["device_key"]["x"])
            )
            public_key.verify(signature_bytes, challenge_bytes)
        except Exception as exc:
            raise RedeemInvalid(
                "signature does not prove possession of the device key"
            ) from exc

        return record

    def redeem_invitation(
        self,
        *,
        owner_id: str,
        invite_id: str,
        token: str,
        challenge: str,
        signature: str,
        transaction: Any = None,
    ) -> dict[str, Any]:
        record = self.prepare_redemption(
            owner_id=owner_id,
            invite_id=invite_id,
            token=token,
            challenge=challenge,
            signature=signature,
            transaction=transaction,
        )
        confirmed_scopes = normalize_scopes(record.get("confirmed_scopes") or [])
        member_id = uuid.uuid4().hex
        now = _now_ms()
        member: dict[str, Any] = {
            "version": RECORD_VERSION,
            "member_id": member_id,
            "mesh_id": record["mesh_id"],
            "owner_id": record["owner_id"],
            "label": record["label"],
            "device_key": record["device_key"],
            "device_key_fingerprint": record["device_key_fingerprint"],
            "status": MEMBER_ACTIVE_STATUS,
            "scopes": confirmed_scopes,
            "invite_id": invite_id,
            "confirmed_by": record.get("confirmed_by"),
            "enrolled_at": now,
            "updated_at": now,
            "authority_revision": now,
            "custody": (record.get("confirmed_by") or {}).get("custody"),
            "revoked_at": None,
        }
        record["status"] = "redeemed"
        record["redeemed_at"] = now
        record["member_id"] = member_id

        expected_updated_at = int(record["updated_at"])
        updated_at = max(_now_ms(), expected_updated_at + 1)
        record["updated_at"] = updated_at
        try:
            with self._transaction(transaction) as current:
                self._lock_mesh(current, owner_id)
                confirmer = record.get("confirmed_by")
                if not isinstance(confirmer, dict):
                    raise ConfirmForbidden("confirmation identity is unavailable")
                if confirmer.get("kind") == "owner":
                    if confirmer.get("id") != owner_id:
                        raise ConfirmForbidden("owner confirmation is foreign")
                elif confirmer.get("kind") == "member":
                    if (
                        type(confirmer.get("revision")) is not int
                        or type(confirmer.get("authority_revision")) is not int
                        or not isinstance(confirmer.get("authority_digest"), str)
                        or not isinstance(confirmer.get("fingerprint"), str)
                    ):
                        raise ConfirmForbidden("member confirmation is unbound")
                    self._fence_confirmer(
                        current,
                        owner_id,
                        confirmer.get("id"),
                        authority_revision=confirmer["authority_revision"],
                        authority_digest=confirmer["authority_digest"],
                        fingerprint=confirmer["fingerprint"],
                        scopes=confirmed_scopes,
                    )
                else:
                    raise ConfirmForbidden("confirmation identity is invalid")
                holder = self._active_key_holder(
                    current, owner_id, record["device_key_fingerprint"]
                )
                if holder is not None:
                    if holder.get("invite_id") == invite_id:
                        raise InvitationStateInvalid(
                            "invitation was redeemed or changed concurrently"
                        )
                    raise IdentityAmbiguous(holder["member_id"])
                if _now_ms() >= record["expires_at"]:
                    raise InvitationExpired(invite_id)
                proven = self._membership.prove_enrollment_challenge(
                    current,
                    owner_id=owner_id,
                    challenge_id=invite_id,
                    challenge_digest=record["challenge_sha256"],
                    as_of=_now_ms(),
                )
                if proven.member_id != invite_id or proven.mesh_id != record["mesh_id"]:
                    raise RedeemInvalid("challenge membership binding is foreign")
                consumed = self._membership.consume_invitation(
                    current,
                    owner_id=owner_id,
                    invitation_id=invite_id,
                    invitation_digest=record["token_sha256"],
                    as_of=_now_ms(),
                )
                mesh = self._membership.get_mesh(
                    current, owner_id=owner_id, mesh_id=record["mesh_id"]
                )
                _, activated = self._membership.confirm_invitation(
                    current,
                    owner_id=owner_id,
                    invitation_id=invite_id,
                    member_id=member_id,
                    expected_invitation_version=consumed.record_version,
                    expected_mesh_version=mesh.record_version,
                    expected_member_version=0,
                    as_of=_now_ms(),
                    display_label=record["label"],
                )
                self._membership.bind_public_identity(
                    current,
                    owner_id=owner_id,
                    mesh_id=record["mesh_id"],
                    member_id=member_id,
                    identity_id=member_id,
                    algorithm="Ed25519",
                    public_key=json.dumps(
                        member["device_key"], sort_keys=True, separators=(",", ":")
                    ),
                    key_fingerprint=_sha256_hex(
                        _b64url_decode(member["device_key"]["x"])
                    ),
                    activated_epoch=activated.membership_epoch,
                )
                member["membership_epoch"] = activated.membership_epoch
                self._credentials.repository.compare_and_set_ciphertext(
                    current,
                    owner_id=owner_id,
                    agent_id=INVITATION_NAMESPACE,
                    credential_key=invite_id,
                    expected_updated_at=expected_updated_at,
                    encrypted_value=_encode_record(record),
                    updated_at=updated_at,
                )
                if _now_ms() >= record["expires_at"]:
                    raise InvitationExpired(invite_id)
                self._credentials.repository.upsert_credential(
                    current,
                    owner_id=owner_id,
                    agent_id=MEMBER_NAMESPACE,
                    credential_key=member_id,
                    encrypted_value=_encode_record(member),
                    updated_at=now,
                )
                self._audit_transition(
                    current,
                    owner_id,
                    "mesh.member.redeem",
                    actor_kind="device",
                    actor_id=member["device_key_fingerprint"],
                    meta={
                        "confirmed_by": confirmer,
                        "invite_id": invite_id,
                        "member_id": member_id,
                        "fingerprint": member["device_key_fingerprint"],
                    },
                )
                if _now_ms() >= record["expires_at"]:
                    raise InvitationExpired(invite_id)
        except RepositoryConflictError as exc:
            raise InvitationStateInvalid(
                "invitation was redeemed or changed concurrently"
            ) from exc
        return {"member": member}


def parse_enrollment_payload(raw: Any) -> dict[str, str]:
    if not isinstance(raw, str) or not raw or len(raw) > _PAYLOAD_MAX_CHARS:
        raise RedeemInvalid("enrollment payload is malformed")
    try:
        parsed = json.loads(_b64url_decode(raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RedeemInvalid("enrollment payload is malformed") from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("v") != RECORD_VERSION
        or not isinstance(parsed.get("o"), str)
        or not isinstance(parsed.get("i"), str)
        or not isinstance(parsed.get("t"), str)
        or not isinstance(parsed.get("c"), str)
    ):
        raise RedeemInvalid("enrollment payload is malformed")
    return {
        "owner_id": parsed["o"],
        "invite_id": parsed["i"],
        "token": parsed["t"],
        "challenge": parsed["c"],
    }
