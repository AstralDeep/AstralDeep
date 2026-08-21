"""Feature 074 T178: no governed effect exists without one matching claim."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.governed_dispatch import GovernedDispatchError
from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_gateway import LETS_CALLER_CAPABILITY, LetsGatewayError
from tests.lets_conformance_support import (
    AUTHORIZED_EFFECT,
    FINAL_ARGUMENTS,
    build_rig,
    host_arguments,
    invoke_executor,
    signed_envelope,
)


async def _execute(rig, invoke):
    return await rig.dispatch.execute(
        owner_id="owner-a",
        agent_id="agent-a",
        tool_id="clinical.search_v2",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-invariant",
        final_arguments=FINAL_ARGUMENTS,
        authorized_effect=AUTHORIZED_EFFECT,
        invoke=invoke,
    )


@pytest.mark.asyncio
async def test_one_receipt_one_claim_one_matching_physical_effect(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path)
    effects: list[str] = []
    captured: list[dict[str, object]] = []

    def invoke(capabilities: dict[str, object]) -> str:
        captured.append(capabilities)
        metadata = capabilities[LETS_CALLER_CAPABILITY]
        assert isinstance(metadata, dict)
        operation_id = metadata["context"]["operation_id"]  # type: ignore[index]

        def actuator() -> str:
            assert len(rig.coordinator.claims) == 1
            effects.append(operation_id)  # type: ignore[arg-type]
            return "effect-result"

        return invoke_executor(rig, capabilities, actuator=actuator)

    try:
        assert await _execute(rig, invoke) == "effect-result"

        receipt = rig.coordinator.receipts[0]
        claim = rig.coordinator.claims[0][0]
        warden_operation = rig.warden.calls[0]["operation_id"]
        assert effects == [warden_operation]
        assert receipt.receipt.request_id == warden_operation
        assert claim.receipt.request_id == warden_operation
        assert claim.receipt.receipt_id == receipt.receipt.receipt_id
        assert claim.receipt.evidence_digest == receipt.receipt.evidence_digest
        assert rig.store.status().claim_sequence == 1

        with pytest.raises(LetsGatewayError, match="^receipt_replayed$"):
            invoke_executor(
                rig,
                captured[0],
                actuator=lambda: effects.append("replayed-effect"),
            )
    finally:
        rig.close()

    assert effects == [warden_operation]
    assert len(rig.coordinator.receipts) == 1
    assert len(rig.coordinator.claims) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("missing_binding", "binding_unavailable"),
        ("missing_capability", "capability_not_bound"),
        ("warden_denied", "permission_denied"),
        ("lease_tamper", "receipt_binding_mismatch"),
        ("lineage_tamper", "receipt_binding_mismatch"),
    ],
)
async def test_authorization_denials_never_reach_a_physical_effect(
    tmp_path, monkeypatch, scenario: str, code: str
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path, name=scenario)
    if scenario == "missing_binding":
        rig.repository.binding = None
    elif scenario == "missing_capability":
        rig.binding.capabilities = ()
    elif scenario == "warden_denied":
        rig.warden.failure = LetsClientBoundaryError(code, retryable=False)
    elif scenario == "lease_tamper":
        rig.warden.receipt_changes = {"lease_id": "lease-b"}
    elif scenario == "lineage_tamper":
        rig.warden.receipt_changes = {"lineage_id": "lineage-b"}
    effects: list[str] = []
    try:
        with pytest.raises(GovernedDispatchError, match=f"^{code}$"):
            await _execute(
                rig,
                lambda capabilities: invoke_executor(
                    rig,
                    capabilities,
                    actuator=lambda: effects.append("effect"),
                ),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []
    assert rig.store.status().claim_sequence == 0


@pytest.mark.asyncio
async def test_plane_claim_failure_leaves_zero_physical_effects(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path)

    class PlaneClaimFailure(RuntimeError):
        code = "plane_claim_unavailable"
        retryable = True

    def refuse_claim(**_values) -> None:
        raise PlaneClaimFailure("plane claim unavailable")

    rig.coordinator.claim_for_execution = refuse_claim  # type: ignore[method-assign]
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match="^plane_claim_unavailable$"):
            await _execute(
                rig,
                lambda capabilities: invoke_executor(
                    rig,
                    capabilities,
                    actuator=lambda: effects.append("effect"),
                ),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []
    assert rig.store.status().claim_sequence == 1


@pytest.mark.parametrize(
    ("metadata", "code"),
    [(None, "missing_protected_permit"), ({}, "invalid_protected_permit")],
)
def test_missing_final_receipt_metadata_never_reaches_effect(
    tmp_path, metadata: object, code: str
) -> None:
    rig = build_rig(tmp_path)
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match=f"^{code}$"):
            rig.executor.claim_and_invoke(
                metadata=metadata,
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []
    assert rig.store.status().claim_sequence == 0


def test_host_lease_and_lineage_are_both_required_for_the_matching_claim(
    tmp_path,
) -> None:
    for field in ("lease_id", "lineage_id"):
        rig = build_rig(tmp_path, name=field)
        effects: list[str] = []
        try:
            with pytest.raises(
                LetsGatewayError, match="^executor_host_binding_mismatch$"
            ):
                rig.executor.claim_and_invoke(
                    metadata=signed_envelope(rig.signer).to_metadata(),
                    actuator=lambda: effects.append("effect"),
                    **host_arguments(**{field: f"wrong-{field}"}),
                )
        finally:
            rig.close()
        assert effects == []
        assert rig.coordinator.claims == []
        assert rig.store.status().claim_sequence == 0
