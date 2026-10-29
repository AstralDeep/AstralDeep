"""Selected HTTP connects only the capability's single private DNS snapshot and original Host.
Unknown, foreign and forged capabilities never redirect ordinary URLs or expand operator policy.
"""

import socket
from types import SimpleNamespace

import pytest

from shared import external_http as http
from shared.tests.test_isolated_http import server as server


def records(address="127.0.0.1", port=80):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


def test_one_snapshot_survives_rebind_without_resolving_or_sending_twice(server, monkeypatch):
    url, seen, *_ = server
    port = int(url.rsplit(":", 1)[1])
    target = f"http://guarded.invalid:{port}/model"
    resolutions = []
    def resolve(host, selected_port):
        resolutions.append((host, selected_port))
        return records("127.0.0.1" if len(resolutions) == 1 else "8.8.8.8", selected_port)
    monkeypatch.setattr(http, "_resolve_private_http_addresses", resolve)
    capture = http.capture_private_http_target(target, allowed_private_hosts=("guarded.invalid",))
    response = http.request("POST", target, api_key="", json_body={"bounded": True},
        allowed_private_hosts=("guarded.invalid",), trust_environment=False,
        require_identity_encoding=True, _private_http_capture=capture)
    assert response.status_code == 201 and resolutions == [("guarded.invalid", port)]
    assert len(seen) == 1 and seen[0][1]["Host"] == f"guarded.invalid:{port}"


@pytest.mark.parametrize("kind", ["forged", "foreign", "host", "policy", "proxy", "redirect", "encoding"])
def test_foreign_or_untrusted_capture_cannot_override_request_scope(monkeypatch, kind):
    monkeypatch.setattr(http, "_resolve_private_http_addresses", lambda *a: records())
    target = "http://guarded.invalid/model"
    capture = http.capture_private_http_target(target, allowed_private_hosts=("guarded.invalid",))
    options = dict(api_key="", json_body={}, allowed_private_hosts=("guarded.invalid",),
                   trust_environment=False, require_identity_encoding=True, _private_http_capture=capture)
    if kind == "forged":
        options["_private_http_capture"] = SimpleNamespace()
    elif kind == "foreign":
        target = "http://public.example/model"
    elif kind == "host":
        options["extra_headers"] = {"hOsT": "public.example"}
    elif kind == "policy":
        options["allowed_private_hosts"] = ("other.invalid",)
    elif kind == "proxy":
        options["trust_environment"] = True
    elif kind == "redirect":
        options["allow_redirects"] = True
    else:
        options["require_identity_encoding"] = False
    with pytest.raises(http.EgressBlockedError):
        http.request("POST", target, **options)


@pytest.mark.parametrize("answer", [[], records("8.8.8.8"), records() + records("8.8.8.8"), records() * 9,
    [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]])
def test_public_mixed_unbounded_or_invalid_resolution_never_mints_capture(monkeypatch, answer):
    monkeypatch.setattr(http, "_resolve_private_http_addresses", lambda *a: answer)
    with pytest.raises(http.EgressBlockedError):
        http.capture_private_http_target("http://guarded.invalid/model", allowed_private_hosts=("guarded.invalid",))


@pytest.mark.parametrize("url,hosts", [("https://guarded.invalid/model", ("guarded.invalid",)),
    ("http://public.example/model", ("guarded.invalid",)), ("http://user:pass@guarded.invalid/model", ("guarded.invalid",)),
    ("http://guarded.invalid/model#private", ("guarded.invalid",)), ("http://guarded.invalid/model", ["guarded.invalid"])])
def test_only_exact_operator_approved_http_target_mints_capture(url, hosts):
    with pytest.raises(http.EgressBlockedError):
        http.capture_private_http_target(url, allowed_private_hosts=hosts)


@pytest.mark.asyncio
async def test_real_isolated_child_uses_private_capture_for_selected_http(server):
    from shared import isolated_http
    url, seen, *_ = server
    reply = await isolated_http.request("POST", url + "/model", json_body={"bounded": True},
        allowed_private_hosts=("127.0.0.1",), require_private_endpoint=True)
    assert reply.status_code == 201 and len(seen) == 1
    assert seen[0][1]["Host"] == url.removeprefix("http://")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
async def test_private_ipc_option_requires_actual_boolean(value):
    from shared import isolated_http
    with pytest.raises(isolated_http.IsolatedHttpError):
        await isolated_http.request("POST", "http://127.0.0.1/model", json_body={},
                                    require_private_endpoint=value)


@pytest.mark.parametrize("address,expected", [("127.0.0.1", True), ("10.0.0.1", True),
    ("172.16.0.1", True), ("192.168.0.1", True), ("::1", True),
    ("::ffff:127.0.0.1", True), ("::ffff:192.168.0.1", True),
    ("8.8.8.8", False), ("169.254.169.254", False), ("fd00::1", False), ("2002:7f00:1::", False)])
def test_private_address_predicate_retains_exact_evidence_semantics(address, expected):
    from shared.net_guard import is_private_http_address
    from llm_config.evidence_transport import _private_address
    assert _private_address is is_private_http_address
    assert is_private_http_address(address) is expected


def test_private_capture_is_frozen_and_cannot_be_minted_by_callers(monkeypatch):
    monkeypatch.setattr(http, "_resolve_private_http_addresses", lambda *a: records())
    cap = http.capture_private_http_target("http://guarded.invalid/model", allowed_private_hosts=("guarded.invalid",))
    with pytest.raises(AttributeError):
        cap.address = "8.8.8.8"
    with pytest.raises(http.EgressBlockedError):
        type(cap)(object(), cap.url, cap.hosts, cap.family, cap.address, cap.port, cap.host_header)
    with pytest.raises(http.EgressBlockedError):
        http.request("GET", cap.url, api_key="", allowed_private_hosts=cap.hosts,
            _private_http_capture=cap, require_identity_encoding=True, trust_environment=False)
    adapter = http._PrivateHttpAdapter(cap)
    try:
        with pytest.raises(http.EgressBlockedError):
            adapter.get_connection("http://public.example/model")
        assert adapter.get_connection(cap.url) is adapter._pool
    finally:
        adapter.close()


def test_refused_private_tcp_connection_has_no_fallback_or_retries():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
        url = f"http://127.0.0.1:{port}/model"
        cap = http.capture_private_http_target(url, allowed_private_hosts=("127.0.0.1",))
        with pytest.raises(http.ServiceUnreachableError):
            http.request("POST", url, api_key="", json_body={}, timeout=1,
                allowed_private_hosts=("127.0.0.1",), _private_http_capture=cap,
                require_identity_encoding=True, trust_environment=False)


@pytest.mark.parametrize("url", ["http://guarded.invalid:0/model", "http://guarded.invalid/model?x=1",
    "http://guarded.invalid/../model", "http://guarded.invalid/a//model", "http://guarded.invalid/\nmodel"])
def test_noncanonical_http_url_never_mints_private_capture(url):
    with pytest.raises(http.EgressBlockedError):
        http.capture_private_http_target(url, allowed_private_hosts=("guarded.invalid",))


@pytest.mark.parametrize("url", [None, {}, "http://[invalid"])
def test_invalid_private_ipc_url_returns_bounded_error(url):
    from shared import isolated_http
    value = {"version": 1, "method": "POST", "url": url, "api_key": "", "json_body": {},
        "allowed_private_hosts": [], "max_response_bytes": 1024, "deadline": 1,
        "require_private_endpoint": True}
    with pytest.raises(isolated_http.IsolatedHttpError):
        isolated_http._validate_request(value)
