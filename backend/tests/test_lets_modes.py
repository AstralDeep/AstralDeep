"""Feature 074 T172: shadow observes; enforce is the fail-closed boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.governed_dispatch import GovernedDispatchError
from orchestrator.lets_client import LetsClientBoundaryError
from tests.lets_conformance_support import build_rig, invoke_executor


@pytest.mark.asyncio
async def test_shadow_authorization_success_never_changes_existing_actuator(
    tmp_path,
) -> None:
    rig = build_rig(tmp_path, mode="shadow")
    seen: list[dict[str, object]] = []
    try:
        result = await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="websocket",
            audit_correlation_id="audit-shadow-success",
            final_arguments={"query": "private patient value", "limit": 5},
            invoke=lambda capabilities: seen.append(capabilities) or "existing-result",
        )
    finally:
        rig.close()

    assert result == "existing-result"
    assert seen == [{}]
    assert len(rig.warden.calls) == 1
    assert rig.coordinator.events == []
    assert rig.coordinator.claims == []


@pytest.mark.asyncio
async def test_shadow_infrastructure_failure_is_diagnostic_only(tmp_path) -> None:
    rig = build_rig(tmp_path, mode="shadow")
    rig.warden.failure = LetsClientBoundaryError("warden_unavailable", retryable=True)
    effects: list[str] = []
    try:
        result = await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="rest",
            audit_correlation_id="audit-shadow-failure",
            final_arguments={"query": "private patient value", "limit": 5},
            invoke=lambda capabilities: effects.append("effect") or capabilities or "ok",
        )
    finally:
        rig.close()

    assert result == "ok"
    assert effects == ["effect"]
    assert len(rig.warden.calls) == 1
    assert rig.coordinator.claims == []


@pytest.mark.asyncio
async def test_enforce_claims_exact_receipt_before_effect(tmp_path, monkeypatch) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path, mode="enforce")
    effects: list[str] = []
    try:
        result = await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="rest",
            audit_correlation_id="audit-enforce-success",
            final_arguments={"query": "private patient value", "limit": 5},
            invoke=lambda capabilities: invoke_executor(
                rig,
                capabilities,
                actuator=lambda: effects.append("effect") or "enforced-result",
            ),
        )
    finally:
        rig.close()

    assert result == "enforced-result"
    assert effects == ["effect"]
    assert rig.coordinator.events == ["intent", "receipt", "claim", "succeeded"]
    assert len(rig.coordinator.receipts) == 1
    assert len(rig.coordinator.claims) == 1
    assert recorder.record.await_count == 1


@pytest.mark.asyncio
async def test_enforce_infrastructure_failure_denies_before_effect(
    tmp_path, monkeypatch
) -> None:
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    rig = build_rig(tmp_path, mode="enforce")
    rig.warden.failure = LetsClientBoundaryError("request_timeout", retryable=True)
    effects: list[str] = []
    try:
        with pytest.raises(GovernedDispatchError, match="request_timeout"):
            await rig.dispatch.execute(
                owner_id="owner-a",
                agent_id="agent-a",
                tool_id="clinical.search_v2",
                scope="tools:read",
                channel="rest",
                audit_correlation_id="audit-enforce-failure",
                final_arguments={"query": "private patient value", "limit": 5},
                invoke=lambda _capabilities: effects.append("effect"),
            )
    finally:
        rig.close()

    assert effects == []
    assert rig.coordinator.events == ["intent"]
    assert rig.coordinator.claims == []
