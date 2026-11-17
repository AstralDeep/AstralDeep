"""A device enrolled through the legacy mesh enrollment keeps its rows when its node turns out to be a personal node, but every step of that flow is closed there: the owner cannot invite, the device cannot redeem, refresh or call in, and no Keycloak exchange happens.
It drives the mesh SDK client through the app Orchestrator.build_http_app serves (tests/helpers/orchestrator_app.py) into orchestrator/mesh_api.py, on real PostgreSQL from tests/helpers/mesh_runtime.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator import mesh_admission as ma, web_auth
from shared import node_identity as ni
from tests.helpers import orchestrator_app
from tests.helpers.mesh_runtime import MeshRuntime
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

CLOSED = (403, {"detail": "node.legacy_enrollment_closed"})


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_mesh_personal") as runtime:
        yield runtime


@pytest.fixture
def node(plane_runtime, monkeypatch, tmp_path):
    fixture = MeshRuntime(plane_runtime, monkeypatch)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    host = orchestrator_app.host(None, **vars(fixture.host))
    app = orchestrator_app.build(monkeypatch, host, surfaces=())
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mesh.invalid")
    fixture.client.close()
    fixture.client = TestClient(app, base_url="https://mesh.invalid")
    fixture.client.cookies.set(web_auth.COOKIE_NAME, web_auth._sign(fixture.sid))
    try:
        yield fixture
    finally:
        fixture.close()
        ni.reset_cache()


def _rows(node):
    return (node.store.list_members(node.owner), node.store.list_invitations(node.owner),
            node.store._list(node.owner, ma.SESSION_NAMESPACE))


def _personal(node, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    node.exchanges.clear()
    node.exchange_forms.clear()


def _refused(call):
    with pytest.raises(Exception, match="403 Forbidden") as refused:
        call()
    return refused.value.response.status_code, refused.value.response.json()


def test_an_enrolled_device_can_no_longer_refresh_or_call_in(node, monkeypatch):
    device, _ = node.enroll()
    before = _rows(node)
    _personal(node, monkeypatch)
    assert _refused(lambda: device.session(agent_id="reader")) == CLOSED
    assert _refused(lambda: device.request("GET", "/api/mesh/me")) == CLOSED
    assert _refused(lambda: device.invoke("reader", "read", {"value": "x"})) == CLOSED
    assert _rows(node) == before
    assert node.exchanges == [] and node.exchange_forms == []
    assert ni.bound_owner() is None


def test_a_pending_invitation_can_no_longer_be_confirmed_or_redeemed(node, monkeypatch):
    device, invitation = node.invitation()
    before = _rows(node)
    _personal(node, monkeypatch)
    invite = invitation["invitation"]["invite_id"]
    confirmed = node.client.post(f"/api/mesh/invitations/{invite}/confirm", headers=node.owner_headers)
    assert (confirmed.status_code, confirmed.json()) == CLOSED
    client = node.sdk.MeshClient("https://mesh.invalid", device, client=node.client)
    assert _refused(lambda: client.redeem(invitation["payload"], agent_id="reader")) == CLOSED
    invited = node.client.post("/api/mesh/invitations", headers=node.owner_headers,
                               json={"label": "Another", "device_key": node.sdk.MeshDevice.generate().public_key})
    assert (invited.status_code, invited.json()) == CLOSED
    assert _rows(node) == before
    assert node.exchanges == []


def test_the_same_flow_still_works_on_the_server_profile(node):
    device, _ = node.enroll()
    assert device.request("GET", "/api/mesh/me")["member"]["member_id"] == device.device.member_id
    assert ma.verify_member_token(device._token)["sub"] == node.owner
