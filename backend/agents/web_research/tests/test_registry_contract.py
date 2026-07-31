"""Registry/schema contract tests for the Web Research agent (feature 029)."""
from agents.web_research import mcp_tools
from agents.web_research.mcp_server import MCPServer
from agents.web_research.web_research_agent import PORT_ENV_VAR, WebResearchAgent
from shared.external_http import EgressBlockedError, ServiceUnreachableError
from shared.protocol import MCPRequest

EXPECTED_TOOLS = {"_credentials_check", "web_search", "fetch_page", "research_brief"}


def test_registry_has_contracted_tools() -> None:
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == EXPECTED_TOOLS


def test_every_registry_entry_is_well_formed() -> None:
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        assert callable(info["function"]), name
        assert str(info["description"]).strip(), name
        schema = info["input_schema"]
        assert schema["type"] == "object", name
        assert isinstance(schema.get("properties", {}), dict), name
        assert info["scope"] in {"tools:read", "tools:search"}, name


def test_web_search_schema_matches_contract() -> None:
    schema = mcp_tools.TOOL_REGISTRY["web_search"]["input_schema"]
    assert schema["required"] == ["query"]
    max_results = schema["properties"]["max_results"]
    assert max_results["default"] == 8
    assert max_results["maximum"] == 20
    assert mcp_tools.TOOL_REGISTRY["web_search"]["scope"] == "tools:search"


def test_fetch_page_schema_matches_contract() -> None:
    schema = mcp_tools.TOOL_REGISTRY["fetch_page"]["input_schema"]
    assert schema["required"] == ["url"]
    assert mcp_tools.TOOL_REGISTRY["fetch_page"]["scope"] == "tools:read"


def test_research_brief_schema_matches_contract() -> None:
    schema = mcp_tools.TOOL_REGISTRY["research_brief"]["input_schema"]
    assert schema["required"] == ["topic"]
    depth = schema["properties"]["depth"]
    assert depth["enum"] == ["shallow", "standard"]
    assert depth["default"] == "standard"


def test_brief_fetch_bounds() -> None:
    """<= 5 fetches per brief; 1 MB / 15 s per fetch (FR-013)."""
    assert mcp_tools.BRIEF_FETCHES == {"shallow": 2, "standard": 5}
    assert mcp_tools.FETCH_MAX_BYTES == 1024 * 1024
    assert mcp_tools.FETCH_TIMEOUT_S == 15


def test_agent_card_attributes() -> None:
    assert WebResearchAgent.agent_id == "web-research-1"
    assert WebResearchAgent.service_name == "Web Research"
    assert WebResearchAgent.skill_tags == ["research", "web", "search", "sources", "brief"]
    assert PORT_ENV_VAR == "WEB_RESEARCH_AGENT_PORT"


def test_search_credential_bundle_is_optional() -> None:
    creds = WebResearchAgent.card_metadata["required_credentials"]
    assert {c["key"] for c in creds} == {"SEARCH_API_URL", "SEARCH_API_KEY"}
    assert all(c["required"] is False for c in creds)


def test_mcp_server_tool_list_matches_registry() -> None:
    server = MCPServer()
    response = server.process_request(
        MCPRequest(request_id="r1", method="tools/list", params={})
    )
    names = {t["name"] for t in response.result["tools"]}
    assert names == EXPECTED_TOOLS


def test_mcp_server_unknown_tool_is_not_retryable() -> None:
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r2", method="tools/call",
        params={"name": "nope", "arguments": {}},
    ))
    assert response.error["code"] == -32601
    assert response.error["retryable"] is False


def test_mcp_server_surfaces_error_alerts_without_renderable_error_payload() -> None:
    """A tool-level error Alert becomes an MCP error with no renderable body."""
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r3", method="tools/call",
        params={"name": "web_search", "arguments": {"query": ""}},
    ))
    assert response.error is not None
    assert response.ui_components is None
    assert "non-empty 'query'" in response.error["message"]


def test_error_classification() -> None:
    assert MCPServer._classify_error(EgressBlockedError("blocked")) is False
    assert MCPServer._classify_error(ServiceUnreachableError("down")) is True
    assert MCPServer._classify_error(ValueError("bad args")) is False
    assert MCPServer._classify_error(ConnectionError("reset")) is True
    assert MCPServer._classify_error(RuntimeError("unknown")) is True


def test_agent_instantiates_and_card_exposes_tools() -> None:
    """Plug-and-play contract: the agent card is built from the registry."""
    agent = WebResearchAgent(port=65123)
    assert agent.card.agent_id == "web-research-1"
    skill_names = {skill.name for skill in agent.card.skills}
    assert {"web_search", "fetch_page", "research_brief"} <= skill_names


def test_mcp_server_plain_dict_result_passthrough() -> None:
    """Tools returning a plain dict (no _ui_components) pass through as result."""
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r4", method="tools/call",
        params={"name": "_credentials_check", "arguments": {}},
    ))
    assert response.error is None
    assert response.result["credential_test"] == "ok"


def test_mcp_server_success_path_returns_data_and_ui(monkeypatch) -> None:
    def fake_tool(**_kwargs):
        return {"_ui_components": [{"type": "card", "title": "ok"}],
                "_data": {"answer": 42}}
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["web_search"], "function", fake_tool)
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r5", method="tools/call",
        params={"name": "web_search", "arguments": {"query": "q"}},
    ))
    assert response.error is None
    assert response.result == {"answer": 42}
    assert response.ui_components[0]["title"] == "ok"


def test_mcp_server_tool_exception_classified(monkeypatch) -> None:
    def boom(**_kwargs):
        raise ValueError("bad input")
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["web_search"], "function", boom)
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r6", method="tools/call",
        params={"name": "web_search", "arguments": {"query": "q"}},
    ))
    assert response.error["retryable"] is False
    assert "bad input" in response.error["message"]


def test_mcp_server_unknown_method() -> None:
    server = MCPServer()
    response = server.process_request(
        MCPRequest(request_id="r7", method="tools/destroy", params={})
    )
    assert response.error["code"] == -32601
