"""Composes a real enrolled native mesh route with the node publisher client and encrypted Plane gateway.
Only the APNs/HTTPS network transports are recorded; principal, signed route, removal, rate admission and owner audit remain real."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import astral_kernel as ak
import pytest

from audit.repository import AuditRepository
from orchestrator.mesh_push_client import RemoteWakeGateway
from orchestrator.mesh_push_gateway import MeshPushPublisher
from orchestrator.mesh_push_registry import MeshPushRegistry
from orchestrator.push_wake import SENT, WakeLimiter
from shared.node_identity import canonical_owner_id
from tests.test_mesh_push_api import Apns
from tests.test_mesh_push_registry import KEY, plane  # noqa: F401
from tests.test_node_mesh_kernel_integration import ISS, OWNER, paired, until


async def route_for(node, laptop, runtime):
    status = await laptop.mesh_status()
    registry = MeshPushRegistry(plane_runtime=runtime, audit_repository=AuditRepository(plane_runtime=runtime),
                                encryption_key=lambda: KEY)
    registered = registry.register(OWNER, mesh_id=status.mesh_id, device_id=status.device_id,
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


async def test_actual_enrollment_signed_route_and_gateway_send_only_one_empty_wake(tmp_path, monkeypatch, plane):  # noqa: F811
    async with paired(tmp_path, monkeypatch) as (node, laptop, _, _bridge):
        sender, target, adapter, calls = await route_for(node, laptop, plane)

        async def no_iam(*args, **kwargs):
            pytest.fail("mesh wake requested per-call IAM")

        monkeypatch.setattr("shared.jwks_cache.get_jwks", no_iam)
        assert (await sender.wake(OWNER, target.device_id)).status == SENT
        assert adapter.tokens == ["private-physical-phone-token"]
        assert calls[0][0:2] == ("POST", "https://publisher.example/api/push/mesh/wake")
        assert calls[0][2]["json_body"] == {"mesh_id": target.mesh_id, "device_id": target.device_id}
        assert (await sender.wake(canonical_owner_id(ISS, "different-owner"), target.device_id)).status != SENT
        assert len(calls) == 1
        laptop.set_wake_route(target.mesh_id, target.device_id, None)
        await until(lambda: not any(item.device_id == target.device_id for item in node.kernel.wake_routes()),
                    "the phone wake withdrawal to reach the node")
        assert (await sender.wake(OWNER, target.device_id)).status != SENT
        assert len(calls) == 1 and adapter.tokens == ["private-physical-phone-token"]


@pytest.mark.parametrize("change", ["stop", "remove"])
async def test_actual_native_stop_or_signed_own_removal_blocks_wake_before_network(tmp_path, monkeypatch, plane, change):  # noqa: F811
    async with paired(tmp_path, monkeypatch) as (node, laptop, node_device, _bridge):
        sender, target, adapter, calls = await route_for(node, laptop, plane)
        assert target.mesh_id == node.kernel.mesh_principal().mesh_id
        if change == "stop":
            node.kernel.raise_stop()
        else:
            laptop.remove_device(node_device)
            await until(lambda: any(item.device_id == node_device for item in node.kernel.roster().removed)
                        and not any(item.device_id == node_device for item in node.kernel.roster().members),
                        "the node to verify its signed own removal")
        with pytest.raises(ak.KernelError):
            node.kernel.mesh_principal()
        assert (await sender.wake(OWNER, target.device_id)).status != SENT
        assert adapter.tokens == [] and calls == []
