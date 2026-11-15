"""Covers orchestrator/institution_api.py and orchestrator/institution_runtime.py: the descriptor declares the node's PHI approval and fresh-attestation rule, an owner opens a pairing offer in that owner's own mesh only, other callers and profiles are refused, and the runtime gives each owner a bridge that enforces the institution policy."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt as jose_jwt

from orchestrator import institution_runtime, mesh_api, node_api, node_mesh, node_mesh_chat as nmc
from orchestrator.institution_meshes import Owner
from shared import institution_profile as ip
from shared import node_identity as ni
from shared.token_verify import with_owner_identity
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_chat import History, answering, no_issuers, settle

KEY = ec.derive_private_key(0x1D2C3B4A5968778695A4B3C2D1E0F00F, ec.SECP256R1())
ISS = "https://idp.example/realms/a"
OWNER_A = ni.canonical_owner_id(ISS, "alice")
OWNER_B = ni.canonical_owner_id(ISS, "bob")


def owner_actor(subject="alice", issuer=ISS):
    claims = with_owner_identity({"sub": subject, "iss": issuer}, issuer)
    return {"kind": "owner", "id": claims["sub"], "member": None, "claims": claims}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISS)
    monkeypatch.setenv(ip.KEY_ENV, ip.key_text(KEY))
    monkeypatch.delenv(ip.APPROVAL_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    ni.reset_cache()
    node_api.reset_state()
    fake.Kernel.instances.clear()
    actor = owner_actor()
    app = FastAPI()
    app.include_router(node_api.node_router)
    app.dependency_overrides[mesh_api._read_actor] = lambda: actor
    app.dependency_overrides[mesh_api._store] = lambda: None
    holder = SimpleNamespace(app=app, actor=actor, tmp=tmp_path, monkeypatch=monkeypatch)
    yield holder
    node_api.reset_state()
    institution_runtime.set_current(None)
    nmc.install(None)
    ni.reset_cache()


def client(env):
    return AsyncClient(transport=ASGITransport(app=env.app), base_url="https://node.test", headers={"origin": "https://node.test"})


def runtime_for(env, **kwargs):
    return institution_runtime.InstitutionRuntime(
        config=ip.load(),
        base_dir=env.tmp / "node",
        history=History(),
        runner=kwargs.pop("runner", answering()),
        calls=None,
        binding=fake,
        issuers=no_issuers,
        settings=node_mesh.MeshSettings(mailbox=node_mesh.MailboxSettings(enabled=False)),
        phi_active=lambda _owner: False,
        **kwargs,
    )


async def test_the_descriptor_declares_the_institution_key_and_rules_without_an_approval(env):
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    body = response.json()
    assert response.status_code == 200 and body["profile"] == "institution"
    assert body["institution_key"] == ip.key_text(KEY) and body["phi_approval"] is None
    assert body["owner_hash"] is None and body["accepted_issuers"] == [ISS]
    assert {"institution", "multi-owner", "fresh-attestation", "peer-phi-group", "chat"} <= set(body["capabilities"])
    assert "d" not in body["node_key"]


async def test_the_descriptor_carries_the_signed_node_approval_while_it_is_current(env):
    node_id = ni.load_node_identity().node_id
    path = env.tmp / "approval.json"
    path.write_text(json.dumps(ip.sign_phi_approval(KEY, subject_kind="node", subject=node_id, issued_s=0, not_after_s=2**40)))
    env.monkeypatch.setenv(ip.APPROVAL_ENV, str(path))
    async with client(env) as http:
        body = (await http.get("/api/node/descriptor")).json()
    approval = body["phi_approval"]
    assert approval["subject"] == node_id and approval["institution"] == ip.key_text(KEY)
    ip.verify_phi_approval(approval, body["institution_key"], subject_kind="node", subject=node_id, now_s=1)


async def test_an_expired_approval_is_left_out_of_the_descriptor(env):
    node_id = ni.load_node_identity().node_id
    path = env.tmp / "approval.json"
    path.write_text(json.dumps(ip.sign_phi_approval(KEY, subject_kind="node", subject=node_id, issued_s=1, not_after_s=2)))
    env.monkeypatch.setenv(ip.APPROVAL_ENV, str(path))
    async with client(env) as http:
        assert (await http.get("/api/node/descriptor")).json()["phi_approval"] is None


async def test_the_descriptor_uses_the_running_configuration_when_there_is_one(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    env.monkeypatch.delenv(ip.KEY_ENV)
    async with client(env) as http:
        assert (await http.get("/api/node/descriptor")).json()["institution_key"] == ip.key_text(KEY)


async def test_a_broken_configuration_is_reported_not_guessed(env):
    env.monkeypatch.delenv(ip.KEY_ENV)
    async with client(env) as http:
        response = await http.get("/api/node/descriptor")
    assert response.status_code == 503 and response.json()["detail"] == "institution_config_invalid"


async def test_the_jwks_is_served_to_an_institution_node(env):
    async with client(env) as http:
        response = await http.get("/.well-known/astral-node/jwks.json")
    assert response.status_code == 200 and response.json()["keys"][0]["kid"] == ni.load_node_identity().kid


async def test_other_profiles_do_not_answer(env):
    env.monkeypatch.delenv(ni.PROFILE_ENV)
    async with client(env) as http:
        assert (await http.get("/api/node/descriptor")).status_code == 404
        assert (await http.post("/api/institution/pairing", json={})).status_code == 404


async def test_the_personal_pairing_route_is_not_open_on_an_institution_node(env):
    async with client(env) as http:
        response = await http.post("/api/node/pairing", json={})
    assert response.status_code == 404


async def test_an_owner_opens_a_pairing_offer_in_their_own_mesh(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        response = await http.post("/api/institution/pairing", json={})
    body = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert body["mesh"]["state"] == "offer" and body["mesh"]["payload"] == "payload-1"
    assert body["owner_hash"] == ni.owner_principal_hash(Owner.verified(ISS, "alice").partition)
    assert body["node"]["profile"] == "institution"
    assert [owner.subject for owner in runtime.meshes.owners] == ["alice"]
    await runtime.stop()


async def test_an_owner_can_request_a_fresh_kernel_attestation_challenge(env, monkeypatch):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        await http.post("/api/institution/pairing", json={})
        kernel = fake.Kernel.instances[0]
        monkeypatch.setattr(
            kernel,
            "begin_attestation",
            lambda: SimpleNamespace(nonce="kernel-nonce", issuer=ISS, client_id="astral", mesh_id="mesh-1"),
            raising=False,
        )
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "nonce": "kernel-nonce", "issuer": ISS, "client_id": "astral", "mesh_id": "mesh-1"
    }
    await runtime.stop()


async def test_attestation_completion_passes_the_owner_bound_token_to_kernel(env, monkeypatch):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    observed = []
    token = jose_jwt.encode({"iss": ISS, "sub": "alice", "nonce": "kernel-nonce"}, KEY, algorithm="ES256")
    async with client(env) as http:
        await http.post("/api/institution/pairing", json={})
        monkeypatch.setattr(fake.Kernel.instances[0], "reattest", observed.append, raising=False)
        response = await http.post("/api/institution/attestation", json={"id_token": token})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"attested": True} and observed == [token]
    await runtime.stop()


async def test_attestation_token_for_another_owner_is_refused_before_kernel(env, monkeypatch):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    observed = []
    token = jose_jwt.encode({"iss": ISS, "sub": "bob", "nonce": "kernel-nonce"}, KEY, algorithm="ES256")
    async with client(env) as http:
        await http.post("/api/institution/pairing", json={})
        monkeypatch.setattr(fake.Kernel.instances[0], "reattest", observed.append, raising=False)
        response = await http.post("/api/institution/attestation", json={"id_token": token})
    assert response.status_code == 403 and response.json()["detail"] == "institution_owner_mismatch"
    assert observed == []
    await runtime.stop()


async def test_malformed_attestation_token_is_refused(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        response = await http.post("/api/institution/attestation", json={"id_token": "not-a-jwt"})
    assert response.status_code == 400 and response.json()["detail"] == "institution_id_token_invalid"
    await runtime.stop()


async def test_non_ascii_attestation_token_is_refused_without_a_server_error(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        response = await http.post(
            "/api/institution/attestation", content=json.dumps({"id_token": "\ud800"}),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400 and response.json()["detail"] == "institution_id_token_invalid"
    await runtime.stop()


async def test_oversized_attestation_token_is_refused(env):
    async with client(env) as http:
        response = await http.post("/api/institution/attestation", json={"id_token": "a" * 65537})
    assert response.status_code == 413 and response.json()["detail"] == "institution_id_token_too_large"


async def test_token_without_owner_identity_is_refused(env, monkeypatch):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    mesh = SimpleNamespace(kernel=object(), _binding=fake)

    async def serve_owner(_owner):
        return mesh

    monkeypatch.setattr(runtime, "serve_owner", serve_owner)
    token = jose_jwt.encode({"iss": ISS}, KEY, algorithm="ES256")
    async with client(env) as http:
        response = await http.post("/api/institution/attestation", json={"id_token": token})
    assert response.status_code == 403 and response.json()["detail"] == "institution_owner_mismatch"


async def test_attestation_routes_are_inactive_on_other_node_profiles(env):
    env.monkeypatch.delenv(ni.PROFILE_ENV)
    async with client(env) as http:
        challenge = await http.post("/api/institution/attestation/challenge", json={})
        completion = await http.post("/api/institution/attestation", json={"id_token": "x.y.z"})
    assert challenge.status_code == completion.status_code == 404


async def test_attestation_requires_authenticated_owner(env):
    from fastapi import HTTPException

    env.app.dependency_overrides[mesh_api._read_actor] = lambda: (_ for _ in ()).throw(HTTPException(401, "Not authenticated"))
    async with client(env) as http:
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 401


@pytest.mark.parametrize(("endpoint", "kernel_method", "status"), [
    ("/api/institution/attestation/challenge", "begin_attestation", 409),
    ("/api/institution/attestation", "reattest", 403),
    ("/api/institution/attestation/challenge", "unavailable", 503),
    ("/api/institution/attestation", "unavailable", 503),
])
async def test_native_kernel_attestation_errors_are_returned_as_denials(env, monkeypatch, endpoint, kernel_method, status):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    token = jose_jwt.encode({"iss": ISS, "sub": "alice"}, KEY, algorithm="ES256")

    def refused(*_args):
        raise fake.KernelError.Verification("attestation.refused")

    if kernel_method == "unavailable":
        def unavailable(*_args):
            raise node_mesh.MeshUnavailable("mesh_not_started")

        method = "begin_attestation" if endpoint.endswith("challenge") else "reattest"
        kernel = SimpleNamespace(**{method: unavailable})
        expected = "mesh_not_started"
    else:
        kernel = SimpleNamespace(**{kernel_method: refused})
        expected = "attestation.refused"
    mesh = SimpleNamespace(kernel=kernel, _binding=fake)

    async def serve_owner(_owner):
        return mesh

    monkeypatch.setattr(runtime, "serve_owner", serve_owner)
    async with client(env) as http:
        body = {"id_token": token} if endpoint.endswith("attestation") else {}
        response = await http.post(endpoint, json=body)
    assert response.status_code == status and response.json()["detail"] == expected


async def test_an_accepted_owner_mesh_failure_is_reported(env, monkeypatch):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)

    async def refuse_owner(_owner):
        raise node_mesh.NodeMeshError("mesh_owner_refused", 409)

    monkeypatch.setattr(runtime, "serve_owner", refuse_owner)
    async with client(env) as http:
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 409 and response.json()["detail"] == "mesh_owner_refused"


async def test_owner_claims_must_match_their_canonical_partition(env):
    env.actor["claims"]["sub"] = "another-owner"
    async with client(env) as http:
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 400 and response.json()["detail"] == "owner_invalid"


async def test_attestation_completion_rejects_extra_nonce_fields(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        response = await http.post("/api/institution/attestation", json={"id_token": "x.y.z", "nonce": "caller"})
    assert response.status_code == 400 and response.json()["detail"] == "institution_id_token_required"
    await runtime.stop()


async def test_attestation_challenge_requires_a_running_runtime(env):
    async with client(env) as http:
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 503 and response.json()["detail"] == "institution_mesh_unavailable"


async def test_a_delegated_member_cannot_attest_an_institution_node(env):
    env.actor.update({"kind": "member", "id": "m1", "member": {"owner_id": "alice", "scopes": ["mesh:confirm"]}, "claims": {}})
    institution_runtime.set_current(runtime_for(env))
    async with client(env) as http:
        response = await http.post("/api/institution/attestation/challenge", json={})
    assert response.status_code == 403 and response.json()["detail"] == "institution_owner_required"


async def test_two_owners_get_two_offers_from_two_separate_peers(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        first = (await http.post("/api/institution/pairing", json={})).json()
        env.actor.update(owner_actor("bob"))
        second = (await http.post("/api/institution/pairing", json={})).json()
    assert first["owner_hash"] != second["owner_hash"]
    assert len(fake.Kernel.instances) == 2 and len({k.config.data_dir for k in fake.Kernel.instances}) == 2
    await runtime.stop()


async def test_an_owner_already_in_a_mesh_is_told_so_not_given_another_offer(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    async with client(env) as http:
        await http.post("/api/institution/pairing", json={})
        fake.Kernel.instances[0].join()
        body = (await http.post("/api/institution/pairing", json={})).json()
    assert body["mesh"]["state"] == "enrolled" and body["mesh"]["mesh_id"] == "mesh-1"
    await runtime.stop()


async def test_a_delegated_member_cannot_open_an_owners_pairing(env):
    env.actor.update({"kind": "member", "id": "m1", "member": {"owner_id": "alice", "scopes": ["mesh:confirm"]}, "claims": {}})
    institution_runtime.set_current(runtime_for(env))
    async with client(env) as http:
        response = await http.post("/api/institution/pairing", json={})
    assert response.status_code == 403 and response.json()["detail"] == "institution_owner_required"


@pytest.mark.parametrize("claims", [
        {"sub": "alice"},
        {"iss": ISS},
        {"sub": " alice", "iss": ISS},
        {"sub": "alice", "iss": ISS},
        {"sub": "oidc.0.alice", "iss": ISS, "astral_idp_sub": "alice"},
    ])
async def test_a_token_without_an_issuer_and_subject_is_refused(env, claims):
    env.actor["claims"] = claims
    institution_runtime.set_current(runtime_for(env))
    async with client(env) as http:
        response = await http.post("/api/institution/pairing", json={})
    assert response.status_code == 400 and response.json()["detail"] == "owner_invalid"


async def test_pairing_without_a_running_institution_runtime_is_unavailable(env):
    async with client(env) as http:
        response = await http.post("/api/institution/pairing", json={})
    assert response.status_code == 503


async def test_the_same_subject_under_a_new_issuer_is_a_separate_owner_keyed_by_its_canonical_id(env):
    runtime = runtime_for(env)
    institution_runtime.set_current(runtime)
    other = "https://idp.example/realms/b"
    async with client(env) as http:
        first = await http.post("/api/institution/pairing", json={})
        env.actor.update(owner_actor("alice", other))
        second = await http.post("/api/institution/pairing", json={})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["owner_hash"] != second.json()["owner_hash"]
    keys = {ni.canonical_owner_id(ISS, "alice"), ni.canonical_owner_id(other, "alice")}
    assert {owner.partition for owner in runtime.meshes.owners} == keys
    assert all(runtime.bridges.get(key) is not None for key in keys)
    assert runtime.bridges.get("alice") is None
    await runtime.stop()


async def test_the_runtime_bridge_enforces_the_policy_and_keeps_owners_apart(env):
    runtime = runtime_for(env)
    nmc.install(runtime.bridges)
    first = await runtime.serve_owner(Owner.verified(ISS, "alice"))
    second = await runtime.serve_owner(Owner.verified(ISS, "bob"))
    kernel_a, kernel_b = fake.Kernel.instances
    for kernel in (kernel_a, kernel_b):
        kernel.join()
        kernel.add_chat("c", [])
    kernel_a.unattested.add("dev-phone")
    kernel_a.add_request("r1", "c")
    kernel_b.add_request("r2", "c")
    assert runtime.bridges.get(OWNER_A) is not runtime.bridges.get(OWNER_B)
    assert (runtime.bridges.get(OWNER_A)._owner, runtime.bridges.get(OWNER_B)._owner) == (OWNER_A, OWNER_B)
    await runtime.bridges.get(OWNER_A).scan()
    await runtime.bridges.get(OWNER_B).scan()
    await settle(lambda: kernel_a.refused and kernel_b.request("r2").state == fake.RequestState.ANSWERED)
    assert kernel_a.refused == [("r1", "institution.attestation_stale")]
    assert kernel_b.request("r2").state == fake.RequestState.ANSWERED and kernel_b.refused == []
    assert first is not second
    await runtime.stop()
    assert runtime.bridges.get(OWNER_A) is None and not kernel_a.network and not kernel_b.network


async def test_a_peer_without_request_support_is_not_served(env, monkeypatch):
    runtime = runtime_for(env)
    monkeypatch.setattr(node_mesh.NodeMesh, "requests_supported", property(lambda self: False))
    with pytest.raises(node_mesh.MeshUnavailable):
        await runtime.serve_owner(Owner.verified(ISS, "alice"))
    assert runtime.meshes.owners == []
    await runtime.stop()


async def test_start_for_installs_the_runtime_and_shutdown_removes_it(env, monkeypatch):
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(institution_runtime.node_mesh, "MeshSettings", node_mesh.MeshSettings)
    orch = SimpleNamespace(history=History())
    runtime = await institution_runtime.start_for(orch)
    assert institution_runtime.current() is runtime and nmc.active() is runtime.bridges
    await nmc.stop_all()
    assert institution_runtime.current() is None and nmc.active() is None


async def test_start_for_without_the_kernel_binding_serves_no_meshes(env, monkeypatch):
    def missing():
        raise node_mesh.MeshUnavailable("kernel_binding_missing")

    monkeypatch.setattr(node_mesh, "load_binding", missing)
    assert await institution_runtime.start_for(SimpleNamespace(history=History())) is None
    assert institution_runtime.current() is None


async def test_start_for_refuses_to_run_without_the_institution_key(env, monkeypatch):
    monkeypatch.delenv(ip.KEY_ENV)
    with pytest.raises(SystemExit) as info:
        await institution_runtime.start_for(SimpleNamespace(history=History()))
    assert info.value.code == 78


async def test_the_chat_bridge_entry_point_hands_an_institution_node_to_its_runtime(env, monkeypatch):
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    await nmc.start_for(SimpleNamespace(history=History()))
    assert institution_runtime.current() is not None
    await nmc.stop_all()
