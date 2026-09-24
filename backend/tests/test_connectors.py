"""Tests for the Claude Connectors agent (backend/agents/connectors/mcp_server.py,
mcp_tools_office.py, mcp_tools_dev.py, mcp_tools_creative.py):
Office/Outlook/dev/creative tool handlers and the MCP dispatcher's tool list.
"""

import json
import os
import shutil
from unittest.mock import patch

import pytest

from agents.connectors.mcp_tools_office import (
    handle_excel_generate,
    handle_ppt_outline,
    handle_word_document,
    handle_outlook_email,
    handle_outlook_credentials_check,
    handle_pitch_template,
)
from agents.connectors.mcp_tools_dev import (
    handle_code_review,
    handle_constitution_critique,
)
from agents.connectors.mcp_tools_runtime import handle_adaptive_routing
from agents.connectors.mcp_tools_creative import (
    handle_blender,
    handle_adobe,
    handle_adobe_credentials_check,
    handle_canva,
    handle_canva_credentials_check,
    handle_artifacts,
    handle_graphs,
    handle_design,
)
from agents.connectors.mcp_server import ConnectorsMCPServer, TOOL_REGISTRY
from shared.protocol import MCPRequest


def _comps(result):
    return result["_ui_components"]


def _by_type(comps, typename):
    return [c for c in comps if c.get("type") == typename]


@pytest.fixture
def session_args(monkeypatch):
    def _fake_write(args, filename, contents):
        sid = args.get("session_id", "default")
        return f"/api/download/{sid}/{filename}"

    monkeypatch.setattr(
        "agents.connectors.mcp_tools_office._write_download_file",
        _fake_write,
    )
    return {"user_id": "u1", "session_id": "s1"}


class TestExcel:
    def test_generates_table_and_download(self, session_args):
        result = handle_excel_generate({
            **session_args,
            "title": "Test Data",
            "columns": ["Name", "Value"],
            "rows": [["A", "1"], ["B", "2"]],
        })
        comps = _comps(result)
        assert len(_by_type(comps, "table")) == 1
        downloads = _by_type(comps, "file_download")
        assert len(downloads) == 1
        assert downloads[0]["filename"] == "Test_Data.csv"

    def test_download_url_is_relative(self, session_args):
        result = handle_excel_generate({
            **session_args,
            "title": "X",
            "columns": ["a"],
            "rows": [["1"]],
        })
        download = _by_type(_comps(result), "file_download")[0]
        assert download["url"] == "/api/download/s1/X.csv"

    def test_real_writer_returns_relative_url(self):
        import agents.connectors.mcp_tools_office as office

        url = office._write_download_file(
            {"user_id": "dl-test-user", "session_id": "dl-test-sess"},
            "f.txt",
            b"hello",
        )
        assert url == "/api/download/dl-test-sess/f.txt"
        backend_dir = os.path.abspath(
            os.path.join(os.path.dirname(office.__file__), "..", "..")
        )
        written = os.path.join(backend_dir, "tmp", "dl-test-user", "dl-test-sess", "f.txt")
        assert os.path.exists(written)
        with open(written, "rb") as f:
            assert f.read() == b"hello"
        shutil.rmtree(os.path.join(backend_dir, "tmp", "dl-test-user", "dl-test-sess"),
                      ignore_errors=True)

    def test_description_or_title_in_header(self, session_args):
        result = handle_excel_generate({
            **session_args,
            "title": "No Desc",
            "columns": ["X"],
            "rows": [["1"]],
        })
        texts = _by_type(_comps(result), "text")
        assert any("No Desc" in t.get("content", "") for t in texts)


class TestPowerPoint:
    def test_generates_slide_collapsibles(self):
        result = handle_ppt_outline({
            "title": "My Deck",
            "slides": [
                {"title": "Intro", "bullets": ["Hello", "World"]},
                {"title": "Conclusion", "bullets": ["Thanks"]},
            ],
        })
        comps = _comps(result)
        collapsibles = _by_type(comps, "collapsible")
        assert len(collapsibles) == 2

    def test_description_rendered(self):
        result = handle_ppt_outline({
            "title": "Deck",
            "slides": [],
            "description": "Subtitle",
        })
        texts = _by_type(_comps(result), "text")
        assert any("Subtitle" in t.get("content", "") for t in texts)


class TestWord:
    def test_generates_collapsible_sections(self, session_args):
        result = handle_word_document({
            **session_args,
            "title": "Report",
            "sections": [{"heading": "Section 1", "content": "Hello world"}],
        })
        comps = _comps(result)
        collapsibles = _by_type(comps, "collapsible")
        assert len(collapsibles) == 1
        assert collapsibles[0]["title"] == "Section 1"

    def test_download_button_by_default(self, session_args):
        result = handle_word_document({
            **session_args,
            "title": "Doc",
            "sections": [{"heading": "A", "content": "B"}],
        })
        downloads = _by_type(_comps(result), "file_download")
        assert len(downloads) == 1
        assert downloads[0]["url"] == "/api/download/s1/Doc.md"

    def test_download_can_be_disabled(self, session_args):
        result = handle_word_document({
            **session_args,
            "title": "Doc",
            "sections": [{"heading": "A", "content": "B"}],
            "include_download": False,
        })
        assert len(_by_type(_comps(result), "file_download")) == 0


class TestOutlook:
    def test_preview_only_without_credentials(self):
        result = handle_outlook_email({
            "to": "alice@example.com",
            "subject": "Hello",
            "body": "This is the body.",
        })
        comps = _comps(result)
        assert len(_by_type(comps, "container")) >= 1
        alerts = _by_type(comps, "alert")
        assert any("Microsoft Graph" in (a.get("message") or "") for a in alerts)

    def test_cc_and_priority_rendered(self):
        result = handle_outlook_email({
            "to": "a@b.com",
            "subject": "Urgent",
            "body": "ASAP",
            "cc": "manager@b.com",
            "priority": "high",
        })
        container = _by_type(_comps(result), "container")[0]
        texts = [c for c in container.get("children", []) if c.get("type") == "text"]
        assert any("high" in t.get("content", "") for t in texts)

    def test_send_requires_credentials(self):
        result = handle_outlook_email({
            "to": "a@b.com",
            "subject": "Hi",
            "body": "Body",
            "send": True,
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("Cannot send" in (a.get("title") or "") for a in alerts)

    def test_send_calls_graph_when_credentialed(self):
        class _Resp:
            status_code = 202
            text = ""
        with patch("agents.connectors.mcp_tools_office.http_request", return_value=_Resp()) as m:
            result = handle_outlook_email({
                "to": "a@b.com",
                "subject": "Hi",
                "body": "Body",
                "send": True,
                "_credentials": {"MS_GRAPH_ACCESS_TOKEN": "fake-token"},
            })
        m.assert_called_once()
        called_method, called_url = m.call_args.args[0], m.call_args.args[1]
        assert called_method == "POST"
        assert called_url.endswith("/me/sendMail")
        alerts = _by_type(_comps(result), "alert")
        assert any((a.get("title") or "") == "Sent" for a in alerts)

    def test_credentials_check_without_token(self):
        verdict = handle_outlook_credentials_check({})
        assert verdict["credential_test"] == "unconfigured"

    def test_credentials_check_ok(self):
        class _Resp:
            status_code = 200
        with patch("agents.connectors.mcp_tools_office.http_request", return_value=_Resp()):
            verdict = handle_outlook_credentials_check({
                "_credentials": {"MS_GRAPH_ACCESS_TOKEN": "x"},
            })
        assert verdict["credential_test"] == "ok"


class TestPitchTemplates:
    def test_all_template_types_exist(self):
        for t in ["startup", "sales", "investor", "product", "project", "strategy"]:
            result = handle_pitch_template({"template_type": t})
            assert len(_comps(result)) > 2

    def test_custom_title(self):
        result = handle_pitch_template({"template_type": "startup", "custom_title": "My Startup"})
        texts = _by_type(_comps(result), "text")
        assert any("My Startup" in t.get("content", "") for t in texts)


class TestCodeReview:
    def test_detects_eval_via_ast(self):
        result = handle_code_review({
            "code": "def f(x):\n    return eval(x)\n",
            "language": "python",
        })
        collapsibles = _by_type(_comps(result), "collapsible")
        sec = next((c for c in collapsibles if c.get("title") == "Security Notes"), None)
        assert sec is not None
        body_texts = [
            t.get("content", "")
            for child in sec.get("content", [])
            for t in child.get("children", [])
        ]
        assert any("eval()" in body and "Line 2" in body for body in body_texts)

    def test_detects_bare_except(self):
        result = handle_code_review({
            "code": "try:\n    f()\nexcept:\n    pass\n",
            "language": "python",
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("bare 'except:'" in (a.get("message") or "") for a in alerts)

    def test_detects_advisory_import(self):
        result = handle_code_review({
            "code": "import pickle\n",
            "language": "python",
        })
        collapsibles = _by_type(_comps(result), "collapsible")
        sec = next((c for c in collapsibles if c.get("title") == "Security Notes"), None)
        assert sec is not None

    def test_syntax_error_falls_back_gracefully(self):
        result = handle_code_review({
            "code": "def f(\n    bad python",
            "language": "python",
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("Python parse error" in (a.get("message") or "") for a in alerts)

    def test_detects_innerhtml(self):
        result = handle_code_review({
            "code": "el.innerHTML = untrusted;",
            "language": "javascript",
        })
        collapsibles = _by_type(_comps(result), "collapsible")
        assert any("Security Notes" in c.get("title", "") for c in collapsibles)

    def test_no_issues_clean_code(self):
        result = handle_code_review({
            "code": "x = 1\ny = 2\nprint(x + y)",
            "language": "python",
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("No issues" in (a.get("title") or "") for a in alerts)


class TestConstitutionCritique:
    def test_detects_missing_tests(self):
        result = handle_constitution_critique({
            "spec": "# Profile Spec\n\nWe will add database tables for user profiles.\n",
            "spec_title": "Profile Spec",
        })
        alerts = _by_type(_comps(result), "alert")
        messages = " ".join(a.get("message", "") for a in alerts)
        assert "test" in messages.lower()

    def test_detects_missing_privacy(self):
        result = handle_constitution_critique({
            "spec": "# User Data Spec\n\nCreate a new endpoint for user data.\n",
            "spec_title": "User Data Spec",
        })
        alerts = _by_type(_comps(result), "alert")
        messages = " ".join(a.get("message", "") for a in alerts)
        assert "privacy" in messages.lower()

    def test_db_change_without_migration_flagged(self):
        result = handle_constitution_critique({
            "spec": (
                "# DB Spec\n## Approach\nAdd a `users` table.\n"
                "## Tests\nCovered.\n## Privacy\nNo PII.\n## Audit\nLogged.\n"
            ),
            "spec_title": "DB Spec",
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("Principle IX" in (a.get("title") or "") for a in alerts)

    def test_clean_spec_passes(self):
        result = handle_constitution_critique({
            "spec": (
                "# Compliant Spec\n## Tests\nPytest covers it.\n"
                "## Privacy\nNo PII or PHI.\n## Audit\nLogged.\n"
                "## Migration\nSchema migrated.\n## Database\nUses Postgres.\n"
            ),
            "spec_title": "Compliant Spec",
        })
        alerts = _by_type(_comps(result), "alert")
        assert any("No issues" in (a.get("title") or "") for a in alerts)


class TestAdaptiveRouting:
    def test_routes_weather_query(self):
        result = handle_adaptive_routing({"query": "what is the weather forecast"})
        assert len(_by_type(_comps(result), "text")) >= 1

    def test_routes_office_query(self):
        result = handle_adaptive_routing({"query": "create an excel spreadsheet"})
        assert len(_comps(result)) >= 2

    def test_no_match_returns_info(self):
        result = handle_adaptive_routing({"query": "xyzzy nothing matches this"})
        alerts = _by_type(_comps(result), "alert")
        assert any("No strong matches" in (a.get("title") or "") for a in alerts)


class TestBlender:
    def test_blender_explains_self_host_requirement(self):
        result = handle_blender({"action": "debug"})
        alerts = _by_type(_comps(result), "alert")
        assert any("no public cloud API" in (a.get("message") or "") for a in alerts)


class TestAdobe:
    def test_adobe_without_credentials(self):
        result = handle_adobe({"app": "photoshop"})
        alerts = _by_type(_comps(result), "alert")
        assert any("Credentials not configured" in (a.get("title") or "") for a in alerts)

    def test_adobe_token_success(self):
        class _Resp:
            status_code = 200
            text = json.dumps({"access_token": "abc"})
        with patch(
            "agents.connectors.mcp_tools_creative._exchange_adobe_ims_token",
            return_value=_Resp(),
        ):
            result = handle_adobe({
                "app": "firefly",
                "_credentials": {
                    "ADOBE_CLIENT_ID": "id",
                    "ADOBE_CLIENT_SECRET": "secret",
                },
            })
        alerts = _by_type(_comps(result), "alert")
        assert any("Credentials verified" in (a.get("title") or "") for a in alerts)

    def test_adobe_credentials_rejected(self):
        class _Resp:
            status_code = 401
            text = "invalid_client"
        with patch(
            "agents.connectors.mcp_tools_creative._exchange_adobe_ims_token",
            return_value=_Resp(),
        ):
            result = handle_adobe({
                "app": "firefly",
                "_credentials": {
                    "ADOBE_CLIENT_ID": "id",
                    "ADOBE_CLIENT_SECRET": "wrong",
                },
            })
        alerts = _by_type(_comps(result), "alert")
        assert any("Credentials rejected" in (a.get("title") or "") for a in alerts)

    def test_adobe_credentials_check_unconfigured(self):
        verdict = handle_adobe_credentials_check({})
        assert verdict["credential_test"] == "unconfigured"


class TestCanva:
    def test_canva_without_credentials(self):
        result = handle_canva({"design_type": "presentation"})
        alerts = _by_type(_comps(result), "alert")
        assert any("Credentials not configured" in (a.get("title") or "") for a in alerts)

    def test_canva_creates_design_when_credentialed(self):
        class _Resp:
            status_code = 200
            text = json.dumps({"design": {"id": "DAF12", "urls": {"edit_url": "https://canva.com/x"}}})
            def json(self):
                return json.loads(self.text)
        with patch("agents.connectors.mcp_tools_creative.http_request", return_value=_Resp()):
            result = handle_canva({
                "design_type": "presentation",
                "_credentials": {"CANVA_API_KEY": "tok"},
            })
        alerts = _by_type(_comps(result), "alert")
        assert any("Design created" in (a.get("title") or "") for a in alerts)
        texts = _by_type(_comps(result), "text")
        assert any("canva.com/x" in (t.get("content") or "") for t in texts)

    def test_canva_credentials_check_unconfigured(self):
        verdict = handle_canva_credentials_check({})
        assert verdict["credential_test"] == "unconfigured"


class TestArtifactsGraphsDesign:
    def test_artifacts_generates_real_widgets(self):
        result = handle_artifacts({
            "title": "Sales Dashboard",
            "sections": [
                {"widget_type": "metric", "title": "Revenue", "data_source": "DB", "value": "12400"},
                {"widget_type": "chart", "title": "Trend", "data_source": "API",
                 "chart_kind": "line", "labels": ["Jan", "Feb"], "values": [5, 9]},
                {"widget_type": "table", "title": "Bookings"},
                {"widget_type": "timeline", "title": "Today"},
            ],
        })
        comps = _comps(result)
        assert comps[0]["type"] == "hero", "dashboard leads with a masthead"
        assert comps[0]["title"] == "Sales Dashboard"
        metric = _by_type(comps, "metric")[0]
        assert metric["value"] == "12400" and "DB" in metric["subtitle"]
        line = _by_type(comps, "line_chart")[0]
        assert line["labels"] == ["Jan", "Feb"]
        assert line["datasets"][0]["data"] == [5, 9]
        table = _by_type(comps, "table")[0]
        assert table["headers"] and table["rows"], "table gets sample rows when none provided"
        timeline = _by_type(comps, "timeline")[0]
        assert timeline["items"], "timeline gets sample entries when none provided"

    def test_graphs_returns_nodes_and_edges(self):
        result = handle_graphs({
            "nodes": ["A", "B", "C"],
            "edges": [{"source": "A", "target": "B", "label": "depends"}],
        })
        collapsibles = _by_type(_comps(result), "collapsible")
        assert len(collapsibles) == 2

    def test_design_returns_palette(self):
        result = handle_design({"context": "web", "style_preferences": "corporate"})
        comps = _comps(result)
        assert comps[0]["type"] == "hero"
        pie = _by_type(comps, "pie_chart")[0]
        assert pie["colors"] == pie["labels"], "swatch wheel is colored by the palette itself"
        assert _by_type(comps, "keyvalue"), "typography/spacing foundations"
        assert _by_type(comps, "alert"), "accessibility callout"


class TestMcpServerDispatch:
    def test_tool_count_matches_registry(self):
        server = ConnectorsMCPServer()
        assert len(server.get_tool_list()) == 17
        assert "outlook_credentials_check" in TOOL_REGISTRY
        assert "canva_credentials_check" in TOOL_REGISTRY
        assert "adobe_credentials_check" in TOOL_REGISTRY

    def test_tools_list_method(self):
        server = ConnectorsMCPServer()
        req = MCPRequest(method="tools/list", request_id="r1", params={})
        resp = server.process_request(req)
        assert resp.error is None
        assert "tools" in resp.result
        names = {t["name"] for t in resp.result["tools"]}
        assert "excel_generate" in names

    def test_tools_call_unknown_tool(self):
        server = ConnectorsMCPServer()
        req = MCPRequest(
            method="tools/call",
            request_id="r2",
            params={"name": "does_not_exist", "arguments": {}},
        )
        resp = server.process_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32601

    def test_tools_call_missing_required_arg(self):
        server = ConnectorsMCPServer()
        req = MCPRequest(
            method="tools/call",
            request_id="r3",
            params={"name": "excel_generate", "arguments": {"title": "T"}},
        )
        resp = server.process_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32602
        assert "columns" in resp.error["message"]

    def test_tools_call_returns_ui_components(self, session_args):
        server = ConnectorsMCPServer()
        req = MCPRequest(
            method="tools/call",
            request_id="r4",
            params={
                "name": "powerpoint_outline",
                "arguments": {"title": "Deck", "slides": [{"title": "Intro", "bullets": ["x"]}]},
            },
        )
        resp = server.process_request(req)
        assert resp.error is None
        assert resp.ui_components is not None
        assert any(c.get("type") == "collapsible" for c in resp.ui_components)

    def test_unknown_method_errors(self):
        server = ConnectorsMCPServer()
        req = MCPRequest(method="something/weird", request_id="r5", params={})
        resp = server.process_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32601
