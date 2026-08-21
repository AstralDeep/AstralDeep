"""
Backend Unit Tests — Protocol, Primitives, Tools, and Orchestrator.
"""
import os
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# =============================================================================
# PROTOCOL TESTS
# =============================================================================
class TestProtocolMessages:
    """Tests for protocol message types and serialization."""

    def test_mcp_request_structure(self):
        from shared.protocol import MCPRequest
        req = MCPRequest(
            request_id="req-1",
            method="tools/call",
            params={"name": "search_patients", "arguments": {"min_age": 30}}
        )
        assert req.type == "mcp_request"
        assert req.request_id == "req-1"
        assert req.method == "tools/call"
        assert req.params["name"] == "search_patients"

    def test_mcp_request_json_roundtrip(self):
        from shared.protocol import MCPRequest, Message
        req = MCPRequest(request_id="r1", method="tools/call", params={"name": "test"})
        json_str = req.to_json()
        parsed = Message.from_json(json_str)
        assert isinstance(parsed, MCPRequest)
        assert parsed.request_id == "r1"
        assert parsed.params["name"] == "test"

    def test_mcp_response_structure(self):
        from shared.protocol import MCPResponse
        resp = MCPResponse(request_id="r1", result={"data": [1, 2]}, ui_components=[{"type": "text"}])
        assert resp.type == "mcp_response"
        assert resp.result == {"data": [1, 2]}
        assert len(resp.ui_components) == 1

    def test_mcp_response_with_error(self):
        from shared.protocol import MCPResponse
        resp = MCPResponse(request_id="r1", error={"code": -32601, "message": "Not found"})
        assert resp.error["code"] == -32601

    def test_ui_event_structure(self):
        from shared.protocol import UIEvent
        evt = UIEvent(action="chat_message", payload={"message": "hello"})
        assert evt.type == "ui_event"
        assert evt.action == "chat_message"
        assert evt.payload["message"] == "hello"

    def test_ui_render_structure(self):
        from shared.protocol import UIRender
        render = UIRender(components=[{"type": "card", "title": "Test"}])
        assert render.type == "ui_render"
        assert len(render.components) == 1

    def test_register_agent_roundtrip(self):
        from shared.protocol import RegisterAgent, AgentCard, AgentSkill
        card = AgentCard(
            name="Test Agent",
            description="A test agent",
            agent_id="test-1",
            skills=[AgentSkill(name="Search", description="Search stuff", id="search")]
        )
        reg = RegisterAgent(agent_card=card)
        json_str = reg.to_json()
        parsed = RegisterAgent.from_json(json_str)
        assert parsed.agent_card.name == "Test Agent"
        assert parsed.agent_card.agent_id == "test-1"
        assert len(parsed.agent_card.skills) == 1
        assert parsed.agent_card.skills[0].id == "search"

    def test_register_ui_roundtrip(self):
        from shared.protocol import RegisterUI
        reg = RegisterUI(capabilities=["render", "stream"], session_id="ui-123")
        json_str = reg.to_json()
        parsed = RegisterUI.from_json(json_str)
        assert parsed.session_id == "ui-123"
        assert "render" in parsed.capabilities

    def test_agent_card_to_dict(self):
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="GP", description="General", agent_id="gp-1",
            skills=[AgentSkill(name="S1", description="Skill 1", id="s1", tags=["tag1"])]
        )
        d = card.to_dict()
        assert d["name"] == "GP"
        assert d["skills"][0]["id"] == "s1"
        assert d["skills"][0]["tags"] == ["tag1"]

    def test_message_from_json_dispatches(self):
        from shared.protocol import Message, UIEvent, UIRender
        evt = Message.from_json('{"type": "ui_event", "action": "test", "payload": {}}')
        assert isinstance(evt, UIEvent)
        render = Message.from_json('{"type": "ui_render", "components": []}')
        assert isinstance(render, UIRender)


# =============================================================================
# PRIMITIVES TESTS
# =============================================================================
class TestPrimitives:
    """Tests for UI Primitives serialization."""

    def test_text_serialization(self):
        from astralprims import Text
        t = Text(content="Hello", variant="h1", id="t1")
        d = t.to_dict()
        assert d["type"] == "text"
        assert d["content"] == "Hello"
        assert d["variant"] == "h1"

    def test_card_with_children(self):
        from astralprims import Card, Text
        card = Card(title="Test Card", content=[
            Text(content="Body text", variant="body")
        ])
        d = card.to_dict()
        assert d["type"] == "card"
        assert d["title"] == "Test Card"
        assert len(d["content"]) == 1
        assert d["content"][0]["type"] == "text"

    def test_table_serialization(self):
        from astralprims import Table
        t = Table(headers=["A", "B"], rows=[["1", "2"], ["3", "4"]])
        d = t.to_dict()
        assert d["headers"] == ["A", "B"]
        assert len(d["rows"]) == 2

    def test_metric_card(self):
        from astralprims import MetricCard
        m = MetricCard(title="CPU", value="45%", progress=0.45, variant="warning")
        d = m.to_dict()
        assert d["title"] == "CPU"
        assert d["progress"] == 0.45

    def test_grid_with_children(self):
        from astralprims import Grid, MetricCard
        g = Grid(columns=2, children=[
            MetricCard(title="A", value="1"),
            MetricCard(title="B", value="2"),
        ])
        d = g.to_dict()
        assert d["columns"] == 2
        assert len(d["children"]) == 2

    def test_bar_chart(self):
        from astralprims import BarChart
        c = BarChart(title="Ages", labels=["A", "B"], datasets=[{"label": "Age", "data": [30, 40]}])
        d = c.to_dict()
        assert d["type"] == "bar_chart"
        assert len(d["labels"]) == 2

    def test_line_chart(self):
        from astralprims import LineChart
        c = LineChart(title="HR", labels=["A"], datasets=[{"label": "HR", "data": [70]}])
        d = c.to_dict()
        assert d["type"] == "line_chart"

    def test_pie_chart(self):
        from astralprims import PieChart
        c = PieChart(title="Status", labels=["A", "B"], data=[60, 40], colors=["#f00", "#0f0"])
        d = c.to_dict()
        assert d["type"] == "pie_chart"
        assert d["data"] == [60, 40]

    def test_alert(self):
        from astralprims import Alert
        a = Alert(message="Error!", variant="error", title="Oh no")
        d = a.to_dict()
        assert d["variant"] == "error"
        assert d["title"] == "Oh no"

    def test_create_ui_response(self):
        from astralprims import Text, create_ui_response
        result = create_ui_response([Text(content="Hello")])
        assert "_ui_components" in result
        assert len(result["_ui_components"]) == 1

    def test_component_from_json(self):
        from astralprims import Primitive, Text
        data = {"type": "text", "content": "Hi", "variant": "body"}
        c = Primitive.from_dict(data)
        assert isinstance(c, Text)
        assert c.content == "Hi"


# =============================================================================
# TOOLS TESTS
# =============================================================================
class TestMCPTools:
    """Tests for MCP tool functions."""

    def test_search_patients_all(self):
        from agents.medical.mcp_tools import search_patients
        result = search_patients()
        assert "_ui_components" in result
        assert "_data" in result
        assert result["_data"]["total"] == 10  # All mock patients

    def test_search_patients_age_filter(self):
        from agents.medical.mcp_tools import search_patients
        result = search_patients(min_age=30)
        assert result["_data"]["total"] > 0
        for p in result["_data"]["patients"]:
            assert p["age"] >= 30

    def test_search_patients_no_results(self):
        from agents.medical.mcp_tools import search_patients
        result = search_patients(min_age=200)
        assert "_ui_components" in result
        # Should contain an alert
        comp = result["_ui_components"][0]
        assert comp["type"] == "alert"

    def test_search_patients_condition_filter(self):
        from agents.medical.mcp_tools import search_patients
        result = search_patients(condition="Degenerative")
        assert result["_data"]["total"] > 0
        for p in result["_data"]["patients"]:
            assert "Degenerative" in p["condition"]

    def test_generate_dynamic_chart_bar(self):
        from agents.general.mcp_tools import generate_dynamic_chart
        data = [{"name": "A", "value": 10}, {"name": "B", "value": 20}]
        result = generate_dynamic_chart(data=data, x_key="name", y_key="value", chart_type="bar")
        assert "_ui_components" in result

    def test_generate_dynamic_chart_pie(self):
        from agents.general.mcp_tools import generate_dynamic_chart
        data = [{"name": "A", "value": 10}, {"name": "B", "value": 20}]
        result = generate_dynamic_chart(data=data, x_key="name", y_key="value", chart_type="pie")
        assert "_ui_components" in result

    def test_generate_dynamic_chart_line(self):
        from agents.general.mcp_tools import generate_dynamic_chart
        data = [{"name": "A", "value": 10}, {"name": "B", "value": 20}]
        result = generate_dynamic_chart(data=data, x_key="name", y_key="value", chart_type="line")
        assert "_ui_components" in result

    def test_system_status(self):
        from agents.general.mcp_tools import get_system_status
        result = get_system_status()
        assert "_ui_components" in result
        assert "_data" in result
        assert "cpu_percent" in result["_data"]
        assert "memory_percent" in result["_data"]

    def test_cpu_info(self):
        from agents.general.mcp_tools import get_cpu_info
        result = get_cpu_info()
        assert "_ui_components" in result
        assert result["_data"]["cores"] > 0

    def test_memory_info(self):
        from agents.general.mcp_tools import get_memory_info
        result = get_memory_info()
        assert "_ui_components" in result
        assert "ram_percent" in result["_data"]

    def test_disk_info(self):
        from agents.general.mcp_tools import get_disk_info
        result = get_disk_info()
        assert "_ui_components" in result

    def test_tool_registry_completeness(self):
        from agents.general.mcp_tools import TOOL_REGISTRY
        expected = ["generate_dynamic_chart", "get_system_status",
                     "get_cpu_info", "get_memory_info", "get_disk_info", "search_wikipedia"]
        for name in expected:
            assert name in TOOL_REGISTRY, f"Missing tool: {name}"
            assert "function" in TOOL_REGISTRY[name]
            assert "description" in TOOL_REGISTRY[name]
            assert "input_schema" in TOOL_REGISTRY[name]


# =============================================================================
# MCP SERVER TESTS
# =============================================================================
class TestMCPServer:
    """Tests for MCP Server dispatch."""

    def test_tools_list(self):
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        req = MCPRequest(request_id="r1", method="tools/list", params={})
        resp = server.process_request(req)
        assert resp.result is not None
        # Registry grows over time as new tools are added; assert the
        # expected core tools are present rather than a fixed count.
        names = {t["name"] for t in resp.result["tools"]}
        for required in {
            "generate_dynamic_chart", "get_system_status",
            # File-upload tools (feature 002-file-uploads)
            "read_document", "read_spreadsheet", "read_presentation",
            "read_text", "read_image", "list_attachments",
        }:
            assert required in names, f"missing tool: {required}"

    def test_tool_call_success(self):
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        req = MCPRequest(
            request_id="r2",
            method="tools/call",
            params={"name": "get_system_status", "arguments": {}}
        )
        resp = server.process_request(req)
        assert resp.error is None
        assert resp.ui_components is not None
        assert len(resp.ui_components) > 0

    def test_tool_call_unknown(self):
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        req = MCPRequest(
            request_id="r3",
            method="tools/call",
            params={"name": "nonexistent_tool", "arguments": {}}
        )
        resp = server.process_request(req)
        assert resp.error is not None
        assert "Unknown tool" in resp.error["message"]
        assert resp.error.get("retryable") is False

    def test_unknown_method(self):
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        req = MCPRequest(request_id="r4", method="unknown/method", params={})
        resp = server.process_request(req)
        assert resp.error is not None
        assert "Unknown method" in resp.error["message"]
        assert resp.error.get("retryable") is False


# =============================================================================
# MCP SERVER ERROR CLASSIFICATION TESTS
# =============================================================================
class TestMCPServerErrorClassification:
    """Tests for MCP server error classification (retryable vs non-retryable)."""

    def test_retryable_connection_error(self):
        """ConnectionError should be classified as retryable."""
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        # Register a mock tool that raises ConnectionError.
        # server.tools IS the module-level TOOL_REGISTRY — pop the key after,
        # or the pollution fails test_no_behavior_change's registry guard.
        server.tools["failing_tool"] = {
            "function": lambda: (_ for _ in ()).throw(ConnectionError("Connection refused")),
            "description": "A tool that fails with ConnectionError",
            "input_schema": {"type": "object", "properties": {}}
        }
        try:
            req = MCPRequest(request_id="r-retry", method="tools/call",
                             params={"name": "failing_tool", "arguments": {}})
            resp = server.process_request(req)
            assert resp.error is not None
            assert resp.error.get("retryable") is True
        finally:
            server.tools.pop("failing_tool", None)

    def test_non_retryable_type_error(self):
        """TypeError should be classified as non-retryable."""
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        server = MCPServer()
        server.tools["bad_args_tool"] = {
            "function": lambda: (_ for _ in ()).throw(TypeError("missing argument")),
            "description": "A tool that fails with TypeError",
            "input_schema": {"type": "object", "properties": {}}
        }
        try:
            req = MCPRequest(request_id="r-noretry", method="tools/call",
                             params={"name": "bad_args_tool", "arguments": {}})
            resp = server.process_request(req)
            assert resp.error is not None
            assert resp.error.get("retryable") is False
        finally:
            server.tools.pop("bad_args_tool", None)

    def test_tool_alert_error_detection(self):
        """Tool returning Alert with variant='error' should be detected as an error."""
        from agents.general.mcp_server import MCPServer
        from shared.protocol import MCPRequest
        from astralprims import Alert, create_ui_response
        server = MCPServer()
        # Register a tool that returns an error alert (like Wikipedia does on failure)
        server.tools["alert_tool"] = {
            "function": lambda: create_ui_response([
                Alert(message="Something went wrong", variant="error", title="Error")
            ]),
            "description": "A tool that returns an error alert",
            "input_schema": {"type": "object", "properties": {}}
        }
        try:
            req = MCPRequest(request_id="r-alert", method="tools/call",
                             params={"name": "alert_tool", "arguments": {}})
            resp = server.process_request(req)
            assert resp.error is not None
            assert resp.error.get("retryable") is True
            assert "Something went wrong" in resp.error["message"]
        finally:
            server.tools.pop("alert_tool", None)

    def test_classify_error_static_method(self):
        """Test the _classify_error static method directly."""
        from agents.general.mcp_server import MCPServer
        assert MCPServer._classify_error(ConnectionError()) is True
        assert MCPServer._classify_error(TimeoutError()) is True
        assert MCPServer._classify_error(TypeError()) is False
        assert MCPServer._classify_error(KeyError()) is False
        assert MCPServer._classify_error(ValueError()) is False
        # Unknown errors default to retryable
        assert MCPServer._classify_error(RuntimeError()) is True


# =============================================================================
# ORCHESTRATOR RETRY TESTS
# =============================================================================
class TestOrchestratorRetry:
    """Tests for the orchestrator retry wrapper."""

    @pytest.fixture
    def orchestrator(self):
        """Create a minimal orchestrator for retry testing."""
        from orchestrator.orchestrator import Orchestrator

        # The retry wrapper is a unit seam: constructing the full application
        # graph here binds process-global Plane consumers that these tests do
        # not exercise. Keep the fake exact to the state this method reads so
        # repeated cases cannot leak one test's runtime binding into the next.
        orch = Orchestrator.__new__(Orchestrator)
        orch.MAX_RETRIES = 3
        orch.RETRY_BACKOFF = (0.001, 0.001, 0.001)
        orch.ui_sessions = {}
        return orch

    @pytest.mark.asyncio
    async def test_retry_success_on_second_attempt(self, orchestrator):
        """Tool succeeds on second attempt after first failure."""
        from shared.protocol import MCPResponse

        call_count = 0
        async def mock_execute(agent_id, tool_name, args, timeout=30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MCPResponse(request_id="r1",
                                   error={"message": "Temporary failure", "retryable": True})
            return MCPResponse(request_id="r2",
                               result={"data": "success"},
                               ui_components=[{"type": "text", "content": "OK"}])

        orchestrator.execute_tool_and_wait = mock_execute
        ws = AsyncMock()

        result = await orchestrator._execute_with_retry(ws, "agent-1", "test_tool", {})
        assert result is not None
        assert result.error is None
        assert result.ui_components is not None
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, orchestrator):
        """All 3 attempts fail — returns last error."""
        from shared.protocol import MCPResponse

        call_count = 0
        async def mock_execute(agent_id, tool_name, args, timeout=30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            return MCPResponse(request_id=f"r{call_count}",
                               error={"message": f"Failure #{call_count}", "retryable": True})

        orchestrator.execute_tool_and_wait = mock_execute
        ws = AsyncMock()

        result = await orchestrator._execute_with_retry(ws, "agent-1", "test_tool", {},
                                                         max_retries=3)
        assert result is not None
        assert result.error is not None
        assert "Failure #3" in result.error["message"]
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_stops_immediately(self, orchestrator):
        """Non-retryable error stops after first attempt."""
        from shared.protocol import MCPResponse

        call_count = 0
        async def mock_execute(agent_id, tool_name, args, timeout=30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            return MCPResponse(request_id="r1",
                               error={"message": "Bad arguments", "retryable": False})

        orchestrator.execute_tool_and_wait = mock_execute
        ws = AsyncMock()

        result = await orchestrator._execute_with_retry(ws, "agent-1", "test_tool", {})
        assert result.error is not None
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_sends_status_updates(self, orchestrator):
        """UI should receive 'retrying' status messages during retries."""
        from shared.protocol import MCPResponse

        call_count = 0
        async def mock_execute(agent_id, tool_name, args, timeout=30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return MCPResponse(request_id=f"r{call_count}",
                                   error={"message": "Temp error", "retryable": True})
            return MCPResponse(request_id="r3", result={"ok": True},
                               ui_components=[{"type": "text"}])

        orchestrator.execute_tool_and_wait = mock_execute
        ws = AsyncMock()
        # _safe_send checks for send_text (FastAPI WebSocket) first
        ws.send_text = AsyncMock()

        result = await orchestrator._execute_with_retry(ws, "agent-1", "test_tool", {},
                                                         max_retries=3)
        assert result.error is None

        # Check that ws.send_text was called with retrying status
        status_calls = [
            call for call in ws.send_text.call_args_list
            if '"retrying"' in str(call)
        ]
        assert len(status_calls) == 2  # Two retries before success


# =============================================================================
# WIKIPEDIA HTTP ERROR TEST
# =============================================================================
class TestWikipediaRobustness:
    """Tests for Wikipedia tool HTTP error handling."""

    @patch('agents.general.mcp_tools.requests.get')
    def test_wikipedia_http_500(self, mock_get):
        """HTTP 500 error should raise an exception."""
        from agents.general.mcp_tools import search_wikipedia
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_get.return_value = mock_resp

        result = search_wikipedia(query="test")
        assert "_ui_components" in result
        comp = result["_ui_components"][0]
        assert comp["variant"] == "error"
        assert "500 Server Error" in comp["message"]

    @patch('agents.general.mcp_tools.requests.get')
    def test_wikipedia_success(self, mock_get):
        """Successful Wikipedia response should return proper UI components."""
        from agents.general.mcp_tools import search_wikipedia
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "query": {
                "search": [
                    {"title": "Test Article", "snippet": "A test snippet", "pageid": 123}
                ]
            }
        }
        mock_get.return_value = mock_resp

        result = search_wikipedia(query="test")
        assert "_ui_components" in result
        assert "_data" in result
        assert result["_data"]["results"][0]["title"] == "Test Article"


# =============================================================================
# LLM ROUTING TESTS (mocked)
# =============================================================================
class TestLLMRouting:
    """Tests for LLM-powered tool routing with mocked OpenAI client."""

    @pytest.fixture
    def orchestrator(self):
        """Create an orchestrator with mocked LLM client."""
        from orchestrator.orchestrator import Orchestrator

        # These tests cover only tool metadata and pending-request bookkeeping;
        # keep them independent of the application-scoped Plane runtime.
        orch = Orchestrator.__new__(Orchestrator)
        orch.agent_cards = {}
        orch.agent_capabilities = {}
        orch.agents = {}
        orch.pending_requests = {}

        # Register a fake agent with capabilities
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="Test Agent", description="Test", agent_id="test-1",
            skills=[
                AgentSkill(name="Search patients", description="Search patients", id="search_patients",
                           input_schema={"type": "object", "properties": {"min_age": {"type": "integer"}}}),
                AgentSkill(name="Graph patients", description="Graph patient data", id="graph_patient_data",
                           input_schema={"type": "object", "properties": {"metric": {"type": "string"}}}),
            ]
        )
        orch.agent_cards["test-1"] = card
        orch.agent_capabilities["test-1"] = [
            {"name": "search_patients", "description": "Search patients",
             "input_schema": {"type": "object", "properties": {"min_age": {"type": "integer"}}}},
            {"name": "graph_patient_data", "description": "Graph patient data",
             "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}}},
        ]
        # Register connected agent mock (required for handle_chat_message tool building)
        orch.agents["test-1"] = MagicMock()
        return orch

    def test_tool_definitions_built_correctly(self, orchestrator):
        """Verify the orchestrator builds correct OpenAI tool definitions."""
        tools_desc = []
        for agent_id, card in orchestrator.agent_cards.items():
            for skill in card.skills:
                tools_desc.append({
                    "type": "function",
                    "function": {
                        "name": skill.id,
                        "description": skill.description,
                        "parameters": skill.input_schema or {}
                    }
                })
        assert len(tools_desc) == 2
        assert tools_desc[0]["function"]["name"] == "search_patients"
        assert tools_desc[1]["function"]["name"] == "graph_patient_data"

    def test_pending_request_lifecycle(self, orchestrator):
        """Test that pending requests can be created and resolved."""
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()

            orchestrator.pending_requests["req-test"] = future
            assert "req-test" in orchestrator.pending_requests

            orchestrator.pending_requests.pop("req-test", None)
            assert "req-test" not in orchestrator.pending_requests
        finally:
            loop.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
