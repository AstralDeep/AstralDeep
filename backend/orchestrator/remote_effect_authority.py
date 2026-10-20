"""Serializes SSH machine authority changes with effect reservations through Plane.
Encrypted owner and machine incarnations preserve revocation across trust and credential replacement.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator import mesh_enrollment as me

_NAMESPACE = "remote.effect-authority.v1"


class RemoteAuthorityUnavailable(RuntimeError):
    pass


def lock_owner(runtime, transaction, owner_id):
    runtime.repositories.stop_epochs.get(transaction, owner_id=owner_id, for_update=True)


def _incarnation(runtime, transaction, owner_id, key, *, rotate=False):
    repository = runtime.repositories.credentials
    row = repository.get_credential(transaction, owner_id=owner_id,
                                    agent_id=_NAMESPACE, credential_key=key)
    if row is not None:
        try:
            value = me._decode_record(row.encrypted_value)
            if (type(value) is not dict or set(value) != {"version", "incarnation"}
                    or value["version"] != 1 or type(value["incarnation"]) is not str
                    or uuid.UUID(value["incarnation"]).hex != value["incarnation"]):
                raise ValueError
        except Exception as exc:
            raise RemoteAuthorityUnavailable("remote_authority_invalid") from exc
        if not rotate:
            return value["incarnation"]
    incarnation = uuid.uuid4().hex
    repository.upsert_credential(transaction, owner_id=owner_id, agent_id=_NAMESPACE,
        credential_key=key, encrypted_value=me._encode_record({"version": 1, "incarnation": incarnation}),
        updated_at=max(int(time.time() * 1000), 1 if row is None else row.updated_at + 1))
    audit = AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
    receipt = audit.insert_in_transaction(AuditEventCreate(
        event_id=str(uuid.uuid4()), actor_user_id=owner_id, auth_principal=owner_id,
        event_class="agent_lifecycle", action_type="remote_machine.authority_rotated",
        description="SSH machine authority changed", correlation_id=incarnation,
        inputs_meta={"target": key}, outcome="success", started_at=datetime.now(UTC)),
        transaction=transaction, plane_runtime=runtime)
    if not isinstance(receipt, AuditEventDTO):
        raise RemoteAuthorityUnavailable("remote_authority_audit_unavailable")
    return incarnation


def rotate_authority(runtime, transaction, owner_id, machine_id=None):
    lock_owner(runtime, transaction, owner_id)
    return _incarnation(runtime, transaction, owner_id,
                        "owner" if machine_id is None else "machine:" + machine_id, rotate=True)


def authority_digest(runtime, transaction, owner_id, machine_id):
    lock_owner(runtime, transaction, owner_id)
    machine = runtime.repositories.remote.get_machine(transaction, owner_id=owner_id, machine_id=machine_id)
    credential = runtime.repositories.credentials.get_machine_credential(
        transaction, owner_id=owner_id, machine_id=machine_id)
    if machine is None or credential is None or not machine.host_key_fingerprint:
        raise RemoteAuthorityUnavailable("remote_authority_unavailable")
    material = [owner_id, machine_id, machine.address, machine.port, machine.username,
        machine.host_key_fingerprint, credential.credential_type, credential.updated_at,
        _incarnation(runtime, transaction, owner_id, "owner"),
        _incarnation(runtime, transaction, owner_id, "machine:" + machine_id)]
    return hashlib.sha256(json.dumps(material, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
