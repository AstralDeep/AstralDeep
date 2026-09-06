"""Page resource paths and bounded, revalidated redirect chains."""
from unittest.mock import patch

import pytest

from agents.web_research import mcp_tools
from shared.external_http import EgressBlockedError, ServiceUnreachableError
from shared.tests._http_mock import HttpMock


@pytest.mark.parametrize("path", ["/", "/downloads/", "/a//", "/a/;v=1?q=2"])
def test_page_read_preserves_resource_path(rmock: HttpMock, path: str) -> None:
    url = "https://example.com" + path
    rmock.add("GET", url, body=b"release notes")
    assert mcp_tools._fetch_url(url).text == "release notes"
    assert [call["url"] for call in rmock.calls] == [url]


@pytest.mark.parametrize(
    "start,location,expected",
    [
        ("/downloads", "/downloads/", "/downloads/"),
        ("/releases/current.html", "next.html", "/releases/next.html"),
        ("/releases/current/", "next.html", "/releases/current/next.html"),
        ("/releases/current.html?old=1", "?new=2", "/releases/current.html?new=2"),
        ("/releases/current/", "../latest/", "/releases/latest/"),
    ],
)
def test_redirect_uses_rfc_resource_base(
    rmock: HttpMock, start: str, location: str, expected: str,
) -> None:
    origin = "https://example.com"
    rmock.add("GET", origin + start, status=302, headers={"Location": location})
    rmock.add("GET", origin + expected, body=b"correct resource")
    assert mcp_tools._fetch_url(origin + start).text == "correct resource"
    assert [call["url"] for call in rmock.calls] == [origin + start, origin + expected]
    assert all(call["allow_redirects"] is False for call in rmock.calls)


@pytest.mark.parametrize("location", [
    "//internal.example.com/private/", "http://127.0.0.1/", "file:///etc/passwd",
])
def test_redirect_destination_is_revalidated_before_transport(
    rmock: HttpMock, location: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/start/"
    rmock.add("GET", url, status=302, headers={"Location": location})
    resolve = mcp_tools.external_http._resolve_host_addresses
    monkeypatch.setattr(mcp_tools.external_http, "_resolve_host_addresses",
                        lambda host: [host] if host == "127.0.0.1" else resolve(host))
    with pytest.raises(EgressBlockedError):
        mcp_tools._fetch_url(url)
    assert [call["url"] for call in rmock.calls] == [url]


def test_redirect_chain_shares_timeout_budget(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/a", status=302, headers={"Location": "b"})
    rmock.add("GET", "https://example.com/b", body=b"latest")
    with patch.object(mcp_tools.time, "monotonic", side_effect=[100, 100, 125, 125, 126]):
        assert mcp_tools._fetch_url("https://example.com/a").text == "latest"
    assert [call["timeout"] for call in rmock.calls] == [15, 5]


@pytest.mark.parametrize("status", [200, 302])
def test_expired_chain_rejects_late_response_and_no_next_hop(
    rmock: HttpMock, status: int,
) -> None:
    url = "https://example.com/a"
    rmock.add("GET", url, status=status, body=b"late", headers={"Location": "b"})
    with patch.object(mcp_tools.time, "monotonic", side_effect=[100, 100, 131]):
        with pytest.raises(ServiceUnreachableError, match="timeout budget"):
            mcp_tools._fetch_url(url)
    assert len(rmock.calls) == 1


def test_expired_chain_before_transport_does_not_issue_request(rmock: HttpMock) -> None:
    with patch.object(mcp_tools.time, "monotonic", side_effect=[100, 131]):
        with pytest.raises(ServiceUnreachableError, match="timeout budget"):
            mcp_tools._fetch_url("https://example.com/a")
    assert rmock.calls == []
