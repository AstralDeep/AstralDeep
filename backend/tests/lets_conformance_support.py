"""Reusable real-boundary fixtures for Feature 074 LETS conformance tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

from orchestrator.governed_dispatch import DispatchRuntime, GovernedFinalDispatch
from orchestrator.lets_config import LetsHostConfig
from orchestrator.lets_gateway import (
    LETS_CALLER_CAPABILITY,
    LetsAuthorizationGateway,
    ProtectedPermitEnvelope,
    ReceiptExecutorGateway,
)
from orchestrator.protected_dispatch import (
    ProtectedDispatchContext,
    build_protected_dispatch_context,
)

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
FINAL_ARGUMENTS = {"query": "private patient value", "limit": 5}
AUTHORIZED_EFFECT = {
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


def context(
    *,
    operation_id: str = "effect-a",
    expected_sequence: int = 0,
    nonce: str = "a5" * 16,
    channel: str = "rest",
    final_arguments: dict[str, object] | None = None,
    agent_id: str = "agent-a",
    runtime_id: str = "runtime-a",
    tool_id: str = "clinical.search_v2",
    scope: str = "tools:read",
    executor_audience: str = "executor-a",
    authorized_effect: dict[str, object] | None = None,
) -> ProtectedDispatchContext:
    return build_protected_dispatch_context(
        operation_id=operation_id,
        agent_id=agent_id,
        runtime_id=runtime_id,
        tool_id=tool_id,
        scope=scope,
        executor_audience=executor_audience,
        channel=channel,
        audit_correlation_id=f"audit-{operation_id}",
        expected_sequence=expected_sequence,
        final_arguments=FINAL_ARGUMENTS if final_arguments is None else final_arguments,
        authorized_effect=(
            AUTHORIZED_EFFECT if authorized_effect is None else authorized_effect
        ),
        nonce=nonce,
    )


def signed_envelope(
    signer: Ed25519Signer,
    *,
    exact_context: ProtectedDispatchContext | None = None,
    receipt_changes: dict[str, object] | None = None,
    resign: bool = True,
    owner_id: str = "owner-a",
    binding_id: str = "binding-a",
    runtime_generation: int = 3,
    tenant_id: str = "tenant-a",
    envelope_id: str = "envelope-a",
    lease_id: str = "lease-a",
    lineage_id: str = "lineage-a",
    subject_id: str | None = None,
) -> ProtectedPermitEnvelope:
    selected = context() if exact_context is None else exact_context
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "envelope_id": envelope_id,
        "config_epoch": 7,
        "receipt_id": f"receipt-{selected.operation_id}",
        "request_id": selected.operation_id,
        "warden_id": signer.warden_id,
        "key_id": signer.key_id,
        "policy_id": "astral-policy",
        "policy_version": "1",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "lease_id": lease_id,
        "lineage_id": lineage_id,
        "subject_id": selected.agent_id if subject_id is None else subject_id,
        "executor_audience": selected.executor_audience,
        "transition": "tool_read",
        "source_state": "ready",
        "target_state": "ready",
        "cost": (1, 0, 0, 0, 0, 0),
        "resulting_sequence": selected.expected_sequence + 1,
        "evidence_digest": canonical_digest(dict(selected.lets_evidence())),
        "nonce": selected.nonce,
        "issued_at_ns": 90,
        "expires_at_ns": 1_000,
    }
    values.update(receipt_changes or {})
    receipt = Receipt(**values)  # type: ignore[arg-type]
    if resign:
        receipt = replace(
            receipt,
            signature=b64url_encode(
                signer.sign(canonical_json(receipt.unsigned_payload()))
            ),
        )
    return ProtectedPermitEnvelope(
        binding_id=binding_id,
        owner_id=owner_id,
        runtime_generation=runtime_generation,
        context=dict(selected.lets_evidence()),
        expected_sequence=selected.expected_sequence,
        nonce=selected.nonce,
        wire_arguments_sha256=selected.wire_arguments_sha256,
        receipt=receipt,
    )


def host_arguments(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
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
    values.update(changes)
    return values


class RecordingCoordinator:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.receipts: list[ProtectedPermitEnvelope] = []
        self.claims: list[tuple[ProtectedPermitEnvelope, object]] = []
        self.outcomes: list[tuple[str, str, str | None]] = []
        self.error: Exception | None = None

    def _fail(self) -> None:
        if self.error is not None:
            raise self.error

    def prepare_authorization(self, **_values: object) -> object:
        self._fail()
        self.events.append("intent")
        return object()

    def record_receipt(self, *, envelope: ProtectedPermitEnvelope, **_values: object) -> None:
        self._fail()
        self.events.append("receipt")
        self.receipts.append(envelope)

    def claim_for_execution(
        self, *, envelope: ProtectedPermitEnvelope, replay_status: object
    ) -> None:
        self._fail()
        self.events.append("claim")
        self.claims.append((envelope, replay_status))

    def record_outcome(
        self,
        *,
        operation_id: str,
        outcome: str,
        error_code: str | None = None,
        **_values: object,
    ) -> None:
        self._fail()
        self.events.append(outcome)
        self.outcomes.append((operation_id, outcome, error_code))

    def fail_before_execution(self, **_values: object) -> None:
        self.events.append("denied")


class SigningWarden:
    def __init__(self, signer: Ed25519Signer) -> None:
        self.signer = signer
        self.calls: list[dict[str, object]] = []
        self.failure: Exception | None = None
        self.receipt_changes: dict[str, object] = {}

    def authorize_tool(self, **values: object) -> Receipt:
        self.calls.append(dict(values))
        if self.failure is not None:
            raise self.failure
        evidence = values["evidence"]
        assert isinstance(evidence, dict)
        receipt_values: dict[str, object] = {
            "tenant_id": "tenant-a",
            "envelope_id": "envelope-a",
            "config_epoch": 7,
            "receipt_id": f"receipt-{values['operation_id']}",
            "request_id": values["operation_id"],
            "warden_id": self.signer.warden_id,
            "key_id": self.signer.key_id,
            "policy_id": "astral-policy",
            "policy_version": "1",
            "policy_digest": POLICY_DIGEST,
            "machine_digest": MACHINE_DIGEST,
            "lease_id": values["lease_id"],
            "lineage_id": "lineage-a",
            "subject_id": values["agent_id"],
            "executor_audience": values["executor_audience"],
            "transition": "tool_read",
            "source_state": "ready",
            "target_state": "ready",
            "cost": (1, 0, 0, 0, 0, 0),
            "resulting_sequence": int(values["expected_sequence"]) + 1,
            "evidence_digest": canonical_digest(evidence),
            "nonce": values["nonce"],
            "issued_at_ns": 90,
            "expires_at_ns": 1_000,
        }
        receipt_values.update(self.receipt_changes)
        receipt = Receipt(**receipt_values)  # type: ignore[arg-type]
        return replace(
            receipt,
            signature=b64url_encode(
                self.signer.sign(canonical_json(receipt.unsigned_payload()))
            ),
        )


class Plane:
    @contextmanager
    def transaction(self, **_options: object):
        yield object()


class BindingRepository:
    def __init__(self, binding: object) -> None:
        self.binding = binding
        self.calls: list[dict[str, object]] = []

    def get_active_binding(self, _transaction: object, **values: object) -> object:
        self.calls.append(dict(values))
        return self.binding


@dataclass(slots=True)
class ConformanceRig:
    signer: Ed25519Signer
    registry: PublicKeyRegistry
    clock: ManualClock
    store: SQLiteReceiptReplayStore
    anchor: ProcessFileExecutorAuthorityAnchor
    coordinator: RecordingCoordinator
    warden: SigningWarden
    binding: object
    authorization: LetsAuthorizationGateway
    executor: ReceiptExecutorGateway
    dispatch: GovernedFinalDispatch
    repository: BindingRepository

    def close(self) -> None:
        self.anchor.close()


def build_rig(
    tmp_path: Path,
    *,
    mode: str = "enforce",
    name: str = "rig",
    key_not_before_ns: int | None = None,
    key_not_after_ns: int | None = None,
) -> ConformanceRig:
    root = tmp_path / name
    root.mkdir()
    signer = Ed25519Signer.generate("warden-a")
    clock = ManualClock(100)
    registry = PublicKeyRegistry(clock=clock)
    registry.register(
        signer.warden_id,
        signer.key_id,
        signer.public_key_bytes,
        not_before_ns=key_not_before_ns,
        not_after_ns=key_not_after_ns,
    )
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
    anchor_root = root / "authority"
    anchor_root.mkdir()
    anchor = ProcessFileExecutorAuthorityAnchor(anchor_root / "executor.anchor")
    store = SQLiteReceiptReplayStore.initialize(
        root / "executor.sqlite3",
        authority_anchor=anchor,
        identity=executor_replay_identity(policy, registry),
    )
    verifier = ReceiptVerifier(registry, store, policy, clock=clock)
    coordinator = RecordingCoordinator()
    executor = ReceiptExecutorGateway(
        verifier,
        replay_status=store.status,
        effect_coordinator=coordinator,
    )
    binding = SimpleNamespace(
        binding_id="binding-a",
        owner_id="owner-a",
        agent_id="agent-a",
        runtime_id="runtime-a",
        runtime_generation=3,
        population="server_dynamic",
        tenant_id="tenant-a",
        envelope_id="envelope-a",
        warden_id=signer.warden_id,
        lease_id="lease-a",
        lineage_id="lineage-a",
        subject_id="agent-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        config_epoch=7,
        capabilities=("astral.tools.read",),
        lease_sequence=0,
        lease_expires_at_ns=1_000,
        state="active",
    )
    warden = SigningWarden(signer)
    config = LetsHostConfig(
        master_enabled=True,
        mode=mode,  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=("server_dynamic", "byo_user"),
        governed_agent_allowlist=(),
    )
    authorization = LetsAuthorizationGateway(
        config,
        warden,  # type: ignore[arg-type]
        effect_coordinator=coordinator,
    )
    repository = BindingRepository(binding)

    async def resolve(_agent_id: str, _owner_id: str | None) -> DispatchRuntime:
        return DispatchRuntime(
            owner_id="owner-a",
            agent_id="agent-a",
            population="server_dynamic",
            runtime_id="runtime-a",
            runtime_generation=3,
            executor_audience="executor-a",
            executor_conformant=True,
            dispatch_posture="protected_executor",
        )

    dispatch = GovernedFinalDispatch.active(
        gateway=authorization,
        plane=Plane(),
        authority_repository=repository,
        runtime_resolver=resolve,
    )
    return ConformanceRig(
        signer=signer,
        registry=registry,
        clock=clock,
        store=store,
        anchor=anchor,
        coordinator=coordinator,
        warden=warden,
        binding=binding,
        authorization=authorization,
        executor=executor,
        dispatch=dispatch,
        repository=repository,
    )


def invoke_executor(
    rig: ConformanceRig,
    capabilities: dict[str, object],
    *,
    final_arguments: dict[str, object] | None = None,
    actuator: Any,
) -> Any:
    return rig.executor.claim_and_invoke(
        metadata=capabilities[LETS_CALLER_CAPABILITY],
        actuator=actuator,
        **host_arguments(
            final_arguments=(
                FINAL_ARGUMENTS if final_arguments is None else final_arguments
            )
        ),
    )


__all__ = (
    "AUTHORIZED_EFFECT",
    "ConformanceRig",
    "FINAL_ARGUMENTS",
    "MACHINE_DIGEST",
    "POLICY_DIGEST",
    "RecordingCoordinator",
    "build_rig",
    "context",
    "host_arguments",
    "invoke_executor",
    "signed_envelope",
)
