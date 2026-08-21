"""Feature 074 T174: every receipt field is bound at the last effect seam."""

from __future__ import annotations

from dataclasses import replace

import pytest
from lets.canonical import b64url_encode

from orchestrator.lets_gateway import LetsGatewayError
from tests.lets_conformance_support import (
    AUTHORIZED_EFFECT,
    FINAL_ARGUMENTS,
    build_rig,
    context,
    host_arguments,
    signed_envelope,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "effect-b"),
        ("tenant_id", "tenant-b"),
        ("envelope_id", "envelope-b"),
        ("warden_id", "warden-b"),
        ("lease_id", "lease-b"),
        ("lineage_id", "lineage-b"),
        ("subject_id", "agent-b"),
        ("policy_digest", "sha256:" + "3" * 64),
        ("machine_digest", "sha256:" + "4" * 64),
        ("config_epoch", 8),
        ("executor_audience", "executor-b"),
        ("transition", "tool_write"),
        ("cost", (0, 1, 0, 0, 0, 0)),
        ("cost", (1,)),
        ("cost", (1, 0, 0, 0, 0, 0, 0)),
        ("nonce", "b6" * 16),
        ("evidence_digest", "sha256:" + "5" * 64),
        ("resulting_sequence", 2),
    ],
)
async def test_authorization_rejects_signed_receipt_binding_mismatch(
    tmp_path, field: str, value: object
) -> None:
    rig = build_rig(tmp_path, name=field)
    rig.warden.receipt_changes = {field: value}
    exact_context = context()
    try:
        with pytest.raises(LetsGatewayError, match="^receipt_binding_mismatch$"):
            await rig.authorization.authorize(
                binding=rig.binding,
                population="server_dynamic",
                executor_conformant=True,
                context=exact_context,
                final_arguments=FINAL_ARGUMENTS,
                authorized_effect=AUTHORIZED_EFFECT,
            )
    finally:
        rig.close()

    assert rig.coordinator.receipts == []
    assert rig.coordinator.claims == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("warden_id", "warden-b", "receipt_policy_invalid"),
        ("key_id", "key-b", "receipt_signature_invalid"),
        ("tenant_id", "tenant-b", "receipt_policy_invalid"),
        ("envelope_id", "envelope-b", "receipt_policy_invalid"),
        ("lease_id", "lease-b", "executor_host_binding_mismatch"),
        ("lineage_id", "lineage-b", "executor_host_binding_mismatch"),
        ("subject_id", "agent-b", "executor_host_binding_mismatch"),
        ("policy_digest", "sha256:" + "3" * 64, "receipt_policy_invalid"),
        ("machine_digest", "sha256:" + "4" * 64, "receipt_policy_invalid"),
        ("config_epoch", 8, "receipt_policy_invalid"),
        ("executor_audience", "executor-b", "executor_host_binding_mismatch"),
        ("request_id", "effect-b", "executor_host_binding_mismatch"),
        ("transition", "tool_write", "executor_host_binding_mismatch"),
        ("cost", (0, 1, 0, 0, 0, 0), "executor_cost_mismatch"),
        ("cost", (1,), "executor_cost_mismatch"),
        ("cost", (1, 0, 0, 0, 0, 0, 0), "executor_cost_mismatch"),
        ("nonce", "b6" * 16, "executor_host_binding_mismatch"),
        ("evidence_digest", "sha256:" + "5" * 64, "executor_evidence_mismatch"),
        ("resulting_sequence", 2, "executor_host_binding_mismatch"),
        ("issued_at_ns", 101, "receipt_policy_invalid"),
        ("expires_at_ns", 100, "receipt_policy_invalid"),
    ],
)
def test_final_executor_rejects_resigned_field_tamper_before_effect(
    tmp_path,
    field: str,
    value: object,
    code: str,
) -> None:
    rig = build_rig(tmp_path, name=field)
    envelope = signed_envelope(rig.signer, receipt_changes={field: value})
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match=f"^{code}$"):
            rig.executor.claim_and_invoke(
                metadata=envelope.to_metadata(),
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.store.status().claim_sequence == 0
    assert rig.coordinator.claims == []


def test_final_executor_rejects_signature_tamper_before_effect(tmp_path) -> None:
    rig = build_rig(tmp_path)
    envelope = signed_envelope(rig.signer)
    envelope = replace(
        envelope,
        receipt=replace(envelope.receipt, signature=b64url_encode(b"x" * 64)),
    )
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match="^receipt_signature_invalid$"):
            rig.executor.claim_and_invoke(
                metadata=envelope.to_metadata(),
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.store.status().claim_sequence == 0


def test_replay_is_denied_after_exactly_one_claimed_effect(tmp_path) -> None:
    rig = build_rig(tmp_path)
    envelope = signed_envelope(rig.signer)
    effects: list[str] = []
    try:
        assert (
            rig.executor.claim_and_invoke(
                metadata=envelope.to_metadata(),
                actuator=lambda: effects.append("effect") or "ok",
                **host_arguments(),
            )
            == "ok"
        )
        with pytest.raises(LetsGatewayError, match="^receipt_replayed$"):
            rig.executor.claim_and_invoke(
                metadata=envelope.to_metadata(),
                actuator=lambda: effects.append("replayed-effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == ["effect"]
    assert rig.store.status().claim_sequence == 1
    assert len(rig.coordinator.claims) == 1
