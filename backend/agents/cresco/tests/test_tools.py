"""Checks retained topology-tool behavior against the source-verified TLS fixture.
Synthetic protocol, refusal, bounded rendering and secret-handling cases do not qualify live fabric execution.
"""

import json
from dataclasses import asdict

import pytest

from agents.cresco import mcp_tools, mcp_server
from agents.cresco.mcp_server import MCPServer
from agents.cresco.wsapi_client import FabricFault
from shared.protocol import MCPRequest

from .conftest import SERVICE_KEY


READ_ARGUMENTS = (
    ("cresco_list_regions", {}),
    ("cresco_list_agents", {"region": "r1"}),
    ("cresco_agent_info", {"target": "r1_a1"}),
    ("cresco_get_sysinfo", {"target": "r1_a1"}),
)


def _call(name, arguments=None):
    return MCPServer().process_request(MCPRequest(
        request_id="req", method="tools/call",
        params={"name": name, "arguments": dict(arguments or {}, user_id="owner")},
    ))


def test_read_registry_keeps_scopes_and_the_effect_surface_separate():
    for name, _arguments in READ_ARGUMENTS:
        info = mcp_tools.TOOL_REGISTRY[name]
        assert info["scope"] == "tools:read"
        assert not info.get("destructive") and not info.get("metadata")
    assert mcp_tools.TOOL_REGISTRY["cresco_dispatch_work"]["scope"] == "tools:write"
    assert mcp_tools.TOOL_REGISTRY["cresco_run_command"]["scope"] == "tools:system"


@pytest.mark.parametrize(("name", "arguments"), READ_ARGUMENTS)
def test_flag_off_read_calls_never_dial(bridge_env, staged_fabric, monkeypatch, name, arguments):
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", False)
    response = _call(name, arguments)
    assert response.error["code"] == "CRESCO_NOT_CONFIGURED"
    assert response.error["data"]["detail"] == "feature_flag_off"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []


def test_flag_off_static_tool_listing_has_no_effect(staged_fabric, monkeypatch):
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", False)
    response = MCPServer().process_request(MCPRequest(request_id="list", method="tools/list"))
    assert {tool["name"] for tool in response.result["tools"]} == set(mcp_tools.TOOL_REGISTRY)
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []


@pytest.mark.parametrize(("name", "arguments"), READ_ARGUMENTS)
def test_missing_service_configuration_refuses_each_read_without_dial(
    bridge_env, staged_fabric, monkeypatch, name, arguments,
):
    monkeypatch.delenv("CRESCO_SERVICE_KEY")
    response = _call(name, arguments)
    assert response.error["code"] == "CRESCO_NOT_CONFIGURED"
    assert response.error["data"]["detail"] == "fabric_not_configured"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []


@pytest.mark.parametrize(("name", "arguments"), READ_ARGUMENTS)
def test_read_tools_render_the_actual_source_profile(bridge_env, name, arguments):
    response = _call(name, arguments)
    assert response.error is None
    assert response.result
    assert response.ui_components[0]["type"] == (
        "table" if name.startswith("cresco_list") else "keyvalue"
    )


def test_flooded_regions_stay_within_the_current_result_and_render_bounds(
    bridge_env, staged_fabric,
):
    staged_fabric.regions = {f"r{index:03}": [] for index in range(250)}
    response = _call("cresco_list_regions")
    assert response.error is None
    assert len(response.result["regions"]) == mcp_tools.MAX_ROWS
    assert len(response.ui_components[0]["rows"]) == mcp_tools.MAX_ROWS
    assert len(staged_fabric.requests) == 1


def test_empty_topology_renders_a_truthful_empty_table(bridge_env, staged_fabric):
    staged_fabric.regions = {}
    response = _call("cresco_list_regions")
    assert response.error is None
    assert response.result["regions"] == []
    assert response.ui_components[0]["rows"] == []


@pytest.mark.parametrize(("name", "arguments"), (
    ("cresco_list_agents", {"region": "../escape"}),
    ("cresco_agent_info", {"target": "r1_a1/escape"}),
    ("cresco_agent_info", {}),
    ("cresco_get_sysinfo", {"target": "x" * 500}),
))
def test_invalid_identifiers_refuse_before_a_socket_is_opened(
    bridge_env, staged_fabric, name, arguments,
):
    response = _call(name, arguments)
    assert response.error["code"] == "CRESCO_BAD_REQUEST"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []


@pytest.mark.parametrize(("behavior", "code", "retryable"), (
    ("malformed", "CRESCO_INVALID_RESPONSE", False),
    ("close_after_send", "CRESCO_UNREACHABLE", True),
))
def test_transport_faults_keep_their_closed_public_dispositions(
    bridge_env, staged_fabric, behavior, code, retryable,
):
    staged_fabric.behavior = behavior
    response = _call("cresco_list_regions")
    assert response.error["code"] == code
    assert response.error["retryable"] is retryable


@pytest.mark.parametrize(("name", "arguments"), READ_ARGUMENTS)
def test_service_key_never_appears_in_authentication_failures(
    bridge_env, staged_fabric, name, arguments,
):
    staged_fabric.reject_auth = True
    response = _call(name, arguments)
    assert response.error["code"] == "CRESCO_AUTH_FAILED"
    assert SERVICE_KEY not in json.dumps(asdict(response))


def test_unknown_methods_and_tools_are_truthfully_refused():
    server = MCPServer()
    response = server.process_request(MCPRequest(request_id="x", method="resources/list"))
    assert response.error["code"] == -32601
    assert _call("cresco_write_something").error["code"] == -32602


def test_rows_require_the_actual_structured_topology_shape():
    assert mcp_tools._rows({"regions": ["r1", "r2"]}, "regions") == ["r1", "r2"]
    with pytest.raises(FabricFault) as refused:
        mcp_tools._rows({"regions": "not a list"}, "regions")
    assert refused.value.state == "protocol"


def test_handler_crash_yields_a_fixed_nonretryable_error_without_internals(
    bridge_env, staged_fabric, captured_audit, monkeypatch, caplog,
):
    def crash(*_arguments, **_keywords):
        raise RuntimeError("unexpected internal detail: " + SERVICE_KEY)

    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["cresco_list_regions"], "function", crash)
    response = _call("cresco_list_regions")
    assert response.error["code"] == "CRESCO_INVALID_RESPONSE"
    assert response.error["data"]["detail"] == "unexpected_handler_failure"
    assert response.error["retryable"] is False
    assert SERVICE_KEY not in json.dumps(asdict(response))
    assert SERVICE_KEY not in json.dumps(captured_audit, default=str)
    assert all(SERVICE_KEY not in record.getMessage() for record in caplog.records)
    assert captured_audit[-1]["outcome"] == "failure"
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []
