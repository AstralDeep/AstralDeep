"""Summarizer's own fetch and LLM errors stay short through MCP dispatch."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agents.summarizer import mcp_tools
from agents.summarizer.mcp_server import MCPServer
from shared.protocol import MCPRequest


@pytest.mark.parametrize(("status", "code"), [
    (401, "UPSTREAM_ACCESS_DENIED"), (404, "UPSTREAM_NOT_FOUND"),
    (429, "UPSTREAM_UNAVAILABLE"), (503, "UPSTREAM_UNAVAILABLE"),
])
def test_summarize_url_http_errors_are_terminal_and_do_not_echo_html(rmock, status, code, caplog):
    url = "https://example.com/source?token=PRIVATE-SENTINEL"
    rmock.add("GET", url, status=status, body=b"<html>PRIVATE-SENTINEL</html>")
    response = MCPServer().process_request(MCPRequest(
        request_id="review", method="tools/call",
        params={"name": "summarize_url", "arguments": {"url": url}},
    ))
    assert response.error["code"] == code
    assert response.error["retryable"] is False
    assert "SENTINEL" not in json.dumps(response.error) + caplog.text
    assert len(rmock.calls) == 1
    if status == 404:
        assert response.error["message"] == "This page was not found. Check the link or choose another source."


@pytest.mark.parametrize("tool", ["summarize_text", "compare_documents"])
def test_summary_provider_failure_does_not_reach_llm_narrative(monkeypatch, tool, caplog):
    monkeypatch.setattr(mcp_tools, "_call_summary_llm", Mock(side_effect=RuntimeError("<html>PRIVATE-SENTINEL</html>")))
    args = {"text": "source"} if tool == "summarize_text" else {
        "text_a": "first", "text_b": "second",
    }
    response = MCPServer().process_request(MCPRequest(
        request_id="review", method="tools/call", params={"name": tool, "arguments": args},
    ))
    assert response.error["code"] == "RESEARCH_SUMMARY_UNAVAILABLE"
    assert response.error["retryable"] is False
    assert "SENTINEL" not in json.dumps(response.error) + caplog.text


@pytest.mark.parametrize("failure", ["exception", "refusal"])
def test_peer_fetch_failures_do_not_log_raw_peer_messages(monkeypatch, failure, caplog):
    import asyncio

    future = SimpleNamespace(result=Mock(
        side_effect=RuntimeError("PRIVATE-SENTINEL") if failure == "exception" else None,
        return_value=SimpleNamespace(error={"message": "PRIVATE-SENTINEL"}),
    ))
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda *args: future)
    runtime = SimpleNamespace(call_agent_tool=Mock(return_value=object()), loop=object())
    assert mcp_tools._fetch_via_peer("https://example.com/source", {"_runtime": runtime}) is None
    assert "SENTINEL" not in caplog.text
