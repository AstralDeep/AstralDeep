"""Tests that agents/web_research/mcp_tools.py never leaks upstream HTML, provider
bodies, or API keys into user-facing search/fetch error messages.
"""

import json
from unittest.mock import patch

import pytest

from agents.web_research import mcp_tools
from agents.web_research.tests.test_web_search import PROVIDER_CREDS, PROVIDER_URL, CHALLENGE_HTML


SECRET_HTML = '<html><script>PRIVATE-CREDENTIAL-SENTINEL</script>Provider diagnostic</html>'


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_provider_errors_hide_upstream_html_and_keys(rmock, status, caplog):
    rmock.add("POST", PROVIDER_URL, status=status, body=SECRET_HTML.encode())
    result = mcp_tools.web_search("query", _credentials=PROVIDER_CREDS)
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert len(alert["message"]) < 180
    assert "SENTINEL" not in json.dumps(result) + caplog.text
    assert "sk-sentinel" not in json.dumps(result) + caplog.text
    assert "quota" not in alert["message"]
    assert len(rmock.calls) == 1


@pytest.mark.parametrize("status", [401, 404, 429, 503])
def test_credentials_probe_never_echoes_provider_body(rmock, status, caplog):
    rmock.add("POST", PROVIDER_URL, status=status, body=SECRET_HTML.encode())
    result = mcp_tools._credentials_check(_credentials=PROVIDER_CREDS)
    assert result["credential_test"] != "ok"
    assert "SENTINEL" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("status", [401, 404, 500])
def test_fetch_errors_are_actionable_and_hide_url_tokens(rmock, status, caplog):
    url = "https://example.com/page?token=PRIVATE-CREDENTIAL-SENTINEL"
    rmock.add("GET", url, status=status, body=SECRET_HTML.encode())
    result = mcp_tools.fetch_page(url)
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "SENTINEL" not in json.dumps(result) + caplog.text
    if status == 404:
        assert alert["message"] == "This page was not found. Check the link or choose another source."


def test_keyless_challenge_is_not_a_quota_claim_and_is_not_retried(rmock, caplog):
    rmock.add("GET", mcp_tools.DDG_HTML_URL, status=202, body=(CHALLENGE_HTML + SECRET_HTML).encode())
    result = mcp_tools.research_brief("query")
    assert result["_ui_components"][0]["message"] == (
        "Keyless search is blocked. Add a search provider API key in agent settings "
        "for reliable/higher-limit search."
    )
    assert "SENTINEL" not in json.dumps(result) + caplog.text
    assert "status=202" in caplog.text
    assert len(rmock.calls) == 1


def test_unexpected_provider_exception_is_sanitized(caplog):
    with patch.object(mcp_tools, "_perform_search", side_effect=RuntimeError(SECRET_HTML)):
        result = mcp_tools.web_search("query")
    assert "SENTINEL" not in json.dumps(result) + caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.parametrize(("status", "code"), [
    (401, "SEARCH_AUTH_FAILED"), (404, "SEARCH_UNAVAILABLE"),
    (429, "SEARCH_RATE_LIMITED"), (503, "SEARCH_UNAVAILABLE"),
])
def test_search_provider_mcp_errors_preserve_codes_without_automatic_retries(rmock, status, code):
    from agents.web_research.mcp_server import MCPServer
    from shared.protocol import MCPRequest

    rmock.add("POST", PROVIDER_URL, status=status, body=SECRET_HTML.encode())
    response = MCPServer().process_request(MCPRequest(
        request_id="review", method="tools/call", params={
            "name": "web_search", "arguments": {"query": "quantum", "_credentials": PROVIDER_CREDS},
        },
    ))
    assert response.error["code"] == code
    assert response.error["retryable"] is False
    assert "SENTINEL" not in json.dumps(response.error)
    assert len(rmock.calls) == 1


def test_keyless_challenge_mcp_response_is_terminal(rmock):
    from agents.web_research.mcp_server import MCPServer
    from shared.protocol import MCPRequest

    rmock.add("GET", mcp_tools.DDG_HTML_URL, status=202, body=CHALLENGE_HTML.encode())
    response = MCPServer().process_request(MCPRequest(
        request_id="review", method="tools/call",
        params={"name": "web_search", "arguments": {"query": "quantum"}},
    ))
    assert response.error["code"] == "SEARCH_BLOCKED"
    assert response.error["retryable"] is False
    assert response.error["message"].startswith("Keyless search is blocked.")
    assert len(rmock.calls) == 1
