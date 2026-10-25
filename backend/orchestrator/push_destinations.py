"""Owner-bound push destination enrollment, token rotation, and revocation over
AstralPlane's encrypted per-owner credential store; provider tokens are Fernet-encrypted
at rest, projected publicly without token material, and every transition is audited.
push_api.py exposes the authenticated owner boundary and push_delivery.py consumes
active destinations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from astralplane.contracts import IsolationLevel
from astralplane.repositories import RepositoryConflictError

from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

DESTINATION_NAMESPACE = "astral-push-destination"
RECORD_VERSION = 1
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
PROVIDER_FOR_PLATFORM = {"android": "fcm", "ios": "apns"}

MAX_DESTINATIONS_PER_OWNER = 32
MAX_LABEL_CHARS = 64
MAX_TOKEN_CHARS = 4096
_LIST_LIMIT = MAX_DESTINATIONS_PER_OWNER + 8
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_FINGERPRINT_PREFIX = "sha256-"
_CREDENTIAL_KEY_SOURCE = "CREDENTIAL_ENCRYPTION_KEY"


class PushDestinationError(Exception):
    status = 500
    code = "push_destination_error"


class DestinationInvalid(PushDestinationError):
    status = 400
    code = "destination_invalid"


class DestinationNotFound(PushDestinationError):
    status = 404
    code = "destination_not_found"


class DestinationCapacity(PushDestinationError):
    status = 409
    code = "destination_capacity"


class DestinationConflict(PushDestinationError):
    status = 409
    code = "destination_conflict"


class DestinationStoreUnavailable(PushDestinationError):
    status = 503
    code = "destination_store_unavailable"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fernet() -> Fernet:
    key = os.environ.get(_CREDENTIAL_KEY_SOURCE, "")
    if not key:
        raise DestinationStoreUnavailable("destination encryption key is unavailable")
    try:
        return Fernet(key.encode())
    except Exception:
        raise DestinationStoreUnavailable(
            "destination encryption key is invalid"
        ) from None


def _encode_record(record: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(record).encode()).decode()


def _decode_record(ciphertext: str) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(ciphertext.encode())
    except InvalidToken:
        raise DestinationStoreUnavailable(
            "destination record is undecryptable"
        ) from None
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise DestinationStoreUnavailable("destination record is malformed")
    return record


def token_fingerprint(token: str) -> str:
    return _FINGERPRINT_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def validate_device_id(raw: Any) -> str:
    if not isinstance(raw, str) or _DEVICE_ID.fullmatch(raw) is None:
        raise DestinationInvalid("device_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
    return raw


def normalize_platform(raw: Any) -> str:
    if raw not in PROVIDER_FOR_PLATFORM:
        raise DestinationInvalid("platform must be one of android, ios")
    return raw


def normalize_token(raw: Any) -> str:
    if not isinstance(raw, str):
        raise DestinationInvalid("push token must be a string")
    token = raw.strip()
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise DestinationInvalid(
            f"push token must be 1..{MAX_TOKEN_CHARS} characters"
        )
    if any(ord(ch) < 0x20 for ch in token):
        raise DestinationInvalid("push token must not contain control characters")
    return token


def normalize_label(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise DestinationInvalid("label must be a string")
    label = raw.strip()
    if not label:
        return None
    if len(label) > MAX_LABEL_CHARS:
        raise DestinationInvalid(f"label must be at most {MAX_LABEL_CHARS} characters")
    return label


def public_destination(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "device_id": record["device_id"],
        "platform": record["platform"],
        "provider": record["provider"],
        "label": record.get("label"),
        "status": record["status"],
        "token_generation": record["token_generation"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "rotated_at": record.get("rotated_at"),
        "revoked_at": record.get("revoked_at"),
    }


class PushDestinationStore:
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

    @contextmanager
    def _enrollment_transaction(self):
        try:
            with self._credentials.plane_runtime.transaction(
                isolation=IsolationLevel.SERIALIZABLE
            ) as transaction:
                yield transaction
        except Exception as exc:
            cause = exc
            for _ in range(4):
                if getattr(cause, "pgcode", None) in {"40001", "40P01"}:
                    raise DestinationConflict("destination inventory changed") from None
                cause = cause.__cause__ or cause.__context__
                if cause is None:
                    break
            raise

    def _write_record(self, transaction, owner_id, record, *, expected_updated_at=None):
        values = {
            "owner_id": owner_id,
            "agent_id": DESTINATION_NAMESPACE,
            "credential_key": record["device_id"],
            "encrypted_value": _encode_record(record),
            "updated_at": record["updated_at"],
        }
        try:
            if expected_updated_at is None:
                self._credentials.repository.upsert_credential(transaction, **values)
            else:
                self._credentials.repository.compare_and_set_ciphertext(
                    transaction, expected_updated_at=expected_updated_at, **values
                )
        except RepositoryConflictError:
            raise DestinationConflict("destination revision changed") from None

    def _get_record(self, transaction, owner_id: str, device_id: str) -> dict | None:
        row = self._credentials.repository.get_credential(
            transaction,
            owner_id=owner_id,
            agent_id=DESTINATION_NAMESPACE,
            credential_key=device_id,
        )
        if row is None:
            return None
        record = _decode_record(row.encrypted_value)
        if (
            type(record.get("updated_at")) is not int
            or record["updated_at"] != row.updated_at
        ):
            raise DestinationStoreUnavailable("destination revision is invalid")
        return record

    def _list_records(self, transaction, owner_id: str) -> list[dict[str, Any]]:
        rows = self._credentials.repository.list_credentials(
            transaction,
            owner_id=owner_id,
            agent_id=DESTINATION_NAMESPACE,
            limit=_LIST_LIMIT,
        )
        if len(rows) >= _LIST_LIMIT:
            raise DestinationStoreUnavailable("destination inventory is incomplete")
        return [_decode_record(row.encrypted_value) for row in rows]

    def _audit_transition(
        self,
        transaction,
        owner_id: str,
        action: str,
        meta: dict[str, Any],
        outcome: str = "success",
    ) -> None:
        event = AuditEventCreate(
            event_id=str(uuid.uuid4()),
            actor_user_id=owner_id,
            auth_principal=f"owner:{owner_id}",
            event_class="agent_lifecycle",
            action_type=action,
            description=action,
            correlation_id=str(uuid.uuid4()),
            inputs_meta=meta,
            outcome=outcome,
            started_at=datetime.now(timezone.utc),
        )
        try:
            if self._audit is None:
                raise DestinationStoreUnavailable()
            receipt = self._audit.insert_in_transaction(
                event,
                transaction=transaction,
                plane_runtime=self._credentials.plane_runtime,
            )
            if (
                not isinstance(receipt, AuditEventDTO)
                or receipt.event_id != event.event_id
                or receipt.action_type != action
                or receipt.outcome != outcome
            ):
                raise DestinationStoreUnavailable()
        except Exception:
            raise DestinationStoreUnavailable(
                "durable push destination audit unavailable"
            ) from None

    def enroll(
        self,
        owner_id: str,
        *,
        device_id: str,
        platform: str,
        token: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        validate_device_id(device_id)
        normalize_platform(platform)
        token = normalize_token(token)
        normalize_label(label)
        fingerprint = token_fingerprint(token)
        now = _now_ms()
        with self._enrollment_transaction() as transaction:
            existing = self._get_record(transaction, owner_id, device_id)
            if existing is not None:
                now = max(now, existing["updated_at"] + 1)
            if existing is not None and (
                existing.get("status") == STATUS_REVOKED
                or existing.get("token_fingerprint") != fingerprint
            ):
                if len(self._list_records(transaction, owner_id)) >= _LIST_LIMIT - 1:
                    raise DestinationStoreUnavailable(
                        "destination inventory is incomplete"
                    )
            active_count = sum(
                1
                for record in self._list_records(transaction, owner_id)
                if record.get("status") == STATUS_ACTIVE
            )
            needs_slot = existing is None or existing.get("status") != STATUS_ACTIVE
            if needs_slot and active_count >= MAX_DESTINATIONS_PER_OWNER:
                self._audit_transition(
                    transaction,
                    owner_id,
                    "push.destination.enroll_denied",
                    {"device_id": device_id, "reason": "destination_capacity"},
                    outcome="failure",
                )
                raise DestinationCapacity(
                    f"at most {MAX_DESTINATIONS_PER_OWNER} active destinations are allowed"
                )
            if existing is not None and existing.get("status") == STATUS_ACTIVE:
                if existing.get("token_fingerprint") == fingerprint:
                    return public_destination(existing)
                action = "push.destination.rotate"
            elif existing is not None:
                action = "push.destination.reactivate"
            else:
                action = "push.destination.enroll"
            record = {
                "version": RECORD_VERSION,
                "device_id": device_id,
                "platform": platform,
                "provider": PROVIDER_FOR_PLATFORM[platform],
                "push_token": token,
                "token_fingerprint": fingerprint,
                "label": normalize_label(label),
                "status": STATUS_ACTIVE,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "rotated_at": now if action == "push.destination.rotate" else existing.get("rotated_at") if existing else None,
                "revoked_at": None,
                "token_generation": (existing or {}).get("token_generation", 0) + 1,
            }
            self._write_record(
                transaction,
                owner_id,
                record,
                expected_updated_at=existing["updated_at"] if existing else None,
            )
            self._audit_transition(
                transaction,
                owner_id,
                action,
                {
                    "device_id": device_id,
                    "platform": platform,
                    "provider": record["provider"],
                    "token_generation": record["token_generation"],
                    "token_fingerprint": fingerprint,
                },
            )
            return public_destination(record)

    def list_destinations(self, owner_id: str) -> list[dict[str, Any]]:
        with self._credentials.transaction() as transaction:
            records = self._list_records(transaction, owner_id)
        return sorted(
            (public_destination(record) for record in records),
            key=lambda item: (item["created_at"], item["device_id"]),
        )

    def get_destination(self, owner_id: str, device_id: str) -> dict[str, Any]:
        validate_device_id(device_id)
        with self._credentials.transaction() as transaction:
            record = self._get_record(transaction, owner_id, device_id)
        if record is None:
            raise DestinationNotFound("destination is unknown for this owner")
        return public_destination(record)

    def revoke(
        self,
        owner_id: str,
        device_id: str,
        *,
        reason: str = "logout",
        expected_token_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        validate_device_id(device_id)
        now = _now_ms()
        with self._credentials.transaction() as transaction:
            record = self._get_record(transaction, owner_id, device_id)
            if record is None:
                raise DestinationNotFound("destination is unknown for this owner")
            if (
                expected_token_fingerprint is not None
                and record.get("token_fingerprint") != expected_token_fingerprint
            ):
                return public_destination(record)
            if record.get("status") == STATUS_REVOKED:
                return public_destination(record)
            expected_updated_at = record["updated_at"]
            now = max(now, expected_updated_at + 1)
            record["status"] = STATUS_REVOKED
            record["revoked_at"] = now
            record["updated_at"] = now
            record["revoke_reason"] = reason
            self._write_record(
                transaction,
                owner_id,
                record,
                expected_updated_at=expected_updated_at,
            )
            self._audit_transition(
                transaction,
                owner_id,
                "push.destination.revoke",
                {"device_id": device_id, "reason": reason},
            )
            return public_destination(record)

    def delivery_targets(self, owner_id: str) -> list[dict[str, Any]]:
        with self._credentials.transaction() as transaction:
            records = self._list_records(transaction, owner_id)
        return [
            {
                "device_id": record["device_id"],
                "provider": record["provider"],
                "push_token": record["push_token"],
                "token_fingerprint": record["token_fingerprint"],
            }
            for record in records
            if record.get("status") == STATUS_ACTIVE
        ]

    def delivery_target(self, owner_id: str, device_id: str) -> dict[str, Any] | None:
        validate_device_id(device_id)
        with self._credentials.transaction() as transaction:
            record = self._get_record(transaction, owner_id, device_id)
        if record is None or record.get("status") != STATUS_ACTIVE:
            return None
        return {
            "device_id": record["device_id"],
            "provider": record["provider"],
            "push_token": record["push_token"],
            "token_fingerprint": record["token_fingerprint"],
        }
