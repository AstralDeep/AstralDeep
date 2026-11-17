"""Tests for the node's tool call runner: tool resolution by plain or agent qualified name, effect kinds from tool scopes, machine authority refusals and the protected dispatch outcome mapping."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import node_mesh_calls as nmcalls, tool_visibility
from shared.protocol import MCPResponse

OWNER = "oidc.abc.owner"
DEFINITION = "sha256:" + "d" * 64


def skill(skill_id, **fields):
    return SimpleNamespace(id=skill_id, **fields)


class Orch:
    def __init__(self, response=None, error=None, authority="authority"):
        self.tool_permissions = SimpleNamespace(
            get_tool_scope=lambda agent, tool: {"write_note": "tools:write"}.get(tool, "tools:read"),
            is_tool_allowed=lambda owner, agent, tool: True,
        )
        self.drafts = set()
        self.authority = authority
        self.response = response
        self.error = error
        self.events = []

    def _is_draft_agent(self, agent_id):
        return agent_id in self.drafts

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


@pytest.mark.parametrize("failure", ["unbound", "wrong_owner", "principal", "policy", "owner_changed"])
async def test_institution_authority_requires_the_current_signed_admission(monkeypatch, failure):
    from orchestrator import node_mesh_admission, vessel_executor
    from orchestrator.node_mesh import NodeMeshError, RequestReference
    from shared import node_identity

    monkeypatch.setattr(node_identity, "is_personal", lambda: False)
    monkeypatch.setattr(node_identity, "is_institution", lambda: True)
    resolved = nmcalls.ResolvedTool("agent-1", "write_note", "tools:write", DEFINITION)
    orch = Orch()
    calls = nmcalls.OrchestratorCalls(orch)
    if failure == "unbound":
        with pytest.raises(nmcalls.CallRefused, match="node.mesh_admission_unavailable"):
            await calls.authorize(OWNER, resolved)
        assert not orch.events
        return

    owner = [OWNER]
    context = AsyncMock()
    if failure == "principal":
        context.side_effect = NodeMeshError("node.owner_mismatch")
    policy = AsyncMock()
    if failure == "policy":
        policy.side_effect = NodeMeshError("institution.attestation_stale")
    mesh = SimpleNamespace(kernel=object(), personal_authority_context=context)
    request = RequestReference("request", "chat", "peer", "node", "call", None,
                               ("call", "write_note", "{}", "{}", "{}"))
    ticket = vessel_executor.CallTicket("{}", "{}", "{}")
    if failure == "owner_changed":
        async def changed_authority(**kwargs):
            owner[0] = "other-owner"
            return "authority"
        orch.derive_machine_authority = changed_authority
    with node_mesh_admission.binding(mesh, lambda: owner[0], OWNER, "chat", resolved, ticket,
                                     request=request, check_current=policy):
        with pytest.raises(nmcalls.CallRefused) as caught:
            await calls.authorize("other-owner" if failure == "wrong_owner" else OWNER, resolved)
    assert caught.value.code == {
        "wrong_owner": "node.mesh_admission_unavailable",
        "principal": "node.owner_mismatch",
        "policy": "institution.attestation_stale",
        "owner_changed": "node.owner_mismatch",
    }[failure]


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


async def test_a_missing_response_or_dispatch_exception_is_unknown_and_unbinds():
    orch = Orch(response=None)
    assert (await run(orch)).code == "node.outcome_unknown"
    broken = Orch(error=RuntimeError("down"))
    outcome = await run(broken)
    assert outcome.status == "error" and outcome.code == "node.outcome_unknown" and "down" not in outcome.content
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


async def test_the_runner_publishes_only_owner_visible_bundled_and_vessel_tools(monkeypatch):
    first_party = skill(
        "roll_dice",
        description="Roll dice",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        metadata={"annotations": {"readOnlyHint": True, "ignored": "value"}},
    )
    external = skill("external_tool", description="External")
    orch = Orch()
    orch.local_agents = {"dice-roller-1": object()}
    orch.tool_permissions.get_tool_scope = lambda agent, tool: "tools:read"
    pairs_by_owner = {
        OWNER: [("dice-roller-1", first_party), ("remote-agent", external)],
        "other-owner": [],
    }
    monkeypatch.setattr(
        tool_visibility, "eligible_tool_pairs", lambda _orch, owner: pairs_by_owner[owner]
    )
    vessel = {"name": "lookup", "annotations": {"agent": "guide"}}
    monkeypatch.setattr(nmcalls.vessel_executor, "published_tools", lambda orch: [vessel])
    calls = nmcalls.OrchestratorCalls(orch)

    published = calls.published_tools(OWNER)

    assert [tool["name"] for tool in published] == ["roll_dice", "lookup"]
    dice = published[0]
    assert dice["annotations"] == {"readOnlyHint": True}
    assert dice["effect"] == "outward_read"
    assert (await calls.resolve(OWNER, "roll_dice")).definition == nmcalls.definition_hash(
        dice["name"], dice["description"], dice["input_schema"], dice["annotations"]
    )
    assert calls.published_tools("other-owner") == [vessel]


async def test_the_runner_omits_ambiguous_published_names(monkeypatch):
    left = skill("lookup", description="left")
    right = skill("lookup", description="right")
    orch = Orch()
    orch.local_agents = {"dice-roller-1": object(), "general-1": object()}
    orch.tool_permissions.get_tool_scope = lambda agent, tool: "tools:read"
    monkeypatch.setattr(
        tool_visibility,
        "eligible_tool_pairs",
        lambda _orch, owner: [("dice-roller-1", left), ("general-1", right)],
    )
    monkeypatch.setattr(nmcalls.vessel_executor, "published_tools", lambda _orch: [])
    calls = nmcalls.OrchestratorCalls(orch)

    assert calls.published_tools(OWNER) == []
    with pytest.raises(nmcalls.CallRefused, match="node.tool_ambiguous"):
        await calls.resolve(OWNER, "lookup")


async def test_a_real_orchestrator_publishes_its_owner_visible_bundled_dice_tool(
    orchestrator_factory, monkeypatch, tmp_path,
):
    import asyncio

    from agents.dice_roller.dice_roller_agent import DiceRollerAgent
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS
    from shared.feature_flags import flags
    from shared.protocol import RegisterAgent

    key = "node-catalog-test-key"
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "personal")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node-data"))
    monkeypatch.setenv("ASTRAL_NODE_OWNER", "oidc.0123456789abcdef.owner")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://auth.example.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-desktop")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "node-catalog-test-audit")
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
    monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setitem(flags._flags, "user_skills", False)
    monkeypatch.delenv("DEFAULT_AGENT_OWNER", raising=False)
    orch = await asyncio.to_thread(orchestrator_factory)
    agent = DiceRollerAgent(port=0)
    await orch.register_agent(None, RegisterAgent(agent_card=agent.card, api_key=key))
    orch.local_agents[agent.agent_id] = agent
    orch.tool_permissions.set_agent_scopes(OWNER, agent.agent_id, {"tools:read": True})
    orch.tool_permissions.set_agent_scopes("oidc.abc.other", agent.agent_id, {"tools:read": False})

    calls = nmcalls.OrchestratorCalls(orch)
    published = calls.published_tools(OWNER)

    assert agent.agent_id in FIRST_PARTY_PUBLIC_AGENT_IDS
    assert [tool["name"] for tool in published] == ["roll_dice"]
    resolved = await calls.resolve(OWNER, "roll_dice")
    tool = published[0]
    assert resolved.definition == nmcalls.definition_hash(
        tool["name"], tool["description"], tool["input_schema"], tool["annotations"]
    )
    assert calls.published_tools("oidc.abc.other") == []


def many_tools(monkeypatch, orch, count=90, agents=("dice-roller-1", "general-1", "weather-1")):
    pairs = [(agents[i % len(agents)], skill(f"tool_{i:03d}", description=f"t{i}")) for i in range(count)]
    orch.local_agents = {agent: object() for agent in agents}
    orch.tool_permissions.get_tool_scope = lambda agent, tool: "tools:read"
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda _orch, owner: list(pairs))
    monkeypatch.setattr(nmcalls.vessel_executor, "published_tools", lambda _orch: [])
    return pairs


def test_ninety_tools_publish_as_the_first_sixty_four_by_agent_then_name(monkeypatch):
    orch = Orch()
    pairs = many_tools(monkeypatch, orch)
    calls = nmcalls.OrchestratorCalls(orch)

    tools, omitted = calls.published_selection(OWNER)

    expected = sorted(((a, s.id) for a, s in pairs))[:64]
    by_name = {s.id: a for a, s in pairs}
    assert [(by_name[t["name"]], t["name"]) for t in tools] == expected
    assert (len(tools), omitted) == (64, 26)
    reversed_pairs = list(reversed(pairs))
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda _orch, owner: list(reversed_pairs))
    assert calls.published_tools(OWNER) == tools


def test_the_allowlist_publishes_exactly_the_named_tools_within_the_cap(monkeypatch):
    orch = Orch()
    pairs = many_tools(monkeypatch, orch)
    calls = nmcalls.OrchestratorCalls(orch)
    chosen = [(a, s.id) for a, s in pairs[:3]]
    monkeypatch.setenv(nmcalls.ALLOWLIST_ENV, " , ".join(f"{a}__{n}" for a, n in chosen) + ", ghost-1__nothing")
    tools, omitted = calls.published_selection(OWNER)
    assert sorted(t["name"] for t in tools) == sorted(n for _, n in chosen) and omitted == 0
    monkeypatch.setenv(nmcalls.ALLOWLIST_ENV, ",".join(f"{a}__{s.id}" for a, s in pairs))
    tools, omitted = calls.published_selection(OWNER)
    assert (len(tools), omitted) == (64, 26)
    monkeypatch.setenv(nmcalls.ALLOWLIST_ENV, "  ")
    assert len(calls.published_selection(OWNER)[0]) == 64


def test_draft_and_disallowed_tools_never_publish_even_when_allowlisted(monkeypatch):
    orch = Orch()
    pairs = many_tools(monkeypatch, orch, count=6, agents=("dice-roller-1", "general-1"))
    orch.drafts = {"general-1"}
    blocked = pairs[0][1].id
    orch.tool_permissions.is_tool_allowed = lambda owner, agent, tool: tool != blocked
    monkeypatch.setenv(nmcalls.ALLOWLIST_ENV, ",".join(f"{a}__{s.id}" for a, s in pairs))
    tools, _ = nmcalls.OrchestratorCalls(orch).published_selection(OWNER)
    assert [t["name"] for t in tools] == [s.id for a, s in pairs if a == "dice-roller-1" and s.id != blocked]


def test_a_permission_check_that_raises_excludes_the_tool(monkeypatch):
    orch = Orch()
    many_tools(monkeypatch, orch, count=2)

    def broken(owner, agent, tool):
        raise RuntimeError("down")

    orch.tool_permissions.is_tool_allowed = broken
    assert nmcalls.OrchestratorCalls(orch).published_tools(OWNER) == []


def grant(tool, scope="*", *, agent="kernel", consented=True):
    return SimpleNamespace(agent=agent, tool=tool, scope=scope, consented=consented)


def test_a_grant_covers_only_the_exact_name_and_definition_with_the_effect_or_any_scope():
    name = "roll_dice@" + "d" * 8
    assert nmcalls.grant_name("roll_dice", DEFINITION) == name
    assert nmcalls.grant_covers([grant(name)], "roll_dice", DEFINITION, "outward_read")
    assert nmcalls.grant_covers([grant(name, "outward_read", agent="*")], "roll_dice", DEFINITION, "outward_read")
    for refused in [
        grant("roll_dice"), grant("*"), grant("roll_dice@" + "e" * 8), grant(name, "outside_effect"),
        grant(name, consented=False), grant(name, agent="assistant"), SimpleNamespace(),
    ]:
        assert not nmcalls.grant_covers([refused], "roll_dice", DEFINITION, "outward_read")


def turn_orchestrator(monkeypatch, *, allowed=lambda owner, agent, tool: True):
    dice = skill("roll_dice", description="Roll dice", input_schema={"type": "object"}, metadata={})
    coin = skill("flip_coin", description="Flip", input_schema={}, metadata={})
    orch = Orch()
    orch.local_agents = {"dice-roller-1": object(), "general-1": object()}
    orch.agent_cards = {"dice-roller-1": SimpleNamespace(skills=[dice]), "general-1": SimpleNamespace(skills=[coin])}
    orch.tool_permissions.get_tool_scope = lambda agent, tool: "tools:read"
    orch.tool_permissions.is_tool_allowed = allowed
    monkeypatch.setattr(
        tool_visibility, "eligible_tool_pairs", lambda _orch, owner: [("dice-roller-1", dice), ("general-1", coin)]
    )
    monkeypatch.setattr(nmcalls.vessel_executor, "published_tools", lambda _orch: [])
    return orch, dice, coin


def test_the_catalog_keys_each_published_tool_by_agent_and_name_with_its_definition(monkeypatch):
    orch, dice, coin = turn_orchestrator(monkeypatch)
    catalog = nmcalls.OrchestratorCalls(orch).catalog(OWNER)
    assert catalog == {
        ("dice-roller-1", "roll_dice"): nmcalls.CatalogTool(nmcalls.skill_definition(dice), "outward_read"),
        ("general-1", "flip_coin"): nmcalls.CatalogTool(nmcalls.skill_definition(coin), "outward_read"),
    }
    assert nmcalls.current_definition(orch, "dice-roller-1", "roll_dice") == nmcalls.skill_definition(dice)
    assert nmcalls.current_definition(orch, "dice-roller-1", "absent") is None
    assert nmcalls.current_definition(orch, "absent", "roll_dice") is None


def test_turn_tools_need_a_grant_for_the_current_hash_and_the_owners_enablement(monkeypatch):
    disabled = {"flip_coin"}
    orch, dice, coin = turn_orchestrator(monkeypatch, allowed=lambda owner, agent, tool: tool not in disabled)
    calls = nmcalls.OrchestratorCalls(orch)
    dice_grant = grant(nmcalls.grant_name("roll_dice", nmcalls.skill_definition(dice)), "outward_read")
    coin_grant = grant(nmcalls.grant_name("flip_coin", nmcalls.skill_definition(coin)))
    assert calls.turn_tools(OWNER, []) == []
    assert calls.turn_tools(OWNER, [dice_grant, coin_grant]) == [
        nmcalls.TurnTool("dice-roller-1", "roll_dice", nmcalls.skill_definition(dice), "tools:read"),
    ]
    disabled.clear()
    assert [tool.tool for tool in calls.turn_tools(OWNER, [dice_grant, coin_grant])] == ["roll_dice", "flip_coin"]
    stale = calls.catalog
    monkeypatch.setattr(calls, "catalog", lambda owner: {
        key: nmcalls.CatalogTool("sha256:" + "0" * 64, entry.effect) for key, entry in stale(owner).items()
    })
    assert calls.turn_tools(OWNER, [dice_grant, coin_grant]) == []
