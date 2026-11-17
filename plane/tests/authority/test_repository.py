"""Tests for astralplane.authority.repository: owner isolation, CAS fencing, and atomic
binding/lifecycle/effect/claim behavior using a scripted transaction.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import pytest

from astralplane.authority.claims import (
    EXECUTOR_ANCHOR_FORMAT,
    ExternalAuthorityAnchorMetadata,
    ReceiptClaim,
    ReceiptSequenceWatermark,
)
from astralplane.authority.effects import (
    AstralToolScope,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)
from astralplane.authority.lifecycle import (
    AuthorityLifecycleKind,
    AuthorityLifecycleOperation,
    AuthorityLifecycleStatus,
)
from astralplane.authority.models import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
)
from astralplane.authority.repository import (
    AuthorityCompareAndSetConflictError,
    AuthorityIdempotencyConflictError,
    AuthorityRepository,
    ReceiptClaimConflictError,
    ReceiptWatermarkConflictError,
)
from astralplane.contracts import OutboxEntry
from astralplane.repositories import RepositoryDataError, RepositoryValidationError

NOW = datetime(2026, 8, 14, 18, tzinfo=UTC)
POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
EFFECT_DIGEST = "3" * 64
CANONICAL_DIGEST = "4" * 64
REQUEST_FINGERPRINT = "5" * 64
NONCE = "nonce-0123456789abcdef"


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = "OK"
    returned_records: tuple[dict[str, Any], ...] = ()


class ScriptedTransaction:
    def __init__(
        self,
        *,
        one: list[dict[str, Any] | None | BaseException] | None = None,
        many: list[tuple[dict[str, Any], ...] | BaseException] | None = None,
        execute: list[Result | BaseException] | None = None,
    ) -> None:
        self.one = deque(one or [])
        self.many = deque(many or [])
        self.execute_results = deque(execute or [])
        self.calls: list[tuple[str, str, object]] = []
        self.savepoints: list[str] = []
        self.rolled_back = False
        self.released = False

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("one", statement, parameters))
        if not self.one:
            raise AssertionError(f"unexpected fetch_one: {statement}")
        value = self.one.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def fetch_all(
        self,
        statement: str,
        parameters: object = (),
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(("all", statement, parameters))
        if not self.many:
            return ()
        value = self.many.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        if not self.execute_results:
            return Result()
        value = self.execute_results.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    @contextmanager
    def savepoint(self, name: str) -> Iterator[ScriptedTransaction]:
        self.savepoints.append(name)
        try:
            yield self
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.released = True


def _binding(**changes: object) -> AgentAuthorityBinding:
    values: dict[str, object] = {
        "binding_id": "binding-1",
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "runtime_id": "runtime-1",
        "runtime_generation": 1,
        "population": AuthorityPopulation.SERVER_DYNAMIC,
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "lineage_id": "lineage-1",
        "subject_id": "agent-1",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "config_epoch": 7,
        "capabilities": ("astral.tools.read",),
        "lease_sequence": 10,
        "lease_expires_at_ns": 1_723_658_430_000_000_000,
        "state": AuthorityBindingState.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 0,
    }
    values.update(changes)
    return AgentAuthorityBinding(**values)  # type: ignore[arg-type]


def _provisioning_binding() -> AgentAuthorityBinding:
    return AgentAuthorityBinding.provisioning_intent(
        binding_id="binding-pending",
        owner_id="owner-1",
        agent_id="agent-1",
        runtime_id="runtime-1",
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-1",
        envelope_id="envelope-1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        created_at=NOW,
    )


def _lifecycle(**changes: object) -> AuthorityLifecycleOperation:
    values: dict[str, object] = {
        "operation_id": "lifecycle-1",
        "owner_id": "owner-1",
        "binding_id": "binding-1",
        "kind": AuthorityLifecycleKind.RENEW,
        "expected_binding_version": 0,
        "expected_lease_sequence": 10,
        "request_fingerprint": REQUEST_FINGERPRINT,
        "status": AuthorityLifecycleStatus.PENDING,
        "remote_request_id": "lifecycle-1",
        "result_digest": None,
        "error_code": None,
        "attempt_count": 0,
        "next_attempt_at": NOW + timedelta(seconds=10),
        "last_attempt_at": None,
        "reconciled_at": None,
        "reconciliation_digest": None,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 0,
    }
    values.update(changes)
    return AuthorityLifecycleOperation(**values)  # type: ignore[arg-type]


def _effect(**changes: object) -> ProtectedEffectOperation:
    values: dict[str, object] = {
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "binding_id": "binding-1",
        "tool_id": "search_records",
        "astral_scope": AstralToolScope.READ,
        "lets_capability": "astral.tools.read",
        "lets_transition": "tool_read",
        "executor_audience": "executor-a",
        "nonce": NONCE,
        "effect_digest": EFFECT_DIGEST,
        "expected_sequence": 10,
        "audit_correlation_id": "audit-1",
        "status": ProtectedEffectStatus.RECEIPT_RECEIVED,
        "receipt_id": "receipt-1",
        "receipt_digest": CANONICAL_DIGEST,
        "effect_result_digest": None,
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 3,
    }
    values.update(changes)
    return ProtectedEffectOperation(**values)  # type: ignore[arg-type]


def _anchor(**changes: object) -> ExternalAuthorityAnchorMetadata:
    values: dict[str, object] = {
        "anchor_format": EXECUTOR_ANCHOR_FORMAT,
        "audience": "executor-a",
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "config_epoch": 7,
        "executor_policy_sha256": "6" * 64,
        "trust_registry_sha256": "7" * 64,
        "schema_version": 5,
        "database_instance_id": "8" * 64,
        "claim_sequence": 12,
        "claim_digest": "9" * 64,
        "clock_floor_ns": 1_723_658_400_000_000_000,
        "confirmed_at": NOW + timedelta(seconds=1),
    }
    values.update(changes)
    return ExternalAuthorityAnchorMetadata(**values)  # type: ignore[arg-type]


def _claim(**changes: object) -> ReceiptClaim:
    values: dict[str, object] = {
        "receipt_id": "receipt-1",
        "operation_id": "operation-1",
        "owner_id": "owner-1",
        "binding_id": "binding-1",
        "tenant_id": "tenant-1",
        "envelope_id": "envelope-1",
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "subject_id": "agent-1",
        "lineage_id": "lineage-1",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "config_epoch": 7,
        "audience": "executor-a",
        "transition": "tool_read",
        "nonce": NONCE,
        "resulting_sequence": 11,
        "evidence_digest": "sha256:" + EFFECT_DIGEST,
        "issued_at_ns": 1_723_658_399_000_000_000,
        "expires_at_ns": 1_723_658_430_000_000_000,
        "claimed_at": NOW,
        "canonical_digest": CANONICAL_DIGEST,
        "authority_anchor": _anchor(),
    }
    values.update(changes)
    return ReceiptClaim(**values)  # type: ignore[arg-type]


def _watermark(**changes: object) -> ReceiptSequenceWatermark:
    values: dict[str, object] = {
        "warden_id": "warden-1",
        "lease_id": "lease-1",
        "audience": "executor-a",
        "last_sequence": 11,
        "updated_at": NOW,
        "expires_at_ns": 1_723_658_430_000_000_000,
        "version": 0,
    }
    values.update(changes)
    return ReceiptSequenceWatermark(**values)  # type: ignore[arg-type]


def _outbox() -> OutboxEntry:
    payload = b'{"owner_id":"owner-1","operation_id":"operation-1"}'
    return OutboxEntry(
        entry_id="authority-receipt-1",
        topic="authority.receipt_claimed",
        canonical_payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        idempotency_key="authority-receipt-1",
        available_at=NOW,
    )


def _row(value: object) -> dict[str, Any]:
    data = asdict(value)  # type: ignore[arg-type]
    if isinstance(value, ReceiptClaim):
        anchor = data.pop("authority_anchor")
        data.update(
            {
                "anchor_format": anchor["anchor_format"],
                "anchor_executor_policy_sha256": anchor["executor_policy_sha256"],
                "anchor_trust_registry_sha256": anchor["trust_registry_sha256"],
                "anchor_schema_version": anchor["schema_version"],
                "anchor_database_instance_id": anchor["database_instance_id"],
                "anchor_claim_sequence": anchor["claim_sequence"],
                "anchor_claim_digest": anchor["claim_digest"],
                "anchor_clock_floor_ns": anchor["clock_floor_ns"],
                "anchor_confirmed_at": anchor["confirmed_at"],
            }
        )
    for key, item in tuple(data.items()):
        if isinstance(item, StrEnum):
            data[key] = item.value
        elif isinstance(item, tuple):
            data[key] = list(item)
    return data


def test_binding_repository_is_owner_scoped_idempotent_and_cas_fenced() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    created_tx = ScriptedTransaction(one=[_row(binding)])
    assert repository.create_binding(created_tx, binding) == binding
    assert created_tx.calls[0][2][1] == "owner-1"  # type: ignore[index]

    replay_tx = ScriptedTransaction(one=[None, _row(binding)])
    assert repository.create_binding(replay_tx, binding) == binding
    assert "WHERE owner_id = %s" in replay_tx.calls[1][1]

    read_tx = ScriptedTransaction(one=[_row(binding)])
    assert repository.get_binding(read_tx, owner_id="owner-1", binding_id="binding-1") == binding
    assert read_tx.calls[0][2] == ("owner-1", "binding-1")

    replacement = replace(
        binding,
        state=AuthorityBindingState.QUIESCENT,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )
    update_tx = ScriptedTransaction(one=[_row(replacement)])
    assert (
        repository.transition_binding(
            update_tx,
            replacement,
            expected_state=AuthorityBindingState.ACTIVE,
            expected_version=0,
        )
        == replacement
    )
    sql = update_tx.calls[0][1]
    assert "WHERE owner_id = %s AND binding_id = %s" in sql
    assert "state = %s AND version = %s" in sql


def test_binding_conflicts_and_corrupt_rows_are_typed() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    different = _row(binding)
    different["runtime_id"] = "other-runtime"
    with pytest.raises(AuthorityIdempotencyConflictError) as caught:
        repository.create_binding(ScriptedTransaction(one=[None, different]), binding)
    assert caught.value.code == "authority_idempotency_conflict"

    replacement = replace(binding, version=1, updated_at=NOW + timedelta(seconds=1))
    with pytest.raises(AuthorityCompareAndSetConflictError):
        repository.transition_binding(
            ScriptedTransaction(one=[None]),
            replacement,
            expected_state=AuthorityBindingState.ACTIVE,
            expected_version=0,
        )
    with pytest.raises(AuthorityCompareAndSetConflictError, match="terminal"):
        repository.transition_binding(
            ScriptedTransaction(),
            replacement,
            expected_state=AuthorityBindingState.CLOSED,
            expected_version=0,
        )
    with pytest.raises(RepositoryValidationError):
        repository.transition_binding(
            ScriptedTransaction(),
            binding,
            expected_state=AuthorityBindingState.ACTIVE,
            expected_version=0,
        )

    corrupt = _row(binding)
    corrupt.pop("owner_id")
    with pytest.raises(RepositoryDataError, match="missing"):
        repository.get_binding(
            ScriptedTransaction(one=[corrupt]),
            owner_id="owner-1",
            binding_id="binding-1",
        )
    invalid_enum = _row(binding)
    invalid_enum["state"] = "unknown"
    with pytest.raises(RepositoryDataError, match="enum"):
        repository.get_binding(
            ScriptedTransaction(one=[invalid_enum]),
            owner_id="owner-1",
            binding_id="binding-1",
        )
    invalid_capabilities = _row(binding)
    invalid_capabilities["capabilities"] = "astral.tools.read"
    with pytest.raises(RepositoryDataError, match="capabilities"):
        repository.get_binding(
            ScriptedTransaction(one=[invalid_capabilities]),
            owner_id="owner-1",
            binding_id="binding-1",
        )


def test_active_binding_query_is_exact_owner_runtime_generation_scoped() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    transaction = ScriptedTransaction(one=[_row(binding)])

    assert (
        repository.get_active_binding(
            transaction,
            owner_id="owner-1",
            agent_id="agent-1",
            runtime_id="runtime-1",
            runtime_generation=1,
        )
        == binding
    )
    _, statement, parameters = transaction.calls[0]
    assert "state = 'active'" in statement
    assert parameters == ("owner-1", "agent-1", "runtime-1", 1)
    assert (
        repository.get_active_binding(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            agent_id="agent-1",
            runtime_id="runtime-1",
            runtime_generation=2,
        )
        is None
    )

    invalid_scopes = (
        {"owner_id": " bad owner "},
        {"agent_id": ""},
        {"runtime_id": "bad runtime"},
        {"runtime_generation": 0},
        {"runtime_generation": True},
    )
    base: dict[str, object] = {
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "runtime_id": "runtime-1",
        "runtime_generation": 1,
    }
    for change in invalid_scopes:
        with pytest.raises(RepositoryValidationError):
            repository.get_active_binding(  # type: ignore[arg-type]
                ScriptedTransaction(),
                **(base | change),
            )

    wrong_owner = _row(binding)
    wrong_owner["owner_id"] = "owner-2"
    with pytest.raises(RepositoryDataError, match="exact scope"):
        repository.get_active_binding(
            ScriptedTransaction(one=[wrong_owner]),
            owner_id="owner-1",
            agent_id="agent-1",
            runtime_id="runtime-1",
            runtime_generation=1,
        )


def test_latest_binding_query_is_population_scoped_and_deterministic() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    transaction = ScriptedTransaction(one=[_row(binding)])

    assert (
        repository.get_latest_binding(
            transaction,
            owner_id="owner-1",
            agent_id="agent-1",
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )
        == binding
    )
    _, statement, parameters = transaction.calls[0]
    assert "ORDER BY created_at DESC, binding_id DESC" in statement
    assert "LIMIT 1" in statement
    assert parameters == ("owner-1", "agent-1", "server_dynamic")

    with pytest.raises(RepositoryValidationError, match="population"):
        repository.get_latest_binding(
            ScriptedTransaction(),
            owner_id="owner-1",
            agent_id="agent-1",
            population="server_dynamic",  # type: ignore[arg-type]
        )
    wrong_population = _row(binding)
    wrong_population["population"] = AuthorityPopulation.BYO_USER.value
    with pytest.raises(RepositoryDataError, match="population scope"):
        repository.get_latest_binding(
            ScriptedTransaction(one=[wrong_population]),
            owner_id="owner-1",
            agent_id="agent-1",
            population=AuthorityPopulation.SERVER_DYNAMIC,
        )


def test_provisioning_binding_activation_is_a_one_time_remote_identity_cas() -> None:
    repository = AuthorityRepository()
    intent = _provisioning_binding()
    activated = replace(
        intent,
        warden_id="warden-1",
        lease_id="lease-1",
        lineage_id="lineage-1",
        subject_id="agent-1",
        lease_sequence=1,
        lease_expires_at_ns=1_800_000_000_000_000_000,
        state=AuthorityBindingState.ACTIVE,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )

    transaction = ScriptedTransaction(one=[_row(activated)])
    assert repository.activate_binding(
        transaction,
        activated,
        expected_version=0,
    ) == activated
    _, statement, parameters = transaction.calls[0]
    assert "state = 'provisioning' AND version = %s" in statement
    assert "lease_sequence = 0 AND lease_expires_at_ns = 0" in statement
    assert intent.warden_id in parameters  # type: ignore[operator]
    assert intent.lease_id in parameters  # type: ignore[operator]

    with pytest.raises(AuthorityCompareAndSetConflictError, match="activation"):
        repository.activate_binding(
            ScriptedTransaction(one=[None]),
            activated,
            expected_version=0,
        )
    with pytest.raises(RepositoryValidationError, match="active state"):
        repository.activate_binding(
            ScriptedTransaction(),
            replace(activated, state=AuthorityBindingState.QUIESCENT),
            expected_version=0,
        )


def test_failed_provisioning_binding_closes_through_a_dedicated_cas() -> None:
    repository = AuthorityRepository()
    intent = _provisioning_binding()
    abandoned = replace(
        intent,
        state=AuthorityBindingState.CLOSED,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )

    transaction = ScriptedTransaction(one=[_row(abandoned)])
    assert repository.abandon_provisioning_binding(
        transaction,
        abandoned,
        expected_version=0,
    ) == abandoned
    _, statement, parameters = transaction.calls[0]
    assert "WHERE owner_id = %s AND binding_id = %s" in statement
    assert parameters[-2:] == (AuthorityBindingState.PROVISIONING.value, 0)  # type: ignore[index]

    with pytest.raises(AuthorityCompareAndSetConflictError):
        repository.abandon_provisioning_binding(
            ScriptedTransaction(one=[None]),
            abandoned,
            expected_version=0,
        )
    with pytest.raises(RepositoryValidationError, match="closed state"):
        repository.abandon_provisioning_binding(
            ScriptedTransaction(),
            intent,
            expected_version=0,
        )
    with pytest.raises(RepositoryValidationError, match="pending authority fence"):
        repository.abandon_provisioning_binding(
            ScriptedTransaction(),
            replace(
                _binding(),
                state=AuthorityBindingState.CLOSED,
                updated_at=NOW + timedelta(seconds=1),
                version=1,
            ),
            expected_version=0,
        )


def test_lifecycle_operations_replay_only_an_identical_request_fingerprint() -> None:
    repository = AuthorityRepository()
    operation = _lifecycle()
    assert (
        repository.create_lifecycle_operation(
            ScriptedTransaction(one=[_row(operation)]),
            operation,
        )
        == operation
    )

    evolved = replace(
        operation,
        status=AuthorityLifecycleStatus.IN_FLIGHT,
        attempt_count=1,
        last_attempt_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )
    replay_tx = ScriptedTransaction(one=[None, _row(evolved)])
    assert repository.create_lifecycle_operation(replay_tx, operation) == evolved
    assert "owner_id = %s AND operation_id = %s" in replay_tx.calls[1][1]

    conflict = _row(operation)
    conflict["request_fingerprint"] = "a" * 64
    with pytest.raises(AuthorityIdempotencyConflictError, match="fingerprint"):
        repository.create_lifecycle_operation(
            ScriptedTransaction(one=[None, conflict]),
            operation,
        )


def test_lifecycle_read_and_transition_use_owner_state_and_version_fences() -> None:
    repository = AuthorityRepository()
    operation = _lifecycle()
    assert (
        repository.get_lifecycle_operation(
            ScriptedTransaction(one=[_row(operation)]),
            owner_id="owner-1",
            operation_id="lifecycle-1",
        )
        == operation
    )
    assert (
        repository.get_lifecycle_operation(
            ScriptedTransaction(one=[None]),
            owner_id="other-owner",
            operation_id="lifecycle-1",
        )
        is None
    )

    replacement = replace(
        operation,
        status=AuthorityLifecycleStatus.IN_FLIGHT,
        attempt_count=1,
        last_attempt_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )
    transaction = ScriptedTransaction(one=[_row(replacement)])
    assert (
        repository.transition_lifecycle_operation(
            transaction,
            replacement,
            expected_status=AuthorityLifecycleStatus.PENDING,
            expected_version=0,
        )
        == replacement
    )
    sql = transaction.calls[0][1]
    assert "WHERE owner_id = %s AND operation_id = %s" in sql
    assert "request_fingerprint = %s" in sql
    assert "status = %s AND version = %s" in sql

    with pytest.raises(AuthorityCompareAndSetConflictError):
        repository.transition_lifecycle_operation(
            ScriptedTransaction(one=[None]),
            replacement,
            expected_status=AuthorityLifecycleStatus.PENDING,
            expected_version=0,
        )
    succeeded = replace(
        replacement,
        status=AuthorityLifecycleStatus.SUCCEEDED,
        result_digest="b" * 64,
        next_attempt_at=None,
        version=2,
    )
    with pytest.raises(AuthorityCompareAndSetConflictError, match="terminal"):
        repository.transition_lifecycle_operation(
            ScriptedTransaction(),
            succeeded,
            expected_status=AuthorityLifecycleStatus.SUCCEEDED,
            expected_version=1,
        )


def test_recoverable_lifecycle_query_is_owner_partitioned_bounded_and_skip_locked() -> None:
    repository = AuthorityRepository()
    operation = _lifecycle()
    transaction = ScriptedTransaction(many=[(_row(operation),)])

    assert repository.list_recoverable_lifecycle_operations(
        transaction,
        owner_id="owner-1",
        due_at=NOW,
        limit=10,
    ) == (operation,)
    _, statement, parameters = transaction.calls[0]
    assert "owner_id = %s" in statement
    assert "status IN ('pending', 'in_flight', 'uncertain')" in statement
    assert "FOR UPDATE SKIP LOCKED" in statement
    assert parameters == ("owner-1", NOW, 10)

    with pytest.raises(RepositoryValidationError, match="scope"):
        repository.list_recoverable_lifecycle_operations(
            ScriptedTransaction(),
            owner_id=" bad owner ",
            due_at=NOW,
        )
    with pytest.raises(RepositoryValidationError, match="limit"):
        repository.list_recoverable_lifecycle_operations(
            ScriptedTransaction(),
            owner_id="owner-1",
            due_at=NOW,
            limit=201,
        )


def test_recovery_owner_partitions_are_bounded_ordered_and_cover_both_queues() -> None:
    repository = AuthorityRepository()
    cutoff = NOW - timedelta(minutes=1)
    transaction = ScriptedTransaction(
        many=[
            (
                {"owner_id": "owner-1", "recovery_at": cutoff},
                {"owner_id": "owner-2", "recovery_at": NOW},
            )
        ]
    )

    assert repository.list_recovery_owners(
        transaction,
        lifecycle_due_at=NOW,
        effect_updated_before=cutoff,
        limit=25,
    ) == ("owner-1", "owner-2")
    _, statement, parameters = transaction.calls[0]
    assert "astralplane_authority_lifecycle_operation" in statement
    assert "astralplane_protected_effect_operation" in statement
    assert "UNION ALL" in statement
    assert "FOR UPDATE" not in statement
    assert parameters == (NOW, cutoff, 25)

    with pytest.raises(RepositoryValidationError, match="UTC-aware"):
        repository.list_recovery_owners(
            ScriptedTransaction(),
            lifecycle_due_at=NOW.replace(tzinfo=None),
            effect_updated_before=cutoff,
        )
    with pytest.raises(RepositoryValidationError, match="limit"):
        repository.list_recovery_owners(
            ScriptedTransaction(),
            lifecycle_due_at=NOW,
            effect_updated_before=cutoff,
            limit=1001,
        )
    with pytest.raises(RepositoryDataError, match="nondeterministic"):
        repository.list_recovery_owners(
            ScriptedTransaction(
                many=[
                    (
                        {"owner_id": "owner-2", "recovery_at": NOW},
                        {"owner_id": "owner-1", "recovery_at": cutoff},
                    )
                ]
            ),
            lifecycle_due_at=NOW,
            effect_updated_before=cutoff,
        )


def test_protected_effect_intent_is_idempotent_and_owner_isolated() -> None:
    repository = AuthorityRepository()
    initial = _effect(
        status=ProtectedEffectStatus.CREATED,
        receipt_id=None,
        receipt_digest=None,
        version=0,
    )
    assert (
        repository.create_protected_effect(
            ScriptedTransaction(one=[_row(initial)]),
            initial,
        )
        == initial
    )
    evolved = replace(
        initial,
        status=ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )
    replay_tx = ScriptedTransaction(one=[None, _row(evolved)])
    assert repository.create_protected_effect(replay_tx, initial) == evolved
    assert "WHERE owner_id = %s" in replay_tx.calls[1][1]

    conflict = _row(initial)
    conflict["effect_digest"] = "a" * 64
    with pytest.raises(AuthorityIdempotencyConflictError):
        repository.create_protected_effect(
            ScriptedTransaction(one=[None, conflict]),
            initial,
        )


def test_protected_effect_transitions_are_explicit_cas_updates() -> None:
    repository = AuthorityRepository()
    current = _effect(
        status=ProtectedEffectStatus.CREATED,
        receipt_id=None,
        receipt_digest=None,
        version=0,
    )
    authorized = replace(
        current,
        status=ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        updated_at=NOW + timedelta(seconds=1),
        version=1,
    )
    transaction = ScriptedTransaction(one=[_row(authorized)])
    assert (
        repository.transition_protected_effect(
            transaction,
            authorized,
            expected_status=ProtectedEffectStatus.CREATED,
            expected_version=0,
        )
        == authorized
    )
    sql = transaction.calls[0][1]
    assert "WHERE owner_id = %s AND operation_id = %s" in sql
    assert "effect_digest = %s" in sql
    assert "status = %s AND version = %s" in sql

    with pytest.raises(AuthorityCompareAndSetConflictError, match="not allowed"):
        repository.transition_protected_effect(
            ScriptedTransaction(),
            replace(
                authorized,
                status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                receipt_id="receipt-1",
                receipt_digest=CANONICAL_DIGEST,
                version=2,
            ),
            expected_status=ProtectedEffectStatus.ASTRAL_AUTHORIZED,
            expected_version=1,
        )
    with pytest.raises(AuthorityCompareAndSetConflictError, match="stale"):
        repository.transition_protected_effect(
            ScriptedTransaction(one=[None]),
            authorized,
            expected_status=ProtectedEffectStatus.CREATED,
            expected_version=0,
        )
    assert (
        repository.get_protected_effect(
            ScriptedTransaction(one=[None]),
            owner_id="other-owner",
            operation_id="operation-1",
        )
        is None
    )


def test_recoverable_effect_query_is_stale_owner_bounded_and_skip_locked() -> None:
    repository = AuthorityRepository()
    pending = _effect(
        operation_id="operation-a",
        status=ProtectedEffectStatus.LETS_PENDING,
        receipt_id=None,
        receipt_digest=None,
        updated_at=NOW,
    )
    receipt = _effect(
        operation_id="operation-b",
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        updated_at=NOW + timedelta(seconds=1),
    )
    uncertain = _effect(
        operation_id="operation-c",
        status=ProtectedEffectStatus.OUTCOME_UNCERTAIN,
        error_code="outcome-unknown",
        updated_at=NOW + timedelta(seconds=2),
    )
    cutoff = NOW + timedelta(seconds=3)
    transaction = ScriptedTransaction(
        many=[tuple(_row(item) for item in (pending, receipt, uncertain))]
    )

    assert repository.list_recoverable_protected_effects(
        transaction,
        owner_id="owner-1",
        updated_before=cutoff,
        limit=3,
    ) == (pending, receipt, uncertain)
    _, statement, parameters = transaction.calls[0]
    for status in (
        "created",
        "astral_authorized",
        "lets_pending",
        "receipt_received",
        "receipt_claimed",
        "executing",
        "outcome_uncertain",
    ):
        assert f"'{status}'" in statement
    for terminal_status in ("succeeded", "denied", "failed_closed", "effect_failed"):
        assert f"'{terminal_status}'" not in statement
    assert "updated_at < %s" in statement
    assert "ORDER BY updated_at, operation_id" in statement
    assert "FOR UPDATE SKIP LOCKED" in statement
    assert parameters == ("owner-1", cutoff, 3)


def test_recoverable_effect_query_rejects_invalid_or_out_of_scope_results() -> None:
    repository = AuthorityRepository()
    cutoff = NOW + timedelta(seconds=1)

    with pytest.raises(RepositoryValidationError, match="owner"):
        repository.list_recoverable_protected_effects(
            ScriptedTransaction(),
            owner_id=" bad owner ",
            updated_before=cutoff,
        )
    with pytest.raises(RepositoryValidationError, match="UTC-aware"):
        repository.list_recoverable_protected_effects(
            ScriptedTransaction(),
            owner_id="owner-1",
            updated_before=datetime(2026, 8, 14, 18),
        )
    for limit in (0, True, 201):
        with pytest.raises(RepositoryValidationError, match="limit"):
            repository.list_recoverable_protected_effects(
                ScriptedTransaction(),
                owner_id="owner-1",
                updated_before=cutoff,
                limit=limit,
            )

    wrong_owner = _row(_effect(updated_at=NOW))
    wrong_owner["owner_id"] = "owner-2"
    with pytest.raises(RepositoryDataError, match="stale owner scope"):
        repository.list_recoverable_protected_effects(
            ScriptedTransaction(many=[(wrong_owner,)]),
            owner_id="owner-1",
            updated_before=cutoff,
        )

    terminal = _effect(
        status=ProtectedEffectStatus.SUCCEEDED,
        effect_result_digest=EFFECT_DIGEST,
        updated_at=NOW,
    )
    with pytest.raises(RepositoryDataError, match="stale owner scope"):
        repository.list_recoverable_protected_effects(
            ScriptedTransaction(many=[(_row(terminal),)]),
            owner_id="owner-1",
            updated_before=cutoff,
        )

    later = _effect(operation_id="operation-z", updated_at=NOW)
    earlier = _effect(operation_id="operation-a", updated_at=NOW)
    with pytest.raises(RepositoryDataError, match="ordered"):
        repository.list_recoverable_protected_effects(
            ScriptedTransaction(many=[(_row(later), _row(earlier))]),
            owner_id="owner-1",
            updated_before=cutoff,
        )


def test_receipt_claim_watermark_effect_and_outbox_share_one_savepoint() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    current = _effect()
    claim = _claim()
    watermark = _watermark()
    claimed = replace(
        current,
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        updated_at=NOW + timedelta(milliseconds=1),
        version=4,
    )
    transaction = ScriptedTransaction(
        one=[
            _row(binding),
            _row(current),
            _row(claim),
            _row(watermark),
            _row(claimed),
        ],
        execute=[Result(rowcount=1)],
    )

    assert (
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=watermark,
            claimed_effect=claimed,
            outbox_entry=_outbox(),
        )
        == claim
    )
    assert transaction.savepoints == ["astralplane_authority_claim"]
    assert transaction.released is True
    assert transaction.rolled_back is False
    statements = [statement for _, statement, _ in transaction.calls]
    expected_tables = [
        "astralplane_authority_binding",
        "astralplane_protected_effect_operation",
        "astralplane_receipt_claim",
        "astralplane_receipt_sequence_watermark",
        "astralplane_protected_effect_operation",
        "astralplane_outbox",
    ]
    actual_tables = [
        next(
            name
            for name in (
                "astralplane_authority_binding",
                "astralplane_protected_effect_operation",
                "astralplane_receipt_claim",
                "astralplane_receipt_sequence_watermark",
                "astralplane_outbox",
            )
            if name in statement
        )
        for statement in statements
    ]
    assert actual_tables == expected_tables
    for statement in statements:
        if (
            statement.startswith(("SELECT", "UPDATE"))
            and "receipt_sequence_watermark" not in statement
            and "astralplane_outbox" not in statement
        ):
            assert "owner_id = %s" in statement


def test_exact_receipt_retry_returns_existing_claim_without_advancing_again() -> None:
    repository = AuthorityRepository()
    binding = _binding()
    claim = _claim()
    claimed = _effect(status=ProtectedEffectStatus.RECEIPT_CLAIMED, version=4)
    transaction = ScriptedTransaction(one=[_row(binding), _row(claimed), None, _row(claim)])

    assert (
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=claimed,
            outbox_entry=_outbox(),
        )
        == claim
    )
    assert len(transaction.calls) == 4
    assert not any(kind == "execute" for kind, _, _ in transaction.calls)


@pytest.mark.parametrize("missing_index", [0, 1])
def test_receipt_claim_requires_owner_binding_and_effect(
    missing_index: int,
) -> None:
    rows = [_row(_binding()), _row(_effect())]
    rows[missing_index] = None
    transaction = ScriptedTransaction(one=rows)
    with pytest.raises(ReceiptClaimConflictError, match="unavailable"):
        AuthorityRepository().claim_receipt(
            transaction,
            claim=_claim(),
            watermark=_watermark(),
            claimed_effect=replace(
                _effect(),
                status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                version=4,
            ),
            outbox_entry=_outbox(),
        )
    assert transaction.rolled_back is True


@pytest.mark.parametrize(
    "claim",
    [
        _claim(lease_id="other-lease"),
        _claim(audience="other-audience", authority_anchor=_anchor(audience="other-audience")),
        _claim(evidence_digest="sha256:" + "a" * 64),
    ],
)
def test_receipt_binding_effect_and_evidence_mismatches_fail_closed(
    claim: ReceiptClaim,
) -> None:
    transaction = ScriptedTransaction(one=[_row(_binding()), _row(_effect())])
    with pytest.raises(ReceiptClaimConflictError):
        AuthorityRepository().claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=replace(
                _effect(),
                status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                version=4,
            ),
            outbox_entry=_outbox(),
        )
    assert transaction.rolled_back is True


@pytest.mark.parametrize(
    ("binding", "effect", "claim", "message"),
    [
        (
            _binding(state=AuthorityBindingState.QUIESCENT),
            _effect(),
            _claim(),
            "not active",
        ),
        (_binding(agent_id="other-agent"), _effect(), _claim(), "agent"),
        (
            _binding(capabilities=("astral.tools.write",)),
            _effect(),
            _claim(),
            "capability",
        ),
        (_binding(lease_sequence=9), _effect(), _claim(), "sequence is stale"),
        (
            _binding(),
            _effect(),
            _claim(resulting_sequence=12),
            "advance exactly once",
        ),
        (
            _binding(lease_expires_at_ns=1_723_658_420_000_000_000),
            _effect(),
            _claim(),
            "expiry exceeds",
        ),
    ],
)
def test_claim_rechecks_binding_security_fences_under_the_savepoint_lock(
    binding: AgentAuthorityBinding,
    effect: ProtectedEffectOperation,
    claim: ReceiptClaim,
    message: str,
) -> None:
    transaction = ScriptedTransaction(one=[_row(binding), _row(effect)])
    with pytest.raises(ReceiptClaimConflictError, match=message):
        AuthorityRepository().claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=replace(
                effect,
                status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                version=effect.version + 1,
            ),
            outbox_entry=_outbox(),
        )
    assert transaction.rolled_back is True


def test_receipt_uniqueness_and_existing_effect_evidence_conflicts_are_typed() -> None:
    repository = AuthorityRepository()
    claim = _claim()
    different = _row(claim)
    different["canonical_digest"] = "a" * 64
    transaction = ScriptedTransaction(one=[_row(_binding()), _row(_effect()), None, different])
    with pytest.raises(ReceiptClaimConflictError, match="uniqueness") as caught:
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=replace(
                _effect(),
                status=ProtectedEffectStatus.RECEIPT_CLAIMED,
                version=4,
            ),
            outbox_entry=_outbox(),
        )
    assert caught.value.code == "authority_receipt_claim_conflict"

    bad_effect = _effect(status=ProtectedEffectStatus.RECEIPT_CLAIMED, version=4)
    bad_effect_row = _row(bad_effect)
    bad_effect_row["receipt_id"] = "other-receipt"
    transaction = ScriptedTransaction(one=[_row(_binding()), bad_effect_row, None, _row(claim)])
    with pytest.raises(ReceiptClaimConflictError, match="effect evidence"):
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=bad_effect,
            outbox_entry=_outbox(),
        )


def test_new_claim_requires_exact_effect_and_watermark_advancement() -> None:
    repository = AuthorityRepository()
    claim = _claim()
    current = _effect()
    invalid_replacement = replace(
        current,
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        version=5,
    )
    transaction = ScriptedTransaction(one=[_row(_binding()), _row(current), _row(claim)])
    with pytest.raises(ReceiptClaimConflictError, match="advance"):
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=invalid_replacement,
            outbox_entry=_outbox(),
        )

    claimed = replace(
        current,
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        version=4,
    )
    transaction = ScriptedTransaction(one=[_row(_binding()), _row(current), _row(claim)])
    with pytest.raises(RepositoryValidationError, match="watermark"):
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(last_sequence=12),
            claimed_effect=claimed,
            outbox_entry=_outbox(),
        )

    transaction = ScriptedTransaction(one=[_row(_binding()), _row(current), _row(claim), None])
    with pytest.raises(ReceiptWatermarkConflictError) as caught:
        repository.claim_receipt(
            transaction,
            claim=claim,
            watermark=_watermark(),
            claimed_effect=claimed,
            outbox_entry=_outbox(),
        )
    assert caught.value.code == "authority_receipt_watermark_conflict"
    assert transaction.rolled_back is True


class FailingOutbox:
    def enqueue(self, _transaction: object, _entry: OutboxEntry) -> Result:
        raise RuntimeError("outbox unavailable")


def test_outbox_failure_rolls_back_claim_watermark_and_effect_savepoint() -> None:
    binding = _binding()
    current = _effect()
    claim = _claim()
    watermark = _watermark()
    claimed = replace(
        current,
        status=ProtectedEffectStatus.RECEIPT_CLAIMED,
        version=4,
    )
    transaction = ScriptedTransaction(
        one=[
            _row(binding),
            _row(current),
            _row(claim),
            _row(watermark),
            _row(claimed),
        ]
    )
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        AuthorityRepository(outbox=FailingOutbox()).claim_receipt(  # type: ignore[arg-type]
            transaction,
            claim=claim,
            watermark=watermark,
            claimed_effect=claimed,
            outbox_entry=_outbox(),
        )
    assert transaction.rolled_back is True
    assert transaction.released is False


def test_repository_rejects_unvalidated_models_and_corrupt_returned_data() -> None:
    repository = AuthorityRepository()
    with pytest.raises(RepositoryValidationError):
        repository.create_binding(ScriptedTransaction(), object())  # type: ignore[arg-type]
    with pytest.raises(RepositoryValidationError):
        repository.create_lifecycle_operation(  # type: ignore[arg-type]
            ScriptedTransaction(),
            object(),
        )
    with pytest.raises(RepositoryValidationError):
        repository.create_protected_effect(  # type: ignore[arg-type]
            ScriptedTransaction(),
            object(),
        )

    corrupt = _row(_binding())
    corrupt["capabilities"] = None
    with pytest.raises(RepositoryDataError, match="capabilities"):
        repository.get_binding(
            ScriptedTransaction(one=[corrupt]),
            owner_id="owner-1",
            binding_id="binding-1",
        )
