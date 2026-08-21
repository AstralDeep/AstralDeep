"""Feature 074 T175: LETS infrastructure faults deny every governed effect."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lets.errors import StorageError

from orchestrator.governed_dispatch import GovernedDispatchError
from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_gateway import LetsGatewayError
from tests.lets_conformance_support import (
    FINAL_ARGUMENTS,
    build_rig,
    context,
    host_arguments,
    signed_envelope,
)


async def _dispatch_effect(rig, effects: list[str]) -> object:
    return await rig.dispatch.execute(
        owner_id="owner-a",
        agent_id="agent-a",
        tool_id="clinical.search_v2",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-fault",
        final_arguments=FINAL_ARGUMENTS,
        invoke=lambda _capabilities: effects.append("effect"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["request_timeout", "warden_unavailable", "invalid_response"],
)
async def test_warden_boundary_faults_deny_before_effect(
    tmp_path, monkeypatch, code: str
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path, name=code)
    rig.warden.failure = LetsClientBoundaryError(code, retryable=True)
    effects: list[str] = []
    try:
        with pytest.raises(GovernedDispatchError, match=f"^{code}$"):
            await _dispatch_effect(rig, effects)
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []


@pytest.mark.asyncio
async def test_malformed_warden_object_is_outcome_uncertain_and_denied(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path)
    rig.warden.authorize_tool = lambda **_values: object()  # type: ignore[method-assign]
    effects: list[str] = []
    try:
        with pytest.raises(
            GovernedDispatchError, match="^authorization_outcome_uncertain$"
        ):
            await _dispatch_effect(rig, effects)
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.receipts == []
    assert rig.coordinator.claims == []


def test_stale_receipt_is_denied_before_effect(tmp_path) -> None:
    rig = build_rig(tmp_path)
    envelope = signed_envelope(
        rig.signer,
        receipt_changes={"expires_at_ns": rig.clock.current_ns},
    )
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match="^receipt_policy_invalid$"):
            rig.executor.claim_and_invoke(
                metadata=envelope.to_metadata(),
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.store.status().claim_sequence == 0


def test_rotated_out_trust_key_is_denied_before_effect(tmp_path) -> None:
    rig = build_rig(tmp_path, key_not_after_ns=101)
    envelope = signed_envelope(rig.signer)
    rig.clock.current_ns = 101
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


def test_clock_rollback_below_durable_floor_denies_next_effect(tmp_path) -> None:
    rig = build_rig(tmp_path)
    effects: list[str] = []
    first = signed_envelope(rig.signer)
    second_context = context(
        operation_id="effect-b",
        expected_sequence=1,
        nonce="b6" * 16,
    )
    second = signed_envelope(
        rig.signer,
        exact_context=second_context,
        receipt_changes={"issued_at_ns": 80},
    )
    try:
        rig.executor.claim_and_invoke(
            metadata=first.to_metadata(),
            actuator=lambda: effects.append("first"),
            **host_arguments(),
        )
        rig.clock.current_ns = 90
        with pytest.raises(LetsGatewayError, match="^clock_uncertain$"):
            rig.executor.claim_and_invoke(
                metadata=second.to_metadata(),
                actuator=lambda: effects.append("second"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == ["first"]
    assert rig.store.status().claim_sequence == 1
    assert len(rig.coordinator.claims) == 1


@pytest.mark.parametrize("fault", ["lost", "full"])
def test_replay_store_loss_or_fullness_denies_before_effect(
    tmp_path, monkeypatch, fault: str
) -> None:
    rig = build_rig(tmp_path, name=fault)

    def fail_claim(*_args, **_kwargs) -> None:
        raise StorageError(f"replay storage {fault}")

    monkeypatch.setattr(type(rig.store), "claim", fail_claim)
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match="^replay_store_unavailable$"):
            rig.executor.claim_and_invoke(
                metadata=signed_envelope(rig.signer).to_metadata(),
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []


def test_authority_anchor_failure_denies_before_effect(tmp_path, monkeypatch) -> None:
    rig = build_rig(tmp_path)

    def fail_anchor(*_args, **_kwargs) -> None:
        raise StorageError("authority anchor unavailable")

    monkeypatch.setattr(type(rig.anchor), "reconcile", fail_anchor)
    effects: list[str] = []
    try:
        with pytest.raises(LetsGatewayError, match="^replay_store_unavailable$"):
            rig.executor.claim_and_invoke(
                metadata=signed_envelope(rig.signer).to_metadata(),
                actuator=lambda: effects.append("effect"),
                **host_arguments(),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.claims == []
