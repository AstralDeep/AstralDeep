"""Exercises bounded topology over synthetic source-verified TLS control exchanges.
Admission refusals, unavailable effects, fault recovery, secret scrubbing and audit are
checked without representing synthetic records as a deployed fabric or work completion.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from shared.protocol import MCPRequest

from agents.cresco import mcp_server
from agents.cresco.mcp_server import MCPServer
from agents.cresco.wsapi_client import FabricConfig, WsapiClient

from .conftest import SERVICE_KEY


def request(name, arguments=None, **context):
    return MCPRequest(request_id="req", method="tools/call",
                      params={"name": name, "arguments": dict(arguments or {}, **context)})


@pytest.fixture
def server(bridge_env):
    return MCPServer()


def _error_of(response):
    return response.error or {}


def test_tools_list_the_bounded_surface_with_tiered_scopes(server):
    listed = server.process_request(MCPRequest(request_id="l", method="tools/list", params={}))
    tools = {tool["name"]: tool for tool in listed.result["tools"]}
    assert set(tools) == {"cresco_list_regions", "cresco_list_agents", "cresco_agent_info",
                          "cresco_get_sysinfo", "cresco_dispatch_work", "cresco_run_command"}
    assert tools["cresco_list_regions"]["scope"] == "tools:read"
    assert tools["cresco_dispatch_work"]["scope"] == "tools:write"
    assert tools["cresco_run_command"]["scope"] == "tools:system"
    assert tools["cresco_dispatch_work"]["input_schema"]["required"] == ["target", "work_kind"]


def test_unknown_methods_and_tools_fail_closed(server):
    response = server.process_request(MCPRequest(request_id="x", method="resources/list", params={}))
    assert response.error["code"] == -32601
    response = server.process_request(request("cresco_nope", {}))
    assert response.error["code"] == -32602


def test_flag_off_is_a_kill_switch(server, monkeypatch):
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", False)
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_NOT_CONFIGURED"
    assert _error_of(response)["data"]["detail"] == "feature_flag_off"


def test_missing_configuration_reports_unavailable_without_dialing(monkeypatch):
    for name in ("CRESCO_WSAPI_URL", "CRESCO_SERVICE_KEY", "CRESCO_CA_BUNDLE",
                 "CRESCO_TLS_FINGERPRINT", "CRESCO_ALLOW_PRIVATE_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", True)
    server = MCPServer()
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_NOT_CONFIGURED"
    assert _error_of(response)["data"]["detail"] == "fabric_not_configured"


def test_list_regions_stages_both_nodes_and_renders_a_table(server, staged_fabric):
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert response.error is None
    assert response.result["regions"] == [{"name": "r1", "type": "local", "agents": "2"}, {"name": "r2", "type": "local", "agents": "1"}]
    assert response.ui_components[0]["type"] == "table"
    assert response.ui_components[0]["rows"] == [["r1"], ["r2"]]


def test_list_agents_validates_the_region_argument(server):
    response = server.process_request(request("cresco_list_agents", {"region": "R1!"}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_BAD_REQUEST"


def test_agent_info_requires_an_approved_target_and_never_dials_otherwise(server, staged_fabric):
    dialed = len(staged_fabric.requests)
    response = server.process_request(request("cresco_agent_info", {"target": "r9_unknown"}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_TARGET_REFUSED"
    assert len(staged_fabric.requests) == dialed
    response = server.process_request(request("cresco_agent_info", {"target": "r1_a1"}, user_id="u1"))
    assert response.error is None
    assert response.result["target"] == "r1_a1"
    assert response.result["info"] == {"agent-data": "cresco-data"}


def test_admitted_dispatch_is_unavailable_and_audited_without_an_effect(server, staged_fabric, captured_audit):
    response = server.process_request(request("cresco_dispatch_work",
        {"target": "r1_a1", "work_kind": "status", "work_payload": "report state"}, user_id="user-7"))
    assert response.error["data"]["detail"] == "dispatch_protocol_unavailable"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []
    rows = [row for row in captured_audit if row["action_type"] == "tool.cresco_dispatch_work.fabric"]
    assert rows[-1]["outcome"] == "failure"
    assert rows[-1]["inputs_meta"]["cresco_target"] == "r1_a1"
    assert rows[-1]["actor_user_id"] == "user-7"
    assert rows[-1]["auth_principal"] == "agent:cresco-1"

def test_dispatch_to_a_non_approved_target_is_refused_locally_and_audited(server, staged_fabric, captured_audit):
    dialed = len(staged_fabric.requests)
    response = server.process_request(request(
        "cresco_dispatch_work", {"target": "r1_a1_free", "work_kind": "status"}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_TARGET_REFUSED"
    assert len(staged_fabric.requests) == dialed
    refusals = [row for row in captured_audit if row["outcome"] == "failure"
                and row["action_type"] == "tool.cresco_dispatch_work.fabric"]
    assert refusals and refusals[-1]["inputs_meta"]["cresco_target"] == "r1_a1_free"


def test_operator_revocation_blocks_a_previously_approved_target(server, monkeypatch, staged_fabric):
    monkeypatch.setenv("CRESCO_APPROVED_TARGETS", "r1_a1")
    response = server.process_request(request(
        "cresco_dispatch_work", {"target": "r2_a3", "work_kind": "status"}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_TARGET_REFUSED"


def test_revoked_stale_target_read_returns_a_typed_failure(server, staged_fabric):
    staged_fabric.errors_by_target["r2_a3"] = "unknown_agent r2_a3 (revoked from fabric)"
    response = server.process_request(request("cresco_agent_info", {"target": "r2_a3"}, user_id="u1"))
    error = _error_of(response)
    assert error["code"] == "CRESCO_INVALID_RESPONSE"
    assert error["data"]["detail"] == "stale_target"
    assert error["data"]["fault"] == "protocol"

def test_malformed_topology_response_is_a_protocol_fault(server, staged_fabric):
    staged_fabric.behavior = "malformed"
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert response.error["code"] == "CRESCO_INVALID_RESPONSE"

def test_unavailable_dispatch_does_not_send_even_when_the_fabric_would_not_reply(server, staged_fabric):
    staged_fabric.behavior = "never_reply"
    response = server.process_request(request("cresco_dispatch_work", {"target": "r1_a1", "work_kind": "status"}, user_id="u1"))
    assert response.error["data"]["detail"] == "dispatch_protocol_unavailable"
    assert staged_fabric.requests == []

def test_connection_failure_is_retryable_and_recovery_needs_no_state_reset(server, staged_fabric, monkeypatch):
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://127.0.0.1:1")
    refused = server.process_request(request(
        "cresco_agent_info", {"target": "r1_a1"}, user_id="u1"))
    assert _error_of(refused)["code"] == "CRESCO_UNREACHABLE"
    assert _error_of(refused)["retryable"] is True
    monkeypatch.setenv("CRESCO_WSAPI_URL", staged_fabric.wsapi_url)
    recovered = server.process_request(request(
        "cresco_agent_info", {"target": "r1_a1"}, user_id="u1"))
    assert recovered.error is None and recovered.result["info"] == {"agent-data": "cresco-data"}


def test_fabric_authentication_failure_maps_to_the_auth_fault(server, staged_fabric):
    staged_fabric.reject_auth = True
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert _error_of(response)["code"] == "CRESCO_AUTH_FAILED"


def test_service_key_material_never_reaches_results(server, staged_fabric):
    staged_fabric.echo_service_key = True
    response = server.process_request(request("cresco_agent_info", {"target": "r1_a1"}, user_id="u1"))
    assert response.error is None
    assert SERVICE_KEY not in json.dumps(response.result)
    assert SERVICE_KEY not in json.dumps(response.ui_components)
    assert response.result["info"]["agent-data"] == "[REDACTED]"


def test_executor_is_disabled_until_the_operator_switch_and_override_exist(bridge_env):
    server = MCPServer()
    denied = server.process_request(request(
        "cresco_run_command", {"target": "r1_a2_executor", "command": "ls"}, user_id="u1"))
    assert _error_of(denied)["code"] == "CRESCO_NOT_CONFIGURED"
    assert _error_of(denied)["data"]["detail"] == "executor_disabled_by_operator"


def test_executor_demands_an_explicit_per_user_override(bridge_env, monkeypatch):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")
    server = MCPServer()
    unauthorized = SimpleNamespace(tool_permissions=SimpleNamespace(
        is_tool_allowed=lambda user, agent, tool: False))
    server.orchestrator = unauthorized
    refused = server.process_request(request(
        "cresco_run_command", {"target": "r1_a2_executor", "command": "ls"}, user_id="u1"))
    assert _error_of(refused)["code"] == "CRESCO_BAD_REQUEST"
    assert _error_of(refused)["data"]["detail"] == "executor_not_authorized"


def test_executor_override_does_not_claim_a_bounded_result_transport(bridge_env, monkeypatch, staged_fabric, captured_audit):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")
    server = MCPServer(SimpleNamespace(tool_permissions=SimpleNamespace(is_tool_allowed=lambda *a: True)))
    response = server.process_request(request("cresco_run_command",
        {"target": "r1_a2_executor", "command": "echo hi", "timeout_seconds": 5}, user_id="u1"))
    assert response.error["data"]["detail"] == "executor_result_transport_unavailable"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []
    rows = [row for row in captured_audit if row["action_type"] == "tool.cresco_run_command.fabric"]
    assert rows[-1]["outcome"] == "failure"
    assert rows[-1]["inputs_meta"]["cresco_target"] == "r1_a2_executor"

def test_invalid_dispatch_arguments_are_refused_without_dialing(server, staged_fabric):
    dialed = len(staged_fabric.requests)
    for arguments in (
        {"target": "r1_a1", "work_kind": "STATUS!"},
        {"target": "r1_a1", "work_kind": "status\n"},
        {"target": "r1_a1", "work_kind": "status", "work_payload": "x" * 5000},
        {"target": "r1_a1", "work_kind": "status", "timeout_seconds": 0},
        {"target": "r1_a1", "work_kind": "status", "timeout_seconds": 999},
        {"target": "r1_a1", "work_kind": "status", "timeout_seconds": "soon"},
    ):
        response = server.process_request(request("cresco_dispatch_work", arguments, user_id="u1"))
        assert _error_of(response)["code"] == "CRESCO_BAD_REQUEST", arguments
    assert len(staged_fabric.requests) == dialed


def test_tool_context_builds_its_client_from_the_environment(monkeypatch):
    from agents.cresco.mcp_tools import ToolContext

    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://fabric.example:8282")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    ctx = ToolContext(user_id="u")
    assert isinstance(ctx.client(), WsapiClient)
    assert ctx.client().config.https_form == "https://fabric.example:8282"


def test_fault_states_carry_fixed_public_messages():
    from orchestrator.tool_feedback import PUBLIC_ERRORS, tool_failure_message

    codes = {"CRESCO_NOT_CONFIGURED", "CRESCO_AUTH_FAILED", "CRESCO_UNREACHABLE",
             "CRESCO_TLS_FAILED", "CRESCO_INVALID_RESPONSE", "CRESCO_TARGET_REFUSED",
             "CRESCO_BAD_REQUEST", "CRESCO_RESULT_UNCONFIRMED"}
    assert codes <= set(PUBLIC_ERRORS)
    for code in codes:
        assert tool_failure_message("cresco_dispatch_work", {"code": code}) == PUBLIC_ERRORS[code]
        assert len(PUBLIC_ERRORS[code]) < 160


def test_list_agents_renders_a_scoped_table(server, staged_fabric):
    response = server.process_request(request("cresco_list_agents", {"region": "r1"}, user_id="u1"))
    assert response.error is None
    assert response.result["region"] == "r1"
    assert response.ui_components[0]["type"] == "table"
    assert [row[0] for row in response.ui_components[0]["rows"]] == ["r1_a1", "r1_a2"]


def test_stray_frames_do_not_confuse_topology_correlation(staged_fabric, bridge_env):
    staged_fabric.behavior = "stray_then_result"
    response = MCPServer().process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert response.error is None
    assert len(response.result["regions"]) == 2

def test_bulk_topology_shape_drift_is_a_typed_failure(server, staged_fabric):
    from agents.cresco.wsapi_client import encode_bulk

    staged_fabric.bulk_override = encode_bulk({"unexpected": True})
    response = server.process_request(request("cresco_list_regions", {}, user_id="u1"))
    assert response.error["code"] == "CRESCO_INVALID_RESPONSE"

def test_bulk_encoded_results_round_trip_through_reads(server, staged_fabric, bridge_env):
    from agents.cresco.wsapi_client import WsapiClient, encode_bulk

    staged_fabric.bulk_override = encode_bulk({"regions": [{"region": "bulk-r"}]})
    payload = WsapiClient(FabricConfig.from_env()).call("listregions", {})
    assert payload["regions"] == [{"region": "bulk-r"}]


def test_unmapped_fabric_actions_fail_safe(staged_fabric, bridge_env):
    from agents.cresco.wsapi_client import FabricFault, WsapiClient

    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("teleport_node", {})
    assert fault.value.state == "unavailable" and not fault.value.after_send
