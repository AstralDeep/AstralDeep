"""Exercises institution attestation HTTP routes against the pinned Kernel verifier and an enrolled owner mesh.
The fixture uses signed test-issuer tokens and loopback transport, while the API remains the same production router.
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator import institution_api, institution_runtime, mesh_api, node_mesh_chat
from orchestrator.institution_meshes import Owner
from shared import node_identity as ni
from shared.token_verify import with_owner_identity
from tests.test_institution_kernel_integration import ISS, join_owner, make_runtime
from tests import test_node_mesh_kernel_integration as kernel_test

ISS_B = "https://auth.second.example.test/realms/astral"


def signed_token(nonce, subject, issuer=ISS):
    header = kernel_test.b64(json.dumps({"alg": "ES256", "kid": "k1", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    payload = kernel_test.b64(json.dumps({
        "iss": issuer, "aud": kernel_test.CLIENT, "sub": subject,
        "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": nonce,
    }, separators=(",", ":"), sort_keys=True).encode())
    message = f"{header}.{payload}"
    return f"{message}.{kernel_test.b64(kernel_test.sign(kernel_test.ISSUER_KEY, message.encode()))}"


async def both_test_issuers():
    first = kernel_test.issuer_json()
    second = {**first, "issuer": ISS_B}
    return [first, second]


def open_second_issuer_device(directory, mailbox):
    issuer = {**kernel_test.issuer_json(), "issuer": ISS_B}
    config = kernel_test.ak.KernelConfig(
        data_dir=str(directory),
        operations_secret=kernel_test.ak.new_operations_secret(),
        wrap_secret=kernel_test.ak.new_operations_secret(),
        restore_marker=bytes([92]) * 32,
        device_label="second issuer owner device",
        device_kind=kernel_test.ak.DeviceKind.DESKTOP,
        issuers=[kernel_test.ak.IssuerConfig(**issuer)],
        network=kernel_test.ak.NetworkConfig(
            relay=kernel_test.ak.RelayChoice.DISABLED(),
            local_discovery=False,
            internet_lookup=False,
            loopback_only=True,
            sync_interval_ms=500,
            mailbox=mailbox,
        ),
    )
    return kernel_test.ak.Kernel.open(config, kernel_test.Signer(), None, None, kernel_test.Listener())


async def test_http_attestation_uses_kernel_nonce_and_verifies_the_owner_token(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    runtime = make_runtime(tmp_path)
    institution_runtime.set_current(runtime)
    node_mesh_chat.install(runtime.bridges)
    app = FastAPI()
    app.include_router(institution_api.institution_router)
    claims = with_owner_identity({"sub": "user-1", "iss": ISS}, ISS)
    actor = {"kind": "owner", "id": claims["sub"], "member": None, "claims": claims}
    app.dependency_overrides[mesh_api._read_actor] = lambda: actor
    running = []
    try:
        await runtime.start()
        node, laptop, node_device = await join_owner(runtime, tmp_path, "user-1", 91)
        running.append(laptop)
        standing = await node.peer_standing(node_device)
        assert standing is not None and standing.needs_attestation
        headers = {"origin": "https://node.test"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://node.test", headers=headers) as http:
            challenge = await http.post("/api/institution/attestation/challenge", json={})
            assert challenge.status_code == 200
            data = challenge.json()
            assert data == {
                "nonce": data["nonce"], "issuer": ISS, "client_id": kernel_test.CLIENT, "mesh_id": data["mesh_id"]
            }
            assert data["nonce"] and data["mesh_id"]
            wrong_owner = await http.post(
                "/api/institution/attestation", json={"id_token": signed_token(data["nonce"], "other")}
            )
            assert wrong_owner.status_code == 403
            wrong_nonce = await http.post(
                "/api/institution/attestation", json={"id_token": signed_token("wrong-nonce", "user-1")}
            )
            assert wrong_nonce.status_code == 403
            complete = await http.post(
                "/api/institution/attestation", json={"id_token": signed_token(data["nonce"], "user-1")}
            )
            assert complete.status_code == 200 and complete.json() == {"attested": True}
            assert complete.headers["cache-control"] == "no-store"
        standing = await node.peer_standing(node_device)
        assert standing is not None and not standing.needs_attestation
        assert node.kernel.roster().members
        assert Owner.verified(ISS, "user-1").partition == actor["id"]
    finally:
        node_mesh_chat.install(None)
        institution_runtime.set_current(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()


async def test_http_attestation_orders_a_second_configured_issuer_for_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    runtime = make_runtime(tmp_path)
    runtime.meshes._issuers = both_test_issuers
    institution_runtime.set_current(runtime)
    node_mesh_chat.install(runtime.bridges)
    app = FastAPI()
    app.include_router(institution_api.institution_router)
    claims = with_owner_identity({"sub": "second-user", "iss": ISS_B}, ISS_B)
    actor = {"kind": "owner", "id": claims["sub"], "member": None, "claims": claims}
    app.dependency_overrides[mesh_api._read_actor] = lambda: actor
    running = []
    try:
        await runtime.start()
        owner = Owner.verified(ISS_B, "second-user")
        node = await runtime.serve_owner(owner)
        laptop = open_second_issuer_device(tmp_path / "second-laptop", runtime.meshes._mailbox.address)
        await laptop.start_network()
        running.append(laptop)
        created = laptop.begin_mesh_creation()
        laptop.complete_mesh_creation(signed_token(created.nonce, "second-user", ISS_B))
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], kernel_test.ak.HistoryGrant.ALL)
        await kernel_test.until(lambda: kernel_test.enrolled(node), "the second-issuer node to join")
        node_device = (await node.status())["member_id"]
        standing = await node.peer_standing(node_device)
        assert standing is not None and standing.needs_attestation
        headers = {"origin": "https://node.test"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://node.test", headers=headers) as http:
            challenge = await http.post("/api/institution/attestation/challenge", json={})
            data = challenge.json()
            assert challenge.status_code == 200 and data["issuer"] == ISS_B
            complete = await http.post(
                "/api/institution/attestation",
                json={"id_token": signed_token(data["nonce"], "second-user", ISS_B)},
            )
            assert complete.status_code == 200
        standing = await node.peer_standing(node_device)
        assert standing is not None and not standing.needs_attestation
    finally:
        node_mesh_chat.install(None)
        institution_runtime.set_current(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()
