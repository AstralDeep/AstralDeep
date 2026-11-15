"""Exercises the removed mesh-identity enrollment surface with a real signed roster.
External owner authentication can open its isolated peer, but cannot reintroduce mesh attestations or grant outside identity.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator import institution_api, institution_runtime, mesh_api, node_mesh_chat
from shared import node_identity as ni
from shared.token_verify import with_owner_identity
from tests.test_institution_kernel_integration import ISS, join_owner, make_runtime
from tests.test_node_mesh_kernel_integration import request_state, until


async def test_external_owner_cannot_reintroduce_mesh_identity_attestation(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    runtime = make_runtime(tmp_path)
    institution_runtime.set_current(runtime)
    node_mesh_chat.install(runtime.bridges)
    app = FastAPI()
    app.include_router(institution_api.institution_router)
    claims = with_owner_identity({"sub": "user-1", "iss": ISS}, ISS)
    actor = {"kind": "owner", "id": claims["sub"], "member": None, "claims": claims}
    app.dependency_overrides[mesh_api._read_actor] = lambda: actor
    laptop = None
    try:
        await runtime.start()
        node, laptop, device = await join_owner(runtime, tmp_path, "user-1", 91)
        mesh_id = node.kernel.mesh_principal().mesh_id
        before = {(member.device_id, tuple(member.feeds)) for member in node.kernel.roster().members}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://node.test") as http:
            for path in ("/api/institution/attestation/challenge", "/api/institution/attestation"):
                response = await http.post(path, json={"id_token": "outside-token"})
                assert response.status_code == 404
        assert node.kernel.mesh_principal().mesh_id == mesh_id
        assert {(member.device_id, tuple(member.feeds)) for member in node.kernel.roster().members} == before
        chat = laptop.create_chat("outside identity boundary")
        laptop.send_message(chat, "local membership", None)
        request = laptop.send_request(chat, "run outside", device, None, None)
        await until(request_state(laptop, request, "REFUSED"), "external identity refusal")
        assert laptop.request(request).code == "institution.external_identity_unavailable"
    finally:
        node_mesh_chat.install(None)
        institution_runtime.set_current(None)
        if laptop is not None:
            await laptop.stop_network()
        await runtime.stop()
