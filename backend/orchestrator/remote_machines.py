"""Per-user remote-machine inventory + transport-target construction (feature 063).

Thin, owner-scoped DB helpers over the ``remote_machine`` / ``machine_credential``
tables plus ``build_target()``, which loads a machine scoped to its owner (FR-018)
and decrypts its credential into a transport ``MachineTarget``. Every query is
owner-scoped so one user can never see, name, or address another user's machine
(FR-010, SC-012).
"""
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from orchestrator.credential_manager import CredentialNotConfigured
from orchestrator.remote_transport import MachineTarget


class MachineNotFound(Exception):
    """No machine with that id in the invoking user's inventory (FR-018/SC-012)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_machine(db, owner_user_id: str, label: str, address: str, port: int,
                   username: str, os_family: str, role: str) -> str:
    """Insert a machine into the user's inventory; return its new id."""
    machine_id = uuid.uuid4().hex
    now = _now_ms()
    db.execute(
        """INSERT INTO remote_machine
           (machine_id, owner_user_id, label, address, port, username, os_family, role,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (machine_id, owner_user_id, label, address, int(port), username, os_family, role, now, now),
    )
    return machine_id


def list_machines(db, owner_user_id: str) -> List[Dict]:
    return db.fetch_all(
        """SELECT machine_id, label, address, port, username, os_family, role,
                  last_verdict, last_checked_at
           FROM remote_machine WHERE owner_user_id = ? ORDER BY label""",
        (owner_user_id,),
    )


def get_machine(db, owner_user_id: str, machine_id: str) -> Optional[Dict]:
    return db.fetch_one(
        "SELECT * FROM remote_machine WHERE machine_id = ? AND owner_user_id = ?",
        (machine_id, owner_user_id),
    )


def resolve_machine(db, owner_user_id: str, ref: str) -> Optional[Dict]:
    """Resolve a machine within the caller's inventory by id, then label, then
    address (case-insensitive). Lets a chat verb accept "dgx" or the address, not
    just the opaque machine_id. Owner-scoped throughout (FR-018)."""
    if not ref:
        return None
    row = get_machine(db, owner_user_id, ref)
    if row is not None:
        return row
    return db.fetch_one(
        "SELECT * FROM remote_machine WHERE owner_user_id = ? "
        "AND (lower(label) = lower(?) OR lower(address) = lower(?)) ORDER BY label LIMIT 1",
        (owner_user_id, ref, ref),
    )


def delete_machine(db, owner_user_id: str, machine_id: str) -> bool:
    """Delete an owned machine (credential cascades via the FK). Returns False if
    the machine is not in the caller's inventory."""
    if get_machine(db, owner_user_id, machine_id) is None:
        return False
    db.execute("DELETE FROM remote_machine WHERE machine_id = ? AND owner_user_id = ?",
               (machine_id, owner_user_id))
    return True


def record_probe(db, owner_user_id: str, machine_id: str, verdict: str,
                 host_key: Optional[Dict] = None) -> None:
    """Persist the last reachability verdict; record the host key ONLY on first
    contact (FR-020). Never overwrites an existing recorded key — a change surfaces
    as a host_key_mismatch verdict and requires an explicit re-trust action."""
    now = _now_ms()
    row = get_machine(db, owner_user_id, machine_id)
    if row is None:
        return
    if host_key and not row.get("host_key_fingerprint"):
        db.execute(
            """UPDATE remote_machine SET last_verdict = ?, last_checked_at = ?,
               host_key_type = ?, host_key_fingerprint = ?, host_key_blob = ?, updated_at = ?
               WHERE machine_id = ? AND owner_user_id = ?""",
            (verdict, now, host_key.get("type"), host_key.get("fingerprint"),
             host_key.get("blob_b64"), now, machine_id, owner_user_id),
        )
    else:
        db.execute(
            "UPDATE remote_machine SET last_verdict = ?, last_checked_at = ?, updated_at = ? "
            "WHERE machine_id = ? AND owner_user_id = ?",
            (verdict, now, now, machine_id, owner_user_id),
        )


def retrust_host_key(db, owner_user_id: str, machine_id: str) -> None:
    """Deliberately clear the recorded host key so the next probe re-records it —
    the ONLY path that accepts a changed host identity (FR-020)."""
    db.execute(
        "UPDATE remote_machine SET host_key_type = NULL, host_key_fingerprint = NULL, "
        "host_key_blob = NULL, updated_at = ? WHERE machine_id = ? AND owner_user_id = ?",
        (_now_ms(), machine_id, owner_user_id),
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
    cred = credmgr.get_machine_credential(machine_id)  # may raise CredentialUndecryptable
    if cred is None:
        raise CredentialNotConfigured(machine_id)
    return MachineTarget(
        machine_id=row["machine_id"], label=row["label"], address=row["address"],
        port=int(row["port"]), username=row["username"], cred_type=cred["cred_type"],
        secret=cred["secret"], passphrase=cred["passphrase"],
        host_key_fingerprint=row.get("host_key_fingerprint"),
    )
