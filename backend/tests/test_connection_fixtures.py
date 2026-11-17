"""Exercises the loopback OAuth, MCP and model fixtures in fixtures/connections, switch by switch, and checks what they serve against the contract's connection-oauth-v1.json vectors.
The vector copy beside the fixtures is compared with the contract file when the specs folder is present, as the PHI detector parity test does.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from tests.fixtures.connections import FixtureAuth, FixtureConfig, FixtureRefusal, ModelServer
from tests.fixtures.connections.mcp_server import PROTOCOL_VERSION, VERSION_KEY
from tests.fixtures.connections.model_server import ScriptedTurn
from tests.fixtures.connections.oauth_server import DEVICE_GRANT, WRONG_ISSUER, Challenge, DeviceStep

HERE = Path(__file__).resolve().parent
VECTORS = HERE / "fixtures" / "connections" / "connection-oauth-v1.json"
CONTRACT = HERE.parents[1] / "specs" / "106-sign-in-free-clients" / "contracts" / "vectors" / "connection-oauth-v1.json"
REDIRECT = "https://astraldeep.github.io/oauth/callback"
VERIFIER = "v" * 43
META = {"_meta": {VERSION_KEY: PROTOCOL_VERSION}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def call(url, method="GET", body=None, headers=None, context=None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    lines = response.headers.get_all("www-authenticate") or []
    return response.status if hasattr(response, "status") else response.code, parsed, lines


def post_json(url, payload, token=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return call(url, "POST", json.dumps(payload).encode(), headers)


def post_form(url, form, headers=None):
    return call(url, "POST", urlencode(form).encode(), {"content-type": "application/x-www-form-urlencoded", **(headers or {})})


def rpc(auth, method, token=None, params=None, meta=True):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), **(META if meta else {})}}
    return post_json(auth.rs_url, body, token)


def challenge_of(verifier=VERIFIER):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def register(auth):
    status, body, _ = post_json(f"{auth.urls.as_origin}/register", {"redirect_uris": [REDIRECT], "application_type": "native"})
    assert status == 201
    return body["client_id"]


def authorization_url(auth, client, state="s1", **overrides):
    params = {
        "response_type": "code",
        "client_id": client,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge_of(),
        "code_challenge_method": "S256",
        "resource": auth.rs_url,
        "state": state,
        **overrides,
    }
    return f"{auth.urls.as_origin}/authorize?{urlencode(params)}"


def sign_in(auth, **config):
    client_id = register(auth)
    redirect = auth.approve(authorization_url(auth, client_id))
    query = parse_qs(urlsplit(redirect).query)
    form = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "resource": auth.rs_url,
    }
    status, tokens, _ = post_form(f"{auth.urls.as_origin}/token", form)
    assert status == 200
    return client_id, tokens


def refresh_form(auth, client_id, tokens, resource=None):
    return {
        "grant_type": "refresh_token",
        "refresh_token": tokens.get("refresh_token", ""),
        "client_id": client_id,
        "resource": resource or auth.rs_url,
    }


@pytest.fixture
def make():
    started = []

    def build(**switches):
        auth = FixtureAuth(FixtureConfig(**switches))
        auth.start()
        started.append(auth)
        return auth

    yield build
    for auth in started:
        auth.stop()


def test_urls_are_loopback_and_distinct(make):
    auth = make()
    assert auth.urls.as_origin.startswith("http://127.0.0.1:")
    assert auth.urls.rs_origin.startswith("http://127.0.0.1:")
    assert auth.urls.as_origin != auth.urls.rs_origin
    assert auth.urls.rs_url == auth.urls.rs_origin + "/mcp"
    assert len(auth.urls.egress) == 2


def test_golden_path_issues_a_token_the_tools_accept(make):
    auth = make()
    status, _, lines = rpc(auth, "tools/list")
    assert status == 401
    assert lines == [f'Bearer resource_metadata="{auth.urls.rs_origin}/.well-known/oauth-protected-resource/mcp", scope="files:read"']
    status, prm, _ = call(f"{auth.urls.rs_origin}/.well-known/oauth-protected-resource/mcp")
    assert (status, prm) == (200, {"resource": auth.rs_url, "authorization_servers": [auth.issuer]})
    status, metadata, _ = call(f"{auth.issuer}/.well-known/oauth-authorization-server")
    assert status == 200 and metadata["issuer"] == auth.issuer
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    client_id, tokens = sign_in(auth)
    assert auth.accepts(tokens["access_token"])
    status, listing, _ = rpc(auth, "tools/list", tokens["access_token"])
    assert status == 200 and listing["result"]["tools"][0]["name"] == "search"
    status, result, _ = rpc(auth, "tools/call", tokens["access_token"], {"name": "search"})
    assert result["result"]["content"][0]["text"] == "called search"
    assert len(auth.registrations) == 1 and len(auth.token_requests) == 1
    assert auth.authorize_requests[0]["state"] == "s1"
    assert {tokens["access_token"], tokens["refresh_token"], VERIFIER} <= set(auth.secrets)
    assert [(item.method, item.bearer is not None) for item in auth.mcp.seen] == [
        ("tools/list", True),
        ("tools/call", True),
    ]


def test_authorization_code_is_single_use_and_bound(make):
    auth = make()
    client_id = register(auth)

    def exchange(**changes):
        redirect = auth.approve(authorization_url(auth, client_id))
        form = {
            "grant_type": "authorization_code",
            "code": parse_qs(urlsplit(redirect).query)["code"][0],
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": auth.rs_url,
            **changes,
        }
        return form, post_form(f"{auth.urls.as_origin}/token", form)

    form, (status, _, _) = exchange()
    assert status == 200
    assert post_form(f"{auth.urls.as_origin}/token", form)[1] == {"error": "invalid_grant"}
    for change in ({"code_verifier": "w" * 43}, {"redirect_uri": "https://x.example/cb"}, {"resource": "https://other.example/mcp"}):
        _, (status, body, _) = exchange(**change)
        assert (status, body) == (400, {"error": "invalid_grant"})
    _, (status, body, _) = exchange(client_id="dyn-unknown")
    assert (status, body) == (401, {"error": "invalid_client"})


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"response_type": "token"}, "unsupported request"),
        ({"code_challenge_method": "plain"}, "unsupported request"),
        ({"code_challenge": ""}, "missing code_challenge or resource"),
        ({"resource": ""}, "missing code_challenge or resource"),
        ({"redirect_uri": "https://evil.example/cb"}, "redirect_uri not registered"),
        ({"client_id": "https://client.example/doc"}, "invalid client metadata document"),
    ],
)
def test_approve_refuses_what_a_browser_would_be_refused(make, params, reason):
    auth = make()
    client_id = register(auth)
    with pytest.raises(FixtureRefusal, match=reason):
        auth.approve(authorization_url(auth, client_id, **params))


def test_approve_refuses_other_endpoints(make):
    auth = make()
    with pytest.raises(FixtureRefusal, match="not the authorization endpoint"):
        auth.approve("https://elsewhere.example/authorize?x=1")


def test_preregistered_and_cimd_clients(make):
    auth = make(preregistered_client_ids=("astral-uk",), cimd_supported=True, cimd_documents={"https://c.example/doc": [REDIRECT]})
    assert parse_qs(urlsplit(auth.approve(authorization_url(auth, "astral-uk"))).query)["code"]
    assert parse_qs(urlsplit(auth.approve(authorization_url(auth, "https://c.example/doc"))).query)["code"]
    with pytest.raises(FixtureRefusal):
        auth.approve(authorization_url(auth, "https://other.example/doc"))
    _, metadata, _ = call(f"{auth.issuer}/.well-known/oauth-authorization-server")
    assert metadata["client_id_metadata_document_supported"] is True


@pytest.mark.parametrize(
    ("switch", "path"),
    [("oauth", "/.well-known/oauth-authorization-server/realms/uk"), ("openid", "/.well-known/openid-configuration/realms/uk"), ("openid_append", "/realms/uk/.well-known/openid-configuration")],
)
def test_metadata_location_switch(make, switch, path):
    auth = make(metadata_at=switch, issuer_path="/realms/uk")
    status, metadata, _ = call(auth.urls.as_origin + path)
    assert status == 200 and metadata["issuer"] == auth.urls.as_origin + "/realms/uk"
    assert call(auth.urls.as_origin + "/.well-known/other")[0] == 404


@pytest.mark.parametrize(("where", "path"), [("path", "/.well-known/oauth-protected-resource/mcp"), ("root", "/.well-known/oauth-protected-resource")])
def test_resource_metadata_location_switch(make, where, path):
    auth = make(wellknown_at=where, scopes_supported=["a", "b"], extra_authorization_servers=("https://second.example",))
    status, prm, _ = call(auth.urls.rs_origin + path)
    assert status == 200
    assert prm["scopes_supported"] == ["a", "b"] and prm["authorization_servers"][-1] == "https://second.example"
    other = "/.well-known/oauth-protected-resource" if where == "path" else "/.well-known/oauth-protected-resource/mcp"
    assert call(auth.urls.rs_origin + other)[0] == 404


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Challenge.RESOURCE_METADATA, 1),
        (Challenge.TWO_LINES, 2),
        (Challenge.BARE, 1),
        (Challenge.ABSENT, 0),
    ],
)
def test_challenge_modes(make, mode, expected):
    auth = make(challenge=mode)
    status, _, lines = rpc(auth, "tools/list")
    assert status == 401 and len(lines) == expected
    if mode is Challenge.BARE:
        assert lines == ["Bearer"]
    if mode is Challenge.TWO_LINES:
        assert lines[0].startswith("Basic ") and "resource_metadata=" in lines[1]


def test_challenge_without_scope_and_with_advertised_url(make):
    auth = make(challenge_scope=None, resource_metadata_url="https://other.example/prm")
    assert rpc(auth, "tools/list")[2] == ['Bearer resource_metadata="https://other.example/prm"']


def test_pkce_and_endpoint_switches(make):
    auth = make(pkce_methods=None, registration_endpoint=False, advertise_iss=False, device_endpoint=True, metadata_issuer="https://liar.example")
    _, metadata, _ = call(f"{auth.issuer}/.well-known/oauth-authorization-server")
    assert "code_challenge_methods_supported" not in metadata
    assert "registration_endpoint" not in metadata and "authorization_response_iss_parameter_supported" not in metadata
    assert metadata["device_authorization_endpoint"].endswith("/device") and metadata["issuer"] == "https://liar.example"
    assert post_json(f"{auth.urls.as_origin}/register", {})[0] == 404
    plain = make(pkce_methods=["plain"])
    assert call(f"{plain.issuer}/.well-known/oauth-authorization-server")[1]["code_challenge_methods_supported"] == ["plain"]


def test_endpoint_overrides(make):
    auth = make(authorization_endpoint="https://a.example/authorize", token_endpoint="https://a.example/token", registration_url="https://a.example/register", device_endpoint=True, device_url="https://a.example/device")
    metadata = call(f"{auth.issuer}/.well-known/oauth-authorization-server")[1]
    assert (metadata["authorization_endpoint"], metadata["token_endpoint"], metadata["registration_endpoint"], metadata["device_authorization_endpoint"]) == ("https://a.example/authorize", "https://a.example/token", "https://a.example/register", "https://a.example/device")


def test_registration_switches(make):
    auth = make(reject_native_https_registration=True)
    url = f"{auth.urls.as_origin}/register"
    native = {"redirect_uris": [REDIRECT], "application_type": "native"}
    assert post_json(url, native)[:2] == (400, {"error": "invalid_redirect_uri"})
    status, body, _ = post_json(url, {**native, "application_type": "web"})
    assert status == 201 and body["client_id"].startswith("dyn-")
    assert len(auth.registrations) == 2
    scripted = make(register_script=[(500, {}), (201, {"client_id": "dyn-1"})])
    url = f"{scripted.urls.as_origin}/register"
    assert post_json(url, native)[0] == 500
    assert post_json(url, native)[:2] == (201, {"client_id": "dyn-1"})


def test_confidential_registration_demands_the_secret(make):
    auth = make(registration_auth="client_secret_basic")
    client_id = register(auth)
    secret = next(value for value in auth.secrets if value)
    redirect = auth.approve(authorization_url(auth, client_id))
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER, "resource": auth.rs_url}
    wrong = base64.b64encode(f"{client_id}:nope".encode()).decode()
    assert post_form(f"{auth.urls.as_origin}/token", form, {"authorization": f"Basic {wrong}"})[:2] == (401, {"error": "invalid_client"})
    assert post_form(f"{auth.urls.as_origin}/token", form, {"authorization": "Basic !!!"})[0] == 401
    assert post_form(f"{auth.urls.as_origin}/token", form, {"authorization": "Basic " + base64.b64encode(b"nocolon").decode()})[0] == 401
    right = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    assert post_form(f"{auth.urls.as_origin}/token", form, {"authorization": f"Basic {right}"})[0] == 200


@pytest.mark.parametrize(
    ("mode", "values"),
    [("correct", None), ("wrong", [WRONG_ISSUER]), ("omit", [])],
)
def test_iss_modes(make, mode, values):
    auth = make(iss_mode=mode)
    client_id = register(auth)
    query = parse_qs(urlsplit(auth.approve(authorization_url(auth, client_id))).query)
    assert query.get("iss", []) == ([auth.issuer] if values is None else values)
    assert query["state"] == ["s1"]


def test_refresh_grant(make):
    auth = make()
    client_id, tokens = sign_in(auth)
    status, fresh, _ = post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))
    assert status == 200 and "refresh_token" not in fresh and auth.accepts(fresh["access_token"])
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens, "https://other.example/mcp"))[:2] == (400, {"error": "invalid_grant"})


def test_refresh_refused_switch_and_toggle(make):
    auth = make(refuse_refresh=True)
    client_id, tokens = sign_in(auth)
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))[:2] == (400, {"error": "invalid_grant"})
    auth.configure(lambda config: setattr(config, "refuse_refresh", False))
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))[0] == 200


def test_rotating_refresh_tokens_retire_the_old_one(make):
    auth = make(rotate_refresh=True)
    client_id, tokens = sign_in(auth)
    status, fresh, _ = post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))
    assert status == 200 and fresh["refresh_token"] != tokens["refresh_token"]
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))[0] == 400
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, fresh))[0] == 200


def test_no_refresh_token_switch(make):
    auth = make(omit_refresh=True)
    _, tokens = sign_in(auth)
    assert "refresh_token" not in tokens and tokens["token_type"] == "Bearer"


def test_unsupported_grant_type(make):
    auth = make()
    assert post_form(f"{auth.urls.as_origin}/token", {"grant_type": "password"})[:2] == (400, {"error": "unsupported_grant_type"})


def test_insufficient_scope_on_tool_calls_only(make):
    auth = make(forbid_scope="files.write")
    _, tokens = sign_in(auth)
    token = tokens["access_token"]
    assert rpc(auth, "tools/list", token)[0] == 200
    status, _, lines = rpc(auth, "tools/call", token, {"name": "search"})
    assert (status, lines) == (403, ['Bearer error="insufficient_scope", scope="files.write"'])
    assert auth.mcp.seen[-1].method == "tools/call"
    auth.configure(lambda config: setattr(config, "forbid_scope", None))
    assert rpc(auth, "tools/call", token, {"name": "search"})[0] == 200


def test_device_grant_walks_every_state(make):
    steps = [DeviceStep.PENDING, DeviceStep.SLOW_DOWN, DeviceStep.DENIED, DeviceStep.EXPIRED, DeviceStep.APPROVE]
    auth = make(device_endpoint=True, device_steps=steps)
    status, grant, _ = post_form(f"{auth.urls.as_origin}/device", {"client_id": "x", "resource": auth.rs_url})
    assert status == 200 and grant["user_code"] == "WDJB-MJHT" and grant["verification_uri_complete"].endswith("user_code=WDJB-MJHT")
    assert grant["device_code"] in auth.secrets and auth.device_requests == [{"client_id": "x", "resource": auth.rs_url}]
    poll = {"grant_type": DEVICE_GRANT, "device_code": grant["device_code"], "client_id": "x"}
    answers = [post_form(f"{auth.urls.as_origin}/token", poll) for _ in steps]
    assert [body.get("error") for _, body, _ in answers[:4]] == ["authorization_pending", "slow_down", "access_denied", "expired_token"]
    assert answers[4][0] == 200 and auth.accepts(answers[4][1]["access_token"])
    assert post_form(f"{auth.urls.as_origin}/token", poll)[0] == 200
    assert post_form(f"{auth.urls.as_origin}/token", {**poll, "device_code": "unknown"})[:2] == (400, {"error": "invalid_grant"})


def test_device_endpoint_is_absent_by_default(make):
    auth = make()
    assert post_form(f"{auth.urls.as_origin}/device", {})[0] == 404
    assert "device_authorization_endpoint" not in call(f"{auth.issuer}/.well-known/oauth-authorization-server")[1]


def test_tools_need_the_token_and_the_protocol_version(make):
    auth = make()
    _, tokens = sign_in(auth)
    token = tokens["access_token"]
    assert rpc(auth, "tools/list", "wrong")[0] == 401
    status, body, _ = rpc(auth, "tools/list", token, meta=False)
    assert status == 400 and body["error"] == "UnsupportedProtocolVersionError"
    status, body, _ = rpc(auth, "server/discover", token, meta=False)
    assert body["result"]["supportedVersions"] == [PROTOCOL_VERSION]
    assert rpc(auth, "tools/call", token, {"name": "missing"})[1]["error"]["code"] == -32602
    assert rpc(auth, "resources/list", token)[1]["error"]["code"] == -32601
    assert call(f"{auth.rs_url}?access_token={token}", "POST", b"{}", {"content-type": "application/json", "authorization": f"Bearer {token}"})[0] == 400
    assert call(auth.urls.rs_origin + "/elsewhere", "POST", b"{}")[0] == 404
    auth.configure(lambda config: setattr(config, "require_protocol_meta", False))
    assert rpc(auth, "tools/list", token, meta=False)[0] == 200


def test_a_token_for_another_resource_is_refused(make):
    auth = make()
    client_id = register(auth)
    redirect = auth.approve(authorization_url(auth, client_id, resource="https://other.example/mcp"))
    form = {"grant_type": "authorization_code", "code": parse_qs(urlsplit(redirect).query)["code"][0], "client_id": client_id, "redirect_uri": REDIRECT, "code_verifier": VERIFIER, "resource": "https://other.example/mcp"}
    token = post_form(f"{auth.urls.as_origin}/token", form)[1]["access_token"]
    assert not auth.accepts(token) and rpc(auth, "tools/list", token)[0] == 401


def test_a_changed_tool_definition_shows_in_the_next_listing(make):
    auth = make()
    _, tokens = sign_in(auth)
    token = tokens["access_token"]
    before = rpc(auth, "tools/list", token)[1]["result"]["tools"]
    auth.redefine_tool("search", description="Search everything")
    after = rpc(auth, "tools/list", token)[1]["result"]["tools"]
    assert before[0]["description"] == "Search" and after[0]["description"] == "Search everything"
    assert before[0]["name"] == after[0]["name"] and before[0]["inputSchema"] == after[0]["inputSchema"]
    with pytest.raises(KeyError):
        auth.redefine_tool("absent", description="x")


def test_token_lifecycle_switches(make):
    auth = make()
    client_id, tokens = sign_in(auth)
    token = tokens["access_token"]
    auth.expire_access_tokens()
    assert not auth.accepts(token) and rpc(auth, "tools/list", token)[0] == 401
    auth.revoke_clients()
    assert post_form(f"{auth.urls.as_origin}/token", refresh_form(auth, client_id, tokens))[:2] == (401, {"error": "invalid_client"})
    rejecting = make(reject_bearers=True)
    assert rpc(rejecting, "tools/list", sign_in(rejecting)[1]["access_token"])[0] == 401


def test_resource_server_failure_modes(make):
    failing = make(fail_tools=True)
    assert rpc(failing, "tools/list", sign_in(failing)[1]["access_token"])[0] == 503
    redirecting = make(redirect_to="https://elsewhere.example/mcp")
    conn = http.client.HTTPConnection("127.0.0.1", urlsplit(redirecting.rs_url).port)
    conn.request("POST", "/mcp", b"{}", {"authorization": "Bearer " + sign_in(redirecting)[1]["access_token"]})
    reply = conn.getresponse()
    assert reply.status == 307 and reply.getheader("location") == "https://elsewhere.example/mcp"
    conn.close()
    opened = make(open=True)
    assert rpc(opened, "tools/list")[0] == 200


def test_stopping_the_resource_server(make):
    auth = make()
    auth.stop_resource_server()
    with pytest.raises(OSError):
        rpc(auth, "tools/list")
    assert call(f"{auth.issuer}/.well-known/oauth-authorization-server")[0] == 200
    auth.stop_resource_server()


def test_requests_are_recorded_per_role(make):
    auth = make()
    rpc(auth, "tools/list")
    call(f"{auth.issuer}/.well-known/oauth-authorization-server")
    assert auth.requests_to("rs") == ["POST /mcp"] and auth.requests_to("as") == ["GET /.well-known/oauth-authorization-server"]


def test_context_manager_stops_the_servers():
    with FixtureAuth() as auth:
        url = auth.urls.as_origin
        assert call(f"{url}/x")[0] == 404
    with pytest.raises(OSError):
        call(f"{url}/x")


def test_model_server_scripts_a_tool_call_then_a_reply():
    with ModelServer(tool="roll_dice", arguments={"n": 2}, reply="rolled") as model:
        first = post_json(model.url + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        assert first[0] == 200
        second = post_json(model.url + "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}, {"role": "tool", "content": "7"}]})
        assert len(model.requests) == 2 and model.authorizations == [None, None]
    assert model.requests[1]["messages"][-1]["role"] == "tool"
    assert first[1] is None and second[1] is None


def stream_of(model, messages, key=None):
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(model.url + "/v1/chat/completions", json.dumps({"messages": messages}).encode(), headers)
    with urllib.request.urlopen(request, timeout=10) as reply:
        text = reply.read().decode()
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


def test_model_server_stream_shapes_and_records_authorization():
    with ModelServer(tool="roll_dice", arguments={"n": 2}, reply="rolled") as model:
        calls = stream_of(model, [{"role": "user", "content": "go"}], "key-1")
        done = stream_of(model, [{"role": "user", "content": "go"}, {"role": "tool", "content": "7"}])
        assert model.authorizations == ["Bearer key-1", None]
    call_delta = calls[0]["choices"][0]["delta"]["tool_calls"][0]["function"]
    assert call_delta["name"] == "roll_dice" and json.loads(call_delta["arguments"]) == {"n": 2}
    assert calls[-1]["choices"][0]["finish_reason"] == "tool_calls" and calls[-1]["usage"]["prompt_tokens"] == 12
    assert done[0]["choices"][0]["delta"] == {"content": "rolled"} and done[-1]["choices"][0]["finish_reason"] == "stop"


def test_model_server_follows_an_explicit_script_and_repeats_its_last_turn():
    script = [ScriptedTurn(tool="a"), ScriptedTurn(tool="b", arguments={"k": 1}), ScriptedTurn(text="end")]
    with ModelServer(script=script) as model:
        user = {"role": "user", "content": "x"}
        names = []
        for answered in range(4):
            deltas = stream_of(model, [user] + [{"role": "tool", "content": "r"}] * answered)
            delta = deltas[0]["choices"][0]["delta"]
            names.append(delta["tool_calls"][0]["function"]["name"] if "tool_calls" in delta else delta["content"])
    assert names == ["a", "b", "end", "end"]


def test_model_server_without_a_tool_just_replies():
    with ModelServer(reply="hello") as model:
        assert stream_of(model, [{"role": "user", "content": "x"}])[0]["choices"][0]["delta"] == {"content": "hello"}


def start_runner(*args):
    return subprocess.Popen([sys.executable, "-m", "tests.fixtures.connections.serve", *args], cwd=HERE.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def stop_runner(process):
    process.terminate()
    return process.wait(timeout=30)


def test_standalone_runner_serves_every_endpoint():
    process = start_runner()
    try:
        urls = json.loads(process.stdout.readline())
        assert call(f"{urls['issuer']}/.well-known/oauth-authorization-server")[0] == 200
        assert call(f"{urls['resource_server']}/.well-known/oauth-protected-resource/mcp")[0] == 200
        assert post_json(urls["mcp"], {"method": "tools/list"})[0] == 401
        assert post_json(urls["model"], {"messages": []})[0] == 200
        assert all(value.startswith("http://127.0.0.1:") for value in urls.values())
    finally:
        assert stop_runner(process) == 0


def test_standalone_runner_needs_both_tls_files():
    process = start_runner("--tls-cert", "only-cert.pem")
    assert process.wait(timeout=30) == 2
    assert "go together" in process.stderr.read()


def self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


def test_tls_option_serves_https_in_process_and_standalone(tmp_path):
    cert, key = self_signed(tmp_path)
    context = ssl.create_default_context(cafile=str(cert))
    with FixtureAuth(tls=(str(cert), str(key))) as auth:
        assert auth.urls.as_origin.startswith("https://127.0.0.1:")
        assert call(f"{auth.issuer}/.well-known/oauth-authorization-server", context=context)[0] == 200
    process = start_runner("--tls-cert", str(cert), "--tls-key", str(key))
    try:
        urls = json.loads(process.stdout.readline())
        assert all(value.startswith("https://127.0.0.1:") for value in urls.values())
        assert call(f"{urls['issuer']}/.well-known/oauth-authorization-server", context=context)[0] == 200
        assert call(f"{urls['model']}/x", "POST", b"{}", {"content-type": "application/json"}, context=context)[0] == 200
    finally:
        assert stop_runner(process) == 0


def load_vectors():
    return json.loads(VECTORS.read_text())


def of_kind(kind):
    return [case for case in load_vectors()["cases"] if case["kind"] == kind]


def test_vector_copy_matches_the_contract_when_present():
    if CONTRACT.exists():
        assert VECTORS.read_bytes() == CONTRACT.read_bytes()
    document = load_vectors()
    assert (document["format"], document["name"], len(document["cases"])) == ("astral.vectors/v1", "connection-oauth-v1", 142)


def served(responses):
    """Serves each URL of a vector's response map from a loopback authorization server per host and returns the live URL for each."""
    by_host: dict[str, dict[str, tuple[int, object]]] = {}
    for url, answer in responses.items():
        parts = urlsplit(url)
        if answer is not None:
            by_host.setdefault(parts.netloc, {})[parts.path] = (answer["status"], answer.get("json"))
    live = {}
    servers = {}
    for host, canned in by_host.items():
        servers[host] = FixtureAuth(FixtureConfig(canned=canned))
        servers[host].start()
    for url in responses:
        parts = urlsplit(url)
        origin = servers[parts.netloc].urls.as_origin if parts.netloc in servers else None
        live[url] = origin
    return servers, live


def fetch(url, origin):
    path = urlsplit(url).path
    if origin is None:
        return 404, None
    status, body, _ = call(origin + path)
    return status, body


@pytest.mark.parametrize("kind", ["oauth_resource_metadata", "oauth_select_server"])
def test_vector_response_maps_are_served_as_written(kind):
    cases = of_kind(kind)
    served_urls = 0
    for case in cases:
        responses = case["input"]["responses"]
        servers, live = served(responses)
        try:
            for url, answer in responses.items():
                status, body = fetch(url, live[url])
                if answer is None:
                    assert status == 404
                    continue
                assert status == answer["status"], case["name"]
                if "json" in answer and status != 302:
                    assert body == answer["json"], case["name"]
                served_urls += 1
        finally:
            for server in servers.values():
                server.stop()
    assert len(cases) > 0 and served_urls > 0


def test_vector_challenge_headers_are_served_verbatim():
    cases = of_kind("oauth_challenge")
    assert len(cases) == 19
    for case in cases:
        header = case["input"]["header"]
        lines = tuple(header) if isinstance(header, list) else (() if header is None else (header,))
        with FixtureAuth(FixtureConfig(challenge_lines=lines)) as auth:
            assert rpc(auth, "tools/list")[2] == list(lines), case["name"]


def test_vector_dynamic_registration_scripts_replay():
    replayed = 0
    for case in of_kind("oauth_dynamic_registration"):
        responses = case["input"]["responses"]
        if any(item is None for item in responses):
            continue
        script = [(item["status"], item.get("json", {})) for item in responses]
        outcome = case["expect"].get("ok", case["expect"])
        with FixtureAuth(FixtureConfig(register_script=list(script))) as auth:
            for sent, (status, reply) in zip(outcome["requests"], script, strict=True):
                got = post_json(f"{auth.urls.as_origin}/register", sent)
                assert (got[0], got[1]) == (status, reply), case["name"]
            assert auth.registrations == outcome["requests"], case["name"]
        replayed += 1
    assert replayed == 5


def test_vector_native_rejection_matches_the_two_step_registration():
    case = next(item for item in of_kind("oauth_dynamic_registration") if item["name"] == "dyn_native_400_then_web_accepted")
    native, web = case["expect"]["ok"]["requests"]
    with FixtureAuth(FixtureConfig(reject_native_https_registration=True)) as auth:
        assert post_json(f"{auth.urls.as_origin}/register", native)[0] == 400
        assert post_json(f"{auth.urls.as_origin}/register", web)[0] == 201
        assert auth.registrations == [native, web]


def iss_decision(supported, values, issuer):
    if supported is not True and not values:
        return "ok"
    if not values:
        return "connection.iss_missing"
    return "ok" if values == [issuer] else "connection.iss_mismatch"


def test_vector_iss_cases_agree_with_what_the_fixture_serves():
    cases = of_kind("oauth_iss")
    for case in cases:
        given = case["input"]
        config = FixtureConfig(advertise_iss=given["iss_supported"] is True, iss_override=tuple(given["iss"]), preregistered_client_ids=("c",))
        with FixtureAuth(config) as auth:
            metadata = call(f"{auth.issuer}/.well-known/oauth-authorization-server")[1]
            query = parse_qs(urlsplit(auth.approve(authorization_url(auth, "c"))).query, keep_blank_values=True)
        supported = metadata.get("authorization_response_iss_parameter_supported")
        assert (supported is True) == (given["iss_supported"] is True), case["name"]
        assert query.get("iss", []) == given["iss"], case["name"]
        decision = iss_decision(supported, query.get("iss", []), given["issuer"])
        expected = case["expect"].get("error", "ok")
        assert decision == expected, case["name"]


def test_vector_authorization_server_metadata_paths_are_the_ones_the_fixture_serves():
    checked = 0
    for case in of_kind("oauth_as_metadata_urls"):
        if "ok" not in case["expect"]:
            continue
        issuer_path = urlsplit(case["input"]["issuer"]).path.rstrip("/")
        expected = {urlsplit(url).path for url in case["expect"]["ok"]["urls"]}
        for mode in ("oauth", "openid", "openid_append"):
            auth = FixtureAuth(FixtureConfig(metadata_at=mode, issuer_path=issuer_path))
            auth.start()
            try:
                assert auth._metadata_path() in expected, (case["name"], mode)
                assert call(auth.urls.as_origin + auth._metadata_path())[0] == 200
            finally:
                auth.stop()
        checked += 1
    assert checked == 4


def test_vector_resource_metadata_fallback_paths_are_the_ones_the_fixture_serves():
    checked = 0
    for case in of_kind("oauth_resource_metadata_urls"):
        urls = case["expect"].get("ok", {}).get("urls", [])
        if urlsplit(case["input"]["endpoint"]).path != "/mcp" or len(urls) != 2:
            continue
        paths = {urlsplit(url).path for url in urls}
        for where in ("path", "root"):
            with FixtureAuth(FixtureConfig(wellknown_at=where)) as auth:
                served_path = "/.well-known/oauth-protected-resource" + ("/mcp" if where == "path" else "")
                assert served_path in paths, case["name"]
                assert call(auth.urls.rs_origin + served_path)[0] == 200
        checked += 1
    assert checked >= 1


def test_vector_scope_inputs_are_served_as_challenge_and_scopes_supported():
    served_count = 0
    for case in of_kind("oauth_scopes"):
        given = case["input"]
        challenge = given["challenge"]
        if challenge is not None and re.search(r'["\\]', challenge):
            continue
        with FixtureAuth(FixtureConfig(challenge_scope=challenge or None, scopes_supported=given["scopes_supported"])) as auth:
            line = rpc(auth, "tools/list")[2][0]
            prm = call(f"{auth.urls.rs_origin}/.well-known/oauth-protected-resource/mcp")[1]
        assert (f'scope="{challenge}"' in line) == bool(challenge), case["name"]
        assert prm.get("scopes_supported") == given["scopes_supported"], case["name"]
        served_count += 1
    assert served_count >= 8


def test_vector_step_up_challenges_are_the_ones_the_fixture_emits():
    pattern = re.compile(r'Bearer error="insufficient_scope", scope="([^"\\]*)"')
    emitted = 0
    for case in of_kind("oauth_step_up_sequence"):
        for step in case["input"]["steps"]:
            match = pattern.fullmatch(step.get("forbidden", ""))
            if match is None:
                continue
            with FixtureAuth(FixtureConfig(forbid_scope=match.group(1), open=True)) as auth:
                assert rpc(auth, "tools/call", None, {"name": "search"})[2] == [step["forbidden"]]
            emitted += 1
    assert emitted >= 8


def test_vector_registration_metadata_is_served_as_the_authorization_server_document():
    for case in of_kind("oauth_registration"):
        metadata = case["input"]["metadata"]
        with FixtureAuth(FixtureConfig(canned={"/.well-known/oauth-authorization-server": (200, metadata)})) as auth:
            assert call(f"{auth.issuer}/.well-known/oauth-authorization-server")[1] == metadata, case["name"]


def test_every_vector_kind_is_either_exercised_or_a_pure_client_decision():
    pure_client = {"oauth_bearer_target"}
    exercised = {
        "oauth_challenge",
        "oauth_resource_metadata",
        "oauth_select_server",
        "oauth_dynamic_registration",
        "oauth_iss",
        "oauth_as_metadata_urls",
        "oauth_resource_metadata_urls",
        "oauth_scopes",
        "oauth_step_up_sequence",
        "oauth_registration",
    }
    assert set(Counter(case["kind"] for case in load_vectors()["cases"])) == pure_client | exercised
