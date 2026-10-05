"""Exercises the FHIR client: environment settings, the egress allow-list for the configured
host, paging, and the mapping from transport failures to public error codes.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from agents.fhir import client as fhir_client
from agents.fhir.client import FhirClient, FhirError, load_settings
from agents.fhir.tests.conftest import BASE, TOKEN, FakeResponse
from shared import external_http
from shared.tests._http_mock import _FakeResponse


def test_settings_come_from_the_environment():
    settings = load_settings({"FHIR_BASE_URL": BASE + "/", "FHIR_ACCESS_TOKEN": f" {TOKEN} ", "FHIR_TIMEOUT_SECONDS": "90"})
    assert (settings.base_url, settings.token, settings.timeout) == (BASE, TOKEN, fhir_client.MAX_TIMEOUT_SECONDS)
    assert settings.host == "eicu-fhir" and settings.origin == ("http", "eicu-fhir:8080")
    assert load_settings({"FHIR_BASE_URL": BASE, "FHIR_ACCESS_TOKEN": TOKEN, "FHIR_TIMEOUT_SECONDS": "soon"}).timeout == 20
    assert load_settings({"FHIR_BASE_URL": BASE, "FHIR_ACCESS_TOKEN": TOKEN, "FHIR_TIMEOUT_SECONDS": "0"}).timeout == 1


@pytest.mark.parametrize("environment", [
    {}, {"FHIR_BASE_URL": BASE}, {"FHIR_ACCESS_TOKEN": TOKEN}, {"FHIR_BASE_URL": "ftp://x/fhir", "FHIR_ACCESS_TOKEN": TOKEN},
    {"FHIR_BASE_URL": "http:///fhir", "FHIR_ACCESS_TOKEN": TOKEN},
])
def test_incomplete_settings_are_refused(environment):
    with pytest.raises(FhirError) as failure:
        load_settings(environment)
    assert failure.value.code == "FHIR_NOT_CONFIGURED"
    assert failure.value.as_tool_error()["_error"]["retryable"] is False


def test_settings_default_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("FHIR_BASE_URL", BASE)
    monkeypatch.setenv("FHIR_ACCESS_TOKEN", TOKEN)
    assert load_settings().base_url == BASE


def test_read_search_and_latest(server, settings):
    api = FhirClient(settings, transport=server)
    assert api.read("Patient", "002-1")["gender"] == "female"
    encounters, total = api.search("Encounter", [("patient", "002-1"), ("_count", "999")], 10)
    assert [item["id"] for item in encounters] == ["icu-1", "hosp-1"] and total == 2
    assert server.calls[-1][2]["_count"] == ["10"]
    latest = api.latest([("patient", "002-1"), ("code", "http://loinc.org|8867-4")], 2)
    assert [item["id"] for item in latest] == ["hr-1"]
    assert server.calls[-1][2]["max"] == ["2"]


def test_search_follows_next_links_up_to_the_limit(server, settings):
    server.page_size = 4
    api = FhirClient(settings, transport=server)
    observations, total = api.search("Observation", [("patient", "002-1")], 10)
    assert len(observations) == 10 and total == 22
    assert [call[2].get("_offset") for call in server.calls] == [None, ["4"], ["8"]]
    everything, _ = api.search("Observation", [("patient", "002-1")], 0)
    assert len(everything) == 1


def test_paging_stops_at_the_page_cap(server, settings, monkeypatch):
    server.page_size = 1
    monkeypatch.setattr(fhir_client, "MAX_PAGES", 3)
    observations, _ = FhirClient(settings, transport=server).search("Observation", [("patient", "002-1")], 50)
    assert len(observations) == 3


def test_paging_links_to_other_addresses_are_refused(settings):
    api = FhirClient(settings, transport=lambda *a, **k: FakeResponse(200, {"resourceType": "Bundle"}))
    for url in ("http://evil.example/fhir/Patient?x=1", "https://eicu-fhir:8080/fhir/Patient", "http://eicu-fhir:8080/other/Patient"):
        with pytest.raises(FhirError) as failure:
            api.follow(url)
        assert failure.value.code == "FHIR_INVALID_RESPONSE"
    assert api.follow(f"{BASE}/Patient?_cursor=abc")["resourceType"] == "Bundle"


def test_post_and_delete(server, settings):
    api = FhirClient(settings, transport=server)
    created = api.post("Subscription", {"resourceType": "Subscription"}, expected="Subscription")
    assert created["id"] == "sub-1"
    assert api.delete("Subscription/sub-1") is None and server.deleted == ["sub-1"]


@pytest.mark.parametrize(("failure", "code", "retryable"), [
    (external_http.EgressBlockedError("blocked"), "FHIR_BLOCKED", False),
    (external_http.AuthFailedError("401"), "FHIR_AUTH_FAILED", False),
    (external_http.BadRequestError("Upstream returned 404: gone"), "FHIR_NOT_FOUND", False),
    (external_http.BadRequestError("Upstream returned 410: gone"), "FHIR_NOT_FOUND", False),
    (external_http.BadRequestError("Upstream returned 400: bad"), "FHIR_BAD_REQUEST", False),
    (external_http.ResponseTooLargeError("big"), "FHIR_BAD_REQUEST", False),
    (external_http.ServiceUnreachableError("down"), "FHIR_UNAVAILABLE", True),
    (external_http.RateLimitedError("503"), "FHIR_UNAVAILABLE", True),
])
def test_transport_failures_map_to_public_codes(settings, failure, code, retryable):
    def transport(*_a, **_k):
        raise failure

    with pytest.raises(FhirError) as raised:
        FhirClient(settings, transport=transport).read("Patient", "x")
    assert (raised.value.code, raised.value.retryable) == (code, retryable)


@pytest.mark.parametrize("payload", [b"<html>", [1, 2], {"no": "type"}, {"resourceType": "Patient"}])
def test_unusable_responses_are_reported(settings, payload):
    with pytest.raises(FhirError) as failure:
        FhirClient(settings, transport=lambda *a, **k: FakeResponse(200, payload)).bundle("Patient")
    assert failure.value.code == "FHIR_INVALID_RESPONSE"


def test_bundle_helpers():
    bundle = {
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "a"}},
            {"resource": {"resourceType": "Organization", "id": "b"}, "search": {"mode": "include"}},
            {"search": {"mode": "match"}},
        ],
        "link": [{"relation": "self", "url": "x"}, {"relation": "next", "url": "y"}],
    }
    assert [item["id"] for item in fhir_client.bundle_resources(bundle)] == ["a"]
    assert [item["id"] for item in fhir_client.bundle_resources(bundle, "include")] == ["b"]
    assert fhir_client.next_link(bundle) == "y" and fhir_client.next_link({}) is None


def private_dns(host, *_a, **_k):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.2", 0))]


def test_the_configured_private_host_passes_the_egress_guard(settings):
    sent = {}

    def session_request(method, url, **options):
        sent.update(options, method=method, url=url)
        return _FakeResponse(200, b'{"resourceType": "Patient", "id": "002-1"}')

    with patch("socket.getaddrinfo", private_dns), patch("requests.Session.request", side_effect=session_request):
        assert FhirClient(settings).read("Patient", "002-1")["id"] == "002-1"
    assert (sent["method"], sent["url"]) == ("GET", f"{BASE}/Patient/002-1")
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert sent["headers"]["Accept"] == "application/fhir+json"
    assert sent["allow_redirects"] is False


def test_a_host_that_is_not_the_configured_one_is_blocked(settings):
    with patch("socket.getaddrinfo", private_dns), pytest.raises(external_http.EgressBlockedError):
        external_http.request("GET", "http://internal-db:8080/x", api_key="", allowed_private_hosts=[settings.host])
