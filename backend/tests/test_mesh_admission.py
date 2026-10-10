"""Exercises member admission through real PostgreSQL, custody and REST proofs.
Local Keycloak signing and HTTP boundaries provide deterministic success and
failure inputs while the ordinary dispatcher and Plane fences remain enforced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from jose import jwt

from orchestrator import delegation, mesh_admission as ma, mesh_enrollment as me
from tests.helpers.mesh_runtime import MeshRuntime
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("mesh_admission") as runtime:
        yield runtime


@pytest.fixture
def mesh(plane_runtime, monkeypatch):
    fixture = MeshRuntime(plane_runtime, monkeypatch)
    try:
        yield fixture
    finally:
        fixture.close()


def test_owner_confirmation_real_custody_device_enrollment_and_session(mesh):
    device, invitation = mesh.enroll()
    claims = ma.verify_member_token(device._token)
    assert claims["sub"] == mesh.owner
    assert claims["aud"] == ma.MEMBER_AUDIENCE
    assert claims["cnf"] == {"jkt": ma.thumbprint(device.device.public_key)}
    assert claims["scope"].split() == ["mesh:confirm", "tools:read"]
    assert mesh.exchanges == [("reader", ["read"], ["tools:read", "tool:read"])]
    assert (
        device.request("GET", "/api/mesh/me")["member"]["member_id"]
        == device.device.member_id
    )
    device.session(agent_id="reader")
    assert (
        device.request("GET", "/api/mesh/members")["members"][0]["member_id"]
        == device.device.member_id
    )
    assert "custody" not in json.dumps(mesh.store.list_members(mesh.owner))
    with mesh.runtime.transaction() as transaction:
        neutral = mesh.runtime.repositories.mesh_enrollment.get_member(
            transaction,
            owner_id=mesh.owner,
            mesh_id=me.mesh_id_for_owner(mesh.owner),
            member_id=device.device.member_id,
        )
        assert neutral.member_status == "active"
        stored = mesh.store._get_in_transaction(
            transaction, mesh.owner, ma.SESSION_NAMESPACE, neutral.member_id
        )
    assert stored["delegation_token"] != device._token
    assert (
        jwt.get_unverified_claims(stored["delegation_token"])["aud"]
        == "astral-agent-service"
    )
    assert "refresh_token" not in stored
    replay = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.device.redemption(invitation["payload"]),
    )
    assert replay.status_code == 409


def test_mesh_uses_production_rfc8693_http_form_with_exact_allowed_tools(mesh):
    mesh.enroll()
    assert mesh.host.delegation.mock_auth is False
    url, form = mesh.exchange_forms[0]
    assert url == mesh.issuer + "/protocol/openid-connect/token"
    assert form["grant_type"] == delegation.GRANT_TYPE_TOKEN_EXCHANGE
    assert form["subject_token_type"] == delegation.TOKEN_TYPE_ACCESS
    assert form["requested_token_type"] == delegation.TOKEN_TYPE_ACCESS
    assert form["client_id"] == "astral-frontend"
    assert form["client_secret"] == mesh.host.delegation.client_secret
    assert form["audience"] == "astral-agent-service"
    assert form["scope"].split() == ["tools:read", "tool:read"]
    assert jwt.get_unverified_claims(form["subject_token"])["sub"] == mesh.owner
    assert "tool:write" not in form["scope"].split()


def test_mesh_scope_extension_leaves_existing_owner_exchange_unchanged(mesh):
    result = asyncio.run(
        mesh.host.delegation.exchange_token_for_agent(
            mesh.owner_token, "reader", ["read"], mesh.owner, ["tools:read"]
        )
    )
    assert "access_token" in result
    assert mesh.exchange_forms[0][1]["scope"] == "tools:read"


@pytest.mark.parametrize(
    "mode",
    [
        "error",
        "broad",
        "admin",
        "foreign",
        "audience",
        "expiry",
        "tools",
        "missing_tools",
        "role",
        "extra_audience",
        "exchange_client",
    ],
)
def test_unattenuated_or_invalid_keycloak_exchange_never_activates(mesh, mode):
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/confirm", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    mesh.exchange_mode = mode
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(invitation["payload"], agent_id="reader"),
    )
    assert response.status_code == 403
    assert mesh.store.list_members(mesh.owner) == []
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "confirmed"


@pytest.mark.parametrize("mode", ["offline", "logout"])
def test_offline_or_logged_out_owner_custody_refuses_activation(mesh, mode):
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/confirm", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    mesh.refresh_mode = mode
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(invitation["payload"], agent_id="reader"),
    )
    assert response.status_code == 403
    assert mesh.store.list_members(mesh.owner) == []


@pytest.mark.parametrize(
    "change", ["revoked", "logout", "epoch", "authority", "identity", "malformed"]
)
def test_existing_member_session_fails_current_plane_and_custody_fences(mesh, change):
    client, _ = mesh.enroll()
    member_id = client.device.member_id
    if change == "revoked":
        mesh.store.revoke_member(mesh.owner, member_id)
    elif change == "logout":
        mesh.sessions.delete(mesh.sid)
    elif change == "epoch":
        mesh.enroll(scopes=["tools:read"])
    else:
        record = mesh.store._get(mesh.owner, me.MEMBER_NAMESPACE, member_id)
        if change == "authority":
            record["scopes"] = []
        elif change == "identity":
            record["device_key_fingerprint"] = "sha256:" + "0" * 64
        else:
            record["membership_epoch"] = None
        mesh.store._put(mesh.owner, me.MEMBER_NAMESPACE, member_id, record)
    claims = ma.verify_member_token(client._token)
    with pytest.raises(me.MeshEnrollmentError):
        ma.assert_dispatch_current(mesh.host, claims, "reader", "tools:read")
    response = mesh.client.get(
        "/api/mesh/me", headers={"Authorization": "Bearer " + client._token}
    )
    assert response.status_code == 403
    response = mesh.client.get(
        "/api/mesh/me", headers={"X-Astral-Member-Key": "omitted-binding"}
    )
    assert response.status_code == 401
    assert (
        mesh.client.get("/api/mesh/members", headers=mesh.owner_headers).status_code
        == 200
    )


def test_two_devices_owner_member_confirmation_and_epoch_session_refresh(mesh):
    first, _ = mesh.enroll()
    second = mesh.sdk.MeshDevice.generate()
    created = first.request(
        "POST",
        "/api/mesh/invitations",
        body={
            "label": "Second device",
            "device_key": second.public_key,
            "scopes": ["tools:read", "tools:write", "mesh:confirm"],
        },
    )
    invite_id = created["invitation"]["invite_id"]
    nonce = first.nonce()
    path = f"/api/mesh/invitations/{invite_id}/confirm"
    response = mesh.client.post(
        path,
        headers={
            "Authorization": "DPoP " + first._token,
            "DPoP-Nonce": nonce,
            "DPoP": first.device.proof(
                method="POST",
                target=first.base_url + path,
                nonce=nonce,
                access_token=first._token,
            ),
        },
    )
    assert response.status_code == 403
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/reject", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    second, created = mesh.invitation(
        second, scopes=["tools:read", "tools:write", "mesh:confirm"]
    )
    invite_id = created["invitation"]["invite_id"]
    confirmed = first.request("POST", f"/api/mesh/invitations/{invite_id}/confirm")
    assert confirmed["invitation"]["confirmed_scopes"] == ["mesh:confirm", "tools:read"]
    second_client = mesh.sdk.MeshClient(
        "https://mesh.invalid", second, client=mesh.client
    )
    second_client.redeem(created["payload"], agent_id="reader")
    with pytest.raises(ma.MeshAdmissionError):
        ma.assert_dispatch_current(
            mesh.host, ma.verify_member_token(first._token), "reader", "tools:read"
        )
    first.session(agent_id="reader")
    assert len(first.request("GET", "/api/mesh/members")["members"]) == 2
    assert len(second_client.request("GET", "/api/mesh/members")["members"]) == 2
    mesh.store.revoke_member(mesh.owner, first.device.member_id)
    with pytest.raises(ma.MeshAdmissionError):
        ma.assert_dispatch_current(
            mesh.host,
            ma.verify_member_token(second_client._token),
            "reader",
            "tools:read",
        )
    second_client.session(agent_id="reader")
    assert second_client.request("GET", "/api/mesh/me")["member"]["status"] == "active"


def test_proof_is_single_use_and_exact_method_uri_token_and_key(mesh):
    client, _ = mesh.enroll()
    nonce = client.nonce()
    path = "/api/mesh/me"
    proof = client.device.proof(
        method="GET",
        target=client.base_url + path,
        nonce=nonce,
        access_token=client._token,
    )
    headers = {
        "Authorization": "DPoP " + client._token,
        "DPoP": proof,
        "DPoP-Nonce": nonce,
    }
    assert mesh.client.get(path, headers=headers).status_code == 200
    assert mesh.client.get(path, headers=headers).status_code == 403
    for method, target, access, device in (
        ("POST", client.base_url + path, client._token, client.device),
        ("GET", "https://foreign.invalid" + path, client._token, client.device),
        ("GET", client.base_url + path, "foreign-token", client.device),
        ("GET", client.base_url + path, client._token, mesh.sdk.MeshDevice.generate()),
    ):
        nonce = client.nonce()
        headers["DPoP-Nonce"] = nonce
        headers["DPoP"] = device.proof(
            method=method, target=target, nonce=nonce, access_token=access
        )
        assert mesh.client.get(path, headers=headers).status_code == 403


def test_proof_without_binding_and_duplicate_headers_cannot_downgrade(mesh):
    client, _ = mesh.enroll()
    assert (
        mesh.client.get(
            "/api/mesh/me", headers={"Authorization": "DPoP " + client._token}
        ).status_code
        == 403
    )
    assert (
        mesh.client.get(
            "/api/mesh/me",
            headers=[
                ("Authorization", "DPoP " + client._token),
                ("Authorization", "Bearer " + mesh.owner_token),
            ],
        ).status_code
        == 403
    )
    assert (
        mesh.client.get("/api/mesh/me", headers=mesh.owner_headers).status_code == 403
    )


def test_server_private_delegation_requires_exact_target_and_current_iam(mesh):
    client, _ = mesh.enroll()
    claims = ma.verify_member_token(client._token)
    ma.assert_dispatch_current(mesh.host, claims, "reader", "tools:read")
    agent_token = mesh.service.delegation_token(claims, "reader")
    assert jwt.get_unverified_claims(agent_token)["scope"] == "tools:read tool:read"
    for target, scope in (("foreign", "tools:read"), ("reader", "tools:write")):
        with pytest.raises(ma.MeshAdmissionError):
            ma.assert_dispatch_current(mesh.host, claims, target, scope)
    mesh.sessions.delete(mesh.sid)
    with pytest.raises(ma.MeshAdmissionError):
        mesh.service.delegation_token(claims, "reader")


def test_control_only_token_cannot_target_any_agent(mesh):
    client, _ = mesh.enroll(agent_id=None)
    claims = ma.verify_member_token(client._token)
    assert claims["scope"] == "mesh:confirm"
    assert mesh.exchanges == [("mesh-control", [], ["openid"])]
    with pytest.raises(ma.MeshAdmissionError):
        ma.assert_dispatch_current(mesh.host, claims, "reader", "tools:read")


def test_session_audit_failure_rolls_back_neutral_activation_and_invitation(
    mesh, monkeypatch
):
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    assert (
        mesh.client.post(
            f"/api/mesh/invitations/{invite_id}/confirm", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    original = mesh.audit.insert_in_transaction

    def reject(event, **kwargs):
        return (
            None
            if event.action_type == "mesh.member.session"
            else original(event, **kwargs)
        )

    monkeypatch.setattr(mesh.audit, "insert_in_transaction", reject)
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(invitation["payload"], agent_id="reader"),
    )
    assert response.status_code == 503
    assert mesh.store.list_members(mesh.owner) == []
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "confirmed"
    with mesh.runtime.transaction() as transaction:
        neutral = mesh.runtime.repositories.mesh_enrollment.get_mesh(
            transaction, owner_id=mesh.owner, mesh_id=me.mesh_id_for_owner(mesh.owner)
        )
        assert neutral.membership_epoch == 1


def test_owner_custody_must_match_verified_session_issuer_client_and_owner(mesh):
    device, invitation = mesh.invitation()
    mesh.client.cookies.clear()
    response = mesh.client.post(
        "/api/mesh/invitations/" + invitation["invitation"]["invite_id"] + "/confirm",
        headers=mesh.owner_headers,
    )
    assert response.status_code == 403
    assert (
        mesh.store.get_invitation(mesh.owner, invitation["invitation"]["invite_id"])[
            "status"
        ]
        == "pending"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "foreign"},
        {"iat": int(time.time()) + 3600},
        {"scope": "tools:unknown"},
        {"cnf": {}},
        {"act": {"sub": "owner"}},
        {"delegation": False},
        {"jti": ""},
        {"exp": True},
        {"astral_mesh": {}},
        {"sub": ""},
    ],
)
def test_malformed_member_token_is_rejected(mesh, changes):
    client, _ = mesh.enroll()
    claims = ma.verify_member_token(client._token)
    claims.update(changes)
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_member_token(delegation.encode_delegation_payload(claims))


@pytest.mark.parametrize("proof", [None, "", "a.b", "a.b.c", "a" * 8193])
def test_malformed_possession_proof_is_rejected(mesh, proof):
    device = mesh.sdk.MeshDevice.generate()
    with pytest.raises(ma.MeshAdmissionError):
        ma.verify_possession(
            proof,
            device.public_key,
            method="POST",
            target="https://mesh.invalid/api/mesh/session",
            nonce="n",
        )


@pytest.mark.parametrize(
    "base,path",
    [
        ("", "/api/mesh/me"),
        ("https://u:p@mesh.invalid", "/api/mesh/me"),
        ("https://mesh.invalid/path", "/api/mesh/me"),
        ("https://mesh.invalid", "/other"),
        ("https://mesh.invalid", "/api/mesh/me?token=anything"),
    ],
)
def test_target_origin_must_be_operator_pinned(monkeypatch, base, path):
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    with pytest.raises(ma.MeshAdmissionError):
        ma.target_uri(path)


def test_rest_owner_reject_revoke_scope_denial_and_input_guards(mesh, monkeypatch):
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    path = f"/api/mesh/invitations/{invite_id}/reject"
    assert mesh.client.post(path, headers=mesh.owner_headers).status_code == 200
    assert mesh.client.post(path, headers=mesh.owner_headers).status_code == 409
    assert (
        mesh.client.get("/api/mesh/invitations", headers=mesh.owner_headers).json()[
            "invitations"
        ][0]["status"]
        == "rejected"
    )
    member, _ = mesh.enroll(scopes=["tools:read"])
    nonce = member.nonce()
    path = "/api/mesh/invitations"
    headers = {
        "Authorization": "DPoP " + member._token,
        "DPoP-Nonce": nonce,
        "DPoP": member.device.proof(
            method="POST",
            target=member.base_url + path,
            nonce=nonce,
            access_token=member._token,
        ),
    }
    assert mesh.client.post(path, headers=headers, json={}).status_code == 403
    member_id = member.device.member_id
    assert (
        mesh.client.post(
            f"/api/mesh/members/{member_id}/revoke", headers=mesh.owner_headers
        ).status_code
        == 200
    )
    assert (
        mesh.client.post(
            "/api/mesh/members/foreign/revoke", headers=mesh.owner_headers
        ).status_code
        == 404
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations", headers=mesh.owner_headers, json={}
        ).status_code
        == 400
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations?token=secret", headers=mesh.owner_headers, json={}
        ).status_code
        == 403
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations", headers=mesh.owner_headers, content="form"
        ).status_code
        == 415
    )
    assert mesh.client.post("/api/mesh/enrollment/redeem", json={}).status_code == 400
    assert (
        mesh.client.post(
            "/api/mesh/enrollment/redeem",
            json={"payload": invitation["payload"], "signature": ""},
        ).status_code
        == 400
    )
    assert mesh.client.post("/api/mesh/nonce", json={}).status_code == 403
    assert mesh.client.post("/api/mesh/session", json={}).status_code == 403
    monkeypatch.setattr(
        mesh.audit, "insert_in_transaction", lambda *args, **kwargs: None
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations",
            headers=mesh.owner_headers,
            json={"label": "Device", "device_key": device.public_key, "scopes": []},
        ).status_code
        == 503
    )


def test_cookie_origin_guards_and_missing_identity(mesh, monkeypatch):
    from orchestrator import web_auth

    async def session(request):
        return (
            mesh.sessions.get(mesh.sid)
            if request.cookies.get(web_auth.COOKIE_NAME)
            else None
        )

    monkeypatch.setattr(web_auth, "ensure_session", session)
    body = {
        "label": "Device",
        "device_key": mesh.sdk.MeshDevice.generate().public_key,
        "scopes": [],
    }
    assert mesh.client.post("/api/mesh/invitations", json=body).status_code == 403
    assert (
        mesh.client.post(
            "/api/mesh/invitations",
            headers={"Origin": "https://foreign.invalid"},
            json=body,
        ).status_code
        == 403
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations",
            headers=[
                ("Origin", "https://mesh.invalid"),
                ("Origin", "https://foreign.invalid"),
            ],
            json=body,
        ).status_code
        == 403
    )
    assert (
        mesh.client.post(
            "/api/mesh/invitations",
            headers={"Origin": "https://mesh.invalid"},
            json=body,
        ).status_code
        == 200
    )
    mesh.client.cookies.clear()
    assert mesh.client.get("/api/mesh/members").status_code == 401


def test_authenticated_foreign_owner_has_no_invitation_or_member_authority(mesh):
    member, invitation = mesh.enroll()
    foreign = {"Authorization": "Bearer " + mesh.token(sub="foreign")}
    assert mesh.client.get("/api/mesh/members", headers=foreign).json() == {
        "members": []
    }
    assert (
        mesh.client.post(
            "/api/mesh/invitations/"
            + invitation["invitation"]["invite_id"]
            + "/reject",
            headers=foreign,
        ).status_code
        == 404
    )
    assert (
        mesh.client.post(
            "/api/mesh/members/" + member.device.member_id + "/revoke", headers=foreign
        ).status_code
        == 404
    )


def test_nonce_rate_is_bounded_and_expires_without_wall_clock_wait(monkeypatch):
    monkeypatch.setattr(ma, "_NONCE_HITS", {})
    clock = [1.0]
    monkeypatch.setattr(ma.time, "monotonic", lambda: clock[0])
    for _ in range(30):
        ma._nonce_budget("owner", "member")
    with pytest.raises(ma.MeshRateLimited):
        ma._nonce_budget("owner", "member")
    clock[0] += 61
    ma._nonce_budget("owner", "member")
    assert len(ma._NONCE_HITS[("owner", "member")]) == 1
    monkeypatch.setattr(
        ma,
        "_NONCE_HITS",
        {(str(index), "member"): [clock[0]] for index in range(10_000)},
    )
    with pytest.raises(ma.MeshRateLimited):
        ma._nonce_budget("new-owner", "member")


def test_nonce_failure_does_not_create_usable_challenge_and_rate_maps429(
    mesh, monkeypatch
):
    member, _ = mesh.enroll()
    monkeypatch.setattr(
        ma, "_nonce_budget", lambda *args: (_ for _ in ()).throw(ma.MeshRateLimited())
    )
    response = mesh.client.post(
        "/api/mesh/nonce",
        json={"owner_id": mesh.owner, "member_id": member.device.member_id},
    )
    assert response.status_code == 429


def test_iam_timeout_maps_unavailable_without_activation(mesh, monkeypatch):
    device, invitation = mesh.invitation()
    assert (
        mesh.client.post(
            "/api/mesh/invitations/"
            + invitation["invitation"]["invite_id"]
            + "/confirm",
            headers=mesh.owner_headers,
        ).status_code
        == 200
    )

    async def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(ma.MeshAdmission, "prepare_iam", fail)
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem", json=device.redemption(invitation["payload"])
    )
    assert (
        response.status_code == 503
        and response.json()["detail"] == ma.MeshIAMUnavailable.code
    )
    assert mesh.store.list_members(mesh.owner) == []


def test_denial_audit_failure_is_unavailable(mesh, monkeypatch):
    device, invitation = mesh.invitation()
    monkeypatch.setattr(
        mesh.audit, "insert_in_transaction", lambda *args, **kwargs: None
    )
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem", json=device.redemption(invitation["payload"])
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_member_session_prepared_before_revoke_or_epoch_change_cannot_mint(mesh):
    member, _ = mesh.enroll()
    original = mesh.store.get_member(mesh.owner, member.device.member_id)
    prepared = await mesh.service.prepare_iam(mesh.owner, original, "reader")
    mesh.enroll(scopes=["tools:read"])
    with pytest.raises(me.MeshEnrollmentError):
        with mesh.store._transaction() as transaction:
            mesh.service.mint(transaction, prepared, original)
    prepared = await mesh.service.prepare_iam(mesh.owner, original, "reader")
    mesh.store.revoke_member(mesh.owner, member.device.member_id)
    with pytest.raises(me.MeshEnrollmentError):
        with mesh.store._transaction() as transaction:
            mesh.service.mint(transaction, prepared, original)


async def test_member_session_cannot_mint_while_identity_is_ambiguous(mesh):
    client, _ = mesh.enroll()
    member_id = client.device.member_id
    original = mesh.store.get_member(mesh.owner, member_id)
    shadow_id = "shadow-" + uuid.uuid4().hex[:16]
    with mesh.store._transaction() as transaction:
        mesh_record, _, _ = mesh.store._current_member(
            transaction, mesh.owner, member_id
        )
        activated = mesh.store._membership.activate_member(
            transaction,
            owner_id=mesh.owner,
            mesh_id=mesh_record.mesh_id,
            member_id=shadow_id,
            member_kind="device",
            display_label="Shadow clone",
            expected_mesh_version=mesh_record.record_version,
            expected_member_version=0,
        )
        mesh.store._membership.bind_public_identity(
            transaction,
            owner_id=mesh.owner,
            mesh_id=mesh_record.mesh_id,
            member_id=shadow_id,
            identity_id=shadow_id,
            algorithm="Ed25519",
            public_key=json.dumps(
                original["device_key"], sort_keys=True, separators=(",", ":")
            ),
            key_fingerprint=hashlib.sha256(
                me._b64url_decode(original["device_key"]["x"])
            ).hexdigest(),
            activated_epoch=activated.membership_epoch,
        )
        mesh.store._put(
            mesh.owner,
            me.MEMBER_NAMESPACE,
            shadow_id,
            {
                **original,
                "member_id": shadow_id,
                "membership_epoch": activated.membership_epoch,
                "invite_id": "shadow-invite",
                "label": "Shadow clone",
            },
        )
    current = mesh.store.get_member(mesh.owner, member_id)
    prepared = await mesh.service.prepare_iam(mesh.owner, current, "reader")
    with pytest.raises(ma.MeshAdmissionError):
        with mesh.store._transaction() as transaction:
            mesh.service.mint(transaction, prepared, current)
    mesh.store.revoke_member(mesh.owner, shadow_id)
    current = mesh.store.get_member(mesh.owner, member_id)
    prepared = await mesh.service.prepare_iam(mesh.owner, current, "reader")
    with mesh.store._transaction() as transaction:
        minted = mesh.service.mint(transaction, prepared, current)
    assert minted["member"]["member_id"] == member_id


def test_duplicate_device_key_invitation_is_conflict_over_rest(mesh):
    client, _ = mesh.enroll()
    response = mesh.client.post(
        "/api/mesh/invitations",
        headers=mesh.owner_headers,
        json={
            "label": "Duplicate clone",
            "device_key": client.device.public_key,
            "scopes": ["tools:read"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "mesh_identity_ambiguous"
    members = mesh.client.get(
        "/api/mesh/members", headers=mesh.owner_headers
    ).json()["members"]
    assert len(members) == 1


def test_duplicate_key_redeem_denial_is_audited_and_leaves_member_unbound(mesh):
    device = mesh.sdk.MeshDevice.generate()
    _, first = mesh.invitation(device, scopes=["tools:read"])
    _, second = mesh.invitation(device, scopes=["tools:read"])
    for invitation in (first, second):
        confirmed = mesh.client.post(
            "/api/mesh/invitations/"
            + invitation["invitation"]["invite_id"]
            + "/confirm",
            headers=mesh.owner_headers,
        )
        assert confirmed.status_code == 200, confirmed.text
    first_client = mesh.sdk.MeshClient("https://mesh.invalid", device, client=mesh.client)
    first_client.redeem(first["payload"], agent_id="reader")
    denial = mesh.client.post(
        "/api/mesh/enrollment/redeem",
        json=device.redemption(second["payload"], agent_id="reader"),
    )
    assert denial.status_code == 409
    assert denial.json()["detail"] == "mesh_identity_ambiguous"
    members = mesh.client.get(
        "/api/mesh/members", headers=mesh.owner_headers
    ).json()["members"]
    assert [member["status"] for member in members] == ["active"]
    failures = mesh.audit.list_for_user(
        mesh.owner,
        limit=50,
        event_classes=["agent_lifecycle"],
        outcomes=["failure"],
    )[0]
    redeem_denials = [
        event
        for event in failures
        if event.action_type == "mesh.member.redeem"
        and event.inputs_meta.get("reason") == "mesh_identity_ambiguous"
    ]
    assert len(redeem_denials) == 1
    assert redeem_denials[0].inputs_meta["invite_id"] == second["invitation"][
        "invite_id"
    ]
    mesh.store.revoke_member(mesh.owner, first_client.device.member_id)
    recovered = mesh.sdk.MeshClient("https://mesh.invalid", device, client=mesh.client)
    recovered.redeem(second["payload"], agent_id="reader")
    assert recovered.request("GET", "/api/mesh/me")["member"]["status"] == "active"


def _dispatcher(mesh, monkeypatch):
    from audit import hooks
    from audit.recorder import Recorder
    from orchestrator import policy, taint, supervisor, hitl
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    host = Orchestrator.__new__(Orchestrator)
    for key, value in vars(mesh.host).items():
        setattr(host, key, value)
    host.ui_sessions = {}
    host.security_flags = {}
    host.agents = {"reader": object()}
    host.a2a_clients = {}
    host.local_agents = {}
    host.credential_manager = SimpleNamespace(
        get_agent_credentials_encrypted=MagicMock(return_value=None)
    )
    host._is_long_running_tool = lambda *args: False
    host._auto_subscribe_stream_artifacts = AsyncMock()
    host._delegation_required = lambda: False
    host._delegation_denied_for_permissions = AsyncMock(return_value=False)
    host.audit_recorder = Recorder(mesh.audit)
    host._dispatch_context = {}
    host._pending_cap_entries = {}
    host._chain_budgets = {}
    host.effects = []
    monkeypatch.setattr(hooks, "get_recorder", lambda: host.audit_recorder)
    for module, name in (
        (policy, "policy_enabled"),
        (taint, "taint_enabled"),
        (supervisor, "supervisor_enabled"),
        (hitl, "hitl_enabled"),
    ):
        monkeypatch.setattr(module, name, lambda: False)

    async def physical(agent, tool, arguments, **kwargs):
        async def effect(capabilities):
            host.effects.append((agent, tool, dict(arguments)))
            return MCPResponse(result={"observed": arguments.get("value")})

        host._register_dispatch_context(
            "synthetic-request", agent, arguments, kwargs["ui_websocket"]
        )
        return await host._execute_governed_attempt(
            kwargs["ui_websocket"],
            agent,
            tool,
            arguments,
            user_id=kwargs["protected_owner_id"],
            channel=kwargs["protected_channel"],
            audit_correlation_id=kwargs["protected_audit_correlation_id"],
            actor_user_id=kwargs["protected_actor_user_id"],
            auth_principal=kwargs["protected_auth_principal"],
            conversation_id=kwargs["protected_conversation_id"],
            invoke=effect,
        )

    host.execute_tool_and_wait = physical
    mesh.app.state.orchestrator = host
    return host


def test_member_rest_tool_uses_ordinary_audited_dispatch_and_revoke_denies(
    mesh, monkeypatch
):
    client, _ = mesh.enroll()
    host = _dispatcher(mesh, monkeypatch)
    response = client.invoke("reader", "read", {"value": "synthetic"})
    assert response["result"] == {"observed": "synthetic"}
    assert len(host.effects) == 1
    forwarded = host.effects[0][2]["_delegation_token"]
    assert jwt.get_unverified_claims(forwarded)["scope"] == "tools:read tool:read"
    assert jwt.get_unverified_claims(forwarded)["aud"] == "astral-agent-service"
    parent = host._dispatch_context["synthetic-request"]["parent_token"]
    assert (
        parent[ma.MEMBER_CLAIM]
        == ma.verify_member_token(client._token)[ma.MEMBER_CLAIM]
    )
    assert parent["cnf"] == ma.verify_member_token(client._token)["cnf"]
    mesh.store.revoke_member(mesh.owner, client.device.member_id)
    claims = ma.verify_member_token(client._token)
    result = __import__("asyncio").run(
        host.execute_authorized_tool(
            claims=claims,
            user_id=mesh.owner,
            agent_id="reader",
            tool_name="read",
            arguments={},
            channel="rest",
        )
    )
    assert result.error["code"] == ma.MeshAdmissionError.code
    assert len(host.effects) == 1 and host.ui_sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    ["scope", "owner", "permission", "policy", "delegation", "audit", "before_effect"],
)
async def test_normal_dispatch_fences_member_before_actuator(mesh, monkeypatch, denial):
    from orchestrator import policy

    client, _ = mesh.enroll()
    host = _dispatcher(mesh, monkeypatch)
    claims = ma.verify_member_token(client._token)
    owner = mesh.owner
    tool = "read"
    if denial == "scope":
        tool = "write"
    elif denial == "owner":
        owner = "foreign"
    elif denial == "permission":
        mesh.permissions.set_agent_scopes(mesh.owner, "reader", {"tools:read": False})
    elif denial == "policy":
        monkeypatch.setattr(policy, "policy_enabled", lambda: True)
        monkeypatch.setattr(
            policy,
            "evaluate_policy",
            lambda *args: policy.PolicyDecision(
                effect=policy.DENY, reason="synthetic policy denial"
            ),
        )
    elif denial == "delegation":
        monkeypatch.setattr(
            ma.MeshAdmission,
            "delegation_token",
            lambda *args: (_ for _ in ()).throw(ma.MeshAdmissionError()),
        )
    elif denial == "audit":
        monkeypatch.setattr(
            mesh.audit, "insert_in_transaction", lambda *args, **kwargs: None
        )
    else:
        original = host.execute_tool_and_wait

        async def revoke(*args, **kwargs):
            mesh.store.revoke_member(mesh.owner, client.device.member_id)
            return await original(*args, **kwargs)

        host.execute_tool_and_wait = revoke
    response = await host.execute_authorized_tool(
        claims=claims,
        user_id=owner,
        agent_id="reader",
        tool_name=tool,
        arguments={"value": "denied"},
        channel="rest",
    )
    assert response.error is not None
    assert host.effects == [] and host.ui_sessions == {}


@pytest.mark.asyncio
async def test_chain_cannot_strip_mesh_binding_and_child_preserves_it(
    mesh, monkeypatch
):
    from orchestrator.orchestrator import GateRefusal

    client, _ = mesh.enroll()
    host = _dispatcher(mesh, monkeypatch)
    claims = ma.verify_member_token(client._token)
    socket = object()
    host.ui_sessions[socket] = {"sub": mesh.owner}
    result = await host._run_gate_stack(
        socket, "reader", "read", {}, user_id=mesh.owner, parent_token=claims
    )
    assert (
        isinstance(result, GateRefusal)
        and result.response.error["code"] == ma.MeshAdmissionError.code
    )
    child = delegation.mint_child_delegation(claims, "reader", ["tools:read"])
    assert (
        child[ma.MEMBER_CLAIM] == claims[ma.MEMBER_CLAIM]
        and child["cnf"] == claims["cnf"]
    )
    child[ma.MEMBER_CLAIM]["member_id"] = "foreign"
    assert claims[ma.MEMBER_CLAIM]["member_id"] == client.device.member_id
    for binding, confirmation in ((None, {}), ({}, None)):
        malformed = {**claims, ma.MEMBER_CLAIM: binding, "cnf": confirmation}
        with pytest.raises(delegation.RecursiveDelegationError):
            delegation.mint_child_delegation(malformed, "reader", ["tools:read"])


@pytest.mark.asyncio
async def test_physical_and_delegation_owner_fence_survives_direct_internal_call(
    mesh, monkeypatch
):
    from shared.protocol import MCPResponse

    client, _ = mesh.enroll()
    host = _dispatcher(mesh, monkeypatch)
    socket = object()
    host.ui_sessions[socket] = ma.verify_member_token(client._token)
    assert await host._get_delegation_token(socket, "reader", "foreign") is None
    invoke = AsyncMock(return_value=MCPResponse(result="unexpected"))
    result = await host._execute_governed_attempt(
        socket,
        "reader",
        "read",
        {},
        user_id="foreign",
        channel="rest",
        audit_correlation_id="synthetic",
        actor_user_id="foreign",
        auth_principal="foreign",
        conversation_id=None,
        invoke=invoke,
    )
    assert result.error["code"] == ma.MeshAdmissionError.code
    invoke.assert_not_awaited()
