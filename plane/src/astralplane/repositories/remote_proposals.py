"""Single-use confirmation proposals gating destructive remote-compute operations:
pending, approved/declined, and consumed states. Used by
orchestrator/remote_confirmation.py, which owns the approval policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)

_STATUSES = frozenset({"approved", "consumed", "declined", "expired", "pending"})


@dataclass(frozen=True, slots=True)
class RemoteOperationProposalRecord:
    proposal_id: str
    owner_id: str = field(repr=False)
    conversation_id: str | None
    machine_id: str
    agent_id: str
    tool_name: str
    args_fingerprint: str
    arguments: Mapping[str, Any] = field(repr=False)
    summary: str
    status: str
    created_at: int
    expires_at: int
    decided_at: int | None = None
    consumed_at: int | None = None


class RemoteOperationProposalRepository:
    _FIELDS = (
        "proposal_id, owner_user_id, chat_id, machine_id, agent_id, verb, "
        "args_json, args_fingerprint, summary, status, created_at, expires_at, "
        "decided_at, consumed_at"
    )

    def create(
        self,
        transaction: Transaction,
        record: RemoteOperationProposalRecord,
    ) -> RemoteOperationProposalRecord:
        proposal = _validated(record)
        result = transaction.execute(
            f"""
            INSERT INTO remote_operation_proposal (
                proposal_id, owner_user_id, chat_id, machine_id, agent_id,
                verb, args_json, args_fingerprint, summary, status,
                created_at, expires_at, decided_at, consumed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (proposal_id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                proposal.proposal_id,
                proposal.owner_id,
                proposal.conversation_id,
                proposal.machine_id,
                proposal.agent_id,
                proposal.tool_name,
                _canonical_json(proposal.arguments, "arguments"),
                proposal.args_fingerprint,
                proposal.summary,
                proposal.status,
                proposal.created_at,
                proposal.expires_at,
                proposal.decided_at,
                proposal.consumed_at,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _owned(
                _single_returned(result, "remote_operation_proposals.create"),
                proposal.owner_id,
            )
        existing = self.get(
            transaction,
            owner_id=proposal.owner_id,
            proposal_id=proposal.proposal_id,
        )
        if existing != proposal:
            raise RepositoryConflictError(
                "remote operation proposal identity has conflicting semantics"
            )
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        proposal_id: str,
    ) -> RemoteOperationProposalRecord | None:
        owner = _required_id(owner_id, "owner_id")
        proposal = _required_id(proposal_id, "proposal_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM remote_operation_proposal "
            "WHERE proposal_id = %s AND owner_user_id = %s",
            (proposal, owner),
        )
        return None if row is None else _owned(row, owner)

    def decide_if_pending(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        proposal_id: str,
        decision: str,
        decided_at: int,
    ) -> RemoteOperationProposalRecord | None:
        owner = _required_id(owner_id, "owner_id")
        proposal = _required_id(proposal_id, "proposal_id")
        if decision not in {"approved", "declined"}:
            raise RepositoryValidationError("decision must be approved or declined")
        observed = _non_negative_int(decided_at, "decided_at")
        result = transaction.execute(
            f"""
            UPDATE remote_operation_proposal
            SET status = %s, decided_at = %s
            WHERE proposal_id = %s AND owner_user_id = %s
              AND status = 'pending' AND expires_at >= %s
            RETURNING {self._FIELDS}
            """,
            (decision, observed, proposal, owner, observed),
        )
        rows = getattr(result, "returned_records", ())
        return None if not rows else _owned(
            _single_returned(result, "remote_operation_proposals.decide"), owner
        )

    def expire_if_pending(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        proposal_id: str,
        observed_at: int,
    ) -> RemoteOperationProposalRecord | None:
        owner = _required_id(owner_id, "owner_id")
        proposal = _required_id(proposal_id, "proposal_id")
        observed = _non_negative_int(observed_at, "observed_at")
        result = transaction.execute(
            f"""
            UPDATE remote_operation_proposal SET status = 'expired', decided_at = %s
            WHERE proposal_id = %s AND owner_user_id = %s
              AND status = 'pending' AND expires_at < %s
            RETURNING {self._FIELDS}
            """,
            (observed, proposal, owner, observed),
        )
        rows = getattr(result, "returned_records", ())
        return None if not rows else _owned(
            _single_returned(result, "remote_operation_proposals.expire"), owner
        )

    def consume_if_valid(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        proposal_id: str,
        expected_tool_name: str,
        expected_args_fingerprint: str,
        consumed_at: int,
    ) -> RemoteOperationProposalRecord | None:
        owner = _required_id(owner_id, "owner_id")
        proposal = _required_id(proposal_id, "proposal_id")
        tool_name = _bounded_text(expected_tool_name, "expected_tool_name", maximum=256)
        fingerprint = _bounded_text(
            expected_args_fingerprint, "expected_args_fingerprint", maximum=256
        )
        observed = _non_negative_int(consumed_at, "consumed_at")
        result = transaction.execute(
            f"""
            UPDATE remote_operation_proposal
            SET status = 'consumed', consumed_at = %s
            WHERE proposal_id = %s AND owner_user_id = %s
              AND status = 'approved' AND expires_at >= %s
              AND verb = %s AND args_fingerprint = %s
            RETURNING {self._FIELDS}
            """,
            (observed, proposal, owner, observed, tool_name, fingerprint),
        )
        rows = getattr(result, "returned_records", ())
        return None if not rows else _owned(
            _single_returned(result, "remote_operation_proposals.consume"), owner
        )

    def delete_owner(self, transaction: Transaction, *, owner_id: str) -> int:
        owner = _required_id(owner_id, "owner_id")
        result = transaction.execute(
            "DELETE FROM remote_operation_proposal WHERE owner_user_id = %s",
            (owner,),
        )
        if result.rowcount < 0:
            raise RepositoryDataError(
                "remote proposal owner deletion returned an invalid row count"
            )
        return result.rowcount


def _validated(record: RemoteOperationProposalRecord) -> RemoteOperationProposalRecord:
    if not isinstance(record, RemoteOperationProposalRecord):
        raise RepositoryValidationError("record must be a RemoteOperationProposalRecord")
    proposal_id = _required_id(record.proposal_id, "proposal_id")
    owner_id = _required_id(record.owner_id, "owner_id")
    conversation_id = (
        None
        if record.conversation_id is None
        else _required_id(record.conversation_id, "conversation_id")
    )
    machine_id = _required_id(record.machine_id, "machine_id")
    agent_id = _required_id(record.agent_id, "agent_id")
    tool_name = _bounded_text(record.tool_name, "tool_name", maximum=256)
    fingerprint = _bounded_text(record.args_fingerprint, "args_fingerprint", maximum=256)
    if not isinstance(record.arguments, Mapping):
        raise RepositoryValidationError("arguments must be a mapping")
    arguments = _structured_json(_canonical_json(record.arguments, "arguments"), "arguments")
    if not isinstance(arguments, Mapping):  # pragma: no cover
        raise RepositoryValidationError("arguments must be a mapping")
    summary = _bounded_text(record.summary, "summary", maximum=2048)
    if record.status not in _STATUSES:
        raise RepositoryValidationError("proposal status is not supported")
    created_at = _non_negative_int(record.created_at, "created_at")
    expires_at = _non_negative_int(record.expires_at, "expires_at")
    decided_at = _optional_time(record.decided_at, "decided_at")
    consumed_at = _optional_time(record.consumed_at, "consumed_at")
    if expires_at <= created_at:
        raise RepositoryValidationError("expires_at must follow created_at")
    if decided_at is not None and decided_at < created_at:
        raise RepositoryValidationError("decided_at cannot precede created_at")
    if consumed_at is not None and consumed_at < created_at:
        raise RepositoryValidationError("consumed_at cannot precede created_at")
    if (record.status == "pending") != (decided_at is None and consumed_at is None):
        raise RepositoryValidationError("pending proposal timestamps are inconsistent")
    if record.status == "consumed" and consumed_at is None:
        raise RepositoryValidationError("consumed proposal requires consumed_at")
    return RemoteOperationProposalRecord(
        proposal_id=proposal_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
        machine_id=machine_id,
        agent_id=agent_id,
        tool_name=tool_name,
        args_fingerprint=fingerprint,
        arguments=arguments,
        summary=summary,
        status=record.status,
        created_at=created_at,
        expires_at=expires_at,
        decided_at=decided_at,
        consumed_at=consumed_at,
    )


def _record(row: Mapping[str, Any]) -> RemoteOperationProposalRecord:
    arguments = _structured_json(_row_value(row, "args_json"), "args_json")
    if not isinstance(arguments, Mapping):
        raise RepositoryDataError("persisted remote proposal arguments must be an object")
    try:
        return _validated(
            RemoteOperationProposalRecord(
                proposal_id=str(_row_value(row, "proposal_id")),
                owner_id=str(_row_value(row, "owner_user_id")),
                conversation_id=None if row.get("chat_id") is None else str(row["chat_id"]),
                machine_id=str(_row_value(row, "machine_id")),
                agent_id=str(_row_value(row, "agent_id")),
                tool_name=str(_row_value(row, "verb")),
                args_fingerprint=str(_row_value(row, "args_fingerprint")),
                arguments=arguments,
                summary=str(_row_value(row, "summary")),
                status=str(_row_value(row, "status")),
                created_at=_row_value(row, "created_at"),
                expires_at=_row_value(row, "expires_at"),
                decided_at=row.get("decided_at"),
                consumed_at=row.get("consumed_at"),
            )
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted remote operation proposal is invalid") from exc


def _owned(row: Mapping[str, Any], owner_id: str) -> RemoteOperationProposalRecord:
    record = _record(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("remote proposal query returned another owner's row")
    return record


def _optional_time(value: object, field: str) -> int | None:
    return None if value is None else _non_negative_int(value, field)


__all__ = ("RemoteOperationProposalRecord", "RemoteOperationProposalRepository")
