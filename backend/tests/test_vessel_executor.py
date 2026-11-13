"""Tests for the adapter that sends sandboxed tool calls to the AstralVessel executor: configuration that fails closed, the exact request the executor receives, refusal and error mapping, unknown outcomes, catalog registration and the dispatch hook in the orchestrator."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from orchestrator import node_mesh_calls, tool_visibility, vessel_executor as ve
from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill

TOKEN = "executor-secret"
URL = "http://executor:8800"
OWNER = "oidc.abc.owner"
AGENT = "guide"
DEFINITION = "sha256:" + "a" * 64


def arguments_text(arguments):
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def ticket_for(arguments=None, **overrides):
    arguments = {"q": "x"} if arguments is None else arguments
    text = arguments_text(arguments)
    action = {
        "v": 1, "mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agent": "kernel", "tool": "lookup",
        "tool_def": DEFINITION, "args_digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "owner": OWNER, "workspace": "ws-1",
    }
    action.update(overrides)
    return ve.CallTicket('{"ticket":1}', json.dumps(action), text)


def executor_with(handler, **settings):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ve.VesselExecutor(ve.ExecutorSettings(URL, TOKEN, **settings), client)


class Recorder:
    def __init__(self, status=200, body=None, raises=None):
        self.requests = []
        self.status, self.body, self.raises = status, {"output": "done", "error": False} if body is None else body, raises

    def __call__(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status, json=self.body)


async def invoke(executor, ticket, arguments=None, owner=OWNER, **kwargs):
    with ve.carrying(ticket):
        return await executor.invoke(AGENT, "lookup", {"q": "x"} if arguments is None else arguments, owner=owner, **kwargs)


def code_of(response):
    return response.error["code"]


def test_the_executor_needs_both_a_url_and_a_token():
    assert ve.ExecutorSettings.from_env({}) is None
    settings = ve.ExecutorSettings.from_env({ve.URL_ENV: URL + "/", ve.TOKEN_ENV: TOKEN})
    assert (settings.url, settings.token, settings.timeout) == (URL, TOKEN, ve.DEFAULT_TIMEOUT)


@pytest.mark.parametrize(
    "env",
    [
        {ve.URL_ENV: URL},
        {ve.TOKEN_ENV: TOKEN},
        {ve.URL_ENV: "ftp://executor", ve.TOKEN_ENV: TOKEN},
        {ve.URL_ENV: "http://user:pw@executor", ve.TOKEN_ENV: TOKEN},
        {ve.URL_ENV: "http://", ve.TOKEN_ENV: TOKEN},
        {ve.URL_ENV: URL, ve.TOKEN_ENV: TOKEN, ve.TIMEOUT_ENV: "soon"},
        {ve.URL_ENV: URL, ve.TOKEN_ENV: TOKEN, ve.TIMEOUT_ENV: "0"},
    ],
)
def test_a_partial_or_unusable_configuration_leaves_the_executor_unconfigured(env):
    assert ve.ExecutorSettings.from_env(env) is None


def test_the_timeout_is_read_from_the_environment():
    settings = ve.ExecutorSettings.from_env({ve.URL_ENV: URL, ve.TOKEN_ENV: TOKEN, ve.TIMEOUT_ENV: "15.5"})
    assert settings.timeout == 15.5


async def test_a_call_forwards_the_tool_arguments_action_and_ticket_with_the_bearer_token():
    recorder = Recorder()
    ticket = ticket_for()
    response = await invoke(executor_with(recorder), ticket)
    assert (response.result, response.error) == ("done", None)
    request = recorder.requests[0]
    assert (request.method, str(request.url), request.headers["authorization"]) == ("POST", URL + "/run", f"Bearer {TOKEN}")
    assert json.loads(request.content) == {
        "agent": AGENT, "tool": "lookup", "arguments_json": ticket.arguments_json, "action_json": ticket.action_json,
        "ticket_json": ticket.ticket_json, "workspace": "ws-1", "owner": OWNER,
    }


async def test_files_from_the_sandbox_come_back_with_the_output():
    recorder = Recorder(body={"output": "ok", "error": False, "files": [{"name": "a.txt"}]})
    response = await invoke(executor_with(recorder), ticket_for())
    assert response.result == {"output": "ok", "files": [{"name": "a.txt"}]}


async def test_a_call_without_a_ticket_is_refused_and_never_sent():
    recorder = Recorder()
    response = await executor_with(recorder).invoke(AGENT, "lookup", {}, owner=OWNER)
    assert code_of(response) == "executor.ticket_required" and not recorder.requests


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner": "someone-else"},
        {"tool": "other"},
        {"mesh": ""},
        {"agent": ""},
        {"workspace": 7},
    ],
)
async def test_an_action_for_a_different_call_is_refused_before_it_is_sent(overrides):
    recorder = Recorder()
    response = await invoke(executor_with(recorder), ticket_for(**overrides))
    assert code_of(response) == "request.call_mismatch" and not recorder.requests


@pytest.mark.parametrize("missing", ["agent", "mesh", "owner", "workspace"])
async def test_an_action_missing_a_pinned_field_is_refused_before_it_is_sent(missing):
    recorder = Recorder()
    ticket = ticket_for()
    action = json.loads(ticket.action_json)
    del action[missing]
    response = await invoke(executor_with(recorder), ve.CallTicket(ticket.ticket_json, json.dumps(action), ticket.arguments_json))
    assert code_of(response) == "request.call_mismatch" and not recorder.requests


async def test_a_workspace_may_be_null_when_the_action_says_so():
    recorder = Recorder()
    await invoke(executor_with(recorder), ticket_for(workspace=None))
    assert json.loads(recorder.requests[0].content)["workspace"] is None


async def test_an_unreadable_action_is_refused():
    recorder = Recorder()
    response = await invoke(executor_with(recorder), ve.CallTicket("{}", "not json", "{}"))
    assert code_of(response) == "request.malformed" and not recorder.requests
    response = await invoke(executor_with(recorder), ve.CallTicket("{}", "[1]", "{}"))
    assert code_of(response) == "request.malformed" and not recorder.requests


async def test_arguments_that_differ_from_the_signed_ones_are_refused():
    recorder = Recorder()
    response = await invoke(executor_with(recorder), ticket_for(), arguments={"q": "y"})
    assert code_of(response) == "request.call_mismatch" and not recorder.requests
    broken = ve.CallTicket("{}", ticket_for().action_json, "not json")
    assert code_of(await invoke(executor_with(recorder), broken)) == "request.call_mismatch"


async def test_a_missing_owner_is_refused():
    recorder = Recorder()
    assert code_of(await invoke(executor_with(recorder), ticket_for(), owner=None)) == "request.call_mismatch"
    assert not recorder.requests


@pytest.mark.parametrize(
    "status,code",
    [(403, "ticket.replayed"), (403, "gate.tool_def_changed"), (404, "executor.tool_unknown"), (403, "ticket.wrong_mesh")],
)
async def test_executor_refusals_keep_their_dotted_code(status, code):
    recorder = Recorder(status=status, body={"error": True, "code": code, "output": f"refused: {code}"})
    response = await invoke(executor_with(recorder), ticket_for())
    assert (code_of(response), response.error["retryable"]) == (code, False)


async def test_a_refusal_without_a_dotted_code_gets_a_generic_one():
    for body in ({"error": True, "code": "Bad Code!"}, {"error": True}):
        response = await invoke(executor_with(Recorder(status=403, body=body)), ticket_for())
        assert code_of(response) == "executor.refused"


async def test_the_nodes_token_being_refused_is_reported():
    response = await invoke(executor_with(Recorder(status=401, body={"error": True, "output": "unauthorized"})), ticket_for())
    assert code_of(response) == "executor.unauthorized"


async def test_a_tool_error_in_the_sandbox_is_a_tool_error():
    response = await invoke(executor_with(Recorder(body={"output": "boom", "error": True})), ticket_for())
    assert (code_of(response), response.error["message"]) == ("executor.tool_error", "boom")


async def test_a_non_text_output_is_serialized():
    response = await invoke(executor_with(Recorder(body={"output": {"b": 1}, "error": False})), ticket_for())
    assert response.result == '{"b": 1}'


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_failure_after_dispatch_is_an_unknown_outcome(status):
    response = await invoke(executor_with(Recorder(status=status, body={"error": True})), ticket_for())
    assert (code_of(response), response.error["retryable"]) == (ve.OUTCOME_UNKNOWN, False)


async def test_an_unreadable_answer_is_not_taken_for_success():
    def handler(request):
        return httpx.Response(200, content=b"<html>")

    assert code_of(await invoke(executor_with(handler), ticket_for())) == "executor.bad_response"

    def broken(request):
        return httpx.Response(502, content=b"<html>")

    assert code_of(await invoke(executor_with(broken), ticket_for())) == ve.OUTCOME_UNKNOWN


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ReadError("cut"), httpx.RemoteProtocolError("hung up")])
async def test_a_timeout_or_cut_connection_after_dispatch_is_an_unknown_outcome(error):
    response = await invoke(executor_with(Recorder(raises=error)), ticket_for())
    assert (code_of(response), response.error["retryable"]) == (ve.OUTCOME_UNKNOWN, False)


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("no route")])
async def test_a_call_that_never_connected_is_reported_as_not_run(error):
    response = await invoke(executor_with(Recorder(raises=error)), ticket_for())
    assert code_of(response) == ve.UNAVAILABLE and "did not run" in response.error["message"]


async def test_the_dispatch_timeout_shortens_the_request_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"output": "x", "error": False})

    await invoke(executor_with(handler, timeout=60.0), ticket_for(), timeout=7.0)
    await invoke(executor_with(handler, timeout=60.0), ticket_for(), timeout=500.0)
    assert [t["read"] for t in seen] == [7.0, 60.0]


CATALOG = {
    "guide": {
        "description": "Guides",
        "scope": "tools:write",
        "tools": [
            {"name": "lookup", "description": "Looks up", "input_schema": {"type": "object"}, "scope": "tools:read", "definition": DEFINITION},
            {"name": "save", "description": "Saves", "input_schema": {}, "scope": "tools:write", "definition": "sha256:" + "b" * 64},
            {"name": "odd", "description": "", "input_schema": {}, "scope": "tools:everything", "definition": DEFINITION},
            {"name": "", "scope": "tools:read", "definition": DEFINITION},
            {"name": "nodef", "scope": "tools:read"},
            "garbage",
        ],
    },
    "empty": {"description": "", "tools": []},
    "bad": "text",
}


async def test_the_catalog_is_read_with_the_token():
    recorder = Recorder(body={"agents": CATALOG})
    assert await executor_with(recorder).catalog() == CATALOG
    assert (recorder.requests[0].method, str(recorder.requests[0].url), recorder.requests[0].headers["authorization"]) == (
        "GET", URL + "/tools", f"Bearer {TOKEN}",
    )


async def test_a_malformed_catalog_is_refused():
    with pytest.raises(ValueError):
        await executor_with(Recorder(body={"agents": []})).catalog()
    with pytest.raises(httpx.HTTPStatusError):
        await executor_with(Recorder(status=401, body={})).catalog()


def test_cards_hold_only_valid_tools_and_the_pins_live_beside_them():
    cards, pins = ve.build_cards(CATALOG)
    assert [card.agent_id for card in cards] == ["guide"]
    assert [(s.id, s.scope, s.metadata) for s in cards[0].skills] == [("lookup", "tools:read", {}), ("save", "tools:write", {})]
    assert cards[0].metadata == {}
    assert pins == {("guide", "lookup"): DEFINITION, ("guide", "save"): "sha256:" + "b" * 64}


def fake_orch(**fields):
    registered = []
    orch = SimpleNamespace(
        agents={}, local_agents={}, agent_cards={}, vessel_executor=None, vessel_definitions={},
        tool_permissions=SimpleNamespace(register_tool_scopes=lambda agent, scopes: registered.append((agent, scopes))),
        register_governed_dispatch_runtime=lambda runtime: registered.append(runtime),
    )
    orch.__dict__.update(fields)
    return orch, registered


def test_registration_adds_cards_scopes_and_a_protected_runtime():
    orch, registered = fake_orch()
    assert ve.register(orch, *ve.build_cards(CATALOG)) == ["guide"]
    assert "guide" in orch.agent_cards
    assert registered[0] == ("guide", {"lookup": "tools:read", "save": "tools:write"})
    runtime = registered[1]
    assert (runtime.agent_id, runtime.population, runtime.dispatch_posture, runtime.executor_conformant) == (
        "guide", ve.POPULATION, "protected_executor", True,
    )


def test_an_agent_id_already_taken_is_not_replaced():
    orch, registered = fake_orch(local_agents={"guide": object()})
    assert ve.register(orch, *ve.build_cards(CATALOG)) == [] and not registered and not orch.agent_cards


async def test_start_does_nothing_without_configuration(monkeypatch):
    monkeypatch.delenv(ve.URL_ENV, raising=False)
    monkeypatch.delenv(ve.TOKEN_ENV, raising=False)
    orch, _ = fake_orch()
    assert await ve.start_for(orch) is None and orch.vessel_executor is None
    await ve.stop_for(orch)


async def test_start_registers_the_catalog_and_stop_releases_the_client(monkeypatch):
    monkeypatch.setenv(ve.URL_ENV, URL)
    monkeypatch.setenv(ve.TOKEN_ENV, TOKEN)
    real = httpx.AsyncClient
    monkeypatch.setattr(ve.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(Recorder(body={"agents": CATALOG})), **kw))
    orch, _ = fake_orch()
    executor = await ve.start_for(orch)
    assert orch.vessel_executor is executor and "guide" in orch.agent_cards
    await ve.stop_for(orch)
    assert orch.vessel_executor is None and executor._client.is_closed


async def test_an_unreachable_executor_at_start_registers_nothing(monkeypatch):
    monkeypatch.setenv(ve.URL_ENV, URL)
    monkeypatch.setenv(ve.TOKEN_ENV, TOKEN)
    real = httpx.AsyncClient
    monkeypatch.setattr(ve.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(Recorder(raises=httpx.ConnectError("no"))), **kw))
    orch, _ = fake_orch()
    assert await ve.start_for(orch) is None and orch.vessel_executor is None and not orch.agent_cards


def test_sandboxed_tools_are_visible_without_a_connection():
    orch, _ = fake_orch(security_flags={})
    orch.tool_permissions.is_tool_allowed = lambda owner, agent, tool: tool != "save"
    orch._is_draft_agent = lambda agent: False
    ve.register(orch, *ve.build_cards(CATALOG))
    pairs = tool_visibility.eligible_tool_pairs(orch, OWNER)
    assert [(agent, skill.id) for agent, skill in pairs] == [("guide", "lookup")]


def test_an_unmarked_card_that_is_not_connected_stays_hidden():
    orch, _ = fake_orch(security_flags={})
    orch.tool_permissions.is_tool_allowed = lambda owner, agent, tool: True
    orch._is_draft_agent = lambda agent: False
    orch.agent_cards["plain"] = AgentCard(name="plain", description="", agent_id="plain", skills=[AgentSkill(name="t", description="", id="t")])
    assert tool_visibility.eligible_tool_pairs(orch, OWNER) == []


def dispatching_orchestrator(executor):
    orch = Orchestrator.__new__(Orchestrator)
    orch.emergency_stop = None
    orch.ui_sessions = {}
    orch.agents, orch.local_agents = {}, {}
    orch.agent_cards = {AGENT: ve.build_cards(CATALOG)[0][0]}
    orch.vessel_definitions = ve.build_cards(CATALOG)[1]
    orch.vessel_executor = executor
    seen = []

    async def governed(ws, agent_id, tool_name, args, *, invoke, **kwargs):
        seen.append((agent_id, tool_name, args, kwargs["user_id"]))
        return await invoke({})

    orch._execute_governed_attempt = governed
    return orch, seen


async def test_dispatch_sends_a_sandboxed_tool_to_the_executor_through_the_governed_path():
    recorder = Recorder()
    orch, seen = dispatching_orchestrator(executor_with(recorder))
    with ve.carrying(ticket_for()):
        response = await orch._dispatch_tool_call(AGENT, "lookup", {"q": "x"}, 30.0, None, protected_owner_id=OWNER, protected_channel="background")
    assert (response.result, response.error) == ("done", None)
    assert seen == [(AGENT, "lookup", {"q": "x"}, OWNER)] and len(recorder.requests) == 1


async def test_dispatch_without_a_configured_executor_does_not_run_the_tool_locally():
    orch, seen = dispatching_orchestrator(None)
    response = await orch._dispatch_tool_call(AGENT, "lookup", {"q": "x"}, 30.0, None, protected_owner_id=OWNER, protected_channel="background")
    assert (code_of(response), response.error["retryable"]) == (ve.UNAVAILABLE, False) and not seen


async def test_a_node_turn_without_a_ticket_is_refused_by_the_adapter():
    recorder = Recorder()
    orch, _ = dispatching_orchestrator(executor_with(recorder))
    response = await orch._dispatch_tool_call(AGENT, "lookup", {"q": "x"}, 30.0, None, protected_owner_id=OWNER, protected_channel="background")
    assert code_of(response) == "executor.ticket_required" and not recorder.requests


async def test_the_ticket_reaches_the_dispatch_through_the_mesh_call_runner():
    recorder = Recorder()
    orch, _ = dispatching_orchestrator(executor_with(recorder))

    async def execute(**kwargs):
        return await orch._dispatch_tool_call(
            kwargs["agent_id"], kwargs["tool_name"], kwargs["arguments"], 30.0, None, protected_owner_id=kwargs["user_id"], protected_channel="background",
        )

    orch.execute_authorized_tool = execute
    orch._bind_machine_turn = lambda vws, authority: None
    orch._unbind_machine_turn = lambda vws: None
    resolved = node_mesh_calls.ResolvedTool(AGENT, "lookup", "tools:read", DEFINITION)
    outcome = await node_mesh_calls.OrchestratorCalls(orch).run(OWNER, "local-1", resolved, "authority", {"q": "x"}, ticket_for())
    assert (outcome.status, outcome.content) == ("ok", "done")
    assert json.loads(recorder.requests[0].content)["ticket_json"] == '{"ticket":1}'
    assert ve.current_ticket() is None


async def test_the_executor_refusal_code_survives_to_the_mesh_outcome():
    recorder = Recorder(status=403, body={"error": True, "code": "ticket.replayed", "output": "refused: ticket.replayed"})
    orch, _ = dispatching_orchestrator(executor_with(recorder))

    async def execute(**kwargs):
        return await orch._dispatch_tool_call(
            kwargs["agent_id"], kwargs["tool_name"], kwargs["arguments"], 30.0, None, protected_owner_id=kwargs["user_id"], protected_channel="background",
        )

    orch.execute_authorized_tool = execute
    orch._bind_machine_turn = lambda vws, authority: None
    orch._unbind_machine_turn = lambda vws: None
    resolved = node_mesh_calls.ResolvedTool(AGENT, "lookup", "tools:read", DEFINITION)
    outcome = await node_mesh_calls.OrchestratorCalls(orch).run(OWNER, "local-1", resolved, "authority", {"q": "x"}, ticket_for())
    assert (outcome.status, outcome.code) == ("error", "ticket.replayed")


def test_tools_availability_counts_sandboxed_agents():
    orch = Orchestrator.__new__(Orchestrator)
    orch.agents, orch.local_agents, orch.security_flags = {}, {}, {}
    orch.agent_cards = {AGENT: ve.build_cards(CATALOG)[0][0]}
    orch.vessel_definitions = ve.build_cards(CATALOG)[1]
    orch.tool_permissions = SimpleNamespace(is_tool_allowed=lambda owner, agent, tool: True)
    assert orch.compute_tools_available_for_user(OWNER) is True


def resolving_orch(definitions):
    orch = SimpleNamespace(
        tool_permissions=SimpleNamespace(get_tool_scope=lambda agent, tool: "tools:read"), vessel_definitions=definitions
    )
    return orch


async def test_a_self_declared_pin_is_ignored_and_the_computed_hash_is_used(monkeypatch):
    spoof = AgentSkill(name="lookup", id="lookup", description="d", input_schema={}, metadata={"vessel_definition": DEFINITION, "sandbox": "vessel"})
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: [("rogue", spoof)])
    resolved = await node_mesh_calls.OrchestratorCalls(resolving_orch({})).resolve(OWNER, "lookup")
    assert resolved.definition == node_mesh_calls.skill_definition(spoof) != DEFINITION


async def test_a_self_declared_pin_does_not_pass_the_definition_check_of_a_real_call(monkeypatch):
    spoof = AgentSkill(name="lookup", id="lookup", description="d", input_schema={}, metadata={"vessel_definition": DEFINITION})
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: [("rogue", spoof)])
    resolved = await node_mesh_calls.OrchestratorCalls(resolving_orch({})).resolve(OWNER, "lookup")
    assert resolved.definition != DEFINITION


async def test_a_tool_registered_from_the_executor_uses_the_stored_pin(monkeypatch):
    skill = ve.build_cards(CATALOG)[0][0].skills[0]
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: [(AGENT, skill)])
    resolved = await node_mesh_calls.OrchestratorCalls(resolving_orch(ve.build_cards(CATALOG)[1])).resolve(OWNER, "lookup")
    assert resolved.definition == DEFINITION


async def test_a_pin_belongs_to_its_agent_and_tool_only(monkeypatch):
    skill = AgentSkill(name="lookup", id="lookup", description="d", input_schema={})
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda orch, owner: [("other", skill)])
    resolved = await node_mesh_calls.OrchestratorCalls(resolving_orch(ve.build_cards(CATALOG)[1])).resolve(OWNER, "lookup")
    assert resolved.definition == node_mesh_calls.skill_definition(skill)


def test_a_card_that_claims_to_be_sandboxed_is_not_treated_so():
    orch, _ = fake_orch()
    liar = AgentCard(name="x", description="", agent_id="x", metadata={"sandbox": "vessel"})
    orch.agent_cards["x"] = liar
    assert not ve.is_sandboxed(orch, "x")


def test_foreign_cards_lose_the_reserved_metadata_keys():
    card = AgentCard(
        name="x", description="", agent_id="x", metadata={"sandbox": "vessel", "keep": 1},
        skills=[AgentSkill(name="t", id="t", description="", metadata={"vessel_definition": DEFINITION, "keep": 2})],
    )
    ve.strip_foreign_metadata(card)
    assert card.metadata == {"keep": 1} and card.skills[0].metadata == {"keep": 2}
    ve.strip_foreign_metadata(SimpleNamespace(metadata=None, skills=None))


async def test_a_connecting_agent_cannot_take_a_sandboxed_agent_id(monkeypatch):
    from orchestrator import auth

    monkeypatch.setattr(auth, "validate_agent_api_key", lambda key: True)
    orch = Orchestrator.__new__(Orchestrator)
    orch.agents, orch.local_agents, orch.agent_cards = {}, {}, {}
    orch._source_bound_read_tools, orch._source_bound_de_escalations = set(), set()
    orch.vessel_definitions = ve.build_cards(CATALOG)[1]
    closed = []

    class Socket:
        async def close(self, code, reason):
            closed.append((code, reason))

    card = AgentCard(name="x", description="", agent_id=AGENT, skills=[AgentSkill(name="t", id="t", description="", input_schema={"type": "object", "properties": {}})])
    await orch.register_agent(Socket(), SimpleNamespace(agent_card=card, api_key="k"))
    assert closed == [(1008, "agent id reserved")] and AGENT not in orch.agent_cards and AGENT not in orch.agents
