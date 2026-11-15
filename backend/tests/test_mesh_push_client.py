"""Exercises a node's publisher client with verified native route/principal fakes and a recording approved HTTP transport.
Routes carry empty-wake capabilities only, and every final send is fenced against lifecycle and authority changes."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from orchestrator import mesh_push_client as client, push_wake as pw
from shared import external_http
from shared.node_identity import canonical_owner_id

MESH = "sha256:" + "aa" * 32
DEVICE = "sha256:" + "bb" * 32
ISSUER = "https://issuer.example/realms/owners"
OWNER = canonical_owner_id(ISSUER, "owner")
NOW = 1_800_000_000_000
URL = "https://publisher.example"


class Native:
    def __init__(self):
        self.identities = [NS(issuer=ISSUER, subject="owner")]
        self.routes = [
            NS(
                mesh_id=MESH,
                device_id=DEVICE,
                route=NS(
                    publisher="astral",
                    capability="opaque_capability=" * 4,
                    expires_at_ms=NOW + 60_000,
                ),
            )
        ]
        self.calls = 0
        self.on_read = lambda: None

    def mesh_principal(self):
        return NS(mesh_id=MESH, identities=self.identities)

    def wake_routes(self):
        self.calls += 1
        self.on_read()
        return self.routes


class Transport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.status = 204

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return NS(status_code=self.status)


def setup():
    native, http = Native(), Transport()
    native.routes[0].route.capability = "opaque_capability_" * 4 + "="
    mesh = NS(kernel=native, started=True)
    gateway = client.RemoteWakeGateway(
        URL,
        transport=http,
        clock=lambda: NOW,
        limiter=pw.WakeLimiter(min_interval_seconds=0),
    ).for_mesh(mesh)
    return mesh, native, http, gateway


async def test_node_sends_only_empty_wake_through_configured_publisher_without_iam_or_apns():
    mesh, native, http, gateway = setup()
    assert await gateway.wake(OWNER, DEVICE) == pw.WakeOutcome(pw.SENT)
    method, url, options = http.calls[0]
    assert method == "POST" and url == URL + "/api/push/mesh/wake"
    assert options == dict(
        api_key=native.routes[0].route.capability,
        json_body={"mesh_id": MESH, "device_id": DEVICE},
        timeout=8,
        max_response_bytes=1024,
        allow_redirects=False,
        trust_environment=False,
        require_identity_encoding=True,
    )
    assert native.calls >= 2 and mesh.kernel is native


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "mesh",
        "removed",
        "stopped",
        "old_api",
        "wrong_device",
        "expired",
        "service",
        "capability",
        "bool_expiry",
        "too_long_expiry",
        "duplicate",
    ],
)
async def test_unverified_or_unavailable_native_context_never_reaches_http(mutation):
    mesh, native, http, gateway = setup()
    if mutation == "owner":
        native.identities = [NS(issuer=ISSUER, subject="different")]
    elif mutation == "mesh":
        native.routes[0].mesh_id = DEVICE
    elif mutation in {"removed", "stopped"}:

        def denied():
            raise RuntimeError("native.current_authority_denied")

        native.mesh_principal = denied
    elif mutation == "old_api":
        native.wake_routes = None
    elif mutation == "wrong_device":
        native.routes[0].device_id = MESH
    elif mutation == "expired":
        native.routes[0].route.expires_at_ms = NOW
    elif mutation == "service":
        native.routes[0].route.publisher = "arbitrary"
    elif mutation == "capability":
        native.routes[0].route.capability = "bad\ncapability"
    elif mutation == "bool_expiry":
        native.routes[0].route.expires_at_ms = True
    elif mutation == "too_long_expiry":
        native.routes[0].route.expires_at_ms = NOW + 86_400_001
    elif mutation == "duplicate":
        native.routes *= 2
    assert (await gateway.wake(OWNER, DEVICE)).status != pw.SENT
    assert http.calls == []


@pytest.mark.parametrize("mutation", ["replace", "close", "owner", "rotate", "revoke"])
async def test_authority_or_route_change_during_final_read_prevents_actual_send(
    mutation,
):
    mesh, native, http, gateway = setup()

    def change():
        if native.calls != 2:
            return
        if mutation == "replace":
            mesh.kernel = Native()
        elif mutation == "close":
            mesh.started = False
        elif mutation == "owner":
            native.identities = [NS(issuer=ISSUER, subject="other")]
        elif mutation == "rotate":
            native.routes[0].route.capability = "new_capability_" * 4
        else:
            native.routes.clear()

    native.on_read = change
    assert (await gateway.wake(OWNER, DEVICE)).status != pw.SENT
    assert http.calls == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (external_http.AuthFailedError("secret error"), pw.NO_DESTINATION),
        (external_http.BadRequestError("secret error"), pw.NO_DESTINATION),
        (external_http.RateLimitedError("secret error"), pw.RATE_LIMITED),
        (external_http.ServiceUnreachableError("secret error"), pw.FAILED),
        (RuntimeError("secret error"), pw.FAILED),
    ],
)
async def test_transport_denials_and_failures_are_truthful_and_do_not_echo_capabilities(
    error, status
):
    _, _, http, gateway = setup()
    http.error = error
    result = await gateway.wake(OWNER, DEVICE)
    assert result.status == status and "secret" not in repr(result)


async def test_nonempty_success_response_is_not_fabricated_as_a_sent_wake():
    _, _, http, gateway = setup()
    http.status = 200
    assert (await gateway.wake(OWNER, DEVICE)).status == pw.FAILED


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://publisher.example",
        "https://u:p@publisher.example",
        "https://publisher.example?q=x",
        "https://publisher.example/#fragment",
        "https://publisher.example/path",
        "https://publisher.example/\\evil",
    ],
)
def test_publisher_url_is_explicit_https_origin_only(url):
    with pytest.raises(ValueError):
        client.RemoteWakeGateway(url)


async def test_unstarted_context_missing_route_and_local_limiter_send_nothing():
    mesh, _, http, gateway = setup()
    mesh.started = False
    assert (await gateway.wake(OWNER, DEVICE)).status != pw.SENT
    mesh.started = True
    gateway._gateway._limiter = pw.WakeLimiter()
    assert (await gateway.wake(OWNER, DEVICE)).status == pw.SENT
    assert (await gateway.wake(OWNER, DEVICE)).status == pw.RATE_LIMITED
    assert len(http.calls) == 1


async def test_accountless_principal_without_a_publisher_binding_has_no_destination():
    _, native, http, gateway = setup()
    native.mesh_principal = lambda: NS(mesh_id=MESH)
    assert await gateway.wake(MESH, DEVICE) == pw.WakeOutcome(pw.NO_DESTINATION)
    assert http.calls == []
