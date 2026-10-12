"""Mesh member-management controls: owner rename and invitation removal through
real PostgreSQL, REST guards, fencing, audit rollback, and redemption refusal
after a live confirmed grant is removed.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator import mesh_enrollment as me
from tests.helpers.mesh_runtime import MeshRuntime
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("mesh_member_controls") as runtime:
        yield runtime


@pytest.fixture
def mesh(plane_runtime, monkeypatch):
    fixture = MeshRuntime(plane_runtime, monkeypatch)
    try:
        yield fixture
    finally:
        fixture.close()


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


def _member_id(mesh) -> str:
    return mesh.store.list_members(mesh.owner)[0]["member_id"]


def _rename(mesh, member_id, label):
    return mesh.client.post(
        f"/api/mesh/members/{member_id}/label",
        headers=mesh.owner_headers,
        json={"label": label},
    )


def _remove(mesh, invite_id):
    return mesh.client.post(
        f"/api/mesh/invitations/{invite_id}/remove", headers=mesh.owner_headers
    )


def _member_post(mesh, client, path, body):
    nonce = client.nonce()
    return mesh.client.post(
        path,
        headers={
            "Authorization": f"DPoP {client._token}",
            "DPoP": client.device.proof(
                method="POST",
                target=str(mesh.client.base_url) + path,
                nonce=nonce,
                access_token=client._token,
            ),
            "DPoP-Nonce": nonce,
        },
        json=body,
    )


def test_owner_renames_active_member(mesh, audit_events):
    client, invitation = mesh.enroll()
    member_id = _member_id(mesh)
    response = _rename(mesh, member_id, "Renamed tablet")
    assert response.status_code == 200, response.text
    member = response.json()["member"]
    assert member["label"] == "Renamed tablet"
    listed = mesh.store.list_members(mesh.owner)
    assert [m["label"] for m in listed] == ["Renamed tablet"]
    assert [event.action_type for event in audit_events if "rename" in event.action_type] == [
        "mesh.member.rename"
    ]
    assert client.request("GET", "/api/mesh/me")["member"]["label"] == "Renamed tablet"


def test_rename_preserves_authority_revision_and_token(mesh):
    client, _ = mesh.enroll()
    member_id = _member_id(mesh)
    before = mesh.store.get_member(mesh.owner, member_id)["authority_revision"]
    assert _rename(mesh, member_id, "Second name").status_code == 200
    after = mesh.store.get_member(mesh.owner, member_id)["authority_revision"]
    assert after == before
    assert client.request("GET", "/api/mesh/me")["member"]["label"] == "Second name"


def test_rename_same_label_is_accepted_without_revision_change(mesh):
    mesh.enroll()
    member_id = _member_id(mesh)
    before = mesh.store.get_member(mesh.owner, member_id)
    response = _rename(mesh, member_id, "Synthetic device")
    assert response.status_code == 200
    after = mesh.store.get_member(mesh.owner, member_id)
    assert after["authority_revision"] == before["authority_revision"]
    assert after["updated_at"] == before["updated_at"]


def test_rename_rejects_invalid_and_unknown_labels(mesh):
    mesh.enroll()
    member_id = _member_id(mesh)
    assert _rename(mesh, member_id, "").status_code == 400
    assert _rename(mesh, member_id, "   ").status_code == 400
    assert _rename(mesh, member_id, "x" * 65).status_code == 400
    non_string = mesh.client.post(
        f"/api/mesh/members/{member_id}/label",
        headers=mesh.owner_headers,
        json={"label": 123},
    )
    assert non_string.status_code == 400
    missing = mesh.client.post(
        f"/api/mesh/members/{member_id}/label", headers=mesh.owner_headers
    )
    assert missing.status_code == 422
    assert _rename(mesh, "f" * 32, "Whatever").status_code == 404


def test_rename_revoked_member_denied(mesh):
    mesh.enroll()
    member_id = _member_id(mesh)
    assert (
        mesh.client.post(
            f"/api/mesh/members/{member_id}/revoke", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    response = _rename(mesh, member_id, "After revocation")
    assert response.status_code == 401
    assert response.json()["detail"] == "member_unauthorized"
    assert mesh.store.list_members(mesh.owner)[0]["label"] == "Synthetic device"


def test_member_identity_cannot_rename(mesh):
    client, _ = mesh.enroll()
    member_id = _member_id(mesh)
    response = _member_post(
        mesh, client, f"/api/mesh/members/{member_id}/label", {"label": "From member"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"
    assert mesh.store.list_members(mesh.owner)[0]["label"] == "Synthetic device"


def test_rename_audit_failure_rolls_back(mesh, monkeypatch):
    mesh.enroll()
    member_id = _member_id(mesh)
    original = mesh.audit.insert_in_transaction

    def reject(event, **kwargs):
        return (
            None
            if event.action_type == "mesh.member.rename"
            else original(event, **kwargs)
        )

    monkeypatch.setattr(mesh.audit, "insert_in_transaction", reject)
    assert _rename(mesh, member_id, "Rollback name").status_code == 503
    assert mesh.store.list_members(mesh.owner)[0]["label"] == "Synthetic device"


def test_owner_removes_rejected_invitation(mesh, audit_events):
    _, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/reject", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    assert _remove(mesh, invite_id).status_code == 200
    assert mesh.store.list_invitations(mesh.owner) == []
    assert [
        event.action_type for event in audit_events if event.action_type == "mesh.invitation.remove"
    ]
    assert _remove(mesh, invite_id).status_code == 404


def test_owner_removes_redeemed_invitation(mesh):
    client, invitation = mesh.enroll()
    invite_id = invitation["invitation"]["invite_id"]
    assert _remove(mesh, invite_id).status_code == 200
    assert mesh.store.list_invitations(mesh.owner) == []
    assert len(mesh.store.list_members(mesh.owner)) == 1
    assert client.request("GET", "/api/mesh/me")["member"]["member_id"] == client.device.member_id


def test_owner_removes_expired_confirmed_invitation(mesh, monkeypatch):
    _, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/confirm", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    real_now = me._now_ms

    def late_now():
        return real_now() + 2 * 60 * 1000

    with monkeypatch.context() as late:
        late.setattr(me, "_now_ms", late_now)
        response = _remove(mesh, invite_id)
    assert response.status_code == 200
    assert mesh.store.list_invitations(mesh.owner) == []


def test_remove_pending_invitation_requires_rejection(mesh):
    _, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    response = _remove(mesh, invite_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "invitation_state_invalid"
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "pending"


def test_removing_live_confirmed_invitation_cancels_redemption(mesh, audit_events):
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/confirm", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    assert _remove(mesh, invite_id).status_code == 200
    assert mesh.store.list_invitations(mesh.owner) == []
    assert mesh.store.list_members(mesh.owner) == []
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(invitation["payload"], agent_id="reader"),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "invitation_not_found"
    assert [
        event.inputs_meta.get("cancelled")
        for event in audit_events
        if event.action_type == "mesh.invitation.remove"
    ] == [True]


def test_member_identity_cannot_remove(mesh):
    client, invitation = mesh.enroll()
    invite_id = invitation["invitation"]["invite_id"]
    response = _member_post(mesh, client, f"/api/mesh/invitations/{invite_id}/remove", {})
    assert response.status_code == 401
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "redeemed"


def test_remove_audit_failure_rolls_back(mesh, monkeypatch):
    _, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/reject", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    original = mesh.audit.insert_in_transaction

    def reject(event, **kwargs):
        return (
            None
            if event.action_type == "mesh.invitation.remove"
            else original(event, **kwargs)
        )

    monkeypatch.setattr(mesh.audit, "insert_in_transaction", reject)
    assert _remove(mesh, invite_id).status_code == 503
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "rejected"


def test_concurrent_removal_reports_missing_invitation(mesh, monkeypatch):
    _, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/reject", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    with monkeypatch.context() as missing_delete:
        missing_delete.setattr(
            mesh.store._credentials.repository,
            "delete_credential",
            lambda *args, **kwargs: False,
        )
        response = _remove(mesh, invite_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "invitation_not_found"
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "rejected"


def test_member_control_write_guards(mesh, monkeypatch):
    from orchestrator import web_auth

    monkeypatch.setattr(web_auth, "_STORE", mesh.sessions)
    mesh.enroll()
    member_id = _member_id(mesh)
    token_query = mesh.client.post(
        f"/api/mesh/members/{member_id}/label?token=x",
        headers=mesh.owner_headers,
        json={"label": "Nope"},
    )
    assert token_query.status_code == 403
    assert token_query.json()["detail"] == "mesh_query_token_refused"
    form_body = mesh.client.post(
        f"/api/mesh/members/{member_id}/label",
        headers={**mesh.owner_headers, "Content-Type": "text/plain"},
        content="label=Nope",
    )
    assert form_body.status_code == 415
    foreign = mesh.client.post(
        f"/api/mesh/members/{member_id}/label",
        headers={"Origin": "https://foreign.invalid"},
        json={"label": "Nope"},
    )
    assert foreign.status_code == 403
    assert foreign.json()["detail"] == "mesh_origin_refused"
    remove_query = mesh.client.post(
        "/api/mesh/invitations/abc/remove?token=x", headers=mesh.owner_headers
    )
    assert remove_query.status_code == 403


def test_shared_surface_renders_real_mesh_inventory(mesh):
    from orchestrator.projection_surfaces import mesh as mesh_surface

    mesh.enroll(scopes=["tools:read"])
    html = asyncio.run(mesh_surface.render(mesh.host, mesh.owner, ["user"], {}))
    assert "Synthetic device" in html
    assert ">Active<" in html
    listed = mesh.store.list_members(mesh.owner)[0]
    assert listed["device_key"]["kty"] not in html
    components = asyncio.run(mesh_surface.components(mesh.host, mesh.owner, ["user"], {}))
    assert any(c.get("type") == "card" for c in components)
    assert not hasattr(mesh_surface, "HANDLERS")
