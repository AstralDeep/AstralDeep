"""Owner-isolated inventory of remote machines and their execution-attempt metadata. SSH
transport, credentials, polling, and notification delivery stay with the caller; used
by orchestrator/remote_machines.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from astralplane.contracts import Record, Transaction
from astralplane.errors import PlaneError


@dataclass(frozen=True, slots=True)
class RemoteMachine:
    machine_id: str
    owner_id: str
    label: str
    address: str
    port: int
    username: str
    os_family: str
    role: str
    host_key_type: str | None
    host_key_fingerprint: str | None
    host_key_blob: str | None
    last_verdict: str | None
    last_checked_at: int | None
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("machine_id", self.machine_id, 128),
            ("owner_id", self.owner_id, 512),
            ("label", self.label, 256),
            ("address", self.address, 512),
            ("username", self.username, 256),
        ):
            _required(name, value, maximum)
        if not 1 <= self.port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if self.os_family not in {"linux", "windows", "macos"}:
            raise ValueError("os_family is not supported")
        if self.role not in {"cluster", "plain"}:
            raise ValueError("role is not supported")
        if self.created_at < 0 or self.updated_at < self.created_at:
            raise ValueError("remote machine timestamps are invalid")


@dataclass(frozen=True, slots=True)
class RemoteExecution:
    execution_id: str
    owner_id: str
    machine_id: str
    scheduler_job_id: str
    chat_id: str | None
    submit_marker: str | None
    output_path: str | None
    component_id: str | None
    job_name: str
    state: str
    exit_code: str | None
    terminal: bool
    notify_on_finish: bool
    notified: bool
    failure_count: int
    created_at: int
    last_polled_at: int | None
    finished_at: int | None

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("execution_id", self.execution_id, 128),
            ("owner_id", self.owner_id, 512),
            ("machine_id", self.machine_id, 128),
            ("scheduler_job_id", self.scheduler_job_id, 128),
        ):
            _required(name, value, maximum)
        if self.failure_count < 0:
            raise ValueError("failure_count cannot be negative")
        if self.terminal != (self.finished_at is not None):
            raise ValueError(
                "terminal executions require finished_at and live executions forbid it"
            )


class RemoteRepository:
    def create_machine(self, transaction: Transaction, machine: RemoteMachine) -> RemoteMachine:
        row = transaction.fetch_one(
            """
            INSERT INTO remote_machine (
                machine_id, owner_user_id, label, address, port, username,
                os_family, role, host_key_type, host_key_fingerprint,
                host_key_blob, last_verdict, last_checked_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (machine_id) DO NOTHING
            RETURNING *
            """,
            (
                machine.machine_id,
                machine.owner_id,
                machine.label,
                machine.address,
                machine.port,
                machine.username,
                machine.os_family,
                machine.role,
                machine.host_key_type,
                machine.host_key_fingerprint,
                machine.host_key_blob,
                machine.last_verdict,
                machine.last_checked_at,
                machine.created_at,
                machine.updated_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM remote_machine WHERE machine_id = %s AND owner_user_id = %s",
                (machine.machine_id, machine.owner_id),
            )
        if row is None or _machine(row) != machine:
            raise PlaneError(
                "remote machine identity has conflicting semantics",
                code="remote_machine_conflict",
                metadata={"owner_id": machine.owner_id},
            )
        return machine

    def get_machine(
        self, transaction: Transaction, *, owner_id: str, machine_id: str
    ) -> RemoteMachine | None:
        row = transaction.fetch_one(
            "SELECT * FROM remote_machine WHERE machine_id = %s AND owner_user_id = %s",
            (machine_id, owner_id),
        )
        return None if row is None else _machine(row)

    def resolve_machine(
        self, transaction: Transaction, *, owner_id: str, reference: str
    ) -> RemoteMachine | None:
        _required("reference", reference, 512)
        row = transaction.fetch_one(
            """
            SELECT * FROM remote_machine
            WHERE owner_user_id = %s
              AND (machine_id = %s OR lower(label) = lower(%s)
                   OR lower(address) = lower(%s))
            ORDER BY CASE WHEN machine_id = %s THEN 0 ELSE 1 END, label
            LIMIT 1
            """,
            (owner_id, reference, reference, reference, reference),
        )
        return None if row is None else _machine(row)

    def list_machines(
        self, transaction: Transaction, *, owner_id: str, limit: int = 200
    ) -> tuple[RemoteMachine, ...]:
        _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM remote_machine
            WHERE owner_user_id = %s ORDER BY label, machine_id LIMIT %s
            """,
            (owner_id, limit),
        )
        return tuple(_machine(row) for row in rows)

    def record_probe(
        self,
        transaction: Transaction,
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
        _required("verdict", verdict, 128)
        if checked_at < 0 or expected_updated_at < 0:
            raise ValueError("probe timestamps cannot be negative")
        host_values = (host_key_type, host_key_fingerprint, host_key_blob)
        if any(value is not None for value in host_values) and any(
            value is None for value in host_values
        ):
            raise ValueError("host-key type, fingerprint, and blob are all-or-none")
        row = transaction.fetch_one(
            """
            UPDATE remote_machine
            SET last_verdict = %s, last_checked_at = %s,
                host_key_type = CASE WHEN host_key_fingerprint IS NULL
                                     THEN COALESCE(%s, host_key_type) ELSE host_key_type END,
                host_key_fingerprint = CASE WHEN host_key_fingerprint IS NULL
                                            THEN COALESCE(%s, host_key_fingerprint)
                                            ELSE host_key_fingerprint END,
                host_key_blob = CASE WHEN host_key_fingerprint IS NULL
                                     THEN COALESCE(%s, host_key_blob) ELSE host_key_blob END,
                updated_at = %s
            WHERE machine_id = %s AND owner_user_id = %s AND updated_at = %s
            RETURNING *
            """,
            (
                verdict,
                checked_at,
                host_key_type,
                host_key_fingerprint,
                host_key_blob,
                checked_at,
                machine_id,
                owner_id,
                expected_updated_at,
            ),
        )
        return _required_machine(row, owner_id)

    def clear_host_trust(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        machine_id: str,
        expected_updated_at: int,
        updated_at: int,
    ) -> RemoteMachine:
        row = transaction.fetch_one(
            """
            UPDATE remote_machine
            SET host_key_type = NULL, host_key_fingerprint = NULL,
                host_key_blob = NULL, updated_at = %s
            WHERE machine_id = %s AND owner_user_id = %s AND updated_at = %s
            RETURNING *
            """,
            (updated_at, machine_id, owner_id, expected_updated_at),
        )
        return _required_machine(row, owner_id)

    def delete_machine(self, transaction: Transaction, *, owner_id: str, machine_id: str) -> bool:
        row = transaction.fetch_one(
            """
            DELETE FROM remote_machine
            WHERE machine_id = %s AND owner_user_id = %s
            RETURNING machine_id
            """,
            (machine_id, owner_id),
        )
        return row is not None

    def delete_owner(self, transaction: Transaction, *, owner_id: str) -> int:
        _required("owner_id", owner_id, 512)
        result = transaction.execute(
            "DELETE FROM remote_machine WHERE owner_user_id = %s",
            (owner_id,),
        )
        if result.rowcount < 0:
            raise PlaneError(
                "remote owner deletion returned an invalid row count",
                code="remote_owner_delete_invalid",
                metadata={"owner_id": owner_id},
            )
        return result.rowcount

    def create_execution(
        self, transaction: Transaction, execution: RemoteExecution
    ) -> RemoteExecution:
        row = transaction.fetch_one(
            """
            INSERT INTO tracked_job (
                tracked_job_id, owner_user_id, machine_id, chat_id,
                scheduler_job_id, submit_marker, output_path, component_id,
                job_name, state, exit_code, terminal, notify_on_finish,
                notified, fail_count, created_at, last_polled_at, finished_at
            ) SELECT %s, %s, machine_id, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s
                FROM remote_machine
               WHERE machine_id = %s AND owner_user_id = %s
            ON CONFLICT (machine_id, scheduler_job_id) DO NOTHING
            RETURNING *
            """,
            (
                execution.execution_id,
                execution.owner_id,
                execution.chat_id,
                execution.scheduler_job_id,
                execution.submit_marker,
                execution.output_path,
                execution.component_id,
                execution.job_name,
                execution.state,
                execution.exit_code,
                execution.terminal,
                execution.notify_on_finish,
                execution.notified,
                execution.failure_count,
                execution.created_at,
                execution.last_polled_at,
                execution.finished_at,
                execution.machine_id,
                execution.owner_id,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM tracked_job
                WHERE owner_user_id = %s AND machine_id = %s AND scheduler_job_id = %s
                """,
                (execution.owner_id, execution.machine_id, execution.scheduler_job_id),
            )
        if row is None or _execution(row) != execution:
            raise PlaneError(
                "remote execution identity has conflicting semantics or an unknown machine",
                code="remote_execution_conflict",
                metadata={"owner_id": execution.owner_id},
            )
        return execution

    def get_execution(
        self, transaction: Transaction, *, owner_id: str, execution_id: str
    ) -> RemoteExecution | None:
        row = transaction.fetch_one(
            "SELECT * FROM tracked_job WHERE tracked_job_id = %s AND owner_user_id = %s",
            (execution_id, owner_id),
        )
        return None if row is None else _execution(row)

    def list_open_executions(
        self, transaction: Transaction, *, owner_id: str, limit: int = 200
    ) -> tuple[RemoteExecution, ...]:
        _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM tracked_job
            WHERE owner_user_id = %s AND terminal = FALSE
            ORDER BY created_at, tracked_job_id LIMIT %s
            """,
            (owner_id, limit),
        )
        return tuple(_execution(row) for row in rows)

    def update_execution(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        execution_id: str,
        expected_state: str,
        expected_failure_count: int,
        state: str,
        exit_code: str | None,
        terminal: bool,
        failure_count: int,
        polled_at: int,
        notified: bool | None = None,
    ) -> RemoteExecution:
        if min(expected_failure_count, failure_count, polled_at) < 0:
            raise ValueError("execution counters and timestamps cannot be negative")
        finished_at = polled_at if terminal else None
        row = transaction.fetch_one(
            """
            UPDATE tracked_job
            SET state = %s, exit_code = %s, terminal = %s, fail_count = %s,
                last_polled_at = %s, finished_at = %s,
                notified = COALESCE(%s, notified)
            WHERE tracked_job_id = %s AND owner_user_id = %s
              AND state = %s AND fail_count = %s AND terminal = FALSE
            RETURNING *
            """,
            (
                state,
                exit_code,
                terminal,
                failure_count,
                polled_at,
                finished_at,
                notified,
                execution_id,
                owner_id,
                expected_state,
                expected_failure_count,
            ),
        )
        if row is None:
            raise PlaneError(
                "remote execution observation fence is stale",
                code="remote_execution_state_conflict",
                metadata={"owner_id": owner_id},
            )
        return _execution(row)


def _required(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


def _limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _machine(row: Record) -> RemoteMachine:
    return RemoteMachine(
        machine_id=str(row["machine_id"]),
        owner_id=str(row["owner_user_id"]),
        label=str(row["label"]),
        address=str(row["address"]),
        port=int(row["port"]),
        username=str(row["username"]),
        os_family=str(row["os_family"]),
        role=str(row["role"]),
        host_key_type=None if row.get("host_key_type") is None else str(row["host_key_type"]),
        host_key_fingerprint=(
            None if row.get("host_key_fingerprint") is None else str(row["host_key_fingerprint"])
        ),
        host_key_blob=None if row.get("host_key_blob") is None else str(row["host_key_blob"]),
        last_verdict=None if row.get("last_verdict") is None else str(row["last_verdict"]),
        last_checked_at=(
            None if row.get("last_checked_at") is None else int(row["last_checked_at"])
        ),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _required_machine(row: Record | None, owner_id: str) -> RemoteMachine:
    if row is None:
        raise PlaneError(
            "remote machine owner or revision fence is stale",
            code="remote_machine_state_conflict",
            metadata={"owner_id": owner_id},
        )
    return _machine(row)


def _execution(row: Record) -> RemoteExecution:
    return RemoteExecution(
        execution_id=str(row["tracked_job_id"]),
        owner_id=str(row["owner_user_id"]),
        machine_id=str(row["machine_id"]),
        scheduler_job_id=str(row["scheduler_job_id"]),
        chat_id=None if row.get("chat_id") is None else str(row["chat_id"]),
        submit_marker=None if row.get("submit_marker") is None else str(row["submit_marker"]),
        output_path=None if row.get("output_path") is None else str(row["output_path"]),
        component_id=None if row.get("component_id") is None else str(row["component_id"]),
        job_name=str(row.get("job_name") or ""),
        state=str(row["state"]),
        exit_code=None if row.get("exit_code") is None else str(row["exit_code"]),
        terminal=bool(row["terminal"]),
        notify_on_finish=bool(row["notify_on_finish"]),
        notified=bool(row["notified"]),
        failure_count=int(row["fail_count"]),
        created_at=int(row["created_at"]),
        last_polled_at=(None if row.get("last_polled_at") is None else int(row["last_polled_at"])),
        finished_at=None if row.get("finished_at") is None else int(row["finished_at"]),
    )


__all__ = ("RemoteExecution", "RemoteMachine", "RemoteRepository")
