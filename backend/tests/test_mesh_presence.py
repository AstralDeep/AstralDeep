"""Exercises mesh presence resolution through real PostgreSQL, custody and REST proofs.
Deterministic Keycloak signing supplies success, stale, ambiguous, revoked and denial
inputs while the ordinary dispatcher admission stays the only dispatch authority.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from orchestrator import mesh_admission as ma
from orchestrator import mesh_enrollment as me
from orchestrator import mesh_presence as mp
from tests.test_mesh_admission import mesh as mesh  # noqa: F401
from tests.test_mesh_admission import plane_runtime as plane_runtime  # noqa: F401


@pytest.fixture
def presence(mesh):
    return mp.MeshPresence(mesh.host.plane_repository_source, store=mesh.store)


@pytest.fixture
def audit_events(mesh, monkeypatch) -> list:
    events = []
    original = mesh.audit.insert_in_transaction

    def append(event, **kwargs):
        receipt = original(event, **kwargs)
        events.append(event)
        return receipt

    monkeypatch.setattr(mesh.audit, "insert_in_transaction", append)
    return events


def _member_request(mesh, client, method, path, *, body=None, nonce=None):
    if nonce is None:
        issued = mesh.client.post(
            "/api/mesh/nonce",
            json={
                "owner_id": client.device.owner_id,
                "member_id": client.device.member_id,
            },
        )
        if issued.status_code != 200:
            return issued
        nonce = issued.json()["nonce"]
    headers = {
        "Authorization": "DPoP " + client._token,
        "DPoP": client.device.proof(
            method=method,
            target=client.base_url + path,
            nonce=nonce,
            access_token=client._token,
        ),
        "DPoP-Nonce": nonce,
    }
    return mesh.client.request(
        method,
        path,
        headers=headers,
        **({"json": body} if body is not None else {}),
    )


def _publish(mesh, client, *, endpoint="wss://fabric.example:9000", capabilities=None):
    body = {"endpoint": endpoint}
    if capabilities is not None:
        body["capabilities"] = capabilities
    response = _member_request(mesh, client, "POST", "/api/mesh/presence", body=body)
    assert response.status_code == 200, response.text
    return response.json()["presence"]


def _presence_of(mesh, client):
    response = _member_request(mesh, client, "GET", "/api/mesh/presence")
    assert response.status_code == 200, response.text
    return response.json()["presence"]


def _topology(mesh):
    return {
        target["member_id"]: target
        for target in mesh.client.get(
            "/api/mesh/topology", headers=mesh.owner_headers
        ).json()["targets"]
    }


def _advance(monkeypatch, milliseconds):
    original = me._now_ms
    monkeypatch.setattr(me, "_now_ms", lambda: original() + milliseconds)


def _freeze_at(monkeypatch, target_ms):
    monkeypatch.setattr(me, "_now_ms", lambda: target_ms)


def test_published_presence_resolves_online_with_audit(mesh, audit_events):
    client, _ = mesh.enroll()
    record = _publish(mesh, client, capabilities=["archival"])
    assert record["presence"] == "online"
    assert record["reason"] is None
    assert record["endpoint"] == "wss://fabric.example:9000"
    assert record["capabilities"] == ["archival"]
    assert type(record["last_seen"]) is int
    targets = _topology(mesh)
    target = targets[client.device.member_id]
    assert target["presence"] == "online"
    assert set(target) == {
        "member_id",
        "label",
        "presence",
        "reason",
        "last_seen",
        "endpoint",
        "capabilities",
    }
    assert "token" not in str(targets) and "scope" not in str(targets)
    assert any(
        event.action_type == "mesh.member.presence"
        and event.inputs_meta.get("member_id") == client.device.member_id
        for event in audit_events
    )


def test_unapproved_and_anonymous_publishers_are_refused(mesh):
    client, _ = mesh.enroll()
    assert (
        mesh.client.post(
            "/api/mesh/presence",
            headers=mesh.owner_headers,
            json={"endpoint": "wss://fabric.example:9000"},
        ).status_code
        == 403
    )
    from fastapi.testclient import TestClient

    anonymous = TestClient(mesh.app, base_url="https://mesh.invalid")
    assert (
        anonymous.post(
            "/api/mesh/presence", json={"endpoint": "wss://fabric.example:9000"}
        ).status_code
        == 401
    )


def test_publish_validates_endpoint_and_capabilities(mesh):
    client, _ = mesh.enroll()
    for endpoint in (
        "wss://fabric.example:9000/x?query=1",
        "https://user:pass@fabric.example",
        "ftp://fabric.example",
        "",
        "wss://host.example/" + "a" * 300,
        None,
    ):
        response = _member_request(
            mesh, client, "POST", "/api/mesh/presence", body={"endpoint": endpoint}
        )
        assert response.status_code == 403, endpoint
    for capabilities in (["archival"] * 9, ["Archival"], "archival"):
        response = _member_request(
            mesh,
            client,
            "POST",
            "/api/mesh/presence",
            body={"endpoint": "wss://fabric.example", "capabilities": capabilities},
        )
        assert response.status_code == 403, capabilities
    own = _presence_of(mesh, client)
    assert own["presence"] == "offline"
    assert own["reason"] == "unpublished"


def test_production_posture_refuses_cleartext_endpoint(mesh, monkeypatch):
    client, _ = mesh.enroll()
    monkeypatch.setenv("ASTRAL_ENV", "production")
    response = _member_request(
        mesh, client, "POST", "/api/mesh/presence", body={"endpoint": "ws://fabric.example:9000"}
    )
    assert response.status_code == 403
    record = _publish(mesh, client, endpoint="wss://fabric.example:9000")
    assert record["presence"] == "online"


def test_stale_observation_expires_offline_and_keeps_last_seen(mesh, monkeypatch):
    client, _ = mesh.enroll()
    record = _publish(mesh, client)
    assert record["presence"] == "online"
    _advance(monkeypatch, (mp.ttl_seconds() + 1) * 1000)
    expired = _presence_of(mesh, client)
    assert expired["presence"] == "offline"
    assert expired["reason"] == "stale_observation"
    assert expired["endpoint"] is None
    assert expired["last_seen"] == record["last_seen"]


def test_reconnect_replaces_endpoint_and_restores_online(mesh, monkeypatch):
    client, _ = mesh.enroll()
    first = _publish(mesh, client, endpoint="wss://old.example:9000")
    _advance(monkeypatch, (mp.ttl_seconds() + 1) * 1000)
    assert _presence_of(mesh, client)["presence"] == "offline"
    refreshed = _publish(mesh, client, endpoint="wss://new.example:8443")
    assert refreshed["presence"] == "online"
    assert refreshed["endpoint"] == "wss://new.example:8443"
    assert refreshed["last_seen"] > first["last_seen"]
    assert _topology(mesh)[client.device.member_id]["presence"] == "online"


def test_ttl_boundary_is_monotonic(mesh, monkeypatch):
    client, _ = mesh.enroll()
    record = _publish(mesh, client)
    boundary = record["last_seen"] + mp.ttl_seconds() * 1000
    _freeze_at(monkeypatch, boundary)
    assert _presence_of(mesh, client)["presence"] == "online"
    _freeze_at(monkeypatch, boundary + 1)
    assert _presence_of(mesh, client)["presence"] == "offline"


def test_ambiguous_endpoint_is_unavailable_and_recovers(mesh):
    first, _ = mesh.enroll()
    second, _ = mesh.enroll()
    first.session(agent_id="reader")
    endpoint = "wss://shared.example:9000"
    _publish(mesh, first, endpoint=endpoint)
    _publish(mesh, second, endpoint=endpoint)
    targets = _topology(mesh)
    assert targets[first.device.member_id]["presence"] == "unavailable"
    assert targets[second.device.member_id]["presence"] == "unavailable"
    assert targets[first.device.member_id]["reason"] == "ambiguous_endpoint"
    assert targets[first.device.member_id]["endpoint"] is None
    _publish(mesh, second, endpoint="wss://other.example:9000")
    targets = _topology(mesh)
    assert targets[first.device.member_id]["presence"] == "online"
    assert targets[second.device.member_id]["presence"] == "online"


def test_revoked_member_is_excluded_and_cannot_publish(mesh):
    client, _ = mesh.enroll()
    _publish(mesh, client)
    revoked = mesh.client.post(
        "/api/mesh/members/" + client.device.member_id + "/revoke",
        headers=mesh.owner_headers,
    )
    assert revoked.status_code == 200
    assert _topology(mesh) == {}
    nonce = mesh.client.post(
        "/api/mesh/nonce",
        json={
            "owner_id": client.device.owner_id,
            "member_id": client.device.member_id,
        },
    )
    assert nonce.status_code == 401
    response = _member_request(
        mesh,
        client,
        "POST",
        "/api/mesh/presence",
        body={"endpoint": "wss://fabric.example:9000"},
    )
    assert response.status_code in (401, 403)


def test_identity_and_membership_bindings_stale_the_record(mesh, presence):
    client, _ = mesh.enroll()
    _publish(mesh, client)
    member = mesh.store._get(mesh.owner, me.MEMBER_NAMESPACE, client.device.member_id)
    record = mesh.store._get(
        mesh.owner, mp.PRESENCE_NAMESPACE, client.device.member_id
    )
    record["device_key_fingerprint"] = "sha256-" + "0" * 64
    mesh.store._put(mesh.owner, mp.PRESENCE_NAMESPACE, client.device.member_id, record)
    resolved = presence.resolve(mesh.owner)[0]
    assert resolved["presence"] == "offline"
    assert resolved["reason"] == "identity_replaced"
    record = mesh.store._get(
        mesh.owner, mp.PRESENCE_NAMESPACE, client.device.member_id
    )
    record["device_key_fingerprint"] = member["device_key_fingerprint"]
    record["membership_epoch"] = member["membership_epoch"] + 1
    mesh.store._put(mesh.owner, mp.PRESENCE_NAMESPACE, client.device.member_id, record)
    resolved = presence.resolve(mesh.owner)[0]
    assert resolved["reason"] == "membership_changed"


def test_replacement_identity_must_reannounce(mesh):
    replaced, _ = mesh.enroll()
    _publish(mesh, replaced)
    mesh.client.post(
        "/api/mesh/members/" + replaced.device.member_id + "/revoke",
        headers=mesh.owner_headers,
    )
    device = mesh.sdk.MeshDevice.generate()
    created = mesh.client.post(
        "/api/mesh/invitations",
        headers=mesh.owner_headers,
        json={
            "label": "Replacement device",
            "device_key": device.public_key,
            "scopes": ["tools:read"],
        },
    ).json()
    mesh.client.post(
        "/api/mesh/invitations/" + created["invitation"]["invite_id"] + "/confirm",
        headers=mesh.owner_headers,
    )
    successor = mesh.sdk.MeshClient("https://mesh.invalid", device, client=mesh.client)
    successor.redeem(created["payload"], agent_id="reader")
    targets = list(_topology(mesh).values())
    assert [target["presence"] for target in targets] == ["offline"]
    assert targets[0]["reason"] == "unpublished"
    published = _publish(mesh, successor, endpoint="wss://replacement.example")
    assert published["presence"] == "online"
    successor.close()


def test_topology_observation_is_owner_only_and_owner_scoped(mesh):
    client, _ = mesh.enroll()
    _publish(mesh, client)
    assert _member_request(mesh, client, "GET", "/api/mesh/topology").status_code == 401
    foreign = mesh.token(sub="foreign-owner-" + uuid.uuid4().hex)
    foreign_read = mesh.client.get(
        "/api/mesh/topology", headers={"Authorization": "Bearer " + foreign}
    )
    assert foreign_read.status_code == 200
    assert foreign_read.json()["targets"] == []
    assert _topology(mesh)


def test_member_self_presence_is_own_record_only(mesh):
    first, _ = mesh.enroll()
    second, _ = mesh.enroll()
    first.session(agent_id="reader")
    _publish(mesh, first)
    own = _presence_of(mesh, first)
    assert own["member_id"] == first.device.member_id
    assert own["presence"] == "online"
    other = _presence_of(mesh, second)
    assert other["member_id"] == second.device.member_id
    assert other["presence"] == "offline"
    assert other["reason"] == "unpublished"


def test_presence_read_denials_and_failures_fail_closed(mesh, monkeypatch):
    client, _ = mesh.enroll()
    _publish(mesh, client)
    assert (
        mesh.client.get("/api/mesh/presence", headers=mesh.owner_headers).status_code
        == 403
    )

    def unavailable(store, owner, namespace, **kwargs):
        raise me.MeshInventoryIncomplete("complete mesh inventory is required")

    monkeypatch.setattr(me.MeshEnrollmentStore, "_list", unavailable)
    own = _member_request(mesh, client, "GET", "/api/mesh/presence")
    assert own.status_code == 503
    assert own.json()["detail"] == "mesh_presence_unavailable"


def test_publish_conflict_and_inventory_failures_fail_closed(mesh, monkeypatch):
    client, _ = mesh.enroll()
    _publish(mesh, client, endpoint="wss://fabric.example")

    def conflict(*args, **kwargs):
        raise me.InvitationStateInvalid("credential changed concurrently")

    monkeypatch.setattr(me.MeshEnrollmentStore, "_transition", conflict)
    response = _member_request(
        mesh, client, "POST", "/api/mesh/presence", body={"endpoint": "wss://fabric.example"}
    )
    assert response.status_code == 409

    def unavailable(store, owner, namespace, **kwargs):
        raise me.MeshInventoryIncomplete("complete mesh inventory is required")

    monkeypatch.setattr(me.MeshEnrollmentStore, "_list", unavailable)
    topology = mesh.client.get("/api/mesh/topology", headers=mesh.owner_headers)
    assert topology.status_code == 503
    assert topology.json()["detail"] == "mesh_presence_unavailable"


def test_publish_audit_failure_rolls_back(mesh, monkeypatch):
    client, _ = mesh.enroll()
    nonce = mesh.client.post(
        "/api/mesh/nonce",
        json={
            "owner_id": client.device.owner_id,
            "member_id": client.device.member_id,
        },
    ).json()["nonce"]
    monkeypatch.setattr(
        mesh.audit, "insert_in_transaction", lambda *args, **kwargs: None
    )
    response = _member_request(
        mesh,
        client,
        "POST",
        "/api/mesh/presence",
        body={"endpoint": "wss://fabric.example:9000"},
        nonce=nonce,
    )
    assert response.status_code == 503
    assert (
        mesh.store._get(mesh.owner, mp.PRESENCE_NAMESPACE, client.device.member_id)
        is None
    )


@pytest.mark.asyncio
async def test_online_target_authorizes_no_dispatch(mesh, monkeypatch):
    from tests.test_mesh_admission import _dispatcher

    client, _ = mesh.enroll(scopes=[])
    _publish(mesh, client)
    targets = mesh.client.get(
        "/api/mesh/topology", headers=mesh.owner_headers
    ).json()["targets"]
    assert targets[0]["presence"] == "online"
    assert not {"access_token", "delegation_token", "scope"} & set(targets[0])
    host = _dispatcher(mesh, monkeypatch)
    claims = ma.verify_member_token(client._token)
    result = await host.execute_authorized_tool(
        claims=claims,
        user_id=mesh.owner,
        agent_id="reader",
        tool_name="read",
        arguments={},
        channel="rest",
    )
    assert result.error is not None
    assert result.error["code"] == ma.MeshAdmissionError.code
    assert host.effects == []
    assert _member_request(mesh, client, "GET", "/api/mesh/presence").status_code == 200


def test_presence_inventory_failure_fails_closed(mesh, monkeypatch):
    from orchestrator.mesh_enrollment import MeshInventoryIncomplete

    service = mp.MeshPresence(mesh.host.plane_repository_source, store=mesh.store)
    monkeypatch.setattr(
        service.store,
        "_list",
        lambda *args, **kwargs: (_ for _ in ()).throw(MeshInventoryIncomplete("full")),
    )
    with pytest.raises(mp.PresenceStoreUnavailable):
        service.resolve(mesh.owner)


def test_surface_renders_honest_presence_states(mesh):
    from orchestrator.projection_surfaces import mesh as mesh_surface

    online, _ = mesh.enroll(scopes=["tools:read"])
    _publish(
        mesh,
        online,
        endpoint="wss://fabric.example:9000",
        capabilities=["archival"],
    )
    html = asyncio.run(mesh_surface.render(mesh.host, mesh.owner, ["user"], {}))
    assert ">Online<" in html
    assert "wss://fabric.example:9000" in html
    assert "archival" in html
    components = asyncio.run(mesh_surface.components(mesh.host, mesh.owner, ["user"], {}))
    badges = [
        component
        for card in components
        if card.get("type") == "card"
        for component in card.get("content", [])
        if component.get("type") == "badge"
    ]
    assert any(
        badge.get("label") == "Online" and badge.get("variant") == "success"
        for badge in badges
    )
    second, _ = mesh.enroll(scopes=["tools:read"])
    online.session(agent_id="reader")
    _publish(mesh, second, endpoint="wss://fabric.example:9000")
    html = asyncio.run(mesh_surface.render(mesh.host, mesh.owner, ["user"], {}))
    assert ">Unavailable<" in html
    assert html.count("wss://fabric.example:9000") == 0
    invitations = asyncio.run(
        mesh_surface.components(mesh.host, mesh.owner, ["user"], {"view": "invitations"})
    )
    assert any(component.get("type") == "card" for component in invitations)


def test_surface_presence_failure_is_honest(mesh, monkeypatch):
    from orchestrator.projection_surfaces import mesh as mesh_surface

    client, _ = mesh.enroll(scopes=["tools:read"])
    _publish(mesh, client)

    def unavailable(presence, owner, **kwargs):
        raise mp.PresenceStoreUnavailable("mesh presence inventory is unavailable")

    monkeypatch.setattr(mp.MeshPresence, "resolve", unavailable)
    loaded = asyncio.run(mesh_surface._load_inventory(mesh.host, mesh.owner))
    assert loaded is not None and loaded[0] and loaded[2] is None

    async def unresolved_inventory(orch, user_id):
        return [
            mesh.store.list_members(user_id),
            mesh.store.list_invitations(user_id),
            None,
        ]

    monkeypatch.setattr(mesh_surface, "_load_inventory", unresolved_inventory)
    html = asyncio.run(mesh_surface.render(mesh.host, mesh.owner, ["user"], {}))
    assert "Presence is unavailable right now" in html
    assert ">Online<" not in html and ">Offline<" not in html
    components = asyncio.run(mesh_surface.components(mesh.host, mesh.owner, ["user"], {}))
    assert any(
        component.get("type") == "alert" and component.get("variant") == "warning"
        for component in components
    )


def test_ttl_configuration_bounds(mesh, monkeypatch):
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "10")
    assert mp.ttl_seconds() == mp.MIN_TTL_SECONDS
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "99999")
    assert mp.ttl_seconds() == mp.MAX_TTL_SECONDS
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "not-a-number")
    assert mp.ttl_seconds() == mp.DEFAULT_TTL_SECONDS
    monkeypatch.setenv("MESH_PRESENCE_TTL_SECONDS", "120")
    assert mp.ttl_seconds() == 120
    monkeypatch.delenv("MESH_PRESENCE_TTL_SECONDS")
    assert mp.ttl_seconds() == mp.DEFAULT_TTL_SECONDS


def test_endpoint_origin_normalizes_default_ports():
    assert (
        mp.endpoint_origin({"scheme": "wss", "hostname": "f.example", "port": 443})
        == "wss://f.example"
    )
    assert (
        mp.endpoint_origin({"scheme": "https", "hostname": "f.example", "port": 8443})
        == "https://f.example:8443"
    )
    assert (
        mp.endpoint_origin({"scheme": "http", "hostname": "::1", "port": 8080})
        == "http://[::1]:8080"
    )
    assert mp._endpoint_of({"endpoint": None}) is None
    assert mp._endpoint_of(None) is None
