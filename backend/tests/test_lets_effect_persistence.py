"""Durable Plane evidence around the final LETS protected-effect boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
    ProtectedEffectOperation,
    ProtectedEffectStatus,
)
from lets.canonical import canonical_digest
from lets.models import Receipt

from orchestrator.lets_effects import (
    LetsEffectError,
    PlaneProtectedEffectCoordinator,
)
from orchestrator.protected_dispatch import build_protected_dispatch_context


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
AUTHORIZED = {
    "effect_class": "read",
    "target_class": "database",
    "data_classification": "phi",
    "egress_class": "none",
    "credential_class": "service_identity",
    "idempotency_class": "read_only",
    "confirmation_class": "confirmed",
    "writes_state": False,
    "network_effect": False,
    "external_actuator": False,
    "target_binding_sha256": "a" * 64,
}


@dataclass(frozen=True)
class ReplayIdentity:
    audience: str
    tenant_id: str
    envelope_id: str
    config_epoch: int
    executor_policy_sha256: bytes
    trust_registry_sha256: bytes


@dataclass(frozen=True)
class AuthorityCheckpoint:
    identity: ReplayIdentity
    schema_version: int
    database_instance_id: bytes
    claim_sequence: int
    claim_digest: bytes
    clock_floor_ns: int | None


@dataclass(frozen=True)
class ReplayStatus:
    rollback_protected: bool
    authority_healthy: bool
    identity: ReplayIdentity
    claim_sequence: int
    clock_floor_ns: int | None
    authority_checkpoint: AuthorityCheckpoint


@dataclass(frozen=True)
class Permit:
    binding_id: str
    owner_id: str
    runtime_generation: int
    context: object
    expected_sequence: int
    nonce: str
    wire_arguments_sha256: str
    receipt: Receipt


class MemoryPlane:
    @contextmanager
    def transaction(self, **_options: object) -> Iterator[object]:
        yield object()


class MemoryRepository:
    def __init__(self, binding: AgentAuthorityBinding) -> None:
        self.binding = binding
        self.effects: dict[tuple[str, str], ProtectedEffectOperation] = {}
        self.claims: list[object] = []
        self.watermarks: list[object] = []
        self.outbox: list[object] = []

    def get_binding(
        self, _transaction: object, *, owner_id: str, binding_id: str
    ) -> AgentAuthorityBinding | None:
        if (owner_id, binding_id) == (self.binding.owner_id, self.binding.binding_id):
            return self.binding
        return None

    def transition_binding(
        self,
        _transaction: object,
        replacement: AgentAuthorityBinding,
        *,
        expected_state: AuthorityBindingState,
        expected_version: int,
    ) -> AgentAuthorityBinding:
        assert self.binding.state is expected_state
        assert self.binding.version == expected_version
        assert replacement.version == expected_version + 1
        self.binding = replacement
        return replacement

    def create_protected_effect(
        self, _transaction: object, operation: ProtectedEffectOperation
    ) -> ProtectedEffectOperation:
        key = operation.owner_operation_key
        current = self.effects.get(key)
        if current is not None and current != operation:
            raise RuntimeError("effect conflict")
        self.effects[key] = operation
        return operation

    def get_protected_effect(
        self, _transaction: object, *, owner_id: str, operation_id: str
    ) -> ProtectedEffectOperation | None:
        return self.effects.get((owner_id, operation_id))

    def transition_protected_effect(
        self,
        _transaction: object,
        replacement: ProtectedEffectOperation,
        *,
        expected_status: ProtectedEffectStatus,
        expected_version: int,
    ) -> ProtectedEffectOperation:
        key = replacement.owner_operation_key
        current = self.effects[key]
        assert current.status is expected_status
        assert current.version == expected_version
        assert replacement.version == expected_version + 1
        self.effects[key] = replacement
        return replacement

    def claim_receipt(
        self,
        _transaction: object,
        *,
        claim: object,
        watermark: object,
        claimed_effect: ProtectedEffectOperation,
        outbox_entry: object,
    ) -> object:
        current = self.effects[claimed_effect.owner_operation_key]
        assert current.status is ProtectedEffectStatus.RECEIPT_RECEIVED
        assert claimed_effect.status is ProtectedEffectStatus.RECEIPT_CLAIMED
        self.effects[claimed_effect.owner_operation_key] = claimed_effect
        self.claims.append(claim)
        self.watermarks.append(watermark)
        self.outbox.append(outbox_entry)
        return claim


def _binding() -> AgentAuthorityBinding:
    return AgentAuthorityBinding(
        binding_id="binding-a",
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        runtime_generation=3,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        warden_id="warden-a",
        lease_id="lease-a",
        lineage_id="lineage-a",
        subject_id="agent-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        lease_sequence=0,
        lease_expires_at_ns=10_000,
        state=AuthorityBindingState.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )


def _context(*, operation_id: str = "effect-a", nonce: str = "a5" * 16):
    return build_protected_dispatch_context(
        operation_id=operation_id,
        agent_id="agent-a",
        runtime_id="runtime-a",
        tool_id="clinical.search_v2",
        scope="tools:read",
        executor_audience="executor-a",
        channel="rest",
        audit_correlation_id=f"audit-{operation_id}",
        expected_sequence=0,
        final_arguments={"query": "private patient value"},
        authorized_effect=AUTHORIZED,
        nonce=nonce,
    )


def _envelope(context) -> Permit:
    receipt = Receipt(
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        receipt_id=f"receipt-{context.operation_id}",
        request_id=context.operation_id,
        warden_id="warden-a",
        key_id="warden-a:key-1",
        policy_id="astral-policy",
        policy_version="1",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        lease_id="lease-a",
        lineage_id="lineage-a",
        subject_id="agent-a",
        executor_audience="executor-a",
        transition="tool_read",
        source_state="ready",
        target_state="ready",
        cost=(1, 0, 0, 0, 0, 0),
        resulting_sequence=1,
        evidence_digest=canonical_digest(dict(context.lets_evidence())),
        nonce=context.nonce,
        issued_at_ns=1,
        expires_at_ns=9_000,
        signature="synthetic-signature",
    )
    return Permit(
        binding_id="binding-a",
        owner_id="owner-a",
        runtime_generation=3,
        context=dict(context.lets_evidence()),
        expected_sequence=0,
        nonce=context.nonce,
        wire_arguments_sha256=context.wire_arguments_sha256,
        receipt=receipt,
    )


def _replay_status(*, healthy: bool = True) -> ReplayStatus:
    identity = ReplayIdentity(
        audience="executor-a",
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        executor_policy_sha256=b"p" * 32,
        trust_registry_sha256=b"t" * 32,
    )
    checkpoint = AuthorityCheckpoint(
        identity=identity,
        schema_version=1,
        database_instance_id=b"d" * 32,
        claim_sequence=1,
        claim_digest=b"c" * 32,
        clock_floor_ns=100,
    )
    return ReplayStatus(
        rollback_protected=healthy,
        authority_healthy=healthy,
        identity=identity,
        claim_sequence=1,
        clock_floor_ns=100,
        authority_checkpoint=checkpoint,
    )


def _coordinator():
    repository = MemoryRepository(_binding())
    coordinator = PlaneProtectedEffectCoordinator(
        plane=MemoryPlane(),
        repository=repository,  # type: ignore[arg-type]
    )
    return coordinator, repository


def test_intent_receipt_claim_sequence_and_execution_are_durable_and_redacted() -> None:
    coordinator, repository = _coordinator()
    context = _context()
    envelope = _envelope(context)

    pending = coordinator.prepare_authorization(
        binding=repository.binding,
        context=context,
    )
    assert pending.status is ProtectedEffectStatus.LETS_PENDING
    assert pending.version == 2
    assert pending.effect_digest == envelope.receipt.evidence_digest.removeprefix(
        "sha256:"
    )
    received = coordinator.record_receipt(
        binding=repository.binding,
        context=context,
        envelope=envelope,
    )
    assert received.status is ProtectedEffectStatus.RECEIPT_RECEIVED
    executing = coordinator.claim_for_execution(
        envelope=envelope,
        replay_status=_replay_status(),
    )

    assert executing.status is ProtectedEffectStatus.EXECUTING
    assert repository.binding.lease_sequence == 1
    assert len(repository.claims) == len(repository.watermarks) == len(repository.outbox) == 1
    claim = repository.claims[0]
    assert claim.authority_anchor.claim_sequence == 1
    assert claim.canonical_digest == canonical_digest(
        envelope.receipt.to_dict()
    ).removeprefix("sha256:")
    payload = repository.outbox[0].canonical_payload
    assert b"synthetic-signature" not in payload
    assert b"private patient value" not in payload


def test_same_intent_is_repeat_safe_but_same_id_different_nonce_conflicts() -> None:
    coordinator, repository = _coordinator()
    context = _context()
    first = coordinator.prepare_authorization(
        binding=repository.binding,
        context=context,
    )
    repeated = coordinator.prepare_authorization(
        binding=repository.binding,
        context=context,
    )
    assert repeated == first

    with pytest.raises(LetsEffectError, match="effect_request_conflict"):
        coordinator.prepare_authorization(
            binding=repository.binding,
            context=_context(nonce="b6" * 16),
        )


def test_unhealthy_external_authority_fails_before_plane_claim_or_effect() -> None:
    coordinator, repository = _coordinator()
    context = _context()
    envelope = _envelope(context)
    coordinator.prepare_authorization(binding=repository.binding, context=context)
    coordinator.record_receipt(
        binding=repository.binding,
        context=context,
        envelope=envelope,
    )

    with pytest.raises(LetsEffectError, match="executor_authority_unavailable"):
        coordinator.claim_for_execution(
            envelope=envelope,
            replay_status=_replay_status(healthy=False),
        )
    assert repository.effects[("owner-a", "effect-a")].status is ProtectedEffectStatus.RECEIPT_RECEIVED
    assert repository.binding.lease_sequence == 0
    assert repository.claims == []


def test_pre_execution_failure_is_terminal_and_repeat_safe() -> None:
    coordinator, repository = _coordinator()
    context = _context()
    coordinator.prepare_authorization(binding=repository.binding, context=context)
    failed = coordinator.fail_before_execution(
        owner_id="owner-a",
        operation_id="effect-a",
        error_code="warden_unavailable",
    )
    assert failed.status is ProtectedEffectStatus.FAILED_CLOSED
    assert failed.error_code == "warden_unavailable"
    assert coordinator.fail_before_execution(
        owner_id="owner-a",
        operation_id="effect-a",
        error_code="warden_unavailable",
    ) == failed


@pytest.mark.parametrize(
    ("outcome", "error_code", "expected"),
    [
        ("succeeded", None, ProtectedEffectStatus.SUCCEEDED),
        ("effect_failed", "tool_failed", ProtectedEffectStatus.EFFECT_FAILED),
        (
            "outcome_uncertain",
            "effect_result_lost",
            ProtectedEffectStatus.OUTCOME_UNCERTAIN,
        ),
    ],
)
def test_execution_outcomes_are_redacted_and_idempotent(
    outcome: str,
    error_code: str | None,
    expected: ProtectedEffectStatus,
) -> None:
    coordinator, repository = _coordinator()
    context = _context()
    envelope = _envelope(context)
    coordinator.prepare_authorization(binding=repository.binding, context=context)
    coordinator.record_receipt(
        binding=repository.binding,
        context=context,
        envelope=envelope,
    )
    coordinator.claim_for_execution(
        envelope=envelope,
        replay_status=_replay_status(),
    )

    recorded = coordinator.record_outcome(
        owner_id="owner-a",
        operation_id="effect-a",
        outcome=outcome,
        error_code=error_code,
    )
    assert recorded.status is expected
    assert recorded.error_code == error_code
    assert (
        recorded.effect_result_digest is None
    ) is (expected is ProtectedEffectStatus.OUTCOME_UNCERTAIN)
    assert coordinator.record_outcome(
        owner_id="owner-a",
        operation_id="effect-a",
        outcome=outcome,
        error_code=error_code,
    ) == recorded


def test_uncertain_outcome_can_be_reconciled_to_known_failure() -> None:
    coordinator, repository = _coordinator()
    context = _context()
    envelope = _envelope(context)
    coordinator.prepare_authorization(binding=repository.binding, context=context)
    coordinator.record_receipt(
        binding=repository.binding,
        context=context,
        envelope=envelope,
    )
    coordinator.claim_for_execution(envelope=envelope, replay_status=_replay_status())
    coordinator.record_outcome(
        owner_id="owner-a",
        operation_id="effect-a",
        outcome="outcome_uncertain",
        error_code="effect_result_lost",
    )

    resolved = coordinator.record_outcome(
        owner_id="owner-a",
        operation_id="effect-a",
        outcome="effect_failed",
        error_code="compensated",
    )
    assert resolved.status is ProtectedEffectStatus.EFFECT_FAILED
    assert resolved.effect_result_digest is not None
