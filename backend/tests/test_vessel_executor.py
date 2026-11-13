"""Tests for the adapter that sends sandboxed tool calls to the AstralVessel executor: configuration that fails closed, the exact request the executor receives, refusal and error mapping, unknown outcomes, catalog registration and the dispatch hook in the orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from orchestrator import node_mesh_calls, tool_visibility, vessel_executor as ve
from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill

TOKEN = "executor-secret"
AGENT_TOKEN = "agent-secret"
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
    }
    action.update(overrides)
    return ve.CallTicket('{"ticket":1}', json.dumps(action), text)


def executor_with(handler, **settings):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings.setdefault("agent_tokens", {AGENT: AGENT_TOKEN})
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


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "executor-agents.json"
    path.write_text(json.dumps({AGENT: AGENT_TOKEN, "other": "other-secret"}))
    return path


def env_with(path, **extra):
    return {ve.URL_ENV: URL, ve.TOKEN_ENV: TOKEN, ve.AGENT_TOKENS_ENV: str(path), **extra}


def test_nothing_configured_is_not_a_problem():
    assert ve.ExecutorSettings.from_env({}) is None
    assert ve.configuration_problems({}) == []
    assert ve.configuration_problems({ve.TIMEOUT_ENV: "5"}) == []


def test_the_settings_hold_the_operator_token_and_each_agents_token(tokens_file):
    settings = ve.ExecutorSettings.from_env(env_with(tokens_file, **{ve.URL_ENV: URL + "/"}))
    assert (settings.url, settings.token, settings.timeout) == (URL, TOKEN, ve.DEFAULT_TIMEOUT)
    assert dict(settings.agent_tokens) == {AGENT: AGENT_TOKEN, "other": "other-secret"}
    assert ve.configuration_problems(env_with(tokens_file)) == []


def test_the_timeout_is_read_from_the_environment(tokens_file):
    assert ve.ExecutorSettings.from_env(env_with(tokens_file, **{ve.TIMEOUT_ENV: "15.5"})).timeout == 15.5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e, f: e.pop(ve.URL_ENV),
        lambda e, f: e.pop(ve.TOKEN_ENV),
        lambda e, f: e.pop(ve.AGENT_TOKENS_ENV),
        lambda e, f: e.update({ve.URL_ENV: "ftp://executor"}),
        lambda e, f: e.update({ve.URL_ENV: "http://user:pw@executor"}),
        lambda e, f: e.update({ve.URL_ENV: "http://"}),
        lambda e, f: e.update({ve.TIMEOUT_ENV: "soon"}),
        lambda e, f: e.update({ve.TIMEOUT_ENV: "0"}),
        lambda e, f: e.update({ve.AGENT_TOKENS_ENV: str(f) + ".missing"}),
        lambda e, f: f.write_text("not json"),
        lambda e, f: f.write_text("[]"),
        lambda e, f: f.write_text("{}"),
        lambda e, f: f.write_text('{"guide": ""}'),
        lambda e, f: f.write_text('{"guide": 7}'),
        lambda e, f: f.write_text("x" * (ve.MAX_TOKENS_FILE_BYTES + 1)),
    ],
)
def test_a_partial_or_unusable_configuration_is_a_problem_and_leaves_the_executor_off(tokens_file, mutate):
    env = env_with(tokens_file)
    mutate(env, tokens_file)
    problems = ve.configuration_problems(env)
    assert problems and ve.ExecutorSettings.from_env(env) is None
    assert not any(TOKEN in p or AGENT_TOKEN in p for p in problems)


def test_a_production_node_refuses_to_start_on_a_broken_executor_setting(monkeypatch, tmp_path):
    from orchestrator import session_store

    monkeypatch.setenv(ve.URL_ENV, URL)
    monkeypatch.setenv(ve.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ve.AGENT_TOKENS_ENV, str(tmp_path / "missing.json"))
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    with pytest.raises(SystemExit) as stopped:
        session_store.assert_production_posture()
    assert stopped.value.code == 78


async def test_a_call_forwards_the_tool_arguments_action_and_ticket_with_the_bearer_token():
    recorder = Recorder()
    ticket = ticket_for()
    response = await invoke(executor_with(recorder), ticket)
    assert (response.result, response.error) == ("done", None)
    request = recorder.requests[0]
    assert (request.method, str(request.url), request.headers["authorization"]) == ("POST", URL + "/run", f"Bearer {AGENT_TOKEN}")
    assert json.loads(request.content) == {
        "agent": AGENT, "tool": "lookup", "arguments_json": ticket.arguments_json, "action_json": ticket.action_json,
        "ticket_json": ticket.ticket_json, "owner": OWNER,
    }
    assert TOKEN not in request.headers["authorization"]


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
        {"tool": "other"},
        {"mesh": ""},
        {"agent": ""},
        {"mesh": 7},
    ],
)
async def test_an_action_for_a_different_call_is_refused_before_it_is_sent(overrides):
    recorder = Recorder()
    response = await invoke(executor_with(recorder), ticket_for(**overrides))
    assert code_of(response) == "request.call_mismatch" and not recorder.requests


@pytest.mark.parametrize("missing", ["agent", "mesh"])
async def test_an_action_missing_a_pinned_field_is_refused_before_it_is_sent(missing):
    recorder = Recorder()
    ticket = ticket_for()
    action = json.loads(ticket.action_json)
    del action[missing]
    response = await invoke(executor_with(recorder), ve.CallTicket(ticket.ticket_json, json.dumps(action), ticket.arguments_json))
    assert code_of(response) == "request.call_mismatch" and not recorder.requests


async def test_an_action_without_an_owner_or_workspace_is_the_real_kernel_shape_and_is_sent_without_a_workspace():
    recorder = Recorder()
    ticket = ticket_for()
    action = json.loads(ticket.action_json)
    assert "owner" not in action and "workspace" not in action
    await invoke(executor_with(recorder), ticket)
    assert "workspace" not in json.loads(recorder.requests[0].content)


async def test_an_agent_without_a_token_is_refused_before_it_is_sent():
    recorder = Recorder()
    response = await invoke(executor_with(recorder, agent_tokens={"other": "x"}), ticket_for())
    assert code_of(response) == "executor.unauthorized" and not recorder.requests


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


async def test_the_catalog_is_read_with_the_operator_token_only():
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
    monkeypatch.delenv(ve.AGENT_TOKENS_ENV, raising=False)
    orch, _ = fake_orch()
    assert await ve.start_for(orch) is None and orch.vessel_executor is None
    await ve.stop_for(orch)


async def test_start_registers_the_catalog_and_stop_releases_the_client(monkeypatch, tokens_file):
    monkeypatch.setenv(ve.URL_ENV, URL)
    monkeypatch.setenv(ve.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ve.AGENT_TOKENS_ENV, str(tokens_file))
    real = httpx.AsyncClient
    monkeypatch.setattr(ve.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(Recorder(body={"agents": CATALOG})), **kw))
    orch, _ = fake_orch()
    executor = await ve.start_for(orch)
    assert orch.vessel_executor is executor and "guide" in orch.agent_cards
    await ve.stop_for(orch)
    assert orch.vessel_executor is None and executor._client.is_closed


async def test_an_unreachable_executor_at_start_registers_nothing(monkeypatch, tokens_file):
    monkeypatch.setenv(ve.URL_ENV, URL)
    monkeypatch.setenv(ve.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ve.AGENT_TOKENS_ENV, str(tokens_file))
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


def run_response(workspace="ws-abc"):
    return {"output": "done", "error": False, "workspace": workspace}


async def test_the_workspace_a_run_returns_is_ended_with_the_agents_own_token():
    recorder = Recorder(body=run_response())
    executor = executor_with(recorder)
    await invoke(executor, ticket_for())
    recorder.body = {"ok": True}
    await executor.end_all()
    request = recorder.requests[-1]
    assert (str(request.url), request.headers["authorization"]) == (URL + "/end", f"Bearer {AGENT_TOKEN}")
    assert json.loads(request.content) == {"agent": AGENT, "workspace": "ws-abc"}
    await executor.end_all()
    assert len(recorder.requests) == 2


async def test_a_run_that_returns_no_workspace_or_failed_opens_none():
    for status, body in ((200, {"output": "x", "error": False}), (403, {"error": True, "code": "ticket.replayed", "workspace": "w"})):
        recorder = Recorder(status=status, body=body)
        executor = executor_with(recorder)
        await invoke(executor, ticket_for())
        await executor.end_all()
        assert len(recorder.requests) == 1
    unreadable = executor_with(lambda request: httpx.Response(200, content=b"<html>"))
    await invoke(unreadable, ticket_for())
    assert not unreadable._open


async def test_a_workspace_that_could_not_be_ended_stays_open_for_the_next_try():
    recorder = Recorder(body=run_response())
    executor = executor_with(recorder)
    await invoke(executor, ticket_for())
    recorder.status, recorder.body = 403, {"error": True, "code": "executor.token_agent_mismatch"}
    assert await executor.end(AGENT, "ws-abc") is False and executor._open
    recorder.raises = httpx.ReadTimeout("slow")
    assert await executor.end(AGENT, "ws-abc") is False and executor._open
    assert await executor_with(recorder, agent_tokens={}).end(AGENT, "ws-abc") is False
    recorder.raises, recorder.status, recorder.body = None, 200, {"ok": True}
    assert await executor.end(AGENT, "ws-abc") is True and not executor._open


async def test_only_idle_workspaces_are_swept():
    now = [0.0]
    recorder = Recorder(body=run_response("old"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    executor = ve.VesselExecutor(ve.ExecutorSettings(URL, TOKEN, agent_tokens={AGENT: AGENT_TOKEN}), client, clock=lambda: now[0])
    await invoke(executor, ticket_for())
    now[0] = ve.WORKSPACE_IDLE_SECONDS / 2
    recorder.body = run_response("new")
    await invoke(executor, ticket_for())
    now[0] = ve.WORKSPACE_IDLE_SECONDS + 1
    recorder.body = {"ok": True}
    assert await executor.sweep() == 1
    assert list(executor._open) == [(AGENT, "new")]


async def test_close_ends_what_is_open_and_stops_the_sweeper(monkeypatch):
    recorder = Recorder(body=run_response())
    real = httpx.AsyncClient
    monkeypatch.setattr(ve.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(recorder), **kw))
    executor = ve.VesselExecutor(ve.ExecutorSettings(URL, TOKEN, agent_tokens={AGENT: AGENT_TOKEN}))
    executor.start_sweeping()
    executor.start_sweeping()
    await invoke(executor, ticket_for())
    recorder.body = {"ok": True}
    await executor.close()
    assert str(recorder.requests[-1].url) == URL + "/end" and executor._sweeper is None and executor._client.is_closed


async def test_the_sweeper_survives_a_failing_sweep(monkeypatch):
    executor = executor_with(Recorder())
    calls = []

    async def failing():
        calls.append(1)
        raise RuntimeError("boom")

    async def instant(_seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(executor, "sweep", failing)
    monkeypatch.setattr(ve.asyncio, "sleep", instant)
    with pytest.raises(asyncio.CancelledError):
        await executor._sweep_forever()
    assert len(calls) == 2


def test_published_tools_carry_the_agent_annotation_and_the_hash_the_executor_pins():
    from orchestrator.node_mesh_calls import definition_hash

    cards, pins = ve.build_cards({"guide": {"tools": [
        {"name": "lookup", "description": "Looks up", "input_schema": {"type": "object"}, "scope": "tools:read",
         "definition": definition_hash("lookup", "Looks up", {"type": "object"}, {"agent": "guide"})},
        {"name": "save", "description": "Saves", "input_schema": {}, "scope": "tools:write",
         "definition": definition_hash("save", "Saves", {}, {"agent": "guide"})},
        {"name": "drift", "description": "Drifts", "input_schema": {}, "scope": "tools:read", "definition": DEFINITION},
    ]}})
    orch, _ = fake_orch()
    ve.register(orch, cards, pins)
    tools = ve.published_tools(orch)
    assert [(t["name"], t["effect"], t["annotations"]) for t in tools] == [
        ("lookup", "outward_read", {"agent": "guide"}), ("save", "outside_effect", {"agent": "guide"}),
    ]
    orch.vessel_definitions[("ghost", "x")] = DEFINITION
    assert len(ve.published_tools(orch)) == 2
    assert ve.published_tools(SimpleNamespace()) == []
