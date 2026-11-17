"""Tests for astralplane.repositories.remote: machine and execution-record validation,
owner-scoped CRUD and replay, trusted-key compare-and-set, and count-checked account
retirement.
"""

from __future__ import annotations

import pytest
from _support import Result, ScriptedTransaction

from astralplane.errors import PlaneError
from astralplane.repositories.remote import RemoteExecution, RemoteMachine, RemoteRepository


def machine(**overrides: object) -> RemoteMachine:
    values: dict[str, object] = {
        "machine_id": "machine-1",
        "owner_id": "owner-1",
        "label": "compute",
        "address": "compute.internal",
        "port": 22,
        "username": "runner",
        "os_family": "linux",
        "role": "cluster",
        "host_key_type": None,
        "host_key_fingerprint": None,
        "host_key_blob": None,
        "last_verdict": None,
        "last_checked_at": None,
        "created_at": 1,
        "updated_at": 1,
    }
    values.update(overrides)
    return RemoteMachine(**values)  # type: ignore[arg-type]


def machine_row(**overrides: object) -> dict[str, object]:
    value = machine(**overrides)
    return {
        "machine_id": value.machine_id,
        "owner_user_id": value.owner_id,
        "label": value.label,
        "address": value.address,
        "port": value.port,
        "username": value.username,
        "os_family": value.os_family,
        "role": value.role,
        "host_key_type": value.host_key_type,
        "host_key_fingerprint": value.host_key_fingerprint,
        "host_key_blob": value.host_key_blob,
        "last_verdict": value.last_verdict,
        "last_checked_at": value.last_checked_at,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def execution(**overrides: object) -> RemoteExecution:
    values: dict[str, object] = {
        "execution_id": "execution-1",
        "owner_id": "owner-1",
        "machine_id": "machine-1",
        "scheduler_job_id": "42",
        "chat_id": "chat-1",
        "submit_marker": "marker-1",
        "output_path": "/safe/output",
        "component_id": "component-1",
        "job_name": "training",
        "state": "submitted",
        "exit_code": None,
        "terminal": False,
        "notify_on_finish": True,
        "notified": False,
        "failure_count": 0,
        "created_at": 1,
        "last_polled_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return RemoteExecution(**values)  # type: ignore[arg-type]


def execution_row(**overrides: object) -> dict[str, object]:
    value = execution(**overrides)
    return {
        "tracked_job_id": value.execution_id,
        "owner_user_id": value.owner_id,
        "machine_id": value.machine_id,
        "scheduler_job_id": value.scheduler_job_id,
        "chat_id": value.chat_id,
        "submit_marker": value.submit_marker,
        "output_path": value.output_path,
        "component_id": value.component_id,
        "job_name": value.job_name,
        "state": value.state,
        "exit_code": value.exit_code,
        "terminal": value.terminal,
        "notify_on_finish": value.notify_on_finish,
        "notified": value.notified,
        "fail_count": value.failure_count,
        "created_at": value.created_at,
        "last_polled_at": value.last_polled_at,
        "finished_at": value.finished_at,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"port": 0},
        {"os_family": "vms"},
        {"role": "controller"},
        {"updated_at": 0},
    ],
)
def test_machine_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        machine(**changes)


def test_machine_create_replay_reads_and_conflict() -> None:
    repository = RemoteRepository()
    value = machine()
    assert repository.create_machine(ScriptedTransaction(one=[machine_row()]), value) == value
    assert repository.create_machine(ScriptedTransaction(one=[None, machine_row()]), value) == value
    assert (
        repository.get_machine(
            ScriptedTransaction(one=[machine_row()]), owner_id="owner-1", machine_id="machine-1"
        )
        == value
    )
    assert (
        repository.get_machine(
            ScriptedTransaction(one=[None]), owner_id="other", machine_id="machine-1"
        )
        is None
    )
    with pytest.raises(PlaneError) as raised:
        repository.create_machine(
            ScriptedTransaction(one=[None, machine_row(label="different")]), value
        )
    assert raised.value.code == "remote_machine_conflict"


def test_machine_resolve_list_delete_are_owner_scoped() -> None:
    repository = RemoteRepository()
    tx = ScriptedTransaction(one=[machine_row()])
    assert repository.resolve_machine(tx, owner_id="owner-1", reference="compute") == machine()
    assert tx.calls[0][2][0] == "owner-1"  # type: ignore[index]
    assert (
        repository.resolve_machine(
            ScriptedTransaction(one=[None]), owner_id="other", reference="compute"
        )
        is None
    )
    assert repository.list_machines(
        ScriptedTransaction(all_rows=[(machine_row(),)]), owner_id="owner-1", limit=1
    ) == (machine(),)
    assert repository.delete_machine(
        ScriptedTransaction(one=[{"machine_id": "machine-1"}]),
        owner_id="owner-1",
        machine_id="machine-1",
    )
    assert not repository.delete_machine(
        ScriptedTransaction(one=[None]), owner_id="other", machine_id="machine-1"
    )
    with pytest.raises(ValueError):
        repository.resolve_machine(ScriptedTransaction(), owner_id="owner-1", reference="")
    with pytest.raises(ValueError):
        repository.list_machines(ScriptedTransaction(), owner_id="owner-1", limit=0)


def test_account_retirement_deletes_only_owner_machines_with_count_evidence() -> None:
    repository = RemoteRepository()
    transaction = ScriptedTransaction(execute=[Result(rowcount=3)])
    assert repository.delete_owner(transaction, owner_id="owner-1") == 3
    assert transaction.calls[0][2] == ("owner-1",)
    assert "WHERE owner_user_id = %s" in transaction.calls[0][1]
    with pytest.raises(ValueError):
        repository.delete_owner(ScriptedTransaction(), owner_id="")
    with pytest.raises(PlaneError) as caught:
        repository.delete_owner(
            ScriptedTransaction(execute=[Result(rowcount=-1)]), owner_id="owner-1"
        )
    assert caught.value.code == "remote_owner_delete_invalid"


def test_probe_preserves_first_trusted_key_and_compare_and_set() -> None:
    repository = RemoteRepository()
    trusted = machine_row(
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:abc",
        host_key_blob="blob",
        last_verdict="ok",
        last_checked_at=2,
        updated_at=2,
    )
    tx = ScriptedTransaction(one=[trusted])
    result = repository.record_probe(
        tx,
        owner_id="owner-1",
        machine_id="machine-1",
        expected_updated_at=1,
        verdict="ok",
        checked_at=2,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:abc",
        host_key_blob="blob",
    )
    assert result.host_key_fingerprint == "SHA256:abc"
    assert "owner_user_id = %s" in tx.fetch_sql()
    with pytest.raises(ValueError):
        repository.record_probe(
            ScriptedTransaction(),
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=1,
            verdict="ok",
            checked_at=2,
            host_key_type="ssh-ed25519",
        )
    with pytest.raises(PlaneError):
        repository.record_probe(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            machine_id="machine-1",
            expected_updated_at=1,
            verdict="timeout",
            checked_at=2,
        )
    cleared = repository.clear_host_trust(
        ScriptedTransaction(one=[machine_row(updated_at=3)]),
        owner_id="owner-1",
        machine_id="machine-1",
        expected_updated_at=2,
        updated_at=3,
    )
    assert cleared.host_key_fingerprint is None


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": ""},
        {"failure_count": -1},
        {"terminal": True, "finished_at": None},
        {"terminal": False, "finished_at": 2},
    ],
)
def test_execution_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        execution(**changes)


def test_execution_create_replay_read_list_and_conflict() -> None:
    repository = RemoteRepository()
    value = execution()
    assert repository.create_execution(ScriptedTransaction(one=[execution_row()]), value) == value
    assert (
        repository.create_execution(ScriptedTransaction(one=[None, execution_row()]), value)
        == value
    )
    assert (
        repository.get_execution(
            ScriptedTransaction(one=[execution_row()]),
            owner_id="owner-1",
            execution_id="execution-1",
        )
        == value
    )
    assert (
        repository.get_execution(
            ScriptedTransaction(one=[None]), owner_id="other", execution_id="execution-1"
        )
        is None
    )
    assert repository.list_open_executions(
        ScriptedTransaction(all_rows=[(execution_row(),)]), owner_id="owner-1", limit=1
    ) == (value,)
    with pytest.raises(PlaneError) as raised:
        repository.create_execution(
            ScriptedTransaction(one=[None, execution_row(job_name="different")]), value
        )
    assert raised.value.code == "remote_execution_conflict"


def test_execution_update_uses_owner_state_and_failure_fences() -> None:
    repository = RemoteRepository()
    completed_row = execution_row(
        state="COMPLETED",
        exit_code="0:0",
        terminal=True,
        notified=True,
        last_polled_at=5,
        finished_at=5,
    )
    tx = ScriptedTransaction(one=[completed_row])
    result = repository.update_execution(
        tx,
        owner_id="owner-1",
        execution_id="execution-1",
        expected_state="submitted",
        expected_failure_count=0,
        state="COMPLETED",
        exit_code="0:0",
        terminal=True,
        failure_count=0,
        polled_at=5,
        notified=True,
    )
    assert result.terminal and result.notified
    assert tx.calls[0][2][-4:] == ("execution-1", "owner-1", "submitted", 0)  # type: ignore[index]
    with pytest.raises(ValueError):
        repository.update_execution(
            ScriptedTransaction(),
            owner_id="owner-1",
            execution_id="execution-1",
            expected_state="submitted",
            expected_failure_count=-1,
            state="RUNNING",
            exit_code=None,
            terminal=False,
            failure_count=0,
            polled_at=5,
        )
    with pytest.raises(PlaneError) as raised:
        repository.update_execution(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            execution_id="execution-1",
            expected_state="submitted",
            expected_failure_count=0,
            state="RUNNING",
            exit_code=None,
            terminal=False,
            failure_count=0,
            polled_at=5,
        )
    assert raised.value.code == "remote_execution_state_conflict"
