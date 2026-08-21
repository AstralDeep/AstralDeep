"""Final-boundary LETS verification, Plane claim, and actuator ordering."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.clock import ManualClock
from lets.crypto import Ed25519Signer, PublicKeyRegistry
from lets.executor import (
    ExecutorPolicy,
    ReceiptVerifier,
    SQLiteReceiptReplayStore,
    executor_replay_identity,
)
from lets.executor_authority import ProcessFileExecutorAuthorityAnchor
from lets.models import Receipt

from orchestrator.lets_gateway import (
    LETS_CALLER_CAPABILITY,
    LetsGatewayError,
    ProtectedPermitEnvelope,
    ReceiptExecutorGateway,
)
from orchestrator.protected_dispatch import build_protected_dispatch_context


POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
FINAL_ARGUMENTS = {"query": "private patient value", "limit": 5}
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


class CoordinatorFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class RecordingCoordinator:
    def __init__(self) -> None:
        self.claims: list[tuple[ProtectedPermitEnvelope, object]] = []
        self.outcomes: list[tuple[str, str, str | None]] = []
        self.claim_error: Exception | None = None

    def claim_for_execution(self, *, envelope, replay_status):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append((envelope, replay_status))

    def record_outcome(
        self,
        *,
        owner_id: str,
        operation_id: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self.outcomes.append((operation_id, outcome, error_code))


def _context(*, nonce: str = "a5" * 16):
    return build_protected_dispatch_context(
        operation_id="effect-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        tool_id="clinical.search_v2",
        scope="tools:read",
        executor_audience="executor-a",
        channel="rest",
        audit_correlation_id="audit-a",
        expected_sequence=0,
        final_arguments=FINAL_ARGUMENTS,
        authorized_effect=AUTHORIZED,
        nonce=nonce,
    )


def _signed_envelope(
    signer: Ed25519Signer,
    *,
    context=None,
    **receipt_changes: object,
) -> ProtectedPermitEnvelope:
    exact_context = _context() if context is None else context
    receipt = Receipt(
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        receipt_id="receipt-a",
        request_id="effect-a",
        warden_id=signer.warden_id,
        key_id=signer.key_id,
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
        evidence_digest=canonical_digest(dict(exact_context.lets_evidence())),
        nonce=exact_context.nonce,
        issued_at_ns=90,
        expires_at_ns=1_000,
        **receipt_changes,
    )
    receipt = replace(
        receipt,
        signature=b64url_encode(
            signer.sign(canonical_json(receipt.unsigned_payload()))
        ),
    )
    return ProtectedPermitEnvelope(
        binding_id="binding-a",
        owner_id="owner-a",
        runtime_generation=3,
        context=dict(exact_context.lets_evidence()),
        expected_sequence=0,
        nonce=exact_context.nonce,
        wire_arguments_sha256=exact_context.wire_arguments_sha256,
        receipt=receipt,
    )


def _gateway(tmp_path: Path):
    signer = Ed25519Signer.generate("warden-a")
    registry = PublicKeyRegistry()
    registry.register_signer(signer)
    policy = ExecutorPolicy(
        audience="executor-a",
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        config_epoch=7,
        allowed_policy_digests=frozenset({POLICY_DIGEST}),
        allowed_machine_digests=frozenset({MACHINE_DIGEST}),
        trusted_wardens=frozenset({signer.warden_id}),
        max_clock_uncertainty_ns=5,
    )
    anchor_dir = tmp_path / "authority"
    anchor_dir.mkdir()
    anchor = ProcessFileExecutorAuthorityAnchor(anchor_dir / "executor.anchor")
    store = SQLiteReceiptReplayStore.initialize(
        tmp_path / "executor.sqlite3",
        authority_anchor=anchor,
        identity=executor_replay_identity(policy, registry),
    )
    verifier = ReceiptVerifier(
        registry,
        store,
        policy,
        clock=ManualClock(100),
    )
    coordinator = RecordingCoordinator()
    gateway = ReceiptExecutorGateway(
        verifier,
        replay_status=store.status,
        effect_coordinator=coordinator,
    )
    return gateway, store, anchor, coordinator, signer


def _claim(gateway: ReceiptExecutorGateway, envelope: ProtectedPermitEnvelope):
    return gateway.verify_and_claim(
        metadata=envelope.to_metadata(),
        final_arguments=FINAL_ARGUMENTS,
        owner_id="owner-a",
        binding_id="binding-a",
        lease_id="lease-a",
        lineage_id="lineage-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        runtime_generation=3,
        tool_id="clinical.search_v2",
        executor_audience="executor-a",
    )


def test_local_replay_and_plane_claim_commit_before_actuator(tmp_path: Path) -> None:
    gateway, _store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)
    ordering: list[str] = []
    original_claim = coordinator.claim_for_execution

    def claim(**values: object) -> None:
        original_claim(**values)
        ordering.append("plane_claim")

    coordinator.claim_for_execution = claim  # type: ignore[method-assign]
    try:
        result = gateway.claim_and_invoke(
            metadata={LETS_CALLER_CAPABILITY: envelope.to_metadata()}[
                LETS_CALLER_CAPABILITY
            ],
            final_arguments=FINAL_ARGUMENTS,
            owner_id="owner-a",
            binding_id="binding-a",
            lease_id="lease-a",
            lineage_id="lineage-a",
            agent_id="agent-a",
            runtime_id="runtime-a",
            runtime_generation=3,
            tool_id="clinical.search_v2",
            executor_audience="executor-a",
            actuator=lambda: ordering.append("actuator") or "ok",
        )
    finally:
        anchor.close()

    assert result == "ok"
    assert ordering == ["plane_claim", "actuator"]
    assert coordinator.claims[0][1].claim_sequence == 1
    assert coordinator.outcomes == [("effect-a", "succeeded", None)]


def test_claimed_receipt_replay_never_reaches_plane_or_actuator(tmp_path: Path) -> None:
    gateway, _store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)
    try:
        _claim(gateway, envelope)
        with pytest.raises(LetsGatewayError, match="receipt_replayed"):
            gateway.claim_and_invoke(
                metadata=envelope.to_metadata(),
                final_arguments=FINAL_ARGUMENTS,
                owner_id="owner-a",
                binding_id="binding-a",
                lease_id="lease-a",
                lineage_id="lineage-a",
                agent_id="agent-a",
                runtime_id="runtime-a",
                runtime_generation=3,
                tool_id="clinical.search_v2",
                executor_audience="executor-a",
                actuator=lambda: pytest.fail("replayed actuator ran"),
            )
    finally:
        anchor.close()
    assert len(coordinator.claims) == 1


def test_plane_failure_after_local_claim_omits_effect_and_consumes_receipt(
    tmp_path: Path,
) -> None:
    gateway, _store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)
    coordinator.claim_error = CoordinatorFailure("plane_claim_unavailable")
    invoked = False

    def actuator() -> None:
        nonlocal invoked
        invoked = True

    try:
        with pytest.raises(LetsGatewayError, match="plane_claim_unavailable"):
            gateway.claim_and_invoke(
                metadata=envelope.to_metadata(),
                final_arguments=FINAL_ARGUMENTS,
                owner_id="owner-a",
                binding_id="binding-a",
                lease_id="lease-a",
                lineage_id="lineage-a",
                agent_id="agent-a",
                runtime_id="runtime-a",
                runtime_generation=3,
                tool_id="clinical.search_v2",
                executor_audience="executor-a",
                actuator=actuator,
            )
        coordinator.claim_error = None
        with pytest.raises(LetsGatewayError, match="receipt_replayed"):
            _claim(gateway, envelope)
    finally:
        anchor.close()
    assert invoked is False
    assert coordinator.claims == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("owner_id", "owner-b", "executor_host_binding_mismatch"),
        ("binding_id", "binding-b", "executor_host_binding_mismatch"),
        ("agent_id", "agent-b", "executor_host_binding_mismatch"),
        ("runtime_id", "runtime-b", "executor_host_binding_mismatch"),
        ("runtime_generation", 4, "executor_host_binding_mismatch"),
        ("tool_id", "clinical.other", "executor_host_binding_mismatch"),
        ("executor_audience", "executor-b", "executor_host_binding_mismatch"),
    ],
)
def test_exact_host_binding_mismatch_is_denied_before_claim(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    gateway, store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)
    values = {
        "metadata": envelope.to_metadata(),
        "final_arguments": FINAL_ARGUMENTS,
        "owner_id": "owner-a",
        "binding_id": "binding-a",
        "lease_id": "lease-a",
        "lineage_id": "lineage-a",
        "agent_id": "agent-a",
        "runtime_id": "runtime-a",
        "runtime_generation": 3,
        "tool_id": "clinical.search_v2",
        "executor_audience": "executor-a",
    }
    values[field] = value
    try:
        with pytest.raises(LetsGatewayError, match=code):
            gateway.verify_and_claim(**values)  # type: ignore[arg-type]
    finally:
        anchor.close()
    assert store.status().claim_sequence == 0
    assert coordinator.claims == []


def test_argument_mutation_and_signature_tamper_are_denied(tmp_path: Path) -> None:
    gateway, store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)
    try:
        with pytest.raises(LetsGatewayError, match="executor_arguments_mutated"):
            gateway.verify_and_claim(
                metadata=envelope.to_metadata(),
                final_arguments={**FINAL_ARGUMENTS, "limit": 6},
                owner_id="owner-a",
                binding_id="binding-a",
                lease_id="lease-a",
                lineage_id="lineage-a",
                agent_id="agent-a",
                runtime_id="runtime-a",
                runtime_generation=3,
                tool_id="clinical.search_v2",
                executor_audience="executor-a",
            )
        tampered = replace(envelope.receipt, signature=b64url_encode(b"x" * 64))
        with pytest.raises(LetsGatewayError, match="receipt_signature_invalid"):
            _claim(gateway, replace(envelope, receipt=tampered))
    finally:
        anchor.close()
    assert store.status().claim_sequence == 0
    assert coordinator.claims == []


def test_actuator_exception_is_uncertain_by_default(tmp_path: Path) -> None:
    gateway, _store, anchor, coordinator, signer = _gateway(tmp_path)
    envelope = _signed_envelope(signer)

    def actuator() -> None:
        raise TimeoutError("result transport lost")

    try:
        with pytest.raises(TimeoutError, match="result transport lost"):
            gateway.claim_and_invoke(
                metadata=envelope.to_metadata(),
                final_arguments=FINAL_ARGUMENTS,
                owner_id="owner-a",
                binding_id="binding-a",
                lease_id="lease-a",
                lineage_id="lineage-a",
                agent_id="agent-a",
                runtime_id="runtime-a",
                runtime_generation=3,
                tool_id="clinical.search_v2",
                executor_audience="executor-a",
                actuator=actuator,
            )
    finally:
        anchor.close()
    assert coordinator.outcomes == [
        ("effect-a", "outcome_uncertain", "effect_result_lost")
    ]
