"""Exercises the read-only Cresco tool registry through real MCP dispatch against the
staged gateway: allowed renders, denial and failure alerts, flag-off inertness, bounds,
and service-key hygiene across every output.
"""

import json

import pytest
from shared.feature_flags import flags
from shared.protocol import MCPRequest

from agents.cresco import mcp_tools
from agents.cresco.mcp_server import MCPServer
from agents.cresco.tests.conftest import SERVICE_KEY
from agents.cresco.wsapi_client import CrescoProtocolError

ALLOWED_ACTIONS = {"listregions", "listagents", "getagentinfo", "getsysinfo"}


def _call(name, arguments=None):
    return MCPServer().process_request(
        MCPRequest(request_id="req-1", method="tools/call", params={"name": name, "arguments": dict(arguments or {})})
    )


def _alert_text(response):
    return json.dumps({"result": response.result, "components": response.ui_components})


def _set_flag(monkeypatch, enabled):
    monkeypatch.setitem(flags._flags, "cresco", enabled)


def test_registry_is_read_only_scoped_and_bounded():
    from agents.cresco.wsapi_client import IDENTIFIER

    assert set(mcp_tools.TOOL_REGISTRY) == {
        "cresco_list_regions",
        "cresco_list_agents",
        "cresco_agent_info",
        "cresco_get_sysinfo",
    }
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        assert info["scope"] == "tools:read"
        assert not info.get("destructive") and not info.get("metadata")
        schema = info["input_schema"]
        assert schema["additionalProperties"] is False
        for property_schema in schema.get("properties", {}).values():
            assert property_schema["maxLength"] <= 128
            assert property_schema["pattern"] == f"^{IDENTIFIER.pattern}$"


def test_flag_off_refuses_every_surface_without_dialing(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, False)
    server = MCPServer()
    response = server.process_request(MCPRequest(request_id="req-1", method="tools/call",
                                                 params={"name": "cresco_list_regions", "arguments": {}}))
    assert response.error["code"] == -32000
    response = server.process_request(MCPRequest(request_id="req-2", method="tools/list"))
    assert response.error["code"] == -32000
    result = mcp_tools.cresco_list_regions()
    assert "disabled by feature flag" in result["_data"]["summary"]
    assert gateway.captured.empty


def test_missing_config_reports_unavailable_without_dialing(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    monkeypatch.delenv("CRESCO_WSAPI_URL", raising=False)
    monkeypatch.delenv("CRESCO_SERVICE_KEY", raising=False)
    response = _call("cresco_list_regions")
    assert "not configured" in _alert_text(response)
    assert gateway.captured.empty
    response = _call("cresco_agent_info", {"region": "global", "agent": "agent-1"})
    assert "not configured" in _alert_text(response)
    assert gateway.captured.empty


def test_list_regions_renders_staged_topology(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = _call("cresco_list_regions")
    assert response.error is None
    table, note = response.ui_components
    assert table["type"] == "table"
    assert table["headers"] == ["region", "agents", "status"]
    assert table["rows"] == [
        ["global", "2", "online"],
        ["edge-west", "3", "online"],
    ]
    assert table["total_rows"] == 2
    assert "2 region(s)" in note["content"]
    assert response.result["regions"] == 2
    assert "already on the user's canvas" in response.result["presentation"]


def test_list_agents_filters_by_region(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = _call("cresco_list_agents", {"region": "global"})
    table, note = response.ui_components
    assert [row[1] for row in table["rows"]] == ["agent-1", "agent-2"]
    assert "in region 'global'" in response.result["summary"]


def test_agent_info_renders_state_card_and_plugin_table(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = _call("cresco_agent_info", {"region": "global", "agent": "agent-1"})
    card, table = response.ui_components
    assert card["type"] == "card" and card["title"] == "Agent agent-1 in region global"
    assert card["content"][0]["items"] == [{"label": "Status", "value": "connected"}]
    assert table["rows"] == [
        ["executor", "STARTED", "HEALTHY"],
        ["filerepo", "STARTED", "DEGRADED"],
    ]
    assert response.result["region"] == "global" and response.result["agent"] == "agent-1"


def test_get_sysinfo_renders_key_value_panel(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = _call("cresco_get_sysinfo", {"region": "edge-west", "agent": "edge-a"})
    panel = response.ui_components[0]
    assert panel["type"] == "keyvalue"
    assert {"label": "hostname", "value": "edge-host-1"} in panel["items"]
    assert response.result["agent"] == "edge-a"


def test_flooded_topology_is_bounded_and_notes_truncation(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    gateway.state["mode"] = "flood"
    response = _call("cresco_list_regions")
    table, note = response.ui_components
    assert len(table["rows"]) == mcp_tools.MAX_TABLE_ROWS
    assert table["total_rows"] == 250
    assert "Showing the first 200 of 250" in note["content"]


def test_invalid_identifiers_return_invalid_argument_alerts(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    for arguments in (
        {"region": "../escape", "agent": "agent-1"},
        {"region": "global", "agent": "x" * 129},
        {"region": "global"},
        {},
    ):
        response = _call("cresco_agent_info", arguments)
        assert "identifier" in _alert_text(response)
    response = _call("cresco_list_agents", {"region": "bad id!"})
    assert "identifier" in _alert_text(response)
    assert gateway.captured.empty


def test_failure_alerts_map_typed_errors(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    gateway.state["mode"] = "auth"
    response = _call("cresco_list_regions")
    assert "Cresco authentication failed." in _alert_text(response)
    assert response.result["retryable"] is False

    gateway.state["mode"] = "closed"
    response = _call("cresco_list_regions")
    assert "Cresco fabric unreachable." in _alert_text(response)
    assert response.result["retryable"] is True

    gateway.state["mode"] = "malformed"
    response = _call("cresco_list_regions")
    assert "Unexpected fabric response." in _alert_text(response)
    assert response.result["retryable"] is False


def test_tls_failure_alert(gateway, monkeypatch):
    gateway.configure(monkeypatch, ca=False)
    _set_flag(monkeypatch, True)
    response = _call("cresco_list_regions")
    assert "Cresco TLS verification failed." in _alert_text(response)
    assert response.result["retryable"] is False


def test_service_key_never_appears_in_any_output(gateway, monkeypatch):
    gateway.configure(monkeypatch, key="wrong-key")
    _set_flag(monkeypatch, True)
    for name, arguments in (
        ("cresco_list_regions", {}),
        ("cresco_list_agents", {"region": "global"}),
        ("cresco_agent_info", {"region": "global", "agent": "agent-1"}),
        ("cresco_get_sysinfo", {"region": "global", "agent": "agent-1"}),
    ):
        response = _call(name, arguments)
        assert SERVICE_KEY not in _alert_text(response)
        assert SERVICE_KEY not in json.dumps(response.error or {})


def test_unknown_method_and_tool_are_refused(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = MCPServer().process_request(MCPRequest(request_id="req-1", method="resources/list"))
    assert response.error["code"] == -32601
    response = _call("cresco_write_something")
    assert response.error["code"] == -32601 and "Unknown tool" in response.error["message"]


def test_tools_list_exposes_scope_and_schemas(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    response = MCPServer().process_request(MCPRequest(request_id="req-1", method="tools/list"))
    tools = {tool["name"]: tool for tool in response.result["tools"]}
    assert set(tools) == set(mcp_tools.TOOL_REGISTRY)
    assert all(tool["scope"] == "tools:read" for tool in tools.values())
    assert tools["cresco_agent_info"]["input_schema"]["required"] == ["region", "agent"]


def test_handler_crash_yields_generic_error_without_internals(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    monkeypatch.setattr(mcp_tools, "FabricConfig", lambda: (_ for _ in ()).throw(RuntimeError("boom SECRET")))
    response = _call("cresco_list_regions")
    assert response.error["code"] == -32603
    assert "SECRET" not in json.dumps(response.error)


def test_empty_topology_renders_an_empty_table_with_note(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    gateway.state["mode"] = "empty"
    response = _call("cresco_list_regions")
    table, note = response.ui_components
    assert table["rows"] == [] and table["total_rows"] == 0
    assert "reported no region" in note["content"]
    assert response.result["regions"] == 0


def test_malformed_sysinfo_shape_fails_safe(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _set_flag(monkeypatch, True)
    gateway.state["mode"] = "nosysinfo"
    response = _call("cresco_get_sysinfo", {"region": "global", "agent": "agent-1"})
    assert "Unexpected fabric response." in _alert_text(response)


def test_row_helpers_pad_scalars_and_reject_non_lists():
    from agents.cresco.mcp_tools import _rows

    assert _rows(["r1", 42], ["region", "agents"]) == [["r1", ""], ["42", ""]]
    with pytest.raises(CrescoProtocolError):
        _rows({"not": "a list"}, ["region"])


def test_unclassified_failure_falls_back_to_protocol_summary():
    from agents.cresco.mcp_tools import _failure_result
    from agents.cresco.wsapi_client import CrescoError

    result = _failure_result(CrescoError("internal detail"))
    assert result["_data"]["summary"] == "Unexpected fabric response."
    assert result["_ui_components"][0]["variant"] == "error"


def test_respond_passthrough_shapes():
    server = MCPServer()
    error_response = server._respond("r", {"_error": {"code": -3, "message": "refused"}})
    assert error_response.error == {"code": -3, "message": "refused"}
    plain_response = server._respond("r", {"plain": True})
    assert plain_response.result == {"plain": True}
