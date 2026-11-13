"""Tests for the node's tool call runner: tool resolution by plain or agent qualified name, effect kinds from tool scopes, machine authority refusals and the protected dispatch outcome mapping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import node_mesh_calls as nmcalls, tool_visibility
from shared.protocol import MCPResponse

OWNER = "oidc.abc.owner"


def skill(skill_id):
    return SimpleNamespace(id=skill_id)


class Orch:
    def __init__(self, response=None, error=None, authority="authority"):
        self.tool_permissions = SimpleNamespace(get_tool_scope=lambda agent, tool: {"write_note": "tools:write"}.get(tool, "tools:read"))
        self.authority = authority
        self.response = response
        self.error = error
        self.events = []

    async def derive_machine_authority(self, **kwargs):
        self.events.append(("derive", kwargs))
        return self.authority

    def _bind_machine_turn(self, vws, authority):
        self.events.append(("bind", authority))

    def _unbind_machine_turn(self, vws):
        self.events.append(("unbind",))

    async def execute_authorized_tool(self, **kwargs):
        self.events.append(("execute", kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def pairs(monkeypatch):
    found = [("agent-1", skill("lookup")), ("agent-1", skill("write_note")), ("agent-2", skill("lookup")), ("agent-2", skill("only_here"))]
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: found)
    return found


@pytest.mark.parametrize(
    "requested,agent,tool,effect",
    [
        ("agent-1__lookup", "agent-1", "lookup", "outward_read"),
        ("only_here", "agent-2", "only_here", "outward_read"),
        ("write_note", "agent-1", "write_note", "outside_effect"),
    ],
)
async def test_tools_resolve_by_plain_or_qualified_name(pairs, requested, agent, tool, effect):
    resolved = await nmcalls.OrchestratorCalls(Orch()).resolve(OWNER, requested)
    assert (resolved.agent_id, resolved.tool, resolved.effect_kind) == (agent, tool, effect)


@pytest.mark.parametrize(
    "requested,code",
    [("lookup", "node.tool_ambiguous"), ("missing", "node.tool_unknown"), ("agent-3__lookup", "node.tool_unknown"), ("agent-1__only_here", "node.tool_unknown")],
)
async def test_unknown_and_ambiguous_tools_are_refused(pairs, requested, code):
    with pytest.raises(nmcalls.CallRefused) as caught:
        await nmcalls.OrchestratorCalls(Orch()).resolve(OWNER, requested)
    assert caught.value.code == code


async def test_authority_is_derived_for_the_one_scope_of_the_tool():
    orch = Orch()
    resolved = nmcalls.ResolvedTool("agent-1", "write_note", "tools:write")
    assert await nmcalls.OrchestratorCalls(orch).authorize(OWNER, resolved) == "authority"
    assert orch.events[0][1] == {"user_id": OWNER, "agent_id": "agent-1", "turn_class": "mesh_request", "consented_scopes": ["tools:write"]}


async def test_missing_authority_is_refused_with_a_dotted_code():
    from orchestrator.chain_authority import AuthoritySkip

    orch = Orch(authority=AuthoritySkip("empty_scopes", "gone"))
    with pytest.raises(nmcalls.CallRefused) as caught:
        await nmcalls.OrchestratorCalls(orch).authorize(OWNER, nmcalls.ResolvedTool("a", "t", "tools:read"))
    assert caught.value.code == "node.authority_empty_scopes"


RESOLVED = nmcalls.ResolvedTool("agent-1", "lookup", "tools:read")


async def run(orch):
    return await nmcalls.OrchestratorCalls(orch).run(OWNER, "local-1", RESOLVED, "authority", {"q": 1})


async def test_a_call_goes_through_the_protected_dispatch_bound_to_machine_authority():
    orch = Orch(response=MCPResponse(result={"temp": 21}))
    outcome = await run(orch)
    assert (outcome.status, outcome.content, outcome.code) == ("ok", '{"temp": 21}', None)
    kinds = [event[0] for event in orch.events]
    assert kinds == ["bind", "execute", "unbind"]
    call = orch.events[1][1]
    assert (call["agent_id"], call["tool_name"], call["arguments"], call["channel"], call["chat_id"], call["claims"]) == (
        "agent-1", "lookup", {"q": 1}, "background", "local-1", None,
    )
    assert call["websocket"] is not None and call["user_id"] == OWNER


async def test_a_text_result_is_passed_through_and_clipped():
    outcome = await run(Orch(response=MCPResponse(result="x" * (nmcalls.MAX_CONTENT + 10))))
    assert len(outcome.content) == nmcalls.MAX_CONTENT
    assert (await run(Orch(response=MCPResponse(result=None)))).content == ""


async def test_a_gate_refusal_becomes_an_error_outcome_with_a_dotted_code():
    outcome = await run(Orch(response=MCPResponse(error={"code": "Permission Denied", "message": "not allowed"})))
    assert (outcome.status, outcome.content, outcome.code) == ("error", "not allowed", "node.permission_denied")
    bare = await run(Orch(response=MCPResponse(error={})))
    assert bare.code == "node.tool_failed" and bare.content


async def test_a_missing_response_or_a_dispatch_failure_is_an_error_outcome_and_unbinds():
    orch = Orch(response=None)
    assert (await run(orch)).code == "node.tool_failed"
    broken = Orch(error=RuntimeError("down"))
    outcome = await run(broken)
    assert outcome.status == "error" and outcome.code == "node.tool_failed" and "down" not in outcome.content
    assert broken.events[-1] == ("unbind",)


@pytest.mark.parametrize(
    "raw,expected",
    [("node.x", "node.x"), ("Tool Failed", "node.tool_failed"), ("9bad", "node.bad"), ("", "node.failed"), ("a" * 200 + ".b", "node." + "a" * 75)],
)
def test_dotted_codes_are_always_valid(raw, expected):
    assert nmcalls.dotted(raw) == expected
