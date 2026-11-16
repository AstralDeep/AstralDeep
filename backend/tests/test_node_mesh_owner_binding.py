"""Exercises production personal receiver startup and owner binding after verified pairing.
Only the Kernel's signed-state accessor is faked; startup, bridge selection and durable owner custody are real.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest

from orchestrator import node_mesh, node_mesh_chat
from shared import node_identity
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_chat import History, settle

MESH = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64


@pytest.fixture
async def receiver(tmp_path, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, "personal")
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh_chat, "OrchestratorCalls", lambda _orch: NS(published_tools=lambda _owner: []))
    monkeypatch.setattr(node_mesh_chat.push_wake, "gateway_for", lambda _orch: None)
    fake.Kernel.instances.clear()
    await node_mesh_chat.start_for(NS(history=History()))
    mesh = node_mesh.current()
    kernel = mesh.kernel
    state = NS(mesh_id=MESH, roster_mesh=MESH, stopped=False, local=True, proof_error=None, proof_calls=0)

    def principal():
        state.proof_calls += 1
        if state.proof_error:
            raise fake.KernelError.State(state.proof_error)
        return NS(mesh_id=state.mesh_id)

    kernel.mesh_principal = principal
    kernel.roster = lambda: NS(mesh_id=state.roster_mesh, stopped=state.stopped, removed=[], members=[
        NS(device_id=kernel.device_id, is_local=True)] if state.local else [])
    bridge = node_mesh_chat.active()
    try:
        yield NS(mesh=mesh, kernel=kernel, bridge=bridge, state=state, home=tmp_path)
    finally:
        await node_mesh_chat.stop_all()
        node_identity.reset_cache()


async def test_production_pairing_transition_binds_the_verified_mesh_without_external_auth(receiver, monkeypatch):
    monkeypatch.setattr(node_identity.NodeIdentity, "mint_token", lambda *_args, **_kwargs: pytest.fail("setup cannot mint HTTP credentials"))
    host = receiver
    await host.bridge.scan()
    assert node_identity.bound_owner() is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "setup_required", "code": "mesh.not_enrolled"}
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert node_identity.bound_owner() == MESH == host.bridge._owner
    assert (await host.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    assert (host.home / node_identity.OWNER_FILE).read_text() == MESH
    assert host.state.proof_calls >= 2


@pytest.mark.parametrize("change,code", [
    ("invalid", "node.owner_unproven"), ("wrong_mesh", "node.mesh_mismatch"),
    ("stopped", "gate.mesh_stopped"), ("removed", "gate.device_not_on_roster"),
    ("proof_denied", "gate.device_not_on_roster"), ("missing_api", "principal_unsupported"),
])
async def test_unproven_pairing_never_binds_or_routes_an_owner(receiver, change, code):
    host = receiver
    host.kernel.join(MESH)
    if change == "invalid":
        host.state.mesh_id = "not-a-mesh"
    elif change == "wrong_mesh":
        host.state.roster_mesh = OTHER
    elif change == "stopped":
        host.state.stopped = True
    elif change == "removed":
        host.state.local = False
    elif change == "proof_denied":
        host.state.proof_error = code
    else:
        host.kernel.mesh_principal = None
    await host.bridge.scan()
    assert node_identity.bound_owner() is None and host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": code}
    assert not (host.home / node_identity.OWNER_FILE).exists()


@pytest.mark.parametrize("owner", ["oidc.old.owner", OTHER])
@pytest.mark.parametrize("source", ["file", "env"])
async def test_existing_owner_is_preserved_and_reports_explicit_refusal(receiver, monkeypatch, owner, source):
    host = receiver
    if source == "file":
        node_identity.bind_owner(owner)
    else:
        monkeypatch.setenv(node_identity.OWNER_ENV, owner)
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert node_identity.bound_owner() == owner
    assert host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.owner_mismatch"}
    if source == "file":
        assert (host.home / node_identity.OWNER_FILE).read_text() == owner


async def test_current_membership_is_rechecked_after_owner_binding(receiver):
    host = receiver
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert host.bridge._owner == MESH
    host.state.stopped = True
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert node_identity.bound_owner() == MESH
    assert (await host.mesh.status())["owner_binding"]["code"] == "gate.mesh_stopped"


async def test_reopened_production_receiver_retains_its_bound_mesh_namespace(receiver, monkeypatch):
    host = receiver
    host.kernel.join(MESH)
    await host.bridge.scan()
    await node_mesh_chat.stop_all()
    await node_mesh_chat.start_for(NS(history=History()))
    mesh = node_mesh.current()
    kernel = mesh.kernel
    kernel.join(MESH)
    kernel.mesh_principal = lambda: NS(mesh_id=MESH)
    kernel.roster = lambda: NS(mesh_id=MESH, stopped=False, removed=[], members=[NS(device_id=kernel.device_id, is_local=True)])
    await node_mesh_chat.active().scan()
    assert node_identity.bound_owner() == MESH == node_mesh_chat.active()._owner


@pytest.mark.parametrize("change_name", ["RosterChanged", "KernelChange.ROSTER_CHANGED"])
async def test_roster_notification_binds_the_production_bridge_without_manual_scan(receiver, change_name):
    host = receiver
    await host.bridge.scan()
    host.kernel.join(MESH)
    host.mesh._listener.dispatch(type(change_name, (), {})())
    await settle(lambda: host.bridge._owner == MESH)
    assert node_identity.bound_owner() == MESH


async def test_concurrent_refresh_never_marks_a_competing_owner_ready(receiver, monkeypatch):
    host = receiver
    host.kernel.join(MESH)
    monkeypatch.setattr(node_identity, "_bind_owner_file", lambda _mesh: OTHER)
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.owner_mismatch"}


async def test_binding_storage_failure_is_an_explicit_refusal(receiver, monkeypatch):
    host = receiver
    host.kernel.join(MESH)

    def fail(_mesh):
        raise node_identity.NodeIdentityError("private storage failure")

    monkeypatch.setattr(node_identity, "bind_mesh_owner", fail)
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.owner_binding_unavailable"}


async def test_refused_legacy_binding_never_reassigns_existing_owner_data(receiver):
    import sqlite3

    host = receiver
    node_identity.bind_owner("oidc.old.owner")
    database = host.home / "plane.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing_data (owner TEXT, value TEXT)")
        connection.execute("INSERT INTO existing_data VALUES (?, ?)", ("oidc.old.owner", "preserved"))
    before = database.read_bytes()
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT owner, value FROM existing_data").fetchall() == [("oidc.old.owner", "preserved")]
    assert host.bridge._history.created == []


async def test_principal_change_during_binding_cannot_publish_a_new_owner(receiver):
    host = receiver
    host.kernel.join(MESH)
    proofs = iter([NS(mesh_id=MESH), NS(mesh_id=OTHER)])
    host.kernel.mesh_principal = lambda: next(proofs)
    await host.bridge.scan()
    assert node_identity.bound_owner() is None and host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"]["code"] == "node.mesh_admission_unavailable"


async def test_binding_is_personal_only_and_uses_its_exact_data_directory(receiver, monkeypatch):
    host = receiver
    host.kernel.join(MESH)
    monkeypatch.setenv(node_identity.PROFILE_ENV, "institution")
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"]["code"] == "node.personal_mesh_required"
    monkeypatch.setenv(node_identity.PROFILE_ENV, "personal")
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(host.home / "different"))
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert (await host.mesh.status())["owner_binding"]["code"] == "node.personal_mesh_required"


async def test_kernel_replacement_after_storage_cannot_mark_binding_ready(receiver, monkeypatch):
    host = receiver
    host.kernel.join(MESH)

    def replace(_mesh):
        host.mesh._kernel = object()
        return MESH

    monkeypatch.setattr(node_identity, "bind_mesh_owner", replace)
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert host.mesh._owner_binding == {"state": "refused", "code": "node.mesh_admission_unavailable"}
    host.mesh._kernel = host.kernel


async def test_request_policy_does_not_suppress_owner_setup(receiver):
    host = receiver
    host.kernel.join(MESH)
    host.bridge._policy = node_mesh_chat.NodePolicy(serve_requests=False)
    assert await host.bridge.scan() == 0
    assert node_identity.bound_owner() == MESH


async def test_unbound_receiver_shutdown_preserves_cancellation_after_roster_notification(receiver):
    receiver.mesh._listener.dispatch(type("RosterChanged", (), {})())
    async with asyncio.timeout(5):
        await node_mesh_chat.stop_all()
    assert node_mesh_chat.active() is None and node_mesh.current() is None
