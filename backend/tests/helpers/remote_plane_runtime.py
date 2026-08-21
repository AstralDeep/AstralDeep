"""Typed in-memory Plane boundary for remote-compute tests."""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from typing import Any

from astralplane.repositories.credentials import MachineCredentialRecord
from astralplane.repositories.remote import RemoteMachine
from astralplane.repositories.remote_proposals import RemoteOperationProposalRecord
from astralplane.repositories.tracked_jobs import TrackedJobRecord
from orchestrator.plane_repository_context import ApplicationPlaneSource


def _machine(storage: Any, machine_id: str) -> RemoteMachine | None:
    row = storage.machines.get(machine_id)
    if row is None:
        return None
    return RemoteMachine(
        machine_id=row["machine_id"],
        owner_id=row["owner_user_id"],
        label=row["label"],
        address=row["address"],
        port=int(row["port"]),
        username=row["username"],
        os_family=row["os_family"],
        role=row["role"],
        host_key_type=row.get("host_key_type"),
        host_key_fingerprint=row.get("host_key_fingerprint"),
        host_key_blob=row.get("host_key_blob"),
        last_verdict=row.get("last_verdict"),
        last_checked_at=row.get("last_checked_at"),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _store_machine(storage: Any, machine: RemoteMachine) -> None:
    storage.machines[machine.machine_id] = {
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


class InMemoryRemoteRepository:
    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def create_machine(self, transaction: object, machine: RemoteMachine) -> RemoteMachine:
        existing = _machine(self._storage, machine.machine_id)
        if existing is not None and existing != machine:
            raise RuntimeError("remote machine replay changed semantics")
        _store_machine(self._storage, machine)
        return machine

    def get_machine(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
    ) -> RemoteMachine | None:
        machine = _machine(self._storage, machine_id)
        return machine if machine is not None and machine.owner_id == owner_id else None

    def resolve_machine(
        self,
        transaction: object,
        *,
        owner_id: str,
        reference: str,
    ) -> RemoteMachine | None:
        owned = self.list_machines(transaction, owner_id=owner_id, limit=1000)
        for machine in owned:
            if machine.machine_id == reference:
                return machine
        folded = reference.casefold()
        return next(
            (
                machine
                for machine in owned
                if machine.label.casefold() == folded
                or machine.address.casefold() == folded
            ),
            None,
        )

    def list_machines(
        self,
        transaction: object,
        *,
        owner_id: str,
        limit: int = 200,
    ) -> tuple[RemoteMachine, ...]:
        records = (
            _machine(self._storage, machine_id)
            for machine_id in self._storage.machines
        )
        return tuple(
            sorted(
                (
                    machine
                    for machine in records
                    if machine is not None and machine.owner_id == owner_id
                ),
                key=lambda machine: (machine.label, machine.machine_id),
            )[:limit]
        )

    def record_probe(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
        expected_updated_at: int,
        verdict: str,
        checked_at: int,
        host_key_type: str | None = None,
        host_key_fingerprint: str | None = None,
        host_key_blob: str | None = None,
    ) -> RemoteMachine:
        machine = self.get_machine(
            transaction,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if machine is None or machine.updated_at != expected_updated_at:
            raise RuntimeError("remote machine probe fence is stale")
        row = dict(self._storage.machines[machine_id])
        row.update(
            last_verdict=verdict,
            last_checked_at=checked_at,
            updated_at=checked_at,
        )
        if row.get("host_key_fingerprint") is None and host_key_fingerprint is not None:
            row.update(
                host_key_type=host_key_type,
                host_key_fingerprint=host_key_fingerprint,
                host_key_blob=host_key_blob,
            )
        self._storage.machines[machine_id] = row
        return _machine(self._storage, machine_id)  # type: ignore[return-value]

    def clear_host_trust(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
        expected_updated_at: int,
        updated_at: int,
    ) -> RemoteMachine:
        machine = self.get_machine(
            transaction,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if machine is None or machine.updated_at != expected_updated_at:
            raise RuntimeError("remote machine trust fence is stale")
        self._storage.machines[machine_id].update(
            host_key_type=None,
            host_key_fingerprint=None,
            host_key_blob=None,
            updated_at=updated_at,
        )
        return _machine(self._storage, machine_id)  # type: ignore[return-value]

    def delete_machine(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
    ) -> bool:
        machine = self.get_machine(
            transaction,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if machine is None:
            return False
        del self._storage.machines[machine_id]
        getattr(self._storage, "credentials", {}).pop(machine_id, None)
        return True

    def delete_owner(self, transaction: object, *, owner_id: str) -> int:
        machine_ids = [
            machine_id
            for machine_id, row in self._storage.machines.items()
            if row["owner_user_id"] == owner_id
        ]
        for machine_id in machine_ids:
            self.delete_machine(
                transaction,
                owner_id=owner_id,
                machine_id=machine_id,
            )
        return len(machine_ids)


def _credential(storage: Any, machine_id: str) -> MachineCredentialRecord | None:
    row = storage.credentials.get(machine_id)
    if row is None:
        return None
    return MachineCredentialRecord(
        machine_id=row["machine_id"],
        owner_id=row["owner_user_id"],
        credential_type=row["cred_type"],
        encrypted_secret=row["encrypted_secret"],
        encrypted_passphrase=row.get("encrypted_passphrase"),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class InMemoryCredentialRepository:
    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def get_machine_credential(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
    ) -> MachineCredentialRecord | None:
        record = _credential(self._storage, machine_id)
        return record if record is not None and record.owner_id == owner_id else None

    def create_machine_credential(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
        credential_type: str,
        encrypted_secret: str,
        encrypted_passphrase: str | None,
        created_at: int,
    ) -> MachineCredentialRecord:
        machine = _machine(self._storage, machine_id)
        if machine is None or machine.owner_id != owner_id:
            raise RuntimeError("machine credential owner is invalid")
        existing = _credential(self._storage, machine_id)
        candidate = MachineCredentialRecord(
            machine_id=machine_id,
            owner_id=owner_id,
            credential_type=credential_type,
            encrypted_secret=encrypted_secret,
            encrypted_passphrase=encrypted_passphrase,
            created_at=created_at,
            updated_at=created_at,
        )
        if existing is not None and existing != candidate:
            raise RuntimeError("machine credential replay changed semantics")
        self._storage.credentials[machine_id] = {
            "machine_id": machine_id,
            "owner_user_id": owner_id,
            "cred_type": credential_type,
            "encrypted_secret": encrypted_secret,
            "encrypted_passphrase": encrypted_passphrase,
            "created_at": created_at,
            "updated_at": created_at,
        }
        return candidate

    def compare_and_set_machine_credential(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
        expected_updated_at: int,
        credential_type: str,
        encrypted_secret: str,
        encrypted_passphrase: str | None,
        updated_at: int,
    ) -> MachineCredentialRecord:
        current = self.get_machine_credential(
            transaction,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if current is None or current.updated_at != expected_updated_at:
            raise RuntimeError("machine credential fence is stale")
        row = self._storage.credentials[machine_id]
        row.update(
            cred_type=credential_type,
            encrypted_secret=encrypted_secret,
            encrypted_passphrase=encrypted_passphrase,
            updated_at=updated_at,
        )
        return _credential(self._storage, machine_id)  # type: ignore[return-value]

    def delete_machine_credential(
        self,
        transaction: object,
        *,
        owner_id: str,
        machine_id: str,
    ) -> bool:
        current = self.get_machine_credential(
            transaction,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if current is None:
            return False
        del self._storage.credentials[machine_id]
        return True

    def delete_owner_machine_credentials(
        self,
        transaction: object,
        *,
        owner_id: str,
    ) -> int:
        machine_ids = [
            machine_id
            for machine_id, row in self._storage.credentials.items()
            if row["owner_user_id"] == owner_id
        ]
        for machine_id in machine_ids:
            del self._storage.credentials[machine_id]
        return len(machine_ids)


def _job(row: dict[str, Any]) -> TrackedJobRecord:
    return TrackedJobRecord(
        tracked_job_id=row["tracked_job_id"],
        owner_id=row["owner_user_id"],
        machine_id=row["machine_id"],
        scheduler_job_id=str(row["scheduler_job_id"]),
        conversation_id=row.get("chat_id"),
        submit_marker=row.get("submit_marker"),
        output_path=row.get("output_path"),
        component_id=row.get("component_id"),
        job_name=row.get("job_name") or "",
        state=row.get("state") or "submitted",
        exit_code=row.get("exit_code"),
        terminal=bool(row.get("terminal")),
        notify_on_finish=bool(row.get("notify_on_finish")),
        notified=bool(row.get("notified")),
        fail_count=int(row.get("fail_count") or 0),
        created_at=int(row.get("created_at") or 0),
        last_polled_at=row.get("last_polled_at"),
        finished_at=row.get("finished_at"),
    )


def _store_job(storage: Any, record: TrackedJobRecord) -> None:
    storage.jobs[record.tracked_job_id] = {
        "tracked_job_id": record.tracked_job_id,
        "owner_user_id": record.owner_id,
        "machine_id": record.machine_id,
        "scheduler_job_id": record.scheduler_job_id,
        "chat_id": record.conversation_id,
        "submit_marker": record.submit_marker,
        "output_path": record.output_path,
        "component_id": record.component_id,
        "job_name": record.job_name,
        "state": record.state,
        "exit_code": record.exit_code,
        "terminal": record.terminal,
        "notify_on_finish": record.notify_on_finish,
        "notified": record.notified,
        "fail_count": record.fail_count,
        "created_at": record.created_at,
        "last_polled_at": record.last_polled_at,
        "finished_at": record.finished_at,
    }


class InMemoryTrackedJobRepository:
    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def create(self, transaction: object, record: TrackedJobRecord) -> TrackedJobRecord:
        existing = self._storage.jobs.get(record.tracked_job_id)
        if existing is not None and _job(existing) != record:
            raise RuntimeError("tracked job replay changed semantics")
        _store_job(self._storage, record)
        return record

    def get_by_scheduler_job(
        self,
        transaction: object,
        *,
        owner_id: str,
        scheduler_job_id: str,
        machine_id: str | None = None,
    ) -> TrackedJobRecord | None:
        return next(
            (
                _job(row)
                for row in self._storage.jobs.values()
                if row["owner_user_id"] == owner_id
                and str(row["scheduler_job_id"]) == scheduler_job_id
                and (machine_id is None or row["machine_id"] == machine_id)
            ),
            None,
        )

    def list_open_for_administration(
        self,
        transaction: object,
        *,
        limit: int = 200,
    ) -> tuple[TrackedJobRecord, ...]:
        rows = sorted(
            (row for row in self._storage.jobs.values() if not row.get("terminal")),
            key=lambda row: (int(row.get("created_at") or 0), row["tracked_job_id"]),
        )
        return tuple(_job(row) for row in rows[:limit])

    def apply_poll(
        self,
        transaction: object,
        *,
        owner_id: str,
        tracked_job_id: str,
        expected_fail_count: int,
        expected_last_polled_at: int | None,
        state: str,
        exit_code: str | None,
        terminal: bool,
        fail_count: int,
        polled_at: int,
    ) -> TrackedJobRecord:
        row = self._storage.jobs[tracked_job_id]
        if (
            row["owner_user_id"] != owner_id
            or int(row.get("fail_count") or 0) != expected_fail_count
            or row.get("last_polled_at") != expected_last_polled_at
            or row.get("terminal")
        ):
            raise RuntimeError("tracked job poll fence is stale")
        row.update(
            state=state,
            exit_code=exit_code,
            terminal=terminal,
            fail_count=fail_count,
            last_polled_at=polled_at,
        )
        if terminal:
            row["finished_at"] = row.get("finished_at") or polled_at
        return _job(row)

    def mark_notified(
        self,
        transaction: object,
        *,
        owner_id: str,
        tracked_job_id: str,
    ) -> bool:
        row = self._storage.jobs[tracked_job_id]
        if (
            row["owner_user_id"] != owner_id
            or not row.get("terminal")
            or not row.get("notify_on_finish")
            or row.get("notified")
        ):
            return False
        row["notified"] = True
        return True

    def delete_owner(self, transaction: object, *, owner_id: str) -> int:
        job_ids = [
            job_id
            for job_id, row in self._storage.jobs.items()
            if row["owner_user_id"] == owner_id
        ]
        for job_id in job_ids:
            del self._storage.jobs[job_id]
        return len(job_ids)


def _proposal(row: dict[str, Any]) -> RemoteOperationProposalRecord:
    arguments = row.get("args_json", {})
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    return RemoteOperationProposalRecord(
        proposal_id=row["proposal_id"],
        owner_id=row["owner_user_id"],
        conversation_id=row.get("chat_id"),
        machine_id=row["machine_id"],
        agent_id=row["agent_id"],
        tool_name=row["verb"],
        args_fingerprint=row["args_fingerprint"],
        arguments=arguments,
        summary=row["summary"],
        status=row["status"],
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
        decided_at=row.get("decided_at"),
        consumed_at=row.get("consumed_at"),
    )


class InMemoryRemoteProposalRepository:
    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def create(
        self,
        transaction: object,
        record: RemoteOperationProposalRecord,
    ) -> RemoteOperationProposalRecord:
        existing = self._storage.rows.get(record.proposal_id)
        if existing is not None and _proposal(existing) != record:
            raise RuntimeError("remote proposal replay changed semantics")
        self._storage.rows[record.proposal_id] = {
            "proposal_id": record.proposal_id,
            "owner_user_id": record.owner_id,
            "chat_id": record.conversation_id,
            "machine_id": record.machine_id,
            "agent_id": record.agent_id,
            "verb": record.tool_name,
            "args_json": json.dumps(
                dict(record.arguments),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "args_fingerprint": record.args_fingerprint,
            "summary": record.summary,
            "status": record.status,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "decided_at": record.decided_at,
            "consumed_at": record.consumed_at,
        }
        return record

    def get(
        self,
        transaction: object,
        *,
        owner_id: str,
        proposal_id: str,
    ) -> RemoteOperationProposalRecord | None:
        row = self._storage.rows.get(proposal_id)
        if row is None or row["owner_user_id"] != owner_id:
            return None
        return _proposal(row)

    def decide_if_pending(
        self,
        transaction: object,
        *,
        owner_id: str,
        proposal_id: str,
        decision: str,
        decided_at: int,
    ) -> RemoteOperationProposalRecord | None:
        if getattr(self._storage, "lose_next_decision", False):
            self._storage.lose_next_decision = False
            return None
        row = self._storage.rows.get(proposal_id)
        if (
            row is None
            or row["owner_user_id"] != owner_id
            or row["status"] != "pending"
            or int(row["expires_at"]) < decided_at
        ):
            return None
        row.update(status=decision, decided_at=decided_at)
        return _proposal(row)

    def expire_if_pending(
        self,
        transaction: object,
        *,
        owner_id: str,
        proposal_id: str,
        observed_at: int,
    ) -> RemoteOperationProposalRecord | None:
        row = self._storage.rows.get(proposal_id)
        if (
            row is None
            or row["owner_user_id"] != owner_id
            or row["status"] != "pending"
            or int(row["expires_at"]) >= observed_at
        ):
            return None
        row.update(status="expired", decided_at=observed_at)
        return _proposal(row)

    def consume_if_valid(
        self,
        transaction: object,
        *,
        owner_id: str,
        proposal_id: str,
        expected_tool_name: str,
        expected_args_fingerprint: str,
        consumed_at: int,
    ) -> RemoteOperationProposalRecord | None:
        row = self._storage.rows.get(proposal_id)
        if (
            row is None
            or row["owner_user_id"] != owner_id
            or row["status"] != "approved"
            or int(row["expires_at"]) < consumed_at
            or row["verb"] != expected_tool_name
            or row["args_fingerprint"] != expected_args_fingerprint
        ):
            return None
        row.update(status="consumed", consumed_at=consumed_at)
        return _proposal(row)


class InMemoryPlaneRuntime:
    def __init__(self, storage: Any) -> None:
        tracked = (
            InMemoryTrackedJobRepository(storage)
            if hasattr(storage, "jobs")
            else SimpleNamespace()
        )
        self.repositories = SimpleNamespace(
            credentials=InMemoryCredentialRepository(storage),
            remote=InMemoryRemoteRepository(storage),
            tracked_jobs=tracked,
        )
        self._transaction_lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._transaction_lock:
            yield object()


def make_remote_plane_source(storage: Any) -> ApplicationPlaneSource:
    runtime = InMemoryPlaneRuntime(storage)
    return ApplicationPlaneSource(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def make_remote_confirmation_plane_source(storage: Any) -> ApplicationPlaneSource:
    if not hasattr(storage, "machines"):
        storage.machines = {}
    if not hasattr(storage, "credentials"):
        storage.credentials = {}
    runtime = InMemoryPlaneRuntime(storage)
    runtime.repositories.remote_operation_proposals = (
        InMemoryRemoteProposalRepository(storage)
    )
    return ApplicationPlaneSource(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
