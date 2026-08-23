"""Unit tests for shared.external_http.request — error mapping + bearer + size cap."""
from unittest.mock import patch

import pytest
import requests

from shared.external_http import (
    AuthFailedError,
    BadRequestError,
    RateLimitedError,
    ResponseTooLargeError,
    ServiceUnreachableError,
    request as ext_request,
)
from shared.tests._http_mock import HttpMock


SAFE_HOST = "public.example.com"
SAFE_URL = f"https://{SAFE_HOST}/"


@pytest.fixture
def rmock():
    with HttpMock() as m:
        yield m


@pytest.fixture(autouse=True)
def stub_dns():
    """Pretend SAFE_HOST resolves to a public IP so SSRF guard passes."""
    import socket
    def _fake(host, *_a, **_kw):
        if host == SAFE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise socket.gaierror(host)
    with patch("socket.getaddrinfo", _fake):
        yield


def test_happy_path_200_returns_response(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=200, json={"ok": True})
    resp = ext_request("GET", SAFE_URL, api_key="sentinel-key")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_authorization_header_is_bearer(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=200, body=b"{}")
    ext_request("GET", SAFE_URL, api_key="sentinel-api-key-deadbeef")
    assert rmock.calls[0]["headers"]["Authorization"] == "Bearer sentinel-api-key-deadbeef"


@pytest.mark.parametrize("blank_key", ["", "   ", "\t\n"])
def test_blank_api_key_sends_no_authorization_header(rmock: HttpMock, blank_key: str) -> None:
    """Keyless callers (summarizer/web_research/journal_review pass ``""``)
    must not emit a malformed ``Authorization: Bearer `` — some origins
    (Wikipedia, verified live) answer ANY bearer header with 400."""
    rmock.add("GET", SAFE_URL, status=200, body=b"{}")
    ext_request("GET", SAFE_URL, api_key=blank_key)
    sent = rmock.calls[0]["headers"]
    assert not any(k.lower() == "authorization" for k in sent)


def test_blank_api_key_keeps_extra_headers(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=200, body=b"{}")
    ext_request("GET", SAFE_URL, api_key="",
                extra_headers={"User-Agent": "AstralBot/1.0", "Accept": "text/html"})
    sent = rmock.calls[0]["headers"]
    assert "Authorization" not in sent
    assert sent["User-Agent"] == "AstralBot/1.0"
    assert sent["Accept"] == "text/html"


def test_non_empty_api_key_merges_extra_headers(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=200, body=b"{}")
    ext_request("GET", SAFE_URL, api_key="sk-live",
                extra_headers={"User-Agent": "AstralBot/1.0"})
    sent = rmock.calls[0]["headers"]
    assert sent["Authorization"] == "Bearer sk-live"
    assert sent["User-Agent"] == "AstralBot/1.0"


def test_extra_headers_may_override_authorization(rmock: HttpMock) -> None:
    """``extra_headers`` merge AFTER the bearer (pre-existing precedence)."""
    rmock.add("GET", SAFE_URL, status=200, body=b"{}")
    ext_request("GET", SAFE_URL, api_key="sk-live",
                extra_headers={"Authorization": "Basic abc"})
    assert rmock.calls[0]["headers"]["Authorization"] == "Basic abc"


def test_json_body_content_type_set_without_api_key(rmock: HttpMock) -> None:
    rmock.add("POST", SAFE_URL, status=200, body=b"{}")
    ext_request("POST", SAFE_URL, api_key="", json_body={"q": 1})
    sent = rmock.calls[0]["headers"]
    assert "Authorization" not in sent
    assert sent["Content-Type"] == "application/json"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failed_mapped(rmock: HttpMock, status: int) -> None:
    rmock.add("GET", SAFE_URL, status=status, body=b"{}")
    with pytest.raises(AuthFailedError):
        ext_request("GET", SAFE_URL, api_key="x")


def test_rate_limited_429(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=429, body=b"{}")
    with pytest.raises(RateLimitedError):
        ext_request("GET", SAFE_URL, api_key="x")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_5xx_maps_to_rate_limited(rmock: HttpMock, status: int) -> None:
    rmock.add("GET", SAFE_URL, status=status, body=b"{}")
    with pytest.raises(RateLimitedError):
        ext_request("GET", SAFE_URL, api_key="x")


@pytest.mark.parametrize("status", [400, 404, 422])
def test_bad_request_4xx_other(rmock: HttpMock, status: int) -> None:
    rmock.add("GET", SAFE_URL, status=status, json={"detail": "nope"})
    with pytest.raises(BadRequestError):
        ext_request("GET", SAFE_URL, api_key="x")


def test_connection_error_maps_to_unreachable() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("nope")):
        with pytest.raises(ServiceUnreachableError):
            ext_request("GET", SAFE_URL, api_key="x")


def test_timeout_maps_to_unreachable() -> None:
    with patch("requests.request", side_effect=requests.Timeout("slow")):
        with pytest.raises(ServiceUnreachableError):
            ext_request("GET", SAFE_URL, api_key="x")


def test_response_size_cap_enforced(rmock: HttpMock) -> None:
    rmock.add("GET", SAFE_URL, status=200, body=b"x" * 200_000)
    with pytest.raises(ResponseTooLargeError):
        ext_request("GET", SAFE_URL, api_key="x", max_response_bytes=50_000)
