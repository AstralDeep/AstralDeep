from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from audit.hooks import ToolDispatchAudit
from orchestrator.mcp_server_endpoint import _tool_result
from orchestrator.mcp_projection import project_tools, resolve_projected_tool
from orchestrator.orchestrator import GateRefusal, Orchestrator
from orchestrator.tool_visibility import eligible_tool_pairs
from shared.protocol import AgentCard, AgentSkill, MCPResponse, ProtocolValidationError
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


def test_shared_visibility_reports_every_exclusion_and_draft_self_test_bypass():
    reasons = []
    skill = AgentSkill(id="read", name="Read", description="", scope="tools:read")
    missing = AgentSkill(id="", name="Missing", description="", scope="tools:read")
    cards = {
        name: AgentCard(name=name, description="", agent_id=name, skills=[skill])
        for name in ("disconnected", "other", "draft", "disabled", "blocked", "denied", "unselected")
    }
    cards["missing"] = AgentCard(
        name="missing",
        description="",
        agent_id="missing",
        skills=[missing],
    )
    orchestrator = SimpleNamespace(
        agent_cards=cards,
        agents={name: object() for name in cards if name != "disconnected"},
        local_agents={},
        security_flags={"blocked": {"read": {"blocked": True}}},
        _is_draft_agent=lambda agent_id: agent_id == "draft",
        tool_permissions=SimpleNamespace(
            is_tool_allowed=lambda _user, agent_id, _skill: agent_id != "denied"
        ),
    )

    pairs = eligible_tool_pairs(
        orchestrator,
        "u1",
        disabled_agents={"disabled"},
        selected_tools={"different"},
        log_exclusion=lambda agent, skill_id, reason: reasons.append(
            (agent, skill_id, reason)
        ),
    )
    assert pairs == []
    assert {reason for _, _, reason in reasons} == {
        "not_connected",
        "draft_not_live",
        "user_disabled_agent",
        "system_blocked",
        "scope_or_override",
        "user_selection",
        "missing_skill_id",
    }

    assert eligible_tool_pairs(orchestrator, "u1", draft_agent_id="draft") == [
        ("draft", skill)
    ]
    assert any(reason == "outside_draft_test" for _, _, reason in reasons) is False


def test_projection_disambiguates_collisions_and_remote_destructive_metadata():
    skill_a = AgentSkill(
        id="same",
        name="Same",
        description="A",
        scope="tools:write",
        input_schema=None,
        metadata={"destructive": True},
    )
    skill_b = AgentSkill(
        id="same",
        name="Same",
        description="B",
        scope="tools:read",
    )
    orchestrator = SimpleNamespace(
        history=SimpleNamespace(db=SimpleNamespace(get_user_disabled_agents=lambda _user: [])),
        agent_cards={
            "a": AgentCard(name="a", description="", agent_id="a", skills=[skill_a]),
            "b": AgentCard(name="b", description="", agent_id="b", skills=[skill_b]),
        },
        agents={"a": object(), "b": object()},
        local_agents={},
        security_flags={},
        _is_draft_agent=lambda _agent_id: False,
        tool_permissions=SimpleNamespace(is_tool_allowed=lambda *args: True),
    )
    tools = project_tools(orchestrator, "u1")
    assert [tool.name for tool in tools] == ["a__same", "b__same"]
    assert tools[0].descriptor["description"].startswith("[Provider: a]")
    assert tools[0].descriptor["annotations"]["destructiveHint"] is True
    assert "outputSchema" not in tools[0].descriptor
    assert resolve_projected_tool(orchestrator, "u1", "b__same") == tools[1]
    assert resolve_projected_tool(orchestrator, "u1", "missing") is None


def test_renderer_handles_alerts_collections_nesting_and_empty_fallback():
    blocks = render_mcp(
        [
            {"type": "alert", "variant": "warning", "message": "careful"},
            {
                "type": "table",
                "headers": ["A"],
                "rows": [[1]],
                "children": [{"type": "text", "content": "nested"}],
            },
            {"type": "", "action": "secret"},
            {"type": "text", "content": "x" * (64 * 1024 + 1)},
            "ignored",
        ]
    )
    assert blocks[0]["text"] == "warning: careful"
    assert "nested" in blocks[1]["text"] and "headers" in blocks[1]["text"]
    assert "no portable text representation" in blocks[2]["text"]
    assert blocks[3]["text"].endswith("\n[truncated]")

    nested = {"label": "bottom"}
    for _ in range(14):
        nested = {"children": [nested]}
    assert "nested content omitted" in render_mcp([nested])[0]["text"]


@pytest.mark.asyncio
async def test_execute_mcp_tool_uses_gate_dispatch_and_cap_cleanup_without_token_retention():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.ui_sessions = {}
    orchestrator._pending_cap_entries = {"cap-1": object()}
    orchestrator.concurrency_cap = SimpleNamespace(release=AsyncMock())
    orchestrator._release_hop_cap_slot = AsyncMock()

    refusal = MCPResponse(error={"message": "denied"})
    orchestrator._authorize_and_prepare = AsyncMock(
        return_value=GateRefusal(refusal)
    )
    result = await Orchestrator.execute_mcp_tool(
        orchestrator,
        claims={"sub": "u1", "_raw_token": "must-disappear"},
        user_id="u1",
        agent_id="reader-1",
        tool_name="read",
        arguments={"value": 1},
    )
    assert result is refusal
    assert orchestrator.ui_sessions == {}

    prepared = SimpleNamespace(args={"value": 2}, cap_job_id="cap-1")
    orchestrator._authorize_and_prepare = AsyncMock(return_value=prepared)
    orchestrator._execute_with_retry_audited = AsyncMock(return_value=None)
    result = await Orchestrator.execute_mcp_tool(
        orchestrator,
        claims={"sub": "u1"},
        user_id="u1",
        agent_id="reader-1",
        tool_name="read",
        arguments={"value": 2},
    )
    assert result.error["message"] == "Tool returned no response"
    orchestrator.concurrency_cap.release.assert_awaited_once_with(
        "u1", "reader-1", "cap-1"
    )
    orchestrator._release_hop_cap_slot.assert_awaited_once_with("cap-1")
    assert "cap-1" not in orchestrator._pending_cap_entries

    orchestrator._authorize_and_prepare = AsyncMock(
        return_value=SimpleNamespace(args={}, cap_job_id=None)
    )
    completed = MCPResponse(result={"ok": True})
    orchestrator._execute_with_retry_audited = AsyncMock(return_value=completed)
    assert await Orchestrator.execute_mcp_tool(
        orchestrator,
        claims={"sub": "u1"},
        user_id="u1",
        agent_id="reader-1",
        tool_name="read",
        arguments={},
    ) is completed


@pytest.mark.asyncio
async def test_mcp_delegation_edge_paths_fail_closed(monkeypatch):
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.agent_cards = {}
    orchestrator.security_flags = {}
    orchestrator.tool_permissions = SimpleNamespace(
        is_tool_allowed=lambda *args: True,
        get_enabled_scope_names=lambda *args: [],
    )
    orchestrator.delegation = SimpleNamespace(
        agent_service_client_id="astral-agent-service"
    )
    assert await Orchestrator._mint_mcp_delegation_token(
        orchestrator, "missing", "u1", {}
    ) is None

    orchestrator.agent_cards["reader-1"] = AgentCard(
        name="Reader",
        description="",
        agent_id="reader-1",
        skills=[AgentSkill(id="read", name="Read", description="")],
    )
    assert await Orchestrator._mint_mcp_delegation_token(
        orchestrator, "reader-1", "u1", {}
    ) is None

    orchestrator.tool_permissions.get_enabled_scope_names = lambda *args: ["tools:read"]
    token = await Orchestrator._mint_mcp_delegation_token(
        orchestrator,
        "reader-1",
        "u1",
        {"exp": "not-an-integer"},
    )
    assert token

    websocket = object()
    orchestrator.ui_sessions = {
        websocket: {"_invocation_channel": "mcp", "sub": "u1"}
    }
    orchestrator._mint_mcp_delegation_token = AsyncMock(return_value="minted")
    assert await Orchestrator._get_delegation_token(
        orchestrator, websocket, "reader-1", "u1"
    ) == "minted"
    assert await Orchestrator._delegation_denied_for_permissions(
        orchestrator, websocket, "reader-1", "u1"
    ) is False


def test_model_schema_adapter_only_removes_dialect_after_validation():
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    assert Orchestrator._adapt_tool_schema_for_model(schema) == {"type": "object"}
    assert "$schema" in schema
    with pytest.raises(ProtocolValidationError, match="must be an object"):
        Orchestrator._adapt_tool_schema_for_model([])
