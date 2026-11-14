"""Runs whole turns the node answers for another member with the real kernel wheel on both sides and a real orchestrator, protected dispatch and audit behind the node, checking that each tool call needs a consented mesh grant for its current definition, the owner's enablement and a kernel ticket minted for exactly that call.
Stubbed: the model and its client lookup, chat titling, and the offline-grant store that would exchange a Keycloak refresh token, as in test_node_mesh_peer_tools.py.
The dispatching runner drives node admission and physical dispatch directly, because the orchestrator's gate stack refuses sandboxed agents before admission."""

from __future__ import annotations

import dataclasses
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import node_mesh_admission, node_mesh_chat, vessel_executor as ve
from orchestrator.node_mesh_calls import OrchestratorCalls, definition_hash, grant_name, skill_definition
from shared.feature_flags import flags
from shared.protocol import AgentSkill, RegisterAgent
from tests.test_node_mesh_kernel_integration import ak, paired, pytestmark, until  # noqa: F401
from tests.test_node_mesh_peer_tools import AGENT, OWNER, TOOL, node_host  # noqa: F401

SIBLING = "general-1"
SIBLING_TOOL = "flip_coin"
OUTSIDER = "custom-helper-1"
OUTSIDER_TOOL = "spin_top"
SANDBOXED = "guide"
SANDBOXED_TOOL = "lookup"
SANDBOXED_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}
ANSWER = "the node answered"


class Model:
    def __init__(self, calls):
        self.calls = calls
        self.offered = []
        self.results = []
        self.rounds = 0

    async def __call__(self, websocket, messages, tools_desc=None, temperature=None, feature="tool_dispatch", **kwargs):
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        if tools_desc:
            self.offered.append(sorted(tool["function"]["name"] for tool in tools_desc))
        self.results.extend(m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        if tools_desc and self.rounds == 0 and self.calls:
            self.rounds += 1
            tool_calls = [
                SimpleNamespace(id=f"call-{index}", type="function",
                                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                for index, (name, arguments) in enumerate(self.calls)
            ]
            return SimpleNamespace(role="assistant", content=None, tool_calls=tool_calls, reasoning_content=None), usage
        return SimpleNamespace(role="assistant", content=ANSWER, tool_calls=None, reasoning_content=None), usage


def in_process(host, agent_id, tool, description):
    from agents.dice_roller.dice_roller_agent import DiceRollerAgent

    class Agent(DiceRollerAgent):
        pass

    Agent.agent_id = agent_id
    agent = Agent(port=0)
    agent.card.skills = [AgentSkill(name=tool, id=tool, description=description, input_schema={"type": "object"}, scope="tools:read")]
    return agent


@pytest.fixture
async def turn_host(node_host, monkeypatch):  # noqa: F811
    host = node_host
    orch = host.orch
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    dispatched = {AGENT: [], SIBLING: [], OUTSIDER: []}
    dice_handler = host.agent.handle_mcp_request

    async def dice(*args, **kwargs):
        dispatched[AGENT].append(args)
        return await dice_handler(*args, **kwargs)

    monkeypatch.setattr(host.agent, "handle_mcp_request", dice)
    for agent_id, tool in ((SIBLING, SIBLING_TOOL), (OUTSIDER, OUTSIDER_TOOL)):
        agent = in_process(host, agent_id, tool, f"{tool} for tests")
        await orch.register_agent(None, RegisterAgent(agent_card=agent.card, api_key=os.environ["AGENT_API_KEY"]))
        orch.local_agents[agent_id] = agent

        async def refuse(*args, _agent=agent_id, **kwargs):
            dispatched[_agent].append(args)
            raise AssertionError(f"{_agent} must not run")

        monkeypatch.setattr(agent, "handle_mcp_request", refuse)
        orch.tool_permissions.set_agent_scopes(OWNER, agent_id, {"tools:read": True})

    sent = []

    def executor(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"output": "found it", "error": False})

    definition = definition_hash(SANDBOXED_TOOL, "Looks things up", SANDBOXED_SCHEMA, {ve.AGENT_ANNOTATION: SANDBOXED})
    cards, pins = ve.build_cards({SANDBOXED: {"description": "Guide", "tools": [{
        "name": SANDBOXED_TOOL, "description": "Looks things up", "input_schema": SANDBOXED_SCHEMA,
        "scope": "tools:read", "definition": definition,
    }]}})
    assert ve.register(orch, cards, pins) == [SANDBOXED]
    orch.vessel_executor = ve.VesselExecutor(
        ve.ExecutorSettings("http://executor.test:8800", "operator", agent_tokens={SANDBOXED: "agent-token"}),
        httpx.AsyncClient(transport=httpx.MockTransport(executor)),
    )
    orch.tool_permissions.set_agent_scopes(OWNER, SANDBOXED, {"tools:read": True})
    orch._llm_store.set_sync(OWNER, provider="custom", base_url="http://test.invalid/v1", model="test-model", api_key="test-key")
    orch._resolve_llm_client_for = AsyncMock(return_value=(object(), None, SimpleNamespace(model="test-model", base_url=None)))
    orch.summarize_chat_title = AsyncMock()
    try:
        yield SimpleNamespace(**vars(host), dispatched=dispatched, sent=sent, sandboxed_definition=definition)
    finally:
        await orch.vessel_executor.close()
        orch.vessel_executor = None


def dice_grant(host):
    return grant_name(TOOL, skill_definition(host.skill))


def sandboxed_grant(host):
    return grant_name(SANDBOXED_TOOL, host.sandboxed_definition)


async def grant(node, laptop, tool, scope="outward_read"):
    laptop.grant_tool(tool, scope)
    await until(lambda: any(g.tool == tool and g.consented for g in node.kernel.grants()), f"the node to see the grant {tool}")


class Minted:
    def __init__(self, node):
        self.calls = []
        mint = self.original = node.mint_ticket

        async def watched(request_id, **call):
            result = await mint(request_id, **call)
            self.calls.append(SimpleNamespace(request=request_id, call=call, action=json.loads(result.action_json)))
            return result

        node.mint_ticket = watched


async def run_turn(host, node, laptop, node_device, bridge, calls, *, before=None, text="use your tools"):
    model = Model(calls)
    host.orch._call_llm = model
    minted = Minted(node)
    chat = laptop.create_chat("node turn")
    laptop.send_message(chat, "hello node", None)
    await until(lambda: any(c.chat_id == chat for c in node.kernel.list_chats()), "the chat to reach the node")
    if before is not None:
        await before()
    request = laptop.send_request(chat, text, node_device, None, None)
    await until(
        lambda: laptop.request(request).state in (ak.RequestState.ANSWERED, ak.RequestState.REFUSED),
        "the node to finish the request", seconds=90,
    )
    node.mint_ticket = minted.original
    local = bridge.state.local_for(chat)
    events = host.orch.audit_repo.list_for_user(OWNER, limit=200, conversation_id=local)[0] if local else []
    return SimpleNamespace(
        model=model, chat=chat, request=laptop.request(request), local=local, events=events,
        minted=minted.calls, request_id=request,
    )


def diag(outcome, host):
    return {
        "results": [str(r).split('"message": ', 1)[-1][:160] for r in outcome.model.results],
        "denied_first": [(e.action_type, e.inputs_meta.get("gate"), e.outcome_detail) for e in outcome.events if e.action_type.endswith(".denied")],
        "starts": [e.action_type for e in outcome.events if e.action_type.endswith((".start", ".end"))],
        "code": outcome.request.code, "dispatched": {k: len(v) for k, v in host.dispatched.items()},
        "sent": len(host.sent), "minted": [m.call["tool"] for m in outcome.minted],
        "denied": [(e.action_type, e.outcome_detail) for e in outcome.events if e.action_type.endswith(".denied")],
    }


def denials(outcome, tool):
    return [
        (event.inputs_meta.get("gate"), event.outcome_detail)
        for event in outcome.events if event.action_type == f"tool.{tool}.denied"
    ]


def bridge_for(host, tmp_path, monkeypatch, runner=None):
    calls = OrchestratorCalls(host.orch)
    return paired(
        tmp_path, monkeypatch, calls=calls, history=host.orch.history,
        runner=runner or node_mesh_chat.orchestrator_runner(host.orch),
    )


def dispatching_runner(host, calls, outcomes):
    async def run(owner, local, prompt):
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

        bound = node_mesh_admission.current_turn()
        authority = await host.orch.derive_machine_authority(
            user_id=owner, agent_id=None, turn_class="mesh_request", consented_scopes=bound.scopes,
        )
        authority = dataclasses.replace(authority, allowed_tools=bound.allowed)
        vws = VirtualWebSocket(BackgroundTask(task_id="mesh-direct", chat_id=local, user_id=owner))
        setattr(vws, node_mesh_admission.TURN_MARK, True)
        host.orch._bind_machine_turn(vws, authority)
        try:
            for agent_id, tool, arguments in calls:
                response = await node_mesh_admission.check(host.orch, vws, owner, local, agent_id, tool, dict(arguments))
                if response is None:
                    response = await host.orch._dispatch_tool_call(
                        agent_id, tool, dict(arguments), 30.0, vws, protected_owner_id=owner,
                        protected_channel="background", protected_conversation_id=local,
                    )
                outcomes.append((tool, response))
                node_mesh_chat.observe_step(owner, local, {
                    "name": tool, "args": json.dumps(arguments), "status": "errored" if response.error else "completed",
                    "result": None if response.error else response.result, "error": response.error and response.error.get("code"),
                })
        finally:
            host.orch._unbind_machine_turn(vws)
            await vws.close()
        node_mesh_chat.observe_message(owner, local, "assistant", ANSWER)

    return run


async def test_a_granted_in_process_tool_dispatches_exactly_once_with_its_own_ticket(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        outcome = await run_turn(host, node, laptop, node_device, bridge, [(TOOL, {"n": 2})])
    assert len(host.dispatched[AGENT]) == 1, diag(outcome, host)
    assert len(outcome.minted) == 1
    minted = outcome.minted[0]
    assert minted.request == outcome.request_id and minted.call["tool_def"] == skill_definition(host.skill)
    assert (minted.action["tool"], minted.action["tool_def"], minted.action["agent"]) == (TOOL, skill_definition(host.skill), "kernel")
    assert json.loads(minted.call["arguments_json"]) == {"n": 2} and minted.call["effect_kind"] == "outward_read"
    kinds = {event.action_type for event in outcome.events}
    assert {f"tool.{TOOL}.start", f"tool.{TOOL}.end"} <= kinds and denials(outcome, TOOL) == []
    assert outcome.model.results and "total" in outcome.model.results[0].lower()


async def test_a_granted_sandboxed_tool_reaches_the_executor_once_with_the_ticket_the_kernel_minted(turn_host, tmp_path, monkeypatch):
    host = turn_host
    outcomes = []
    runner = dispatching_runner(host, [(SANDBOXED, SANDBOXED_TOOL, {"q": "weather"})], outcomes)
    async with bridge_for(host, tmp_path, monkeypatch, runner) as (node, laptop, node_device, bridge):
        await grant(node, laptop, sandboxed_grant(host))
        outcome = await run_turn(host, node, laptop, node_device, bridge, [])
    assert outcome.request.state == ak.RequestState.ANSWERED, diag(outcome, host)
    assert len(host.sent) == 1 and [tool for tool, _ in outcomes] == [SANDBOXED_TOOL]
    assert outcomes[0][1].error is None and outcomes[0][1].result == "found it"
    assert [m.call["tool"] for m in outcome.minted] == [SANDBOXED_TOOL]
    sent = host.sent[0]
    assert (sent["agent"], sent["tool"], json.loads(sent["arguments_json"])) == (SANDBOXED, SANDBOXED_TOOL, {"q": "weather"})
    assert json.loads(sent["action_json"]) == outcome.minted[0].action
    assert outcome.minted[0].action["tool_def"] == host.sandboxed_definition and json.loads(sent["ticket_json"])["capability"]
    assert any(turn.author == node_device for turn in laptop.chat(outcome.chat).view.turns)


async def test_granted_sandboxed_and_in_process_tools_each_dispatch_once_and_an_ungranted_sibling_never(turn_host, tmp_path, monkeypatch):
    host = turn_host
    plans = [
        [(SANDBOXED, SANDBOXED_TOOL, {"q": "x"}), (SIBLING, SIBLING_TOOL, {})],
        [(AGENT, TOOL, {"n": 1})],
    ]
    outcomes = []
    turns = []

    async def runner(owner, local, prompt):
        await dispatching_runner(host, plans[len(turns)], outcomes)(owner, local, prompt)

    async with bridge_for(host, tmp_path, monkeypatch, runner) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        await grant(node, laptop, sandboxed_grant(host))
        for _ in plans:
            turns.append(await run_turn(host, node, laptop, node_device, bridge, []))
    assert all(turn.request.state == ak.RequestState.ANSWERED for turn in turns), [diag(turn, host) for turn in turns]
    assert len(host.dispatched[AGENT]) == 1 and len(host.sent) == 1 and host.dispatched[SIBLING] == []
    assert [(tool, response.error and response.error.get("code")) for tool, response in outcomes] == [
        (SANDBOXED_TOOL, None), (SIBLING_TOOL, "gate.permission_denied"), (TOOL, None),
    ]
    assert denials(turns[0], SIBLING_TOOL) == [("node_mesh", "gate.permission_denied")]
    minted = [m for turn in turns for m in turn.minted]
    assert [m.call["tool"] for m in minted] == [SANDBOXED_TOOL, TOOL]
    assert minted[0].request != minted[1].request
    assert json.loads(host.sent[0]["action_json"]) == minted[0].action


async def test_a_second_effect_after_a_tool_result_in_one_turn_needs_consent_and_never_dispatches(turn_host, tmp_path, monkeypatch):
    host = turn_host
    outcomes = []
    runner = dispatching_runner(host, [(AGENT, TOOL, {"n": 1}), (SANDBOXED, SANDBOXED_TOOL, {"q": "x"})], outcomes)
    async with bridge_for(host, tmp_path, monkeypatch, runner) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        await grant(node, laptop, sandboxed_grant(host))
        outcome = await run_turn(host, node, laptop, node_device, bridge, [])
    assert [(tool, response.error and response.error.get("code")) for tool, response in outcomes] == [
        (TOOL, None), (SANDBOXED_TOOL, "consent.required"),
    ]
    assert len(host.dispatched[AGENT]) == 1 and host.sent == []
    assert denials(outcome, SANDBOXED_TOOL) == [("node_mesh", "consent.required")]


@pytest.mark.parametrize("case", ["none", "bare_name", "other_hash", "any_tool"])
async def test_without_a_grant_for_the_current_definition_nothing_dispatches(turn_host, tmp_path, monkeypatch, case):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        if case == "bare_name":
            await grant(node, laptop, TOOL, "*")
        if case == "other_hash":
            await grant(node, laptop, f"{TOOL}@00000000", "*")
        if case == "any_tool":
            await grant(node, laptop, "*", "*")
        outcome = await run_turn(host, node, laptop, node_device, bridge, [(TOOL, {"n": 2})])
    assert host.dispatched[AGENT] == [] and host.sent == [] and outcome.minted == [], diag(outcome, host)
    assert denials(outcome, TOOL) == [("node_mesh", "gate.permission_denied")]


async def test_a_tool_the_owner_has_not_enabled_never_dispatches_even_with_a_mesh_grant(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))

        async def disable():
            host.orch.tool_permissions.set_agent_scopes(OWNER, AGENT, {"tools:read": False})

        outcome = await run_turn(host, node, laptop, node_device, bridge, [(TOOL, {"n": 2})], before=disable)
    assert host.dispatched[AGENT] == [] and outcome.minted == [], diag(outcome, host)
    assert denials(outcome, TOOL) == [("node_mesh", "mint.unknown_tool")] or all(TOOL not in o for o in outcome.model.offered)


async def test_an_in_process_tool_outside_the_published_catalog_never_dispatches(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        await grant(node, laptop, f"{OUTSIDER_TOOL}@" + "0" * 8, "*")
        outcome = await run_turn(host, node, laptop, node_device, bridge, [(OUTSIDER_TOOL, {})])
    assert host.dispatched[OUTSIDER] == [] and outcome.minted == [], diag(outcome, host)
    assert OUTSIDER_TOOL in outcome.model.offered[0]
    assert denials(outcome, OUTSIDER_TOOL) == [("node_mesh", "mint.unknown_tool")]


async def test_a_definition_changed_after_the_grant_never_dispatches(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))

        async def change():
            host.skill.description += " (changed after the grant)"

        outcome = await run_turn(host, node, laptop, node_device, bridge, [(TOOL, {"n": 2})], before=change)
    assert host.dispatched[AGENT] == [] and outcome.minted == [], diag(outcome, host)
    assert denials(outcome, TOOL) == [("node_mesh", "gate.permission_denied")]


async def test_meta_tools_are_refused_in_a_node_turn(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        outcome = await run_turn(host, node, laptop, node_device, bridge, [
            ("memory_search", {"query": "anything"}), ("delegate_subtasks", {"subtasks": [{"task": "roll"}]}),
        ])
    assert host.dispatched[AGENT] == [] and outcome.minted == [], diag(outcome, host)
    details = {(e.inputs_meta.get("gate"), e.outcome_detail) for e in outcome.events if e.action_type.endswith(".denied")}
    offered = set(outcome.model.offered[0]) if outcome.model.offered else set()
    for name in ("memory_search", "delegate_subtasks"):
        assert name not in offered or ("machine_scope", "host_meta_tool_not_consented") in details


@pytest.mark.parametrize("change,code", [
    ("stop", "gate.mesh_stopped"),
    ("definition", "gate.tool_def_changed"),
    ("permission", "mint.unknown_tool"),
    ("unticketed", "node.ticket_required"),
    ("other_arguments", "node.ticket_required"),
])
async def test_authority_lost_after_gate_entry_refuses_the_effect(turn_host, tmp_path, monkeypatch, change, code):
    host = turn_host
    state = {}
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        record = host.orch.audit_recorder.record

        async def mutate(event):
            result = await record(event)
            if event.action_type != f"tool.{TOOL}.start" or state.get("done"):
                return result
            state["done"] = True
            try:
                await change_authority()
            except Exception as exc:
                state["error"] = repr(exc)
                raise
            return result

        async def change_authority():
            bound = node_mesh_admission.current_turn()
            state["held"] = len(bound.held) if bound else None
            if change == "stop":
                node.kernel.raise_stop()
            elif change == "definition":
                host.skill.description += " changed mid-turn"
            elif change == "permission":
                host.orch.tool_permissions.set_agent_scopes(OWNER, AGENT, {"tools:read": False})
            elif change == "unticketed":
                bound.held.clear()
            elif change == "other_arguments":
                held = bound.held[0]
                other = await node.mint_ticket(
                    bridge._sessions[bound.chat].request.id, call_id="node-call-other", tool=TOOL, arguments_json='{"n":6}',
                    tool_def=held.resolved.definition, effect_kind=held.resolved.effect_kind,
                    labels=("private", "tool_trusted"), untrusted_arguments=False,
                )
                bound.held[0] = node_mesh_admission._Held(
                    held.agent, held.tool,
                    ve.CallTicket(other.ticket_json, other.action_json, '{"n":6}'), held.resolved,
                )

        monkeypatch.setattr(host.orch.audit_recorder, "record", mutate)
        outcome = await run_turn(host, node, laptop, node_device, bridge, [(TOOL, {"n": 2})])
    assert state.get("done") and state["held"] == 1 and "error" not in state, (state, diag(outcome, host))
    assert host.dispatched[AGENT] == [] and len(outcome.minted) == (2 if change == "other_arguments" else 1), (state, diag(outcome, host))
    assert denials(outcome, TOOL) == [("node_mesh", code)]


async def test_an_unticketed_dispatch_in_a_node_turn_is_refused_although_the_ceiling_allows_it(turn_host, tmp_path, monkeypatch):
    host = turn_host
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        await grant(node, laptop, dice_grant(host))
        socket = type("TurnSocket", (), {node_mesh_admission.TURN_MARK: True})()
        tools = {(AGENT, TOOL, skill_definition(host.skill)): "tools:read"}

        async def catalog():
            return OrchestratorCalls(host.orch).catalog(OWNER)

        async def never(*args):
            raise AssertionError("no mint without gate entry")

        with node_mesh_admission.turn(node, lambda: OWNER, OWNER, "local-x", tools, catalog, never):
            response = await host.orch._dispatch_tool_call(
                AGENT, TOOL, {"n": 1}, 30.0, socket, protected_owner_id=OWNER,
                protected_channel="background", protected_conversation_id="local-x",
            )
    assert response.error is not None and host.dispatched[AGENT] == []
    assert response.error.get("code") == "node.ticket_required", response.error
