"""Owner-confirmed personal-mesh enrollment: one-time invitations bound to a device
public key and challenge are confirmed by the owner or an authorized member and
redeemed into mesh members with attenuated tool scopes. Records live in AstralPlane's
owner-scoped credential store; endpoints are exposed by mesh_api.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

from astralplane.repositories import RepositoryConflictError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orchestrator.delegation import attenuate_scopes
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.tool_permissions import VALID_SCOPES

logger = logging.getLogger("orchestrator.mesh_enrollment")

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
_MEMBER_KEY_BYTES = 32
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


class ConfirmForbidden(MeshEnrollmentError):
    status = 403
    code = "confirm_forbidden"


class RedeemInvalid(MeshEnrollmentError):
    status = 400
    code = "redeem_invalid"


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
    if os.path.exists(path):
        with open(path, "rb") as file:
            return file.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temporary_path, "wb") as file:
        file.write(key)
    os.replace(temporary_path, path)
    return key


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
    record = json.loads(plaintext)
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
    return {
        "invite_id": record["invite_id"],
        "mesh_id": record["mesh_id"],
        "label": record["label"],
        "device_key_fingerprint": record["device_key_fingerprint"],
        "requested_scopes": record["requested_scopes"],
        "confirmed_scopes": record.get("confirmed_scopes"),
        "status": record["status"],
        "created_by": record.get("created_by"),
        "confirmed_by": record.get("confirmed_by"),
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "redeemed_at": record.get("redeemed_at"),
        "member_id": record.get("member_id"),
    }


def public_member(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "member_id": record["member_id"],
        "mesh_id": record["mesh_id"],
        "label": record["label"],
        "device_key_fingerprint": record["device_key_fingerprint"],
        "status": record["status"],
        "scopes": record["scopes"],
        "enrolled_at": record["enrolled_at"],
        "confirmed_by": record.get("confirmed_by"),
        "revoked_at": record.get("revoked_at"),
    }


class MeshEnrollmentStore:
    def __init__(
        self,
        db: Any = None,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
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

    def _get(self, owner_id: str, namespace: str, key: str) -> dict[str, Any] | None:
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

    def _put(self, owner_id: str, namespace: str, key: str, record: dict[str, Any]) -> int:
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
    ) -> None:
        updated_at = max(_now_ms(), expected_updated_at + 1)
        record["updated_at"] = updated_at
        try:
            self._credentials.call(
                self._credentials.repository.compare_and_set_ciphertext,
                owner_id=owner_id,
                agent_id=namespace,
                credential_key=key,
                expected_updated_at=expected_updated_at,
                encrypted_value=_encode_record(record),
                updated_at=updated_at,
            )
        except RepositoryConflictError as exc:
            raise InvitationStateInvalid(
                "the invitation changed concurrently"
            ) from exc

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
    ) -> dict[str, Any]:
        normalized_label = _normalize_label(label)
        normalized_key = normalize_device_key(device_key)
        normalized_scopes = normalize_scopes(scopes)
        ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be {MIN_TTL_SECONDS}-{MAX_TTL_SECONDS}"
            )
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
        self._put(owner_id, INVITATION_NAMESPACE, invite_id, record)
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

    def get_invitation(self, owner_id: str, invite_id: str) -> dict[str, Any]:
        record = self._get(owner_id, INVITATION_NAMESPACE, invite_id)
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
    ) -> dict[str, Any]:
        if decision not in ("confirmed", "rejected"):
            raise ValueError("decision must be confirmed or rejected")
        record = self.get_invitation(owner_id, invite_id)
        if record["status"] != "pending":
            raise InvitationStateInvalid(
                f"invitation is {record['status']}, not pending"
            )
        if _now_ms() >= record["expires_at"]:
            raise InvitationExpired(invite_id)
        creator = record.get("created_by") or {}
        if (
            decider_kind == "member"
            and creator.get("kind") == "member"
            and creator.get("id") == decider_id
        ):
            raise ConfirmForbidden("a member cannot confirm its own invitation")

        if decision == "confirmed":
            confirmed_by = {"kind": decider_kind, "id": decider_id}
            available = (
                GRANTABLE_SCOPES if decider_kind == "owner" else self._member_scopes(
                    owner_id, decider_id
                )
            )
            record["confirmed_scopes"] = attenuate_scopes(
                available, record["requested_scopes"]
            )
            record["confirmed_by"] = confirmed_by
        else:
            record["confirmed_by"] = None
        record["status"] = decision
        record["decided_at"] = _now_ms()
        self._transition(
            owner_id,
            INVITATION_NAMESPACE,
            invite_id,
            expected_updated_at=int(record["updated_at"]),
            record=record,
        )
        return record

    def _member_scopes(self, owner_id: str, member_id: str) -> list[str]:
        member = self._get(owner_id, MEMBER_NAMESPACE, member_id)
        if member is None or member.get("status") != MEMBER_ACTIVE_STATUS:
            raise MemberNotFound(member_id)
        return list(member["scopes"])

    def list_members(self, owner_id: str) -> list[dict[str, Any]]:
        records = self._list(owner_id, MEMBER_NAMESPACE)
        records.sort(key=lambda item: item["enrolled_at"], reverse=True)
        return [public_member(record) for record in records]

    def get_member(self, owner_id: str, member_id: str) -> dict[str, Any]:
        record = self._get(owner_id, MEMBER_NAMESPACE, member_id)
        if record is None or record.get("owner_id") != owner_id:
            raise MemberNotFound(member_id)
        return record

    def revoke_member(self, owner_id: str, member_id: str) -> dict[str, Any]:
        record = self.get_member(owner_id, member_id)
        if record["status"] == MEMBER_REVOKED_STATUS:
            return record
        record["status"] = MEMBER_REVOKED_STATUS
        record["revoked_at"] = _now_ms()
        self._transition(
            owner_id,
            MEMBER_NAMESPACE,
            member_id,
            expected_updated_at=int(record["updated_at"]),
            record=record,
        )
        return record

    def authenticate_member(
        self, owner_id: str, member_id: str, member_key: str
    ) -> dict[str, Any]:
        if not owner_id or not member_id or not member_key:
            raise MemberUnauthorized("member credential is malformed")
        record = self._get(owner_id, MEMBER_NAMESPACE, member_id)
        if record is None or record.get("owner_id") != owner_id:
            raise MemberNotFound(member_id)
        expected = record.get("member_secret_sha256", "")
        if not hmac.compare_digest(expected, _sha256_hex(member_key.encode())):
            raise MemberUnauthorized("member credential does not match")
        if record.get("status") != MEMBER_ACTIVE_STATUS:
            raise MemberUnauthorized("member is revoked")
        return record

    def redeem_invitation(
        self,
        *,
        owner_id: str,
        invite_id: str,
        token: str,
        challenge: str,
        signature: str,
    ) -> dict[str, Any]:
        record = self._get(owner_id, INVITATION_NAMESPACE, invite_id)
        if record is None or record.get("invite_id") != invite_id:
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

        confirmed_scopes = normalize_scopes(record.get("confirmed_scopes") or [])
        member_id = uuid.uuid4().hex
        member_key = secrets.token_urlsafe(_MEMBER_KEY_BYTES)
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
            "member_secret_sha256": _sha256_hex(member_key.encode()),
            "invite_id": invite_id,
            "confirmed_by": record.get("confirmed_by"),
            "enrolled_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        record["status"] = "redeemed"
        record["redeemed_at"] = now
        record["member_id"] = member_id

        # CAS on updated_at makes concurrent redemption single-winner; the member upsert shares the transaction, so a lost race leaves no orphan member
        expected_updated_at = int(record["updated_at"])
        updated_at = max(_now_ms(), expected_updated_at + 1)
        record["updated_at"] = updated_at
        try:
            with self._credentials.transaction() as transaction:
                self._credentials.repository.compare_and_set_ciphertext(
                    transaction,
                    owner_id=owner_id,
                    agent_id=INVITATION_NAMESPACE,
                    credential_key=invite_id,
                    expected_updated_at=expected_updated_at,
                    encrypted_value=_encode_record(record),
                    updated_at=updated_at,
                )
                self._credentials.repository.upsert_credential(
                    transaction,
                    owner_id=owner_id,
                    agent_id=MEMBER_NAMESPACE,
                    credential_key=member_id,
                    encrypted_value=_encode_record(member),
                    updated_at=now,
                )
        except RepositoryConflictError as exc:
            raise InvitationStateInvalid(
                "invitation was redeemed or changed concurrently"
            ) from exc
        return {"member": member, "member_key": member_key}


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


def audit_mesh_event(
    actor_id: str,
    action_type: str,
    description: str,
    *,
    outcome: str = "success",
    meta: dict[str, Any] | None = None,
) -> None:
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder
        from audit.schemas import AuditEventCreate

        recorder = get_recorder()
        if recorder is None:
            return
        recorder.record_blocking(
            AuditEventCreate(
                actor_user_id=actor_id or "unknown",
                auth_principal=actor_id or "unknown",
                event_class="agent_lifecycle",
                action_type=action_type,
                description=description[:1024],
                correlation_id=str(meta.get("invite_id") or meta.get("member_id") or uuid.uuid4().hex),
                outcome=outcome,
                inputs_meta=meta or {},
                started_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        logger.debug("mesh audit event failed (%s)", action_type, exc_info=True)
