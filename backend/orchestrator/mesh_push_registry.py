"""Owns publisher-only mesh wake registrations in an isolated encrypted Plane credential namespace.
Authorized registration issues an opaque wake-only capability; owner fences, revocation, expiry and durable rate counters gate its use."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from astralplane.contracts import IsolationLevel
from astralplane.repositories import RepositoryConflictError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.plane_repository_context import repository_from

NAMESPACE = "astral-mesh-wake"
PUBLISHER = "astral"
MAX_REVISION = 2**53 - 1
CAPABILITY_LIFETIME_MS = 23 * 3_600_000
MIN_INTERVAL_MS = 300_000
HOUR_MS = 3_600_000
MAX_PER_HOUR = 4
MAX_DESTINATIONS = 32
AUDIENCE = "astral.publisher.mesh-wake.v1"
_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,2046}={0,2}\Z")
_RECORD_FIELDS = frozenset({"v", "owner_id", "mesh_id", "device_id", "revision", "state", "token",
                            "capability_digest", "expires_at_ms", "hits"})
_CLAIM_FIELDS = frozenset({"v", "aud", "owner_id", "mesh_id", "device_id", "revision", "expires_at_ms", "nonce"})


class MeshPushError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class MeshPushStatus:
    mesh_id: str
    device_id: str
    revision: int
    state: str
    expires_at_ms: int


@dataclass(frozen=True)
class MeshPushRegistration:
    publisher: str
    mesh_id: str
    device_id: str
    revision: int
    expires_at_ms: int
    capability: str = field(repr=False)


@dataclass(frozen=True)
class MeshWakeTarget:
    owner_id: str
    mesh_id: str
    device_id: str
    revision: int
    token: str = field(repr=False)
    capability_digest: str = field(repr=False)


def registry_key(mesh_id: str, device_id: str) -> str:
    if type(mesh_id) is not str or type(device_id) is not str or not _ID.fullmatch(mesh_id) or not _ID.fullmatch(device_id):
        raise MeshPushError("registration_invalid")
    return "_mesh_wake_" + hashlib.sha256((mesh_id + "\n" + device_id).encode()).hexdigest()


def _owner(owner_id: Any) -> str:
    if type(owner_id) is not str or not 1 <= len(owner_id) <= 255 or any(ord(c) < 33 for c in owner_id):
        raise MeshPushError("registration_invalid")
    return owner_id


def _revision(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_REVISION:
        raise MeshPushError("registration_invalid")
    return value


def _token(value: Any) -> str:
    if (type(value) is not str or not value or len(value) > 4096 or not value.isascii()
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise MeshPushError("registration_invalid")
    return value


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _status(record: dict) -> MeshPushStatus:
    return MeshPushStatus(*(record[key] for key in ("mesh_id", "device_id", "revision", "state", "expires_at_ms")))


def _configured_key() -> bytes:
    return os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").encode()


class MeshPushRegistry:
    def __init__(self, *, plane_runtime: Any, audit_repository: Any,
                 encryption_key: Callable[[], bytes] = _configured_key,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.repository, self.runtime = repository_from("credentials", plane_runtime=plane_runtime, repositories=None)
        self.audit_repository = audit_repository
        self._key = encryption_key
        self._clock = clock

    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int or not 1 <= now <= MAX_REVISION - CAPABILITY_LIFETIME_MS:
            raise MeshPushError("publisher_unavailable", 503)
        return now

    def _ciphers(self) -> tuple[Fernet, Fernet]:
        try:
            key = self._key()
            record = Fernet(key)
            raw = base64.urlsafe_b64decode(key)
            derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=AUDIENCE.encode()).derive(raw)
            return record, Fernet(base64.urlsafe_b64encode(derived))
        except Exception:
            raise MeshPushError("publisher_unavailable", 503) from None

    @contextmanager
    def _transaction(self):
        try:
            with self.runtime.transaction(isolation=IsolationLevel.SERIALIZABLE) as transaction:
                yield transaction
        except MeshPushError:
            raise
        except Exception as error:
            cause = error
            for _ in range(5):
                if isinstance(cause, RepositoryConflictError) or getattr(cause, "pgcode", None) in {"40001", "40P01"}:
                    raise MeshPushError("revision_conflict", 409) from None
                cause = cause.__cause__ or cause.__context__
                if cause is None:
                    break
            raise MeshPushError("publisher_unavailable", 503) from None

    def _read(self, transaction, owner: str, mesh: str, device: str, cipher: Fernet):
        row = self.repository.get_credential(transaction, owner_id=owner, agent_id=NAMESPACE,
                                             credential_key=registry_key(mesh, device))
        if row is None:
            return None, None
        try:
            plain = cipher.decrypt(row.encrypted_value.encode())
            if len(plain) > 32_768:
                raise ValueError
            record = json.loads(plain)
            if (not isinstance(record, dict) or set(record) != _RECORD_FIELDS or record["v"] != 1
                    or record["owner_id"] != owner or record["mesh_id"] != mesh or record["device_id"] != device
                    or type(record["v"]) is not int or record["state"] not in {"active", "revoked"} or type(row.updated_at) is not int
                    or not 1 <= row.updated_at <= MAX_REVISION):
                raise ValueError
            _revision(record["revision"], 1)
            _revision(record["expires_at_ms"])
            hits = record["hits"]
            if (type(hits) is not list or len(hits) > MAX_PER_HOUR or any(type(hit) is not int or hit < 1 for hit in hits)
                    or hits != sorted(hits)):
                raise ValueError
            if record["state"] == "active":
                _revision(record["expires_at_ms"], 1)
                _token(record["token"])
                if type(record["capability_digest"]) is not str or not _ID.fullmatch(record["capability_digest"]):
                    raise ValueError
            elif record["token"] is not None or record["capability_digest"] is not None or record["expires_at_ms"] != 0:
                raise ValueError
            return record, row.updated_at
        except (InvalidToken, ValueError, TypeError, KeyError, MeshPushError):
            raise MeshPushError("publisher_unavailable", 503) from None

    def _write(self, transaction, record: dict, observed: int | None, now: int, cipher: Fernet):
        updated = max(now, (observed or 0) + 1)
        _revision(updated, 1)
        values = dict(owner_id=record["owner_id"], agent_id=NAMESPACE,
                      credential_key=registry_key(record["mesh_id"], record["device_id"]),
                      encrypted_value=cipher.encrypt(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).decode(),
                      updated_at=updated)
        if observed is None:
            self.repository.upsert_credential(transaction, **values)
        else:
            self.repository.compare_and_set_ciphertext(transaction, expected_updated_at=observed, **values)

    def _audit(self, transaction, owner: str, action: str, record: dict):
        event = AuditEventCreate(event_id=str(uuid.uuid4()), actor_user_id=owner, auth_principal=f"owner:{owner}",
                                 event_class="agent_lifecycle", action_type=action, description=action,
                                 correlation_id=str(uuid.uuid4()), outcome="success", started_at=datetime.now(timezone.utc),
                                 inputs_meta={key: record[key] for key in ("mesh_id", "device_id", "revision")})
        if self.audit_repository is None:
            raise MeshPushError("publisher_unavailable", 503)
        receipt = self.audit_repository.insert_in_transaction(event, transaction=transaction, plane_runtime=self.runtime)
        if (not isinstance(receipt, AuditEventDTO) or receipt.event_id != event.event_id
                or receipt.action_type != action or receipt.outcome != "success"):
            raise MeshPushError("publisher_unavailable", 503)

    def register(self, owner_id: str, *, mesh_id: str, device_id: str, token: str, expected_revision: int) -> MeshPushRegistration:
        owner = _owner(owner_id)
        registry_key(mesh_id, device_id)
        token = _token(token)
        expected = _revision(expected_revision)
        cipher, capability_cipher = self._ciphers()
        now = self._now()
        expires = now + CAPABILITY_LIFETIME_MS
        with self._transaction() as transaction:
            old, observed = self._read(transaction, owner, mesh_id, device_id, cipher)
            if (old["revision"] if old else 0) != expected:
                raise MeshPushError("revision_conflict", 409)
            if old is None:
                keys = self.repository.list_credential_keys(transaction, owner_id=owner, agent_id=NAMESPACE,
                                                             limit=MAX_DESTINATIONS + 1)
                if len(keys) >= MAX_DESTINATIONS:
                    raise MeshPushError("registration_capacity", 409)
            revision = _revision(expected + 1, 1)
            claims = dict(v=1, aud=AUDIENCE, owner_id=owner, mesh_id=mesh_id, device_id=device_id,
                          revision=revision, expires_at_ms=expires, nonce=secrets.token_urlsafe(32))
            capability = capability_cipher.encrypt(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).decode()
            if len(capability) > 2048:
                raise MeshPushError("registration_invalid")
            record = dict(v=1, owner_id=owner, mesh_id=mesh_id, device_id=device_id, revision=revision,
                          state="active", token=token, capability_digest=_digest(capability), expires_at_ms=expires,
                          hits=list(old["hits"]) if old else [])
            self._write(transaction, record, observed, now, cipher)
            self._audit(transaction, owner, "push.mesh.register", record)
        return MeshPushRegistration(PUBLISHER, mesh_id, device_id, revision, expires, capability)

    def status(self, owner_id: str, *, mesh_id: str, device_id: str) -> MeshPushStatus:
        owner = _owner(owner_id)
        registry_key(mesh_id, device_id)
        cipher, _ = self._ciphers()
        with self._transaction() as transaction:
            record, _ = self._read(transaction, owner, mesh_id, device_id, cipher)
            if record is None:
                raise MeshPushError("registration_missing", 404)
            return _status(record)

    def revoke(self, owner_id: str, *, mesh_id: str, device_id: str, expected_revision: int) -> MeshPushStatus:
        owner = _owner(owner_id)
        registry_key(mesh_id, device_id)
        expected = _revision(expected_revision, 1)
        cipher, _ = self._ciphers()
        now = self._now()
        with self._transaction() as transaction:
            record, observed = self._read(transaction, owner, mesh_id, device_id, cipher)
            if record is None:
                raise MeshPushError("registration_missing", 404)
            if record["revision"] != expected:
                raise MeshPushError("revision_conflict", 409)
            if record["state"] != "revoked":
                self._retire(transaction, record, observed, now, cipher, "push.mesh.revoke")
            return _status(record)

    def _claims(self, capability: str) -> dict:
        if type(capability) is not str or len(capability) > 2048 or not _CAPABILITY.fullmatch(capability):
            raise MeshPushError("capability_invalid", 403)
        _, cipher = self._ciphers()
        try:
            claims = json.loads(cipher.decrypt(capability.encode()))
            if (not isinstance(claims, dict) or set(claims) != _CLAIM_FIELDS or type(claims["v"]) is not int
                    or claims["v"] != 1 or claims["aud"] != AUDIENCE
                    or type(claims["nonce"]) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}", claims["nonce"]) is None):
                raise ValueError
            _owner(claims["owner_id"])
            registry_key(claims["mesh_id"], claims["device_id"])
            _revision(claims["revision"], 1)
            _revision(claims["expires_at_ms"], 1)
            return claims
        except (InvalidToken, ValueError, TypeError, KeyError, MeshPushError):
            raise MeshPushError("capability_invalid", 403) from None

    def admit(self, capability: str, *, mesh_id: str, device_id: str) -> MeshWakeTarget:
        claims = self._claims(capability)
        if claims["mesh_id"] != mesh_id or claims["device_id"] != device_id:
            raise MeshPushError("capability_invalid", 403)
        now = self._now()
        if now >= claims["expires_at_ms"]:
            raise MeshPushError("capability_expired", 403)
        cipher, _ = self._ciphers()
        with self._transaction() as transaction:
            record, observed = self._read(transaction, claims["owner_id"], mesh_id, device_id, cipher)
            if (record is None or record["state"] != "active" or record["revision"] != claims["revision"]
                    or record["expires_at_ms"] != claims["expires_at_ms"]
                    or not hmac.compare_digest(record["capability_digest"], _digest(capability))):
                raise MeshPushError("capability_invalid", 403)
            hits = [hit for hit in record["hits"] if now - hit < HOUR_MS]
            if hits and (now - hits[-1] < MIN_INTERVAL_MS or len(hits) >= MAX_PER_HOUR):
                raise MeshPushError("rate_limited", 429)
            record["hits"] = [*hits, now]
            self._write(transaction, record, observed, now, cipher)
            self._audit(transaction, claims["owner_id"], "push.mesh.wake.authorized", record)
            return MeshWakeTarget(record["owner_id"], mesh_id, device_id, record["revision"], record["token"], record["capability_digest"])

    def current(self, target: MeshWakeTarget) -> bool:
        cipher, _ = self._ciphers()
        with self._transaction() as transaction:
            record, _ = self._read(transaction, target.owner_id, target.mesh_id, target.device_id, cipher)
            return self._matches(record, target) and self._now() < record["expires_at_ms"]

    def invalidate(self, target: MeshWakeTarget) -> bool:
        cipher, _ = self._ciphers()
        now = self._now()
        with self._transaction() as transaction:
            record, observed = self._read(transaction, target.owner_id, target.mesh_id, target.device_id, cipher)
            if not self._matches(record, target):
                return False
            self._retire(transaction, record, observed, now, cipher, "push.mesh.invalidated")
            return True

    @staticmethod
    def _matches(record: dict | None, target: MeshWakeTarget) -> bool:
        return bool(record and record["state"] == "active" and record["revision"] == target.revision
                    and hmac.compare_digest(record["capability_digest"], target.capability_digest)
                    and hmac.compare_digest(record["token"], target.token))

    def _retire(self, transaction, record: dict, observed: int, now: int, cipher: Fernet, action: str):
        record.update(revision=_revision(record["revision"] + 1, 1), state="revoked", token=None,
                      capability_digest=None, expires_at_ms=0)
        self._write(transaction, record, observed, now, cipher)
        self._audit(transaction, record["owner_id"], action, record)
