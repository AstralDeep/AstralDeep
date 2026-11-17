"""Proves the personal node's operator connections API through the orchestrator's own app: loopback, pasted and device-code sign-ins reach signed in against the fixture OAuth server, a flow can be cancelled, the fifth open flow evicts the oldest, a failed complete consumes its flow, and polling keeps its own rate window.
It also proves every route refuses a remote client, a forwarded request, a missing, repeated or wrong operator secret and a bearer, that no response or log line carries the secret, a code or a token, and that the real kernel binding lists a connection and refuses a node a connection that is local to another device.
"""

from __future__ import annotations

import base64
import logging
import re
import stat
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import httpx
import jose.jwt
import pytest

from orchestrator import node_connection_api as api
from orchestrator import node_mesh
from orchestrator.node_mailbox import MailboxSettings
from shared import node_identity as ni
from tests import fake_astral_kernel as fake
from tests.fixtures.connections.oauth_server import DeviceStep, FixtureAuth, FixtureConfig
from tests.helpers import orchestrator_app
from tests.helpers.dev_mesh import connection_reached, dev_node
from tests.helpers.fixture_sign_in import CALLBACK, FixtureSignIn, connection

HEADER = "Astral-Node-Operator"
ROOT = "/api/node/connections"
FLOW = ROOT + "/sign-in/{flow}"
GONE = "connection.flow_unknown"
ID = "ab" * 16
CODE = re.compile(r"[a-z0-9_.]{1,64}")
LEGACY_KEYS = ["remote_mcp_one", "other", "remote_mcp_two"]


class Operator:
    def __init__(self, app, secret: str):
        self.app, self.secret = app, secret

    async def send(self, method, path, body=None, *, secret=True, host="127.0.0.1", headers=None, content=None):
        sent = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        if secret is True:
            sent.append((HEADER, self.secret))
        elif secret not in (False, None):
            sent.append((HEADER, secret))
        transport = httpx.ASGITransport(app=self.app, client=(host, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8001") as http:
            return await http.request(method, path, json=body, content=content, headers=sent)

    async def begin(self, connection_id, body):
        response = await self.send("POST", f"{ROOT}/{connection_id}/sign-in", body)
        assert response.status_code == 200, response.text
        return response.json()


def personal(monkeypatch, tmp_path):
    home = tmp_path / "node"
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(home))
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE=")
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    return home


@pytest.fixture
def fixture_server():
    with FixtureAuth(FixtureConfig(device_endpoint=True)) as auth:
        yield auth


@pytest.fixture
async def node(tmp_path, monkeypatch, fixture_server):
    home = personal(monkeypatch, tmp_path)
    fake.Kernel.instances.clear()
    mesh = node_mesh.NodeMesh(data_dir=home, binding=fake,
                              settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
    await mesh.start()
    node_mesh.install(mesh)
    kernel = mesh.kernel
    kernel.join()
    signing = FixtureSignIn(kernel, fixture_server, connection(ID, "fixture", fixture_server.rs_url))
    app = orchestrator_app.build(monkeypatch, orchestrator_app.host(), surfaces=())
    secret = (home / node_mesh.OPERATOR_SECRET_FILE).read_text()
    yield NS(mesh=mesh, kernel=kernel, app=app, op=Operator(app, secret), id=ID, secret=secret, home=home,
             server=fixture_server, signing=signing)
    node_mesh.install(None)
    await mesh.stop()
    ni.reset_cache()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(api, "_clock", lambda: now["t"])
    return now


def approved(node, started) -> str:
    return node.server.approve(started["authorization_url"])


async def complete(node, flow, url):
    return await node.op.send("POST", FLOW.format(flow=flow) + "/complete", {"redirect_url": url})


async def test_the_operator_lists_connections_with_their_state(node):
    response = await node.op.send("GET", ROOT)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "connections": [{"id": ID, "name": "fixture", "status": "ok", "state": "not_signed_in", "reason": None}],
        "legacy_connectors": 0,
    }


async def test_a_loopback_sign_in_reaches_signed_in(node):
    started = await node.op.begin(ID, {"redirect": "loopback", "port": 49152})
    assert set(started) == {"flow", "authorization_url"}
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A49152%2Fcallback" in started["authorization_url"]
    back = approved(node, started)
    assert back.startswith("http://127.0.0.1:49152/callback?")
    done = await complete(node, started["flow"], back)
    assert done.status_code == 200 and done.json() == {"state": "signed_in", "reason": None}
    [row] = (await node.op.send("GET", ROOT)).json()["connections"]
    assert row["state"] == "signed_in"
    again = await complete(node, started["flow"], back)
    assert (again.status_code, again.json()) == (404, {"detail": GONE})


async def test_a_pasted_sign_in_reaches_signed_in(node):
    started = await node.op.begin(ID, {"redirect": "pasted"})
    assert "redirect_uri=https%3A%2F%2Fastraldeep.github.io%2Foauth%2Fcallback" in started["authorization_url"]
    back = approved(node, started)
    assert back.startswith(CALLBACK + "?")
    done = await complete(node, started["flow"], back)
    assert done.json() == {"state": "signed_in", "reason": None}


async def started_device(node, *steps):
    node.server.configure(lambda config: setattr(config, "device_steps", list(steps)))
    started = await node.op.begin(ID, {"redirect": "device_code"})
    assert set(started) == {"flow", "user_code", "verification_uri", "interval", "expires_in"}
    assert started["user_code"] == "WDJB-MJHT" and started["interval"] == 5 and 590 <= started["expires_in"] <= 600
    return started["flow"]


async def poll(node, flow):
    response = await node.op.send("POST", FLOW.format(flow=flow) + "/poll", {})
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_device_code_sign_in_is_pending_until_the_interval_then_signed_in(node):
    flow = await started_device(node, DeviceStep.PENDING, DeviceStep.APPROVE)
    assert await poll(node, flow) == {"status": "authorization_pending"}
    assert node.server.token_requests == []
    node.signing.advance(6)
    assert await poll(node, flow) == {"status": "authorization_pending"}
    assert len(node.server.token_requests) == 1
    node.signing.advance(6)
    assert await poll(node, flow) == {"status": "signed_in"}
    [row] = (await node.op.send("GET", ROOT)).json()["connections"]
    assert row["state"] == "signed_in"
    assert (await node.op.send("POST", FLOW.format(flow=flow) + "/poll", {})).json() == {"detail": GONE}


async def test_slow_down_is_reported_once_and_pending_follows(node):
    flow = await started_device(node, DeviceStep.SLOW_DOWN, DeviceStep.PENDING)
    node.signing.advance(6)
    assert await poll(node, flow) == {"status": "slow_down"}
    node.signing.advance(11)
    assert await poll(node, flow) == {"status": "authorization_pending"}


@pytest.mark.parametrize("step,status", [(DeviceStep.DENIED, "access_denied"), (DeviceStep.EXPIRED, "expired_token")])
async def test_a_device_code_poll_reports_a_final_service_answer_and_ends_the_flow(node, step, status):
    flow = await started_device(node, step)
    node.signing.advance(6)
    assert await poll(node, flow) == {"status": status}
    assert (await node.op.send("POST", FLOW.format(flow=flow) + "/poll", {})).status_code == 404
    assert (await node.op.send("DELETE", FLOW.format(flow=flow))).status_code == 404


async def test_the_kernel_expiring_a_flow_answers_expired_token(node):
    flow = await started_device(node, DeviceStep.PENDING)

    async def expired(_flow):
        raise fake.KernelError.State("connection.flow_expired", "expired")

    node.kernel.poll_connection_sign_in = expired
    assert await poll(node, flow) == {"status": "expired_token"}


async def test_a_flow_can_be_cancelled_once(node):
    started = await node.op.begin(ID, {"redirect": "pasted"})
    cancelled = await node.op.send("DELETE", FLOW.format(flow=started["flow"]))
    assert (cancelled.status_code, cancelled.json()) == (200, {"cancelled": True})
    assert node.signing.cancelled == [started["flow"]] and node.signing.flows == {}
    again = await node.op.send("DELETE", FLOW.format(flow=started["flow"]))
    assert (again.status_code, again.json()) == (404, {"detail": GONE})
    late = await complete(node, started["flow"], approved(node, started))
    assert late.status_code == 404


async def test_the_fifth_open_flow_evicts_the_oldest(node, clock):
    flows = [(await node.op.begin(ID, {"redirect": "pasted"}))["flow"] for _ in range(api.MAX_FLOWS)]
    clock["t"] += api.WINDOW_SECONDS
    fifth = await node.op.begin(ID, {"redirect": "pasted"})
    assert node.signing.cancelled == [flows[0]] and len(node.signing.flows) == api.MAX_FLOWS
    clock["t"] += api.WINDOW_SECONDS
    assert (await node.op.send("DELETE", FLOW.format(flow=flows[0]))).status_code == 404
    for survivor in flows[1:] + [fifth["flow"]]:
        assert (await node.op.send("DELETE", FLOW.format(flow=survivor))).status_code == 200


async def test_a_failed_complete_consumes_its_flow(node):
    started = await node.op.begin(ID, {"redirect": "pasted"})
    bad = await complete(node, started["flow"], CALLBACK + "?code=abc&state=wrong")
    assert (bad.status_code, bad.json()) == (409, {"detail": "oauth.state_mismatch"})
    retry = await complete(node, started["flow"], approved(node, started))
    assert (retry.status_code, retry.json()) == (404, {"detail": GONE})
    [row] = (await node.op.send("GET", ROOT)).json()["connections"]
    assert row["state"] == "not_signed_in"


async def test_a_token_the_service_refuses_fails_the_complete_with_its_code(node):
    started = await node.op.begin(ID, {"redirect": "pasted"})
    back = approved(node, started)
    node.server.config.canned["/token"] = (400, {"error": "invalid_grant"})
    refused = await complete(node, started["flow"], back)
    assert (refused.status_code, refused.json()) == (409, {"detail": "oauth.token_refused"})
    assert node.signing.cancelled == [started["flow"]]


async def test_polls_have_their_own_window_that_allows_a_call_every_six_seconds(node, clock):
    path = FLOW.format(flow="unknown") + "/poll"
    for _ in range(30):
        assert (await node.op.send("POST", path, {})).status_code == 404
        clock["t"] += 6.0
    clock["t"] += api.WINDOW_SECONDS
    for _ in range(api.CALLS_PER_WINDOW):
        assert (await node.op.send("POST", path, {})).status_code == 404
    limited = await node.op.send("POST", path, {})
    assert (limited.status_code, limited.json()) == (429, {"detail": "node.rate_limited"})
    assert 1 <= int(limited.headers["retry-after"]) <= 60 and limited.headers["cache-control"] == "no-store"
    assert (await node.op.send("GET", ROOT)).status_code == 200


async def test_the_other_routes_share_a_window_of_ten_a_minute(node, clock):
    for _ in range(api.CALLS_PER_WINDOW):
        assert (await node.op.send("GET", ROOT)).status_code == 200
    assert (await node.op.send("GET", ROOT)).status_code == 429
    assert (await node.op.send("DELETE", FLOW.format(flow="unknown"))).status_code == 429
    clock["t"] += api.WINDOW_SECONDS
    assert (await node.op.send("GET", ROOT)).status_code == 200


ROUTES = [
    ("GET", ROOT, None),
    ("POST", f"{ROOT}/{ID}/sign-in", {"redirect": "pasted"}),
    ("POST", FLOW.format(flow="f") + "/complete", {"redirect_url": CALLBACK}),
    ("POST", FLOW.format(flow="f") + "/poll", {}),
    ("DELETE", FLOW.format(flow="f"), None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_a_remote_client_and_a_forwarded_request(node, method, path, body):
    remote = await node.op.send(method, path, body, host="203.0.113.9")
    assert (remote.status_code, remote.json()) == (403, {"detail": "node.loopback_required"})
    assert remote.headers["cache-control"] == "no-store"
    for name in ("Forwarded", "X-Forwarded-For"):
        forwarded = await node.op.send(method, path, body, headers={name: "203.0.113.9"})
        assert (forwarded.status_code, forwarded.json()) == (403, {"detail": "node.loopback_required"})
    mapped = await node.op.send(method, path, body, host="::ffff:203.0.113.9")
    assert mapped.status_code == 403
    assert node.signing.flows == {} and node.signing.cancelled == []


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_a_missing_wrong_or_repeated_secret(node, method, path, body):
    for secret in [None, "", "x", node.secret[:-1], node.secret + "x", node.secret.upper()]:
        refused = await node.op.send(method, path, body, secret=secret)
        assert (refused.status_code, refused.json()) == (401, {"detail": "node.operator_secret_required"})
        assert refused.headers["cache-control"] == "no-store"
    repeated = await node.op.send(method, path, body, secret=False, headers=[(HEADER, node.secret), (HEADER, node.secret)])
    assert (repeated.status_code, repeated.json()) == (401, {"detail": "node.operator_secret_required"})
    assert node.signing.flows == {}


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_a_bearer_without_reading_it(node, method, path, body, monkeypatch):
    parsed = []
    for name in ("decode", "get_unverified_claims", "get_unverified_header"):
        monkeypatch.setattr(jose.jwt, name, lambda *args, _name=name, **kwargs: parsed.append(_name))
    bearer = {"Authorization": "Bearer a.b.c"}
    refused = await node.op.send(method, path, body, secret=False, headers=bearer)
    assert (refused.status_code, refused.json()) == (401, {"detail": "node.operator_secret_required"})
    wrong = await node.op.send(method, path, body, secret="wrong", headers=bearer)
    remote = await node.op.send(method, path, body, secret=False, headers=bearer, host="203.0.113.9")
    assert (wrong.status_code, remote.status_code) == (401, 403) and parsed == []


async def test_the_gate_compares_the_secret_in_constant_time(node, monkeypatch):
    compared = []
    real = node_mesh.hmac.compare_digest
    monkeypatch.setattr(node_mesh.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    await node.op.send("GET", ROOT, secret="wrong")
    assert compared == [(node.secret.encode(), b"wrong")]


async def test_a_stopped_mesh_and_a_server_profile_refuse(node, monkeypatch):
    node_mesh.install(None)
    unavailable = await node.op.send("GET", ROOT)
    assert (unavailable.status_code, unavailable.json()) == (503, {"detail": "mesh_unavailable"})
    node_mesh.install(node.mesh)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.INSTITUTION)
    ni.reset_cache()
    refused = await node.op.send("GET", ROOT)
    assert (refused.status_code, refused.json()) == (404, {"detail": "node_profile_inactive"})


async def test_an_unavailable_kernel_listing_refuses_with_its_code(node):
    def broken():
        raise fake.KernelError.Store("store.closed", "closed")

    node.kernel.connections = broken
    refused = await node.op.send("GET", ROOT)
    assert (refused.status_code, refused.json()) == (503, {"detail": "store.closed"})


@pytest.mark.parametrize("body", [
    {"redirect": "loopback"},
    {"redirect": "loopback", "port": 0},
    {"redirect": "loopback", "port": 70000},
    {"redirect": "loopback", "port": True},
    {"redirect": "loopback", "port": "80"},
    {"redirect": "pasted", "port": 80},
    {"redirect": "app_link"},
    {},
])
async def test_a_malformed_sign_in_request_is_refused_with_a_code(node, body):
    refused = await node.op.send("POST", f"{ROOT}/{ID}/sign-in", body)
    assert (refused.status_code, refused.json()) == (400, {"detail": "node.request_invalid"})
    assert node.signing.flows == {}


async def test_malformed_bodies_ids_and_sizes_are_refused(node, clock):
    sign_in = f"{ROOT}/{ID}/sign-in"
    assert (await node.op.send("POST", sign_in, content=b"[1]")).json() == {"detail": "node.request_invalid"}
    assert (await node.op.send("POST", sign_in, content=b"{")).status_code == 400
    assert (await node.op.send("POST", f"{ROOT}/not-hex/sign-in", {"redirect": "pasted"})).json() == {"detail": "connection.unknown"}
    unknown = await node.op.send("POST", f"{ROOT}/{'0' * 32}/sign-in", {"redirect": "pasted"})
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "connection.unknown"})
    started = await node.op.begin(ID, {"redirect": "pasted"})
    clock["t"] += api.WINDOW_SECONDS
    path = FLOW.format(flow=started["flow"]) + "/complete"
    for body in ({}, {"redirect_url": ""}, {"redirect_url": 5}, {"redirect_url": CALLBACK, "x": 1}):
        assert (await node.op.send("POST", path, body)).json() == {"detail": "node.request_invalid"}
    clock["t"] += api.WINDOW_SECONDS
    big = await node.op.send("POST", path, {"redirect_url": "https://x/" + "a" * api.MAX_URL})
    assert big.json() == {"detail": "node.request_invalid"}
    huge = await node.op.send("POST", path, content=b"{" + b" " * api.MAX_BODY + b"}")
    assert (huge.status_code, huge.json()) == (413, {"detail": "node.request_too_large"})
    assert (await node.op.send("DELETE", ROOT + "/sign-in/" + "x" * 200)).json() == {"detail": GONE}
    assert started["flow"] in node.signing.flows


async def test_polling_a_redirect_flow_is_refused_and_an_old_flow_is_gone(node, clock):
    started = await node.op.begin(ID, {"redirect": "pasted"})
    refused = await node.op.send("POST", FLOW.format(flow=started["flow"]) + "/poll", {})
    assert (refused.status_code, refused.json()) == (409, {"detail": "connection.flow_kind"})
    clock["t"] += api.FLOW_TTL_SECONDS + 1
    assert (await node.op.send("DELETE", FLOW.format(flow=started["flow"]))).json() == {"detail": GONE}


async def test_a_kernel_code_outside_the_code_alphabet_is_replaced(node):
    async def odd(*_args):
        raise fake.KernelError.State("Not A Code!", "x")

    node.kernel.begin_connection_sign_in = odd
    refused = await node.op.send("POST", f"{ROOT}/{ID}/sign-in", {"redirect": "pasted"})
    assert (refused.status_code, refused.json()) == (409, {"detail": "node.sign_in_failed"})


async def test_no_response_or_log_line_carries_the_secret_a_code_or_a_token(node, caplog, clock):
    caplog.set_level(logging.DEBUG)
    seen = []
    started = await node.op.begin(ID, {"redirect": "loopback", "port": 49153})
    seen.append(str(started))
    back = approved(node, started)
    for response in (
        await complete(node, started["flow"], back),
        await node.op.send("GET", ROOT),
        await node.op.send("GET", ROOT, secret="wrong"),
        await complete(node, "f", back),
        await node.op.send("POST", FLOW.format(flow="f") + "/complete", {"redirect_url": back}, host="203.0.113.9"),
    ):
        seen.append(response.text + str(response.headers))
    other = await node.op.begin(ID, {"redirect": "pasted"})
    bad = await complete(node, other["flow"], CALLBACK + "?code=SECRETCODE&state=SECRETSTATE")
    seen.append(bad.text)
    text = "\n".join(seen) + caplog.text
    assert node.secret not in text and urlsplit(back).query not in text
    assert "SECRETCODE" not in text and "SECRETSTATE" not in text
    assert node.server.secrets and not any(secret in text for secret in node.server.secrets)
    assert "access_token" not in text and "refresh_token" not in text


def test_the_operator_secret_is_private_random_and_kept(tmp_path):
    secret = node_mesh.OperatorSecret(tmp_path / "home")
    secret.ensure()
    path = tmp_path / "home" / node_mesh.OPERATOR_SECRET_FILE
    first = path.read_text()
    assert len(base64.urlsafe_b64decode(first + "=")) == 32 and "=" not in first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    secret.ensure()
    assert path.read_text() == first and secret.matches(first) and not secret.matches(first + "x")
    assert not secret.matches("")


def test_an_untrusted_secret_file_is_replaced_and_never_matches(tmp_path):
    secret = node_mesh.OperatorSecret(tmp_path)
    path = tmp_path / node_mesh.OPERATOR_SECRET_FILE
    assert not secret.matches("anything")
    secret.ensure()
    path.write_bytes(b"")
    assert not secret.matches("")
    secret.ensure()
    assert secret.matches(path.read_text())
    loosened = path.read_text()
    path.chmod(0o644)
    assert not secret.matches(loosened)
    secret.ensure()
    assert secret.matches(path.read_text()) and stat.S_IMODE(path.stat().st_mode) == 0o600
    path.unlink()
    target = tmp_path / "target"
    target.write_text("planted")
    path.symlink_to(target)
    assert not secret.matches("planted")
    secret.ensure()
    assert not path.is_symlink() and target.read_text() == "planted"


async def test_the_node_keeps_its_secret_across_a_restart(node):
    path = node.home / node_mesh.OPERATOR_SECRET_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    await node.mesh.stop()
    assert path.read_text() == node.secret
    await node.mesh.start()
    assert path.read_text() == node.secret


async def test_a_server_node_writes_no_operator_secret(tmp_path, monkeypatch):
    home = personal(monkeypatch, tmp_path)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.INSTITUTION)
    ni.reset_cache()
    mesh = node_mesh.NodeMesh(data_dir=home, binding=fake,
                              settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
    await mesh.start()
    await mesh.stop()
    ni.reset_cache()
    assert not (home / node_mesh.OPERATOR_SECRET_FILE).exists()


def test_a_row_reports_unsupported_with_its_reason():
    row = api._row({"id": ID, "name": "n", "unsupported": "connection.scheme_unsupported",
                    "this_device": {"state": "not_signed_in", "reason": None}})
    assert row == {"id": ID, "name": "n", "status": "unsupported", "state": "not_signed_in",
                   "reason": "connection.scheme_unsupported"}
    attention = api._row({"id": ID, "name": "n", "unsupported": None,
                          "this_device": {"state": "needs_attention", "reason": "refresh_refused"}})
    assert attention["status"] == "ok" and attention["reason"] == "refresh_refused"


@pytest.fixture
async def real(tmp_path, monkeypatch):
    with FixtureAuth(FixtureConfig(open=True)) as server:
        async with dev_node(tmp_path, monkeypatch) as mesh:
            personal(monkeypatch, tmp_path)
            node_mesh.install(mesh.node)
            credentials = NS(list_credential_keys=lambda owner, agent: LEGACY_KEYS)
            app = orchestrator_app.build(
                monkeypatch, orchestrator_app.host(credential_manager=credentials), surfaces=())
            secret = (tmp_path / "node" / node_mesh.OPERATOR_SECRET_FILE).read_text()
            info = await mesh.owner.add_connection(server.rs_url, "fixture", ["search"])
            await connection_reached(mesh, info.id)
            try:
                yield NS(mesh=mesh, op=Operator(app, secret), id=info.id)
            finally:
                node_mesh.install(None)


async def test_the_real_kernel_lists_a_connection_with_the_legacy_count(real):
    response = await real.op.send("GET", ROOT)
    assert response.status_code == 200
    assert response.json() == {
        "connections": [{"id": real.id, "name": "fixture", "status": "ok", "state": "not_signed_in", "reason": None}],
        "legacy_connectors": 2,
    }


async def test_the_real_kernel_refuses_a_node_a_connection_local_to_another_device(real):
    refused = await real.op.send("POST", f"{ROOT}/{real.id}/sign-in", {"redirect": "pasted"})
    assert (refused.status_code, refused.json()) == (409, {"detail": "connection.local_elsewhere"})
    unknown = await real.op.send("POST", f"{ROOT}/{'0' * 32}/sign-in", {"redirect": "loopback", "port": 49154})
    assert unknown.status_code == 404 and CODE.fullmatch(unknown.json()["detail"])
    assert real.mesh.node.kernel.connections()[0].this_device.state == "not_signed_in"


async def test_a_client_without_an_address_is_not_loopback(node):
    refused = await node.op.send("GET", ROOT, host="testclient")
    assert (refused.status_code, refused.json()) == (403, {"detail": "node.loopback_required"})


async def test_a_declared_oversize_body_is_refused_before_it_is_read(node):
    sent = await node.op.send("POST", f"{ROOT}/{ID}/sign-in", content=b"{}", headers={"Content-Length": str(api.MAX_BODY + 1)})
    assert sent.status_code in (400, 413)


async def test_a_flow_the_kernel_lost_is_dropped_and_a_poll_that_is_not_signed_in_is_refused(node):
    flow = await started_device(node, DeviceStep.PENDING)

    async def lost(_flow):
        raise fake.KernelError.State("connection.flow_unknown", "gone")

    node.kernel.poll_connection_sign_in = lost
    first = await node.op.send("POST", FLOW.format(flow=flow) + "/poll", {})
    assert (first.status_code, first.json()) == (404, {"detail": GONE})
    assert (await node.op.send("POST", FLOW.format(flow=flow) + "/poll", {})).status_code == 404
    other = await started_device(node, DeviceStep.PENDING)

    async def attention(_flow):
        return fake.SignInPoll.DONE(NS(state="needs_attention"))

    node.kernel.poll_connection_sign_in = attention
    refused = await node.op.send("POST", FLOW.format(flow=other) + "/poll", {})
    assert (refused.status_code, refused.json()) == (409, {"detail": "connection.not_signed_in"})


async def test_a_streamed_oversize_body_is_refused_after_it_is_read(node):
    async def chunks():
        yield b"{" + b" " * api.MAX_BODY
        yield b"}"

    sent = await node.op.send("POST", f"{ROOT}/{ID}/sign-in", content=chunks())
    assert (sent.status_code, sent.json()) == (413, {"detail": "node.request_too_large"})
