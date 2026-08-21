"""Tests for the save-time `_credentials_check` invocation in api.set_agent_credentials (T017).

These tests stub `Orchestrator.execute_authorized_tool` and the credential manager
so the route handler can be invoked directly without a full FastAPI lifecycle.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.api import set_agent_credentials
from orchestrator.models import CredentialSetRequest
from shared.protocol import AgentCard, AgentSkill, MCPResponse


def _make_card(agent_id: str, skill_names: list, required_credentials: list) -> AgentCard:
    return AgentCard(
        name=agent_id,
        description="test agent",
        agent_id=agent_id,
        skills=[AgentSkill(id=n, name=n, description="", input_schema={}) for n in skill_names],
        metadata={"required_credentials": required_credentials},
    )


def _make_request_with_orch(orch) -> SimpleNamespace:
    """Build a minimal `Request` stand-in carrying the orchestrator."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orch)))


def _orch_stub(card: AgentCard, dispatch_response):
    """A fake Orchestrator with just the methods set_agent_credentials touches."""
    orch = MagicMock()
    orch.agent_cards = {card.agent_id: card}
    orch.credential_manager = MagicMock()
    orch.credential_manager.set_bulk_credentials = MagicMock()
    orch.credential_manager.list_credential_keys = MagicMock(return_value=["FOO", "BAR"])
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value={"FOO": "x", "BAR": "y"})
    orch.execute_authorized_tool = AsyncMock(return_value=dispatch_response)
    return orch


@pytest.mark.asyncio
async def test_credential_test_omitted_when_agent_has_no_check_skill() -> None:
    card = _make_card("nocheck-1", skill_names=["something_else"], required_credentials=[])
    orch = _orch_stub(card, dispatch_response=None)
    body = CredentialSetRequest(credentials={"FOO": "v"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "nocheck-1", body, user_id="alice")
    assert resp.credential_test is None
    orch.execute_authorized_tool.assert_not_called()


@pytest.mark.asyncio
async def test_credential_test_ok_when_check_returns_ok() -> None:
    card = _make_card("classify-1", skill_names=["_credentials_check", "get_ml_options"], required_credentials=[])
    dispatch = MCPResponse(request_id="req-1", result={"credential_test": "ok"})
    orch = _orch_stub(card, dispatch_response=dispatch)
    body = CredentialSetRequest(credentials={"CLASSIFY_URL": "https://x", "CLASSIFY_API_KEY": "k"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "classify-1", body, user_id="alice")
    assert resp.credential_test == "ok"
    orch.execute_authorized_tool.assert_awaited_once()
    call_args = orch.execute_authorized_tool.call_args
    assert call_args.kwargs["tool_name"] == "_credentials_check"
    assert call_args.kwargs["channel"] == "rest"
    assert call_args.kwargs["arguments"] == {}
    assert call_args.kwargs["timeout"] == 5.0


@pytest.mark.asyncio
async def test_credential_test_auth_failed_propagated() -> None:
    card = _make_card("classify-1", skill_names=["_credentials_check"], required_credentials=[])
    dispatch = MCPResponse(
        request_id="req-1",
        result={"credential_test": "auth_failed", "detail": "401"},
    )
    orch = _orch_stub(card, dispatch_response=dispatch)
    body = CredentialSetRequest(credentials={"CLASSIFY_URL": "x", "CLASSIFY_API_KEY": "bad"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "classify-1", body, user_id="alice")
    assert resp.credential_test == "auth_failed"
    assert resp.credential_test_detail == "401"


@pytest.mark.asyncio
async def test_credential_test_unreachable_when_dispatch_returns_none() -> None:
    card = _make_card("classify-1", skill_names=["_credentials_check"], required_credentials=[])
    orch = _orch_stub(card, dispatch_response=None)
    body = CredentialSetRequest(credentials={"X": "Y"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "classify-1", body, user_id="alice")
    assert resp.credential_test == "unreachable"


@pytest.mark.asyncio
async def test_credential_test_unreachable_when_dispatch_errors() -> None:
    card = _make_card("classify-1", skill_names=["_credentials_check"], required_credentials=[])
    dispatch = MCPResponse(request_id="req-1", error={"message": "agent disconnected"})
    orch = _orch_stub(card, dispatch_response=dispatch)
    body = CredentialSetRequest(credentials={"X": "Y"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "classify-1", body, user_id="alice")
    assert resp.credential_test == "unreachable"
    assert "agent disconnected" in (resp.credential_test_detail or "")


@pytest.mark.asyncio
async def test_credential_test_failure_does_not_block_save() -> None:
    """Even when the probe blows up, the credential save itself must succeed."""
    card = _make_card("classify-1", skill_names=["_credentials_check"], required_credentials=[])
    orch = _orch_stub(card, dispatch_response=None)
    orch.execute_authorized_tool = AsyncMock(side_effect=RuntimeError("disconnected"))
    body = CredentialSetRequest(credentials={"X": "Y"})
    resp = await set_agent_credentials(_make_request_with_orch(orch), "classify-1", body, user_id="alice")
    assert resp.agent_id == "classify-1"
    assert resp.credential_test == "unreachable"
    assert "Credential probe failed" in (resp.credential_test_detail or "")
    # The credential must have been persisted regardless.
    orch.credential_manager.set_bulk_credentials.assert_called_once_with("alice", "classify-1", {"X": "Y"})
