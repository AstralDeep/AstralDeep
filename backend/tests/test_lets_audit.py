"""Redacted actor-specific LETS audit correlation tests for feature 074."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.lets_audit import LetsAuditError, LetsAuditObserver


@pytest.mark.asyncio
async def test_observer_preserves_actor_principal_and_cross_system_correlation(
    monkeypatch,
) -> None:
    recorder = type("Recorder", (), {"record": AsyncMock()})()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    observer = LetsAuditObserver(
        actor_user_id="owner-a",
        auth_principal="agent:initiator",
        agent_id="agent-a",
        conversation_id="chat-a",
    )

    await observer(
        "receipt_received",
        {
            "operation_id": "operation-a",
            "audit_correlation_id": "audit-a",
            "agent_id": "agent-a",
            "runtime_id": "runtime-a",
            "binding_id": "binding-a",
            "receipt_sha256": "a" * 64,
            "resulting_sequence": 4,
            "enforced": True,
        },
    )

    event = recorder.record.await_args.args[0]
    assert event.actor_user_id == "owner-a"
    assert event.auth_principal == "agent:initiator"
    assert event.agent_id == "agent-a"
    assert event.conversation_id == "chat-a"
    assert event.correlation_id == "audit-a"
    assert event.action_type == "lets.receipt_received"
    assert event.inputs_meta["receipt_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_observer_rejects_content_or_credentials_before_recorder_access(
    monkeypatch,
) -> None:
    recorder_lookup = MagicMock()
    monkeypatch.setattr("audit.recorder.get_recorder", recorder_lookup)
    observer = LetsAuditObserver(
        actor_user_id="owner-a",
        auth_principal="owner-a",
        agent_id="agent-a",
    )

    with pytest.raises(LetsAuditError, match="unsafe_lets_audit_metadata"):
        await observer(
            "receipt_received",
            {
                "audit_correlation_id": "audit-a",
                "raw_arguments": "must-never-be-recorded",
            },
        )

    recorder_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_observer_never_blocks_when_recorder_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    observer = LetsAuditObserver(
        actor_user_id="owner-a",
        auth_principal="owner-a",
        agent_id="agent-a",
        strict=False,
    )

    await observer(
        "would_deny",
        {
            "audit_correlation_id": "audit-a",
            "code": "binding_unavailable",
            "enforced": False,
        },
    )


@pytest.mark.asyncio
async def test_enforce_observer_fails_closed_without_recorder(monkeypatch) -> None:
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    observer = LetsAuditObserver(
        actor_user_id="owner-a",
        auth_principal="owner-a",
        agent_id="agent-a",
        strict=True,
    )

    with pytest.raises(LetsAuditError, match="audit_recorder_unavailable"):
        await observer(
            "denied",
            {
                "audit_correlation_id": "audit-a",
                "code": "binding_unavailable",
                "enforced": True,
            },
        )
