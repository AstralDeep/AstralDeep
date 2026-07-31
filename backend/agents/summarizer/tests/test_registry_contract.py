"""Registry/schema contract tests for the Summarizer agent (feature 029)."""
from agents.summarizer import mcp_tools
from agents.summarizer.mcp_server import MCPServer
from agents.summarizer.summarizer_agent import PORT_ENV_VAR, SummarizerAgent
from shared.protocol import MCPRequest

EXPECTED_TOOLS = {"summarize_text", "summarize_url", "compare_documents"}


def test_registry_has_contracted_tools() -> None:
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == EXPECTED_TOOLS


def test_every_registry_entry_is_well_formed() -> None:
    for name, info in mcp_tools.TOOL_REGISTRY.items():
        assert callable(info["function"]), name
        assert str(info["description"]).strip(), name
        schema = info["input_schema"]
        assert schema["type"] == "object", name
        assert isinstance(schema.get("properties", {}), dict), name
        assert info["scope"] == "tools:read", name


def test_summarize_text_schema() -> None:
    schema = mcp_tools.TOOL_REGISTRY["summarize_text"]["input_schema"]
    assert schema["required"] == ["text"]
    assert "focus" in schema["properties"]


def test_summarize_url_schema() -> None:
    schema = mcp_tools.TOOL_REGISTRY["summarize_url"]["input_schema"]
    assert schema["required"] == ["url"]


def test_compare_documents_schema() -> None:
    schema = mcp_tools.TOOL_REGISTRY["compare_documents"]["input_schema"]
    assert schema["required"] == ["text_a", "text_b"]
    labels = schema["properties"]["labels"]
    assert labels["type"] == "array"
    assert labels["minItems"] == 2 and labels["maxItems"] == 2


def test_bounds() -> None:
    """24k-char input cap; 1 MB / 15 s fetch bounds (FR-013/FR-014)."""
    assert mcp_tools.INPUT_CAP == 24_000
    assert mcp_tools.FETCH_MAX_BYTES == 1024 * 1024
    assert mcp_tools.FETCH_TIMEOUT_S == 15


def test_agent_card_attributes() -> None:
    assert SummarizerAgent.agent_id == "summarizer-1"
    assert SummarizerAgent.service_name == "Summarizer"
    assert SummarizerAgent.skill_tags == ["summarize", "digest", "compare", "tldr"]
    assert PORT_ENV_VAR == "SUMMARIZER_AGENT_PORT"


def test_agent_requires_no_credentials() -> None:
    metadata = getattr(SummarizerAgent, "card_metadata", {}) or {}
    assert not metadata.get("required_credentials")


def test_mcp_server_tool_list_matches_registry() -> None:
    server = MCPServer()
    response = server.process_request(
        MCPRequest(request_id="r1", method="tools/list", params={})
    )
    names = {t["name"] for t in response.result["tools"]}
    assert names == EXPECTED_TOOLS


def test_mcp_server_surfaces_error_alerts_without_renderable_error_payload() -> None:
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r2", method="tools/call",
        params={"name": "summarize_text", "arguments": {"text": ""}},
    ))
    assert response.error is not None
    assert response.ui_components is None
    assert "non-empty 'text'" in response.error["message"]


def test_mcp_server_unknown_method() -> None:
    server = MCPServer()
    response = server.process_request(
        MCPRequest(request_id="r3", method="tools/destroy", params={})
    )
    assert response.error["code"] == -32601


def test_mcp_server_unknown_tool_is_not_retryable() -> None:
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r4", method="tools/call",
        params={"name": "nope", "arguments": {}},
    ))
    assert response.error["code"] == -32601
    assert response.error["retryable"] is False


def test_mcp_server_success_path_returns_data_and_ui(monkeypatch) -> None:
    def fake_tool(**_kwargs):
        return {"_ui_components": [{"type": "tabs", "tabs": []}],
                "_data": {"summary": "ok"}}
    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["summarize_text"], "function", fake_tool)
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r5", method="tools/call",
        params={"name": "summarize_text", "arguments": {"text": "hello"}},
    ))
    assert response.error is None
    assert response.result == {"summary": "ok"}
    assert response.ui_components[0]["type"] == "tabs"


def test_mcp_server_plain_result_passthrough(monkeypatch) -> None:
    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["summarize_text"], "function",
        lambda **_kwargs: {"plain": True})
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r6", method="tools/call",
        params={"name": "summarize_text", "arguments": {"text": "hello"}},
    ))
    assert response.error is None
    assert response.result == {"plain": True}


def test_mcp_server_tool_exception_classified(monkeypatch) -> None:
    def boom(**_kwargs):
        raise TypeError("wrong type")
    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["summarize_text"], "function", boom)
    server = MCPServer()
    response = server.process_request(MCPRequest(
        request_id="r7", method="tools/call",
        params={"name": "summarize_text", "arguments": {"text": "hello"}},
    ))
    assert response.error["retryable"] is False


def test_error_classification() -> None:
    from shared.external_http import EgressBlockedError
    assert MCPServer._classify_error(EgressBlockedError("blocked")) is False
    assert MCPServer._classify_error(ConnectionError("down")) is True
    assert MCPServer._classify_error(RuntimeError("unknown")) is True


def test_agent_instantiates_and_card_exposes_tools() -> None:
    """Plug-and-play contract: the agent card is built from the registry."""
    agent = SummarizerAgent(port=65124)
    assert agent.card.agent_id == "summarizer-1"
    skill_names = {skill.name for skill in agent.card.skills}
    assert skill_names == EXPECTED_TOOLS
