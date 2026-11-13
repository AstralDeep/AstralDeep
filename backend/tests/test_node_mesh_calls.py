"""Tests for the node's tool call runner: tool resolution by plain or agent qualified name, effect kinds from tool scopes, machine authority refusals and the protected dispatch outcome mapping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import node_mesh_calls as nmcalls, tool_visibility
from shared.protocol import MCPResponse

OWNER = "oidc.abc.owner"
DEFINITION = "sha256:" + "d" * 64


def skill(skill_id, **fields):
    return SimpleNamespace(id=skill_id, **fields)


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
    resolved = nmcalls.ResolvedTool("agent-1", "write_note", "tools:write", DEFINITION)
    assert await nmcalls.OrchestratorCalls(orch).authorize(OWNER, resolved) == "authority"
    assert orch.events[0][1] == {"user_id": OWNER, "agent_id": "agent-1", "turn_class": "mesh_request", "consented_scopes": ["tools:write"]}


async def test_missing_authority_is_refused_with_a_dotted_code():
    from orchestrator.chain_authority import AuthoritySkip

    orch = Orch(authority=AuthoritySkip("empty_scopes", "gone"))
    with pytest.raises(nmcalls.CallRefused) as caught:
        await nmcalls.OrchestratorCalls(orch).authorize(OWNER, nmcalls.ResolvedTool("a", "t", "tools:read", DEFINITION))
    assert caught.value.code == "node.authority_empty_scopes"


RESOLVED = nmcalls.ResolvedTool("agent-1", "lookup", "tools:read", DEFINITION)


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


def test_the_definition_hash_covers_every_field_and_ignores_key_order():
    base = nmcalls.definition_hash("t", "d", {"type": "object", "a": 1, "b": 2}, {"readOnlyHint": True})
    assert base == nmcalls.definition_hash("t", "d", {"b": 2, "a": 1, "type": "object"}, {"readOnlyHint": True})
    assert base.startswith("sha256:") and len(base) == 71
    for changed in (
        nmcalls.definition_hash("u", "d", {"type": "object", "a": 1, "b": 2}, {"readOnlyHint": True}),
        nmcalls.definition_hash("t", "e", {"type": "object", "a": 1, "b": 2}, {"readOnlyHint": True}),
        nmcalls.definition_hash("t", "d", {"type": "object", "a": 1, "b": 3}, {"readOnlyHint": True}),
        nmcalls.definition_hash("t", "d", {"type": "object", "a": 1, "b": 2}, {"readOnlyHint": False}),
    ):
        assert changed != base


def test_the_definition_hash_follows_the_kernel_canonical_form():
    import hashlib
    from pathlib import PurePosixPath

    expected = (
        '{"annotations":{},"description":"café \\"q\\"","input_schema":{"big":{"$num":"9223372036854775808"},'
        '"flag":true,"half":{"$num":"0.5"},"list":[1,null,"x"]},"name":"t"}'
    )
    schema = {"list": [1, None, "x"], "half": 0.5, "big": 2**63, "flag": True}
    assert nmcalls.definition_hash("t", 'café "q"', schema, {}) == "sha256:" + hashlib.sha256(expected.encode()).hexdigest()
    control = '{"annotations":{},"description":"\\u0001\\n","input_schema":{},"name":"t"}'
    assert nmcalls.definition_hash("t", "\x01\n", {}, {}) == "sha256:" + hashlib.sha256(control.encode()).hexdigest()
    assert nmcalls._portable(PurePosixPath("odd")) == "odd"


async def test_a_resolved_tool_carries_the_hash_of_the_nodes_own_definition(monkeypatch):
    described = skill(
        "lookup",
        description="Find a thing",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        metadata={"annotations": {"readOnlyHint": True, "junk": 1}},
    )
    bare = skill("bare", input_schema="nope", metadata=None)
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: [("agent-1", described), ("agent-1", bare)])
    calls = nmcalls.OrchestratorCalls(Orch())
    assert (await calls.resolve(OWNER, "lookup")).definition == nmcalls.definition_hash(
        "lookup", "Find a thing", {"type": "object", "properties": {"q": {"type": "string"}}}, {"readOnlyHint": True}
    )
    assert (await calls.resolve(OWNER, "bare")).definition == nmcalls.definition_hash("bare", "", {}, {})
