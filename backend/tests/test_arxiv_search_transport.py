"""Tests for agents/general/mcp_tools.py's arxiv search: the installed Client's Atom
parsing through shared/external_http.py's approved transport, result bounding,
private-destination refusal, and error handling.
"""

import json
import socket
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import arxiv
import pytest
import requests

from agents.general import mcp_tools
from shared.external_http import EgressBlockedError, ServiceUnreachableError
from shared.tests._http_mock import HttpMock


ENTRY = """
<entry>
 <id>https://arxiv.org/abs/2601.00001</id>
 <updated>2026-01-02T00:00:00Z</updated><published>2026-01-01T00:00:00Z</published>
 <title>Quantum Research</title><summary>A reproducible summary.</summary>
 <author><name>A Researcher</name></author><author><name>B Researcher</name></author>
 <author><name>C Researcher</name></author>
 <link href="https://arxiv.org/abs/2601.00001" rel="alternate" type="text/html"/>
 <link href="https://arxiv.org/pdf/2601.00001" title="pdf" rel="related" type="application/pdf"/>
 <arxiv:primary_category term="quant-ph"/><category term="quant-ph"/>
</entry>"""


def _feed(entry=ENTRY, total=1):
    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
            f'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
            f'xmlns:arxiv="http://arxiv.org/schemas/atom">'
            f'<title>arXiv search</title><id>https://export.arxiv.org/api/query</id>'
            f'<updated>2026-01-01T00:00:00Z</updated>'
            f'<opensearch:totalResults>{total}</opensearch:totalResults>'
            f'<opensearch:startIndex>0</opensearch:startIndex>'
            f'<opensearch:itemsPerPage>20</opensearch:itemsPerPage>{entry}</feed>').encode()


def _url(limit):
    return arxiv.Client()._format_url(arxiv.Search(query="quantum", max_results=limit), 0, limit)


@pytest.fixture(autouse=True)
def isolate_network_and_llm(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr(requests.Session, "get", Mock(side_effect=AssertionError("unguarded HTTP")))


def test_installed_client_parses_atom_and_preserves_all_paper_fields():
    with HttpMock() as http:
        http.add("GET", _url(10), body=_feed())
        result = mcp_tools.search_arxiv("quantum")
    assert result["_data"] == [{
        "title": "Quantum Research", "authors": ["A Researcher", "B Researcher", "C Researcher"],
        "summary": "A reproducible summary.", "published": "2026-01-01",
        "url": "https://arxiv.org/abs/2601.00001", "pdf_url": "https://arxiv.org/pdf/2601.00001",
    }]
    assert result["_ui_components"][0]["type"] == "card"
    assert len(http.calls) == 1
    assert http.calls[0]["timeout"] == 15
    assert http.calls[0]["allow_redirects"] is False
    assert "Authorization" not in http.calls[0]["headers"]


@pytest.mark.parametrize(("value", "expected"), [(10000, 20), (0, 1), (-2, 1), (None, 10), ("bad", 10)])
def test_result_count_is_bounded_before_constructing_search(value, expected):
    with HttpMock() as http:
        http.add("GET", _url(expected), body=_feed("", total=0))
        result = mcp_tools.search_arxiv("quantum", max_results=value)
    assert result["_data"] == []
    assert parse_qs(urlparse(http.calls[0]["url"]).query)["max_results"] == [str(expected)]


def test_empty_query_does_not_contact_network():
    with HttpMock() as http:
        result = mcp_tools.search_arxiv("  ")
    assert result["_ui_components"][0]["message"] == "Enter a topic to search for papers."
    assert not http.calls


@pytest.mark.parametrize("status", [302, 401, 404, 429, 503])
def test_upstream_errors_are_short_safe_and_do_not_retry(status, caplog):
    sentinel = "<html>private-token-SENTINEL</html>"
    with HttpMock() as http:
        http.add("GET", _url(10), status=status, body=sentinel.encode())
        result = mcp_tools.search_arxiv("quantum")
    assert result["_ui_components"][0]["message"] == (
        "arXiv is unavailable right now. Try again later or search another source."
    )
    assert len(http.calls) == 1
    assert "SENTINEL" not in json.dumps(result) + caplog.text
    assert "error_type=" in caplog.text
    assert result["_error"]["code"] == "ARXIV_UNAVAILABLE"
    assert result["_error"]["retryable"] is False


def test_response_size_is_bounded():
    with HttpMock() as http:
        http.add("GET", _url(10), body=b"x" * (1024 * 1024 + 1))
        result = mcp_tools.search_arxiv("quantum")
    assert result["_ui_components"][0]["variant"] == "error"
    assert len(http.calls) == 1


def test_second_request_is_refused_without_touching_network():
    transport = mcp_tools._ArxivEgressTransport()
    with HttpMock() as http:
        http.add("GET", _url(10), body=_feed())
        transport.get(_url(10), headers={})
        with pytest.raises(ServiceUnreachableError, match="arxiv_request_limit"):
            transport.get(_url(10), headers={})
    assert len(http.calls) == 1


def test_transport_rejects_private_destination_before_request(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
    ])
    with HttpMock() as http, pytest.raises(EgressBlockedError):
        mcp_tools._ArxivEgressTransport().get("https://internal.example/api", headers={})
    assert not http.calls


def test_mcp_arxiv_error_is_terminal_and_preserves_public_code():
    from agents.general.mcp_server import MCPServer
    from shared.protocol import MCPRequest

    with HttpMock() as http:
        http.add("GET", _url(10), status=503, body=b"private response")
        response = MCPServer().process_request(MCPRequest(
            request_id="review", method="tools/call",
            params={"name": "search_arxiv", "arguments": {"query": "quantum"}},
        ))
    assert response.error == {
        "code": "ARXIV_UNAVAILABLE", "retryable": False,
        "message": "arXiv is unavailable right now. Try again later or search another source.",
    }
    assert len(http.calls) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_search_term_optimization_logs_no_provider_payload(monkeypatch, caplog, failure):
    from types import SimpleNamespace

    create = Mock(
        side_effect=RuntimeError("PRIVATE-CREDENTIAL-SENTINEL") if failure else None,
        return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="quantum"))]),
    )
    monkeypatch.setattr(mcp_tools, "OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    assert mcp_tools.extract_search_terms(
        "quantum", _session_llm_credentials={"OPENAI_API_KEY": "test-key"},
    ) == "quantum"
    assert "SENTINEL" not in caplog.text
