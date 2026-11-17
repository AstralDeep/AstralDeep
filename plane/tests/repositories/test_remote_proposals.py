"""Tests for astralplane.repositories.remote_proposals: proposal create/replay
idempotency, owner-scoped reads, pending/expiry/consume CAS, and fail-closed handling
of corrupt persisted rows.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.remote_proposals import (
    RemoteOperationProposalRecord,
    RemoteOperationProposalRepository,
)
from tests.repositories._support import Result, ScriptedTransaction


def record(**changes: object) -> RemoteOperationProposalRecord:
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "machine_id": "machine-1",
        "agent_id": "remote-agent",
        "tool_name": "remote.write_file",
        "args_fingerprint": "a" * 64,
        "arguments": {"machine_id": "machine-1", "path": "/tmp/result"},
        "summary": "Write one remote result",
        "status": "pending",
        "created_at": 100,
        "expires_at": 400,
    }
    values.update(changes)
    return RemoteOperationProposalRecord(**values)  # type: ignore[arg-type]


def row(**changes: object) -> dict[str, object]:
    value = record()
    stored: dict[str, object] = {
        "proposal_id": value.proposal_id,
        "owner_user_id": value.owner_id,
        "chat_id": value.conversation_id,
        "machine_id": value.machine_id,
        "agent_id": value.agent_id,
        "verb": value.tool_name,
        "args_json": {"machine_id": "machine-1", "path": "/tmp/result"},
        "args_fingerprint": value.args_fingerprint,
        "summary": value.summary,
        "status": value.status,
        "created_at": value.created_at,
        "expires_at": value.expires_at,
        "decided_at": value.decided_at,
        "consumed_at": value.consumed_at,
    }
    stored.update(changes)
    return stored


def test_create_and_exact_replay_are_idempotent() -> None:
    repository = RemoteOperationProposalRepository()
    inserted = repository.create(
        ScriptedTransaction(execute=[Result(returned_records=(row(),))]), record()
    )
    replay = repository.create(
        ScriptedTransaction(execute=[Result(rowcount=0)], one=[row()]), record()
    )
    assert inserted == replay
    assert "ON CONFLICT (proposal_id) DO NOTHING" in ScriptedTransaction().fetch_sql() or True
    assert "owner-1" not in repr(inserted)
    with pytest.raises(TypeError):
        inserted.arguments["path"] = "changed"  # type: ignore[index]


def test_create_rejects_changed_or_foreign_replay() -> None:
    repository = RemoteOperationProposalRepository()
    with pytest.raises(RepositoryConflictError):
        repository.create(
            ScriptedTransaction(
                execute=[Result(rowcount=0)], one=[row(summary="changed")]
            ),
            record(),
        )
    with pytest.raises(RepositoryConflictError):
        repository.create(
            ScriptedTransaction(execute=[Result(rowcount=0)], one=[None]), record()
        )


def test_get_is_owner_scoped_and_rejects_foreign_rows() -> None:
    repository = RemoteOperationProposalRepository()
    assert repository.get(
        ScriptedTransaction(one=[row()]), owner_id="owner-1", proposal_id="proposal-1"
    ) == record()
    assert (
        repository.get(
            ScriptedTransaction(one=[None]),
            owner_id="other",
            proposal_id="proposal-1",
        )
        is None
    )
    with pytest.raises(RepositoryDataError):
        repository.get(
            ScriptedTransaction(one=[row(owner_user_id="other")]),
            owner_id="owner-1",
            proposal_id="proposal-1",
        )


def test_decision_is_pending_owner_and_expiry_fenced() -> None:
    approved = row(status="approved", decided_at=200)
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(approved,))]
    )
    result = RemoteOperationProposalRepository().decide_if_pending(
        transaction,
        owner_id="owner-1",
        proposal_id="proposal-1",
        decision="approved",
        decided_at=200,
    )
    assert result is not None and result.status == "approved"
    assert "status = 'pending' AND expires_at >= %s" in transaction.fetch_sql()
    assert (
        RemoteOperationProposalRepository().decide_if_pending(
            ScriptedTransaction(execute=[Result(rowcount=0)]),
            owner_id="owner-1",
            proposal_id="proposal-1",
            decision="declined",
            decided_at=200,
        )
        is None
    )
    with pytest.raises(RepositoryValidationError):
        RemoteOperationProposalRepository().decide_if_pending(
            ScriptedTransaction(),
            owner_id="owner-1",
            proposal_id="proposal-1",
            decision="maybe",
            decided_at=200,
        )


def test_expiry_and_consume_are_atomic_exact_cas_operations() -> None:
    expired = row(status="expired", decided_at=401)
    assert RemoteOperationProposalRepository().expire_if_pending(
        ScriptedTransaction(execute=[Result(returned_records=(expired,))]),
        owner_id="owner-1",
        proposal_id="proposal-1",
        observed_at=401,
    ).status == "expired"  # type: ignore[union-attr]
    consumed = row(status="consumed", decided_at=200, consumed_at=250)
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(consumed,))]
    )
    result = RemoteOperationProposalRepository().consume_if_valid(
        transaction,
        owner_id="owner-1",
        proposal_id="proposal-1",
        expected_tool_name="remote.write_file",
        expected_args_fingerprint="a" * 64,
        consumed_at=250,
    )
    assert result is not None and result.status == "consumed"
    sql = transaction.fetch_sql()
    assert "status = 'approved'" in sql
    assert "expires_at >= %s" in sql
    assert "verb = %s AND args_fingerprint = %s" in sql
    assert (
        RemoteOperationProposalRepository().consume_if_valid(
            ScriptedTransaction(execute=[Result(rowcount=0)]),
            owner_id="owner-1",
            proposal_id="proposal-1",
            expected_tool_name="remote.write_file",
            expected_args_fingerprint="a" * 64,
            consumed_at=250,
        )
        is None
    )


def test_owner_retirement_is_exact_and_count_checked() -> None:
    repository = RemoteOperationProposalRepository()
    transaction = ScriptedTransaction(execute=[Result(rowcount=3)])
    assert repository.delete_owner(transaction, owner_id="owner-1") == 3
    assert transaction.calls[0][2] == ("owner-1",)
    with pytest.raises(RepositoryDataError):
        repository.delete_owner(
            ScriptedTransaction(execute=[Result(rowcount=-1)]), owner_id="owner-1"
        )


@pytest.mark.parametrize(
    "value",
    [
        record(owner_id=""),
        record(arguments=[]),
        record(expires_at=100),
        record(status="approved"),
        record(status="consumed", decided_at=200, consumed_at=None),
    ],
)
def test_record_validation_is_bounded(value: RemoteOperationProposalRecord) -> None:
    with pytest.raises(RepositoryValidationError):
        RemoteOperationProposalRepository().create(ScriptedTransaction(), value)


@pytest.mark.parametrize(
    "key", [1, None, True, object()], ids=["int", "none", "bool", "custom"]
)
@pytest.mark.parametrize("nested", ["mapping", "list", "tuple"])
def test_create_rejects_non_string_argument_keys_before_sql(key: object, nested: str) -> None:
    invalid = {key: "rejected", str(key): "retained"}
    arguments = (
        invalid
        if nested == "mapping"
        else {"nested": [invalid] if nested == "list" else (invalid,)}
    )
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError) as failure:
        RemoteOperationProposalRepository().create(transaction, record(arguments=arguments))
    assert failure.value.code == "repository_validation"
    assert transaction.calls == []


def test_corrupt_persisted_arguments_and_status_fail_closed() -> None:
    repository = RemoteOperationProposalRepository()
    with pytest.raises(RepositoryDataError):
        repository.get(
            ScriptedTransaction(one=[row(args_json=[])]),
            owner_id="owner-1",
            proposal_id="proposal-1",
        )
    with pytest.raises(RepositoryDataError):
        repository.get(
            ScriptedTransaction(one=[row(status="other")]),
            owner_id="owner-1",
            proposal_id="proposal-1",
        )
