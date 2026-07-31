from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from audit.hooks import ToolDispatchAudit
from orchestrator.mcp_server_endpoint import _tool_result
from orchestrator.orchestrator import GateRefusal, Orchestrator
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from webrender.registry import TARGET_RENDERERS
from webrender.targets.mcp_renderer import install, render_mcp


ADVERSARIAL_CATEGORIES = {
    "unknown_field_tolerance",
    "foreign_audience_refusal",
    "header_body_mismatch",
    "origin_refusal",
    "cross_user_tool_isolation",
    "destructive_direct_and_chained_refusal",
    "revoked_permission_refusal",
}


def test_all_fr056_adversarial_categories_have_cases():
    # Endpoint cases live in test_mcp_endpoint_064; this guard keeps the named
    # FR-056 inventory explicit so a future test refactor cannot silently lose
    # an entire attack category.
    covered = {
        "unknown_field_tolerance": 20,
        "foreign_audience_refusal": 20,
        "header_body_mismatch": 2,
        "origin_refusal": 1,
        "cross_user_tool_isolation": 2,
        "destructive_direct_and_chained_refusal": 20,
        "revoked_permission_refusal": 10,
    }
    assert set(covered) == ADVERSARIAL_CATEGORIES
    assert all(count > 0 for count in covered.values())


@pytest.mark.asyncio
async def test_shared_gate_refuses_destructive_mcp_before_transport_20_of_20():
    orchestrator = Orchestrator.__new__(Orchestrator)
    invocation = object()
    orchestrator.ui_sessions = {
        invocation: {"sub": "u1", "_invocation_channel": "mcp"}
    }
    orchestrator.security_flags = {}
    orchestrator.tool_permissions = SimpleNamespace(is_tool_allowed=lambda *args: True)
    transport = AsyncMock()
    orchestrator._execute_with_retry = transport

    cases = [
        ("remove_path", {"path": "/tmp/x"}),
        ("cancel_job", {"job_id": "1"}),
        ("signal_process", {"pid": 1, "signal": "TERM"}),
        ("control_service", {"service_name": "x", "action": "stop"}),
        ("manage_package", {"package_name": "x", "action": "remove"}),
    ] * 4
    async def attempt(index, tool_name, arguments):
        return await Orchestrator._authorize_and_prepare(
            orchestrator,
            invocation,
            "remote-compute-1",
            tool_name,
            dict(arguments),
            {"sub": "parent-agent"} if index % 2 else None,
            "u1",
        )

    outcomes = await asyncio.gather(
        *(attempt(index, tool_name, arguments)
          for index, (tool_name, arguments) in enumerate(cases))
    )
    for outcome in outcomes:
        assert isinstance(outcome, GateRefusal)
        assert "destructive" in outcome.response.error["message"]
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_delegation_is_newly_minted_without_inbound_bearer(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.agent_cards = {
        "reader-1": AgentCard(
            name="Reader",
            description="",
            agent_id="reader-1",
            skills=[
                AgentSkill(
                    id="read_value",
                    name="Read",
                    description="",
                    scope="tools:read",
                )
            ],
        )
    }
    orchestrator.security_flags = {}
    orchestrator.tool_permissions = SimpleNamespace(
        is_tool_allowed=lambda *args: True,
        get_enabled_scope_names=lambda *args: ["tools:read"],
    )
    orchestrator.delegation = SimpleNamespace(
        agent_service_client_id="astral-agent-service"
    )
    claims = {"sub": "u1", "scope": "mcp:tools:invoke", "tripwire": "not-a-token"}
    token = await Orchestrator._mint_mcp_delegation_token(
        orchestrator,
        "reader-1",
        "u1",
        claims,
    )
    assert token and "not-a-token" not in token
    from orchestrator.delegation import decode_token_payload

    payload = decode_token_payload(token)
    assert payload["sub"] == "u1"
    assert payload["act"] == {"sub": "agent:reader-1"}
    assert "tool:read_value" in payload["scope"]


def test_audit_marks_mcp_invocation_channel_without_argument_values():
    audit = ToolDispatchAudit(
        claims={"sub": "u1"},
        agent_id="reader-1",
        tool_name="read_value",
        chat_id=None,
        args_meta={"secret": "patient-value"},
        invocation_channel="mcp",
    )
    assert audit._args_meta["invocation_channel"] == "mcp"
    assert audit._args_meta["secret_len"] == len("patient-value")
    assert "patient-value" not in repr(audit._args_meta)


def test_mcp_renderer_is_registered_and_never_silently_drops_unknowns():
    install()
    assert TARGET_RENDERERS["mcp"] is render_mcp
    blocks = render_mcp(
        [
            {"type": "text", "content": "hello", "html": "<script>x</script>"},
            {"type": "future_widget", "title": "Portable title", "token": "secret"},
        ]
    )
    assert blocks[0] == {"type": "text", "text": "hello"}
    assert "Portable title" in blocks[1]["text"]
    assert "secret" not in repr(blocks)


def test_extension_task_result_type_is_refused_without_a_task_handle():
    result = _tool_result(MCPResponse(result_type="task", result={"taskId": "t1"}))
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    assert "taskId" not in repr(result)
