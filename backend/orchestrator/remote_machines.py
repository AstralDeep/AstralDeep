"""Per-user remote-machine inventory + transport-target construction (feature 063).

Thin, owner-scoped DB helpers over the ``remote_machine`` / ``machine_credential``
tables plus ``build_target()``, which loads a machine scoped to its owner (FR-018)
and decrypts its credential into a transport ``MachineTarget``. Every query is
owner-scoped so one user can never see, name, or address another user's machine
(FR-010, SC-012).
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

logger = logging.getLogger("RemoteMachines")


class MachineNotFound(Exception):
    """No machine with that id in the invoking user's inventory (FR-018/SC-012)."""


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
    """Insert a machine into the user's inventory; return its new id."""
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
    """Existence-only check for tool visibility; no machine data leaves."""
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
    """Resolve a machine within the caller's inventory by id, then label, then
    address (case-insensitive). Lets a chat verb accept "dgx" or the address, not
    just the opaque machine_id. Owner-scoped throughout (FR-018)."""
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
    """Delete an owned machine (credential cascades via the FK). Returns False if
    the machine is not in the caller's inventory."""
    context = _remote_context(db)
    return context.call(
        context.repository.delete_machine,
        owner_id=owner_user_id,
        machine_id=machine_id,
    )


def purge_user_remote_compute(db, owner_user_id: str, credential_manager=None) -> Dict[str, int]:
    """Account-removal leg of FR-015 (data-model.md "Retirement & revocation"):
    destroy every machine credential the user owns, then their remote_machine
    inventory and tracked_job rows. Sibling of the attachments account-deletion
    hook (``attachments.account_lifecycle.purge_user_attachments``) — called by
    the user-management subsystem when an account is removed. Idempotent."""
    if credential_manager is None:
        from orchestrator.credential_manager import CredentialManager
        credential_manager = CredentialManager(db=db)
    credentials = credential_manager.remove_machine_credentials_for_user(owner_user_id)

    remote = _remote_context(db)
    tracked = _tracked_context(db)
    with remote.transaction() as transaction:
        jobs = tracked.repository.delete_owner(transaction, owner_id=owner_user_id)
        machines = remote.repository.delete_owner(transaction, owner_id=owner_user_id)
    return {"credentials": credentials, "machines": machines, "jobs": jobs}


# Verdicts under which the connection genuinely reached + authenticated to the
# machine; anything else audits as a failed connection attempt (FR-047).
_CONNECTED_VERDICTS = ("ok", "partial")


def audit_machine_event(owner_user_id: Optional[str], action_type: str, description: str, *,
                        machine_id: Optional[str] = None, label: Optional[str] = None,
                        verdict: Optional[str] = None, cred_type: Optional[str] = None,
                        outcome: str = "success") -> None:
    """FR-047 hash-chained audit row naming the actor, the machine (id + label)
    and the outcome. Reuses the ``agent_lifecycle`` class the 063 confirmation
    gate already records ``remote_op.*`` rows under (no schema change; same
    posture as feature 056's constant-only ``delegation`` class), correlated by
    ``machine_id`` so one machine's whole lifecycle lines up. SECRETS NEVER
    enter a row (FR-049): only ids, labels, credential *types* and verdicts.
    Best-effort — an audit failure never blocks the operation (mirrors
    ``remote_confirmation._audit_sync``)."""
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
    except Exception:  # noqa: BLE001 — audit is best-effort, never fatal
        logger.debug("remote_machine audit failed (%s)", action_type, exc_info=True)


def record_probe(db, owner_user_id: str, machine_id: str, verdict: str,
                 host_key: Optional[Dict] = None) -> None:
    """Persist the last reachability verdict; record the host key ONLY on first
    contact (FR-020). Never overwrites an existing recorded key — a change surfaces
    as a host_key_mismatch verdict and requires an explicit re-trust action.

    Also the single audit seam for connection attempts (FR-047): every probe and
    verb-level connection already reports its verdict through here, so each one
    lands in the audit log as attempt + verdict without agents/** auditing."""
    now = _now_ms()
    row = get_machine(db, owner_user_id, machine_id)
    if row is None:
        return
    audit_machine_event(
        owner_user_id, "remote_machine.connection",
        f"connection to {row['label']}: {verdict}",
        machine_id=machine_id, label=row["label"], verdict=verdict,
        outcome="success" if verdict in _CONNECTED_VERDICTS else "failure",
    )
    context = _remote_context(db)
    context.call(
        context.repository.record_probe,
        owner_id=owner_user_id,
        machine_id=machine_id,
        expected_updated_at=int(row["updated_at"]),
        verdict=verdict,
        checked_at=max(now, int(row["updated_at"]) + 1),
        host_key_type=(
            host_key.get("type")
            if host_key and not row.get("host_key_fingerprint")
            else None
        ),
        host_key_fingerprint=(
            host_key.get("fingerprint")
            if host_key and not row.get("host_key_fingerprint")
            else None
        ),
        host_key_blob=(
            host_key.get("blob_b64")
            if host_key and not row.get("host_key_fingerprint")
            else None
        )
    )


def retrust_host_key(db, owner_user_id: str, machine_id: str) -> None:
    """Deliberately clear the recorded host key so the next probe re-records it —
    the ONLY path that accepts a changed host identity (FR-020)."""
    row = get_machine(db, owner_user_id, machine_id)
    if row is None:
        return
    context = _remote_context(db)
    context.call(
        context.repository.clear_host_trust,
        owner_id=owner_user_id,
        machine_id=machine_id,
        expected_updated_at=int(row["updated_at"]),
        updated_at=max(_now_ms(), int(row["updated_at"]) + 1),
    )


def build_target(db, credmgr, owner_user_id: str, machine_id: str) -> MachineTarget:
    """Load an owned machine + decrypt its credential into a MachineTarget.

    Raises MachineNotFound (not in the user's inventory), CredentialNotConfigured,
    or credential_manager.CredentialUndecryptable — the verb layer maps each to a
    result verdict. Address/port/username come from the stored row, never the model
    (FR-018).
    """
    row = get_machine(db, owner_user_id, machine_id)
    if row is None:
        raise MachineNotFound(machine_id)
    cred = credmgr.get_machine_credential(
        machine_id,
        owner_user_id,
    )  # may raise CredentialUndecryptable
    if cred is None:
        raise CredentialNotConfigured(machine_id)
    return MachineTarget(
        machine_id=row["machine_id"], label=row["label"], address=row["address"],
        port=int(row["port"]), username=row["username"], cred_type=cred["cred_type"],
        secret=cred["secret"], passphrase=cred["passphrase"],
        host_key_fingerprint=row.get("host_key_fingerprint"),
    )
