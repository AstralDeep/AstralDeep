"""Owner-scoped SSH machine inventory and credential lookup: build_target() loads a
machine and decrypts its credential into a transport MachineTarget for
remote_transport.py. Every query is scoped so one user can never see another's
machines.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from astralplane.repositories.remote import RemoteMachine
from orchestrator.credential_manager import CredentialNotConfigured
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.remote_transport import MachineTarget
from orchestrator.remote_effect_authority import lock_owner, rotate_authority

logger = logging.getLogger("RemoteMachines")


class MachineNotFound(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _remote_context(db) -> PlaneRepositoryContext:
    repository, runtime = repository_from(
        "remote",
        plane_runtime=None,
        repositories=None,
        legacy_database=db,
    )
    return PlaneRepositoryContext(
        repository=repository,
        plane_runtime=runtime,
        legacy_database=db,
    )


def _tracked_context(db) -> PlaneRepositoryContext:
    repository, runtime = repository_from(
        "tracked_jobs",
        plane_runtime=None,
        repositories=None,
        legacy_database=db,
    )
    return PlaneRepositoryContext(
        repository=repository,
        plane_runtime=runtime,
        legacy_database=db,
    )


def _machine_dict(machine: RemoteMachine) -> Dict:
    return {
        "machine_id": machine.machine_id,
        "owner_user_id": machine.owner_id,
        "label": machine.label,
        "address": machine.address,
        "port": machine.port,
        "username": machine.username,
        "os_family": machine.os_family,
        "role": machine.role,
        "host_key_type": machine.host_key_type,
        "host_key_fingerprint": machine.host_key_fingerprint,
        "host_key_blob": machine.host_key_blob,
        "last_verdict": machine.last_verdict,
        "last_checked_at": machine.last_checked_at,
        "created_at": machine.created_at,
        "updated_at": machine.updated_at,
    }


def create_machine(db, owner_user_id: str, label: str, address: str, port: int,
                   username: str, os_family: str, role: str) -> str:
    machine_id = uuid.uuid4().hex
    now = _now_ms()
    context = _remote_context(db)
    context.call(
        context.repository.create_machine,
        machine=RemoteMachine(
            machine_id=machine_id,
            owner_id=owner_user_id,
            label=label,
            address=address,
            port=int(port),
            username=username,
            os_family=os_family,
            role=role,
            host_key_type=None,
            host_key_fingerprint=None,
            host_key_blob=None,
            last_verdict=None,
            last_checked_at=None,
            created_at=now,
            updated_at=now,
        ),
    )
    return machine_id


def list_machines(db, owner_user_id: str) -> List[Dict]:
    context = _remote_context(db)
    records = context.call(
        context.repository.list_machines,
        owner_id=owner_user_id,
        limit=1000,
    )
    return [_machine_dict(machine) for machine in records]


def owns_any_machine(db, owner_user_id: str) -> bool:
    context = _remote_context(db)
    return bool(
        context.call(
            context.repository.list_machines,
            owner_id=owner_user_id,
            limit=1,
        )
    )


def get_machine(db, owner_user_id: str, machine_id: str) -> Optional[Dict]:
    context = _remote_context(db)
    record = context.call(
        context.repository.get_machine,
        owner_id=owner_user_id,
        machine_id=machine_id,
    )
    return None if record is None else _machine_dict(record)


def resolve_machine(db, owner_user_id: str, ref: str) -> Optional[Dict]:
    if not ref:
        return None
    context = _remote_context(db)
    record = context.call(
        context.repository.resolve_machine,
        owner_id=owner_user_id,
        reference=ref,
    )
    return None if record is None else _machine_dict(record)


def delete_machine(db, owner_user_id: str, machine_id: str) -> bool:
    context = _remote_context(db)
    with context.transaction() as transaction:
        lock_owner(context.plane_runtime, transaction, owner_user_id)
        if context.repository.get_machine(transaction, owner_id=owner_user_id, machine_id=machine_id) is None:
            return False
        rotate_authority(context.plane_runtime, transaction, owner_user_id, machine_id)
        return context.repository.delete_machine(
            transaction, owner_id=owner_user_id, machine_id=machine_id)


def purge_user_remote_compute(db, owner_user_id: str, credential_manager=None) -> Dict[str, int]:
    if credential_manager is None:
        from orchestrator.credential_manager import CredentialManager
        credential_manager = CredentialManager(db=db)
    remote = _remote_context(db)
    tracked = _tracked_context(db)
    with remote.transaction() as transaction:
        rotate_authority(remote.plane_runtime, transaction, owner_user_id)
        credentials = remote.plane_runtime.repositories.credentials.delete_owner_machine_credentials(
            transaction, owner_id=owner_user_id)
        jobs = tracked.repository.delete_owner(transaction, owner_id=owner_user_id)
        machines = remote.repository.delete_owner(transaction, owner_id=owner_user_id)
    return {"credentials": credentials, "machines": machines, "jobs": jobs}


_CONNECTED_VERDICTS = ("ok", "partial")


def audit_machine_event(owner_user_id: Optional[str], action_type: str, description: str, *,
                        machine_id: Optional[str] = None, label: Optional[str] = None,
                        verdict: Optional[str] = None, cred_type: Optional[str] = None,
                        outcome: str = "success") -> None:
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        meta: Dict[str, str] = {}
        if machine_id:
            meta["machine_id"] = str(machine_id)
        if label:
            meta["machine_label"] = str(label)
        if verdict:
            meta["verdict"] = str(verdict)
        if cred_type:
            meta["cred_type"] = str(cred_type)
        rec.record_blocking(AuditEventCreate(
            actor_user_id=owner_user_id or "unknown",
            auth_principal=owner_user_id or "unknown",
            event_class="agent_lifecycle",
            action_type=action_type,
            description=description[:1024],
            correlation_id=str(machine_id) if machine_id else uuid.uuid4().hex,
            outcome=outcome,
            inputs_meta=meta,
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:  # noqa: BLE001
        logger.debug("remote_machine audit failed (%s)", action_type, exc_info=True)


def record_probe(db, owner_user_id: str, machine_id: str, verdict: str,
                 host_key: Optional[Dict] = None) -> None:
    now = _now_ms()
    context = _remote_context(db)
    with context.transaction() as transaction:
        lock_owner(context.plane_runtime, transaction, owner_user_id)
        record = context.repository.get_machine(transaction, owner_id=owner_user_id, machine_id=machine_id)
        if record is None:
            return
        row = _machine_dict(record)
        trust = host_key if host_key and not row.get("host_key_fingerprint") else {}
        if trust:
            rotate_authority(context.plane_runtime, transaction, owner_user_id, machine_id)
        context.repository.record_probe(transaction, owner_id=owner_user_id, machine_id=machine_id,
            expected_updated_at=int(row["updated_at"]), verdict=verdict,
            checked_at=max(now, int(row["updated_at"]) + 1), host_key_type=trust.get("type"),
            host_key_fingerprint=trust.get("fingerprint"), host_key_blob=trust.get("blob_b64"))
    audit_machine_event(owner_user_id, "remote_machine.connection",
        f"connection to {row['label']}: {verdict}", machine_id=machine_id,
        label=row["label"], verdict=verdict,
        outcome="success" if verdict in _CONNECTED_VERDICTS else "failure")


def retrust_host_key(db, owner_user_id: str, machine_id: str) -> None:
    context = _remote_context(db)
    with context.transaction() as transaction:
        lock_owner(context.plane_runtime, transaction, owner_user_id)
        row = context.repository.get_machine(transaction, owner_id=owner_user_id, machine_id=machine_id)
        if row is None:
            return
        rotate_authority(context.plane_runtime, transaction, owner_user_id, machine_id)
        context.repository.clear_host_trust(transaction, owner_id=owner_user_id, machine_id=machine_id,
            expected_updated_at=row.updated_at, updated_at=max(_now_ms(), row.updated_at + 1))


def build_target(db, credmgr, owner_user_id: str, machine_id: str) -> MachineTarget:
    from orchestrator.remote_effect_execution import selection_requires_target
    if selection_requires_target(owner_user_id):
        return build_effect_target(db, credmgr, owner_user_id, machine_id)
    row = get_machine(db, owner_user_id, machine_id)
    if row is None:
        raise MachineNotFound(machine_id)
    cred = credmgr.get_machine_credential(machine_id, owner_user_id)
    if cred is None:
        raise CredentialNotConfigured(machine_id)
    return MachineTarget(machine_id=row["machine_id"], label=row["label"], address=row["address"],
        port=int(row["port"]), username=row["username"], cred_type=cred["cred_type"],
        secret=cred["secret"], passphrase=cred["passphrase"],
        host_key_fingerprint=row.get("host_key_fingerprint"))


def build_effect_target(db, credmgr, owner_user_id: str, machine_id: str) -> MachineTarget:
    from orchestrator.remote_effect_authority import authority_digest
    context = _remote_context(db)
    with context.transaction() as transaction:
        lock_owner(context.plane_runtime, transaction, owner_user_id)
        record = context.repository.get_machine(transaction, owner_id=owner_user_id, machine_id=machine_id)
        if record is None:
            raise MachineNotFound(machine_id)
        row = _machine_dict(record)
        cred = credmgr.get_machine_credential(machine_id, owner_user_id, transaction=transaction)
        if cred is None:
            raise CredentialNotConfigured(machine_id)
        authority = (authority_digest(context.plane_runtime, transaction, owner_user_id, machine_id)
                     if row.get("host_key_fingerprint") else None)
    return MachineTarget(
        machine_id=row["machine_id"], label=row["label"], address=row["address"],
        port=int(row["port"]), username=row["username"], cred_type=cred["cred_type"],
        secret=cred["secret"], passphrase=cred["passphrase"],
        host_key_fingerprint=row.get("host_key_fingerprint"), authority_digest=authority,
    )
