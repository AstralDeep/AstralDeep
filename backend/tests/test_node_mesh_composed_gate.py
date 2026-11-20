"""Checks that the receiver's Kernel capability, host-call check, host-call claim and Kernel effect audit all fail closed when the native binding or its inputs are missing.
The composed gate cases themselves run in the Kernel's own tests and in the security benchmark's Kernel-engine driver."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


async def test_receiver_capability_and_effect_audit_fail_closed_without_native_binding(monkeypatch):
    from orchestrator import hitl_confirmation, node_mesh_admission
    from orchestrator.node_mesh import MeshUnavailable, NodeMesh, NodeMeshError, RequestReference
    from shared import node_identity

    monkeypatch.setattr(node_identity, "is_personal", lambda: True)
    assert await node_mesh_admission.source_bound_capability(None, "owner", "chat", "agent", "tool", {}) is None
    assert await node_mesh_admission.source_bound_capability(None, "", "chat", "agent", "tool", {}) is None
    with pytest.raises(NodeMeshError, match="request.malformed"):
        RequestReference.capture(SimpleNamespace(
            id="request", chat_id="chat", from_device="peer", to_device="local", text="work",
            continues=None, call=None, turn=1))
    mesh = object.__new__(NodeMesh)
    mesh._kernel = object()
    with pytest.raises(MeshUnavailable) as check:
        await mesh.check_host_call("request", "definition", "outside_effect")
    assert check.value.code == "host_call_check_unsupported"
    with pytest.raises(MeshUnavailable) as claim:
        await mesh.claim_host_call("request", "definition", "outside_effect")
    assert claim.value.code == "host_call_claim_unsupported"
    with pytest.raises(ValueError, match="invalid Kernel effect audit transition"):
        await hitl_confirmation.audit_kernel_effect(None, None, "invalid")
