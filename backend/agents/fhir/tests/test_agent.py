"""Exercises the agent card, MCP dispatch, the orchestrator's in-process transport,
feature-flag gating, registration, subprocess start-up, safe-seed treatment, taint
classification and tool naming rules.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.fhir import fhir_agent, mcp_tools
from agents.fhir.mcp_server import MCPServer
from orchestrator import taint, tool_feedback
from orchestrator.local_agents import BUILT_IN_AGENT_DIRS, FIRST_PARTY_PUBLIC_AGENT_IDS
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream_manager import declared_lifetime
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared.feature_flags import FeatureFlags
from shared.phi_redactor import PHI_FIELD_PATTERNS
from shared.protocol import MCPRequest, MCPResponse, Message
from shared.stream_sdk import is_streaming_tool

EXPECTED_TOOLS = {
    "icu_census", "patient_overview", "vital_sign_trends", "laboratory_results", "medication_review", "patient_timeline",
    "query_fhir_records", "fhir_source_status", "watch_icu_activity", "stream_patient_vitals",
}


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(fhir_agent.BaseA2AAgent, "_init_crypto", lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    return fhir_agent.FhirAgent(port=8997)


def call(name, arguments=None):
    return MCPServer().process_request(MCPRequest(request_id="r", method="tools/call", params={"name": name, "arguments": arguments or {}}))


class LoopbackOrchestrator:
    def __init__(self, agent):
        self.local_agents = {agent.card.agent_id: agent}
        self.pending_requests = {}
        self.pending_ui_sockets = {}
        self.stream_manager = None
        self.frames = 0
        self._dispatch_context = {}
        self._register_dispatch_context = MethodType(Orchestrator._register_dispatch_context, self)
        self.execute = MethodType(Orchestrator._execute_in_process, self)

    async def handle_agent_message(self, websocket, message):
        self.frames += 1
        reply = Message.from_json(message)
        pending = self.pending_requests.get(reply.request_id) if isinstance(reply, MCPResponse) else None
        if pending is not None and not pending.done():
            pending.set_result(reply)


def test_registry_entries_are_well_formed_read_only_tools():
    assert set(mcp_tools.TOOL_REGISTRY) == EXPECTED_TOOLS
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        assert callable(info["function"]) and str(info["description"]).strip(), name
        assert info["input_schema"]["type"] == "object" and isinstance(info["input_schema"]["properties"], dict), name
        assert info["scope"] in ("tools:read", "tools:search"), name
    assert mcp_tools.TOOL_REGISTRY["patient_overview"]["input_schema"]["required"] == ["patient"]
    assert mcp_tools.TOOL_REGISTRY["query_fhir_records"]["input_schema"]["required"] == ["resource_type"]


@pytest.mark.parametrize("name", ["watch_icu_activity", "stream_patient_vitals"])
def test_stream_tools_declare_push_streaming_and_progress(name):
    entry = mcp_tools.TOOL_REGISTRY[name]
    assert is_streaming_tool(entry["function"])
    assert entry["metadata"] == {
        "streamable": True, "streaming_kind": "push", "max_fps": 1, "min_fps": 1, "max_chunk_bytes": 65536,
        "persist_progress_s": 15, "duration_argument": "minutes", "duration_unit_s": 60.0,
    }


@pytest.mark.parametrize("name, default, longest", [("watch_icu_activity", 120.0, 600.0), ("stream_patient_vitals", 300.0, 900.0)])
def test_stream_tools_declare_the_duration_the_orchestrator_holds_them_to(name, default, longest):
    entry = mcp_tools.TOOL_REGISTRY[name]
    lifetime = declared_lifetime(entry["metadata"], entry["input_schema"])
    assert lifetime.seconds({}) == default and lifetime.seconds({"minutes": 99}) == longest
    assert lifetime.seconds({"minutes": 4}) == 240.0 and lifetime.seconds({"minutes": 0}) == 60.0


def test_tool_names_and_arguments_stay_clear_of_platform_gates():
    analyzer = ToolSecurityAnalyzer()
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        flag = analyzer.analyze_tool(name, info["description"], info["input_schema"])
        assert flag is None, (name, flag)
        assert not taint.is_sink("fhir-1", name), name
        for argument in info["input_schema"]["properties"]:
            assert not any(marker in argument.lower() for marker in PHI_FIELD_PATTERNS), (name, argument)


def test_card_describes_the_agent(agent):
    card = agent.card
    assert (card.agent_id, card.name) == ("fhir-1", "FHIR Clinical Data")
    assert {skill.name for skill in card.skills} == EXPECTED_TOOLS
    assert [example["title"] for example in card.metadata["examples"]] == [
        "ICU census", "Patient overview", "Vital sign trends", "Live activity"]
    assert "most concerning vital signs" in card.metadata["examples"][1]["prompt"]
    assert all(example["prompt"] for example in card.metadata["examples"])
    feed = next(skill for skill in card.skills if skill.name == "watch_icu_activity")
    assert feed.metadata["streaming_kind"] == "push" and feed.scope == "tools:read"
    assert "fhir" in card.skills[0].tags and agent.port == 8997


def test_agent_reads_its_port_from_the_environment(monkeypatch):
    monkeypatch.setattr(fhir_agent.BaseA2AAgent, "_init_crypto", lambda self: setattr(self, "_public_key_jwk", {}))
    monkeypatch.setenv("FHIR_AGENT_PORT", "8996")
    assert fhir_agent.FhirAgent().port == 8996


def test_server_lists_and_dispatches_tools(connected):
    server = MCPServer()
    listed = server.process_request(MCPRequest(request_id="l", method="tools/list", params={}))
    assert {tool["name"] for tool in listed.result["tools"]} == EXPECTED_TOOLS
    assert server.get_tool_list()[0]["input_schema"]["type"] == "object"
    response = call("icu_census", {"limit": 3, "_runtime": object(), "session_id": "s", "user_id": "u"})
    assert response.error is None and response.result["patients_in_icu"] == 1
    assert response.ui_components[0]["id"] == "fhir-icu-census"


@pytest.mark.asyncio
async def test_in_process_transport_carries_cards_and_coded_errors(connected, agent):
    orchestrator = LoopbackOrchestrator(agent)
    census = await orchestrator.execute("fhir-1", "icu_census", {"limit": 3}, timeout=10.0)
    assert census.error is None and census.result["patients_in_icu"] == 1
    assert [component["id"] for component in census.ui_components] == ["fhir-icu-census"]
    overview = await orchestrator.execute("fhir-1", "patient_overview", {"patient": "002-1"}, timeout=10.0)
    assert overview.error is None and overview.ui_components[0]["id"] == "fhir-patient-002-1"
    missing = await orchestrator.execute("fhir-1", "patient_overview", {"patient": "999-9"}, timeout=10.0)
    assert missing.error["code"] == "FHIR_NOT_FOUND" and not missing.ui_components
    feed = await orchestrator.execute("fhir-1", "watch_icu_activity", {"minutes": 1}, timeout=10.0)
    assert feed.error is None and feed.ui_components[0]["title"] == "ICU activity feed"
    live = await orchestrator.execute("fhir-1", "stream_patient_vitals", {"patient": "002-1"}, timeout=10.0)
    assert live.error is None and live.ui_components[0]["title"] == "Live vitals: patient 002-1"
    assert orchestrator.frames == 5 and orchestrator.pending_requests == {}
    assert connected.deleted == ["sub-1", "sub-2"]


def test_server_reports_coded_and_unexpected_failures(connected, monkeypatch):
    assert call("patient_overview", {"patient": "999-9"}).error["code"] == "FHIR_NOT_FOUND"
    assert call("nope").error == {"code": -32601, "message": "Unknown tool: nope", "retryable": False}
    unknown = MCPServer().process_request(MCPRequest(request_id="x", method="resources/list", params={}))
    assert unknown.error["message"] == "Unknown method: resources/list"
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["icu_census"], "function", lambda **_: 1 / 0)
    failed = call("icu_census")
    assert failed.error == {"code": -32603, "message": "The FHIR tool failed unexpectedly", "retryable": False}
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["icu_census"], "function", lambda **_: {"plain": True})
    assert call("icu_census").result == {"plain": True}


def test_flag_defaults_off_and_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("FF_FHIR", raising=False)
    assert FeatureFlags().is_enabled("fhir") is False
    monkeypatch.setenv("FF_FHIR", "true")
    assert FeatureFlags().is_enabled("fhir") is True


def test_agent_is_public_but_not_always_on():
    assert "fhir-1" in FIRST_PARTY_PUBLIC_AGENT_IDS and "fhir" not in BUILT_IN_AGENT_DIRS


def test_standalone_start_refuses_when_the_flag_is_off(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "fhir", False)
    with pytest.raises(SystemExit) as stopped:
        fhir_agent.main()
    assert stopped.value.code == 78


def test_standalone_start_runs_the_agent_when_enabled(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "fhir", True)
    monkeypatch.setattr("sys.argv", ["fhir_agent.py", "--port", "8995"])
    started = {}
    run = AsyncMock()
    monkeypatch.setattr(fhir_agent, "FhirAgent", lambda port=None: started.update(port=port) or SimpleNamespace(run=run))
    fhir_agent.main()
    assert started == {"port": 8995} and run.await_count == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_startup_gate_follows_the_flag(monkeypatch, enabled):
    import start
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "fhir", enabled)
    assert start._fhir_enabled() is enabled


def test_startup_gate_fails_closed(monkeypatch):
    import start
    from shared.feature_flags import flags

    def unavailable(name):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(flags, "is_enabled", unavailable)
    assert start._fhir_enabled() is False


@pytest.mark.parametrize(("enabled", "inprocess", "spawned"), [
    (False, False, False), (False, True, False), (True, True, False), (True, False, True),
])
def test_subprocess_spawn_respects_flag_and_transport_mode(monkeypatch, tmp_path, enabled, inprocess, spawned):
    import start
    from tests.test_start_wait import _agents_tree, _run_main

    backend, agents = _agents_tree(tmp_path)
    directory = agents / "fhir"
    directory.mkdir()
    (directory / "fhir_agent.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(start, "_fhir_enabled", lambda: enabled)
    _, started = _run_main(monkeypatch, backend, inprocess=inprocess, remote_flag=False)
    assert ("fhir" in started) is spawned


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_in_process_registration_follows_the_flag(monkeypatch, enabled):
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver
    from shared.feature_flags import flags

    for name in ("gaiakeep", "cresco", "remote_compute", "computer_use"):
        monkeypatch.setitem(flags._flags, name, False)
    monkeypatch.setitem(flags._flags, "fhir", enabled)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", list)
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)
    monkeypatch.setattr(fhir_agent.BaseA2AAgent, "_init_crypto", lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object())
    orch = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane), local_agents={}, register_agent=AsyncMock())
    registered = await local_agents.register_built_ins(orch)
    assert registered == (["fhir-1"] if enabled else [])
    assert ("fhir-1" in orch.local_agents) is enabled


@pytest.mark.asyncio
async def test_in_process_registration_survives_a_flag_failure(monkeypatch):
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver
    from shared.feature_flags import flags

    def selective(name):
        if name == "fhir":
            raise RuntimeError("unavailable")
        return False

    monkeypatch.setattr(flags, "is_enabled", selective)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", list)
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object())
    orch = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane), local_agents={}, register_agent=AsyncMock())
    assert await local_agents.register_built_ins(orch) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_safe_seed_includes_the_agent_only_when_enabled(monkeypatch, enabled):
    from tests.test_remote_orchestrator_wiring_063 import _drive_start

    _, seeded = await _drive_start(monkeypatch, remote_compute=False, fhir=enabled)
    assert ("fhir-1" in seeded[0][1]) is enabled


def test_feed_data_is_treated_as_untrusted():
    assert taint.classify_source("fhir-1", "patient_overview") == taint.UNTRUSTED


def test_public_error_messages_exist_for_every_code_the_agent_raises():
    codes = {"FHIR_NOT_CONFIGURED", "FHIR_AUTH_FAILED", "FHIR_BLOCKED", "FHIR_UNAVAILABLE", "FHIR_NOT_FOUND",
             "FHIR_BAD_REQUEST", "FHIR_INVALID_RESPONSE"}
    assert codes <= set(tool_feedback.PUBLIC_ERRORS)
    for code in codes:
        message = tool_feedback.tool_failure_message("icu_census", {"code": code})
        assert message == tool_feedback.PUBLIC_ERRORS[code] and len(message) < 160
