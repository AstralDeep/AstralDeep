"""Composes a real enrolled native mesh route with the node publisher client and encrypted Plane gateway.
Only the APNs/HTTPS network transports are recorded; principal, signed route, removal, rate admission, owner audit and the node's mesh-id owner binding remain real."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import astral_kernel as ak
import pytest

from audit.repository import AuditRepository
from orchestrator.mesh_push_client import RemoteWakeGateway
from orchestrator.mesh_push_gateway import MeshPushPublisher
from orchestrator.mesh_push_registry import MeshPushRegistry
from orchestrator.push_wake import NO_DESTINATION, SENT, WakeLimiter
from orchestrator import node_api, node_mesh
from orchestrator.boot_posture import assert_production_posture
from shared import node_identity, token_verify
from tests.test_mesh_push_api import Apns
from tests.test_mesh_push_registry import KEY, plane  # noqa: F401
from tests.test_node_mesh_kernel_integration import model_turn, paired, request_state, until


async def route_for(node, laptop, runtime):
    status = await laptop.mesh_status()
    registry = MeshPushRegistry(plane_runtime=runtime, audit_repository=AuditRepository(plane_runtime=runtime),
                                encryption_key=lambda: KEY)
    registered = registry.register(status.mesh_id, mesh_id=status.mesh_id, device_id=status.device_id,
                                   token="private-physical-phone-token", expected_revision=0)
    laptop.set_wake_route(status.mesh_id, status.device_id, ak.WakeRoute(
        publisher="astral", capability=registered.capability, expires_at_ms=registered.expires_at_ms))
    await until(lambda: any(item.device_id == status.device_id for item in node.kernel.wake_routes()),
                "the signed phone wake route to reach the node")
    adapter = Apns()
    publisher = MeshPushPublisher(registry=registry, adapter=adapter)
    calls = []

    def transport(method, url, **options):
        calls.append((method, url, options))
        asyncio.run(publisher.wake(options["api_key"], **options["json_body"]))
        return SimpleNamespace(status_code=204)

    sender = RemoteWakeGateway("https://publisher.example", transport=transport,
                               limiter=WakeLimiter(min_interval_seconds=0)).for_mesh(node)
    return sender, status, adapter, calls


async def test_a_wake_reaches_the_signed_route_for_the_bound_mesh_owner_and_refuses_any_other(tmp_path, monkeypatch, plane):  # noqa: F811
    async with paired(tmp_path, monkeypatch) as (node, laptop, _, _bridge):
        sender, target, adapter, calls = await route_for(node, laptop, plane)

        async def no_iam(*args, **kwargs):
            pytest.fail("mesh wake requested per-call IAM")

        monkeypatch.setattr("shared.jwks_cache.get_jwks", no_iam)
        owner = node_identity.owner_id_for_mesh(target.mesh_id)
        assert owner == node.personal_owner_id() == node_identity.bound_owner()
        other = node_identity.owner_id_for_mesh("sha256:" + "f" * 64)
        for refused in (other, target.mesh_id, "oidc.0123456789abcdef.owner"):
            assert (await sender.wake(refused, target.device_id)).status == NO_DESTINATION
        assert calls == [] and adapter.tokens == []
        assert (await sender.wake(owner, target.device_id)).status == SENT
        assert [call[2]["json_body"] for call in calls] == [{"mesh_id": target.mesh_id, "device_id": target.device_id}]
        assert adapter.tokens == ["private-physical-phone-token"]
        laptop.set_wake_route(target.mesh_id, target.device_id, None)
        await until(lambda: not any(item.device_id == target.device_id for item in node.kernel.wake_routes()),
                    "the phone wake withdrawal to reach the node")
        assert (await sender.wake(owner, target.device_id)).status == NO_DESTINATION
        assert len(calls) == 1 and len(adapter.tokens) == 1


@pytest.mark.parametrize("change", ["stop", "remove"])
async def test_actual_native_stop_or_signed_own_removal_blocks_wake_before_network(tmp_path, monkeypatch, plane, change):  # noqa: F811
    async with paired(tmp_path, monkeypatch) as (node, laptop, node_device, _bridge):
        sender, target, adapter, calls = await route_for(node, laptop, plane)
        assert target.mesh_id == node.kernel.mesh_principal().mesh_id
        owner = node_identity.owner_id_for_mesh(target.mesh_id)
        assert (await sender.wake(owner, target.device_id)).status == SENT
        if change == "stop":
            node.kernel.raise_stop()
        else:
            laptop.remove_device(node_device)
            await until(lambda: any(item.device_id == node_device for item in node.kernel.roster().removed)
                        and not any(item.device_id == node_device for item in node.kernel.roster().members),
                        "the node to verify its signed own removal")
        with pytest.raises(ak.KernelError):
            node.kernel.mesh_principal()
        assert (await sender.wake(owner, target.device_id)).status != SENT
        assert len(adapter.tokens) == 1 and len(calls) == 1


async def test_no_personal_path_derives_an_issuer_owner_from_pairing_to_wake(tmp_path, monkeypatch, plane):  # noqa: F811
    def derived(*_args, **_kwargs):
        raise AssertionError("a personal node derived an issuer owner id")

    for target, name in ((node_identity, "canonical_owner_id"), (node_identity, "accepted_issuers"),
                         (node_identity, "issuer_digest"), (token_verify, "with_owner_identity")):
        monkeypatch.setattr(target, name, derived)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/still-configured")
    answered = []

    async def runner(owner_id, local_chat, prompt):
        answered.append(owner_id)
        await model_turn(owner_id, local_chat, prompt)

    async with paired(tmp_path, monkeypatch, runner=runner) as (node, laptop, node_device, _bridge):
        mesh_id = laptop.mesh_principal().mesh_id
        owner = node.personal_owner_id()
        assert owner == node_identity.owner_id_for_mesh(mesh_id) == node_identity.bound_owner()
        assert (tmp_path / "node" / node_identity.OWNER_FILE).read_text() == owner
        chat = laptop.create_chat("ask the node")
        request = laptop.send_request(chat, "summarize my calendar", node_device, None, None)
        await until(request_state(laptop, request, "ANSWERED"), "the node to answer")
        assert answered == [owner]
        sender, target, adapter, calls = await route_for(node, laptop, plane)
        assert (await sender.wake(owner, target.device_id)).status == SENT and len(adapter.tokens) == 1
        node_mesh.install(node)
        try:
            document = await node_api.mesh_descriptor_fields(node_api.descriptor())
        finally:
            node_mesh.install(None)
        assert document["format"] == "astral.node.descriptor/v2" and "accepted_issuers" not in document
        assert document["owner_hash"] == node_identity.owner_principal_hash(mesh_id)
        assert document["mesh"]["owner_binding"] == {"state": "ready", "code": None}
        assert "idp.example" not in str(document)
        for name, value in (("WEB_SESSION_ENC_KEY", "x" * 44), ("CREDENTIAL_ENCRYPTION_KEY", "y" * 44),
                            ("AUDIT_HMAC_SECRET", "high-entropy-" + "z" * 32)):
            monkeypatch.setenv(name, value)
        for name in ("ASTRAL_ENV", "KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", "AGENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(node_identity.OWNER_ENV, mesh_id)
        assert_production_posture()
