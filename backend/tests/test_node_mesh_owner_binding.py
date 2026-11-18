"""Exercises production personal receiver startup and owner binding after verified pairing: the owner is the verified mesh id stored as mesh.<hex>, a legacy issuer owner is reported and kept, and existing rows read back unchanged.
Only the Kernel's signed-state accessor is faked; startup, bridge selection, durable owner custody and the Plane history rows are real.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from orchestrator import node_mesh, node_mesh_chat
from shared import node_identity
from tests import fake_astral_kernel as fake
from tests.helpers.owner_file import seed_owner
from tests.test_node_mesh_chat import History, settle

MESH = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64
STORED = "mesh." + "c" * 64
OTHER_STORED = "mesh." + "d" * 64
LEGACY = "oidc.0123456789abcdef.owner-1"
BACKEND = Path(__file__).resolve().parents[1]


async def start_receiver(home, history):
    await node_mesh_chat.start_for(NS(history=history))
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
    return NS(mesh=mesh, kernel=kernel, bridge=node_mesh_chat.active(), state=state, home=home)


@pytest.fixture
def personal_home(tmp_path, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, "personal")
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh_chat, "OrchestratorCalls", lambda _orch: NS(published_tools=lambda _owner: []))
    monkeypatch.setattr(node_mesh_chat.push_wake, "gateway_for", lambda _orch: None)
    fake.Kernel.instances.clear()
    yield tmp_path
    node_identity.reset_cache()


@pytest.fixture
async def receiver(personal_home):
    host = await start_receiver(personal_home, History())
    try:
        yield host
    finally:
        await node_mesh_chat.stop_all()


async def test_production_pairing_transition_binds_the_verified_mesh_without_external_auth(receiver, monkeypatch):
    monkeypatch.setattr(node_identity.NodeIdentity, "mint_token", lambda *_args, **_kwargs: pytest.fail("setup cannot mint HTTP credentials"))
    host = receiver
    await host.bridge.scan()
    assert node_identity.bound_owner() is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "setup_required", "code": "mesh.not_enrolled"}
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert node_identity.bound_owner() == STORED == host.bridge._owner == host.mesh.personal_owner_id()
    assert (await host.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    assert (host.home / node_identity.OWNER_FILE).read_text() == STORED
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


@pytest.mark.parametrize("owner,bound,code", [
    (OTHER, OTHER_STORED, "node.owner_mismatch"),
    (OTHER_STORED, OTHER_STORED, "node.owner_mismatch"),
])
@pytest.mark.parametrize("source", ["file", "env"])
async def test_existing_owner_is_preserved_and_reports_explicit_refusal(receiver, monkeypatch, owner, bound, code, source):
    host = receiver
    if source == "file":
        stored = seed_owner(owner)
    else:
        monkeypatch.setenv(node_identity.OWNER_ENV, owner)
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert node_identity.bound_owner() == bound
    assert host.bridge._owner is None and host.mesh.personal_owner_id() is None
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": code}
    if source == "file":
        assert (host.home / node_identity.OWNER_FILE).read_bytes() == stored
    else:
        assert not (host.home / node_identity.OWNER_FILE).exists()


@pytest.mark.parametrize("source", ["file", "env"])
async def test_a_legacy_owner_is_retired_before_the_node_joins_a_mesh(receiver, monkeypatch, source):
    host = receiver
    if source == "file":
        stored = seed_owner(LEGACY)
    else:
        monkeypatch.setenv(node_identity.OWNER_ENV, LEGACY)
    await host.bridge.scan()
    assert (await host.mesh.status())["owner_binding"] == {"state": "setup_required", "code": "mesh.not_enrolled"}
    assert host.bridge._owner is None
    if source == "file":
        assert not (host.home / node_identity.OWNER_FILE).exists()
        (kept,) = host.home.glob(node_identity.OWNER_FILE + ".legacy-*")
        assert kept.read_bytes() == stored


@pytest.mark.parametrize("source", ["file", "env"])
async def test_pairing_binds_the_mesh_owner_after_a_legacy_owner_is_retired(receiver, monkeypatch, source):
    host = receiver
    if source == "file":
        seed_owner(LEGACY)
    else:
        monkeypatch.setenv(node_identity.OWNER_ENV, LEGACY)
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert (await host.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    assert (host.home / node_identity.OWNER_FILE).read_text() == node_identity.owner_id_for_mesh(MESH)
    assert host.mesh.personal_owner_id() == node_identity.owner_id_for_mesh(MESH)


@pytest.mark.parametrize("stored,code", [(OTHER_STORED, "node.owner_mismatch")])
async def test_a_configured_mesh_owner_cannot_hide_a_different_owner_file_before_the_node_joins(receiver, monkeypatch, stored, code):
    host = receiver
    before = seed_owner(stored)
    monkeypatch.setenv(node_identity.OWNER_ENV, MESH)
    await host.bridge.scan()
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": code}
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": code}
    assert host.bridge._owner is None and (host.home / node_identity.OWNER_FILE).read_bytes() == before


async def test_an_unreadable_owner_binding_is_reported_before_the_node_joins_a_mesh(receiver):
    host = receiver
    host.home.mkdir(parents=True, exist_ok=True)
    (host.home / node_identity.OWNER_FILE).write_text("")
    await host.bridge.scan()
    assert (await host.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.owner_binding_unavailable"}
    assert host.bridge._owner is None and (host.home / node_identity.OWNER_FILE).read_text() == ""


async def test_a_wire_form_owner_file_binds_as_the_same_mesh_owner_and_is_never_rewritten(receiver):
    host = receiver
    stored = seed_owner(MESH)
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert host.bridge._owner == STORED == node_identity.bound_owner()
    assert (await host.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    assert (host.home / node_identity.OWNER_FILE).read_bytes() == stored


async def test_current_membership_is_rechecked_after_owner_binding(receiver):
    host = receiver
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert host.bridge._owner == STORED
    host.state.stopped = True
    await host.bridge.scan()
    assert host.bridge._owner is None
    assert node_identity.bound_owner() == STORED
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
    assert node_identity.bound_owner() == STORED == node_mesh_chat.active()._owner


@pytest.mark.parametrize("change_name", ["RosterChanged", "KernelChange.ROSTER_CHANGED"])
async def test_roster_notification_binds_the_production_bridge_without_manual_scan(receiver, change_name):
    host = receiver
    await host.bridge.scan()
    host.kernel.join(MESH)
    host.mesh._listener.dispatch(type(change_name, (), {})())
    await settle(lambda: host.bridge._owner == STORED)
    assert node_identity.bound_owner() == STORED


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


async def test_retiring_a_legacy_binding_never_rewrites_existing_owner_data(receiver):
    import sqlite3

    host = receiver
    stored = seed_owner(LEGACY)
    database = host.home / "plane.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing_data (owner TEXT, value TEXT)")
        connection.execute("INSERT INTO existing_data VALUES (?, ?)", (LEGACY, "preserved"))
    before = database.read_bytes()
    host.kernel.join(MESH)
    await host.bridge.scan()
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT owner, value FROM existing_data").fetchall() == [(LEGACY, "preserved")]
    (kept,) = host.home.glob(node_identity.OWNER_FILE + ".legacy-*")
    assert kept.read_bytes() == stored
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
        return STORED

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
    assert node_identity.bound_owner() == STORED


async def test_unbound_receiver_shutdown_preserves_cancellation_after_roster_notification(receiver):
    receiver.mesh._listener.dispatch(type("RosterChanged", (), {})())
    async with asyncio.timeout(5):
        await node_mesh_chat.stop_all()
    assert node_mesh_chat.active() is None and node_mesh.current() is None


@pytest.fixture(scope="module")
def plane():
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("node_owner_rows") as runtime:
        yield runtime


def plane_history(plane):
    from tests.helpers.voice_plane_runtime import history_manager

    return history_manager(plane)


def seed_rows(history, owner, text):
    chat = history.create_chat(user_id=owner)
    history.add_message(chat, "user", text, owner)
    history.add_message(chat, "assistant", "answer to " + text, owner)
    return chat


def snapshot(history, owner):
    chats = history.get_recent_chats(limit=50, user_id=owner, include_archived=True)
    return [(chat["id"], chat["title"], history.get_chat(chat["id"], owner)) for chat in chats]


def use_mesh(host, mesh_id):
    host.state.mesh_id = host.state.roster_mesh = mesh_id
    host.kernel.join(mesh_id)


async def test_a_legacy_owner_is_retired_and_its_plane_rows_are_left_untouched(personal_home, plane):
    history = plane_history(plane)
    stored = seed_owner(LEGACY)
    chat = seed_rows(history, LEGACY, "a question from before the cut")
    before = snapshot(history, LEGACY)
    host = await start_receiver(personal_home, history)
    try:
        await host.bridge.scan()
        assert not (personal_home / node_identity.OWNER_FILE).exists()
        use_mesh(host, MESH)
        await host.bridge.scan()
        assert host.mesh.personal_owner_id() == STORED
    finally:
        await node_mesh_chat.stop_all()
    (kept,) = personal_home.glob(node_identity.OWNER_FILE + ".legacy-*")
    assert kept.read_bytes() == stored
    assert snapshot(history, LEGACY) == before and before[0][0] == chat
    assert snapshot(history, STORED) == []


async def test_a_mesh_owner_nodes_existing_rows_read_back_unchanged_after_a_restart(personal_home, plane):
    history = plane_history(plane)
    mesh, owner = "sha256:" + "e" * 64, "mesh." + "e" * 64
    host = await start_receiver(personal_home, history)
    try:
        use_mesh(host, mesh)
        await host.bridge.scan()
        assert host.bridge._owner == owner
        chat = seed_rows(history, owner, "a question after the cut")
        before = snapshot(history, owner)
    finally:
        await node_mesh_chat.stop_all()
    stored = (personal_home / node_identity.OWNER_FILE).read_bytes()
    assert stored == owner.encode()
    host = await start_receiver(personal_home, history)
    try:
        use_mesh(host, mesh)
        await host.bridge.scan()
        assert host.bridge._owner == owner == host.mesh.personal_owner_id()
        assert snapshot(history, host.bridge._owner) == before and before[0][0] == chat
    finally:
        await node_mesh_chat.stop_all()
    assert (personal_home / node_identity.OWNER_FILE).read_bytes() == stored


async def test_rows_an_earlier_build_keyed_by_the_wire_form_are_kept_and_not_reassigned(personal_home, plane):
    history = plane_history(plane)
    wire, owner = "sha256:" + "9" * 64, "mesh." + "9" * 64
    stored = seed_owner(wire)
    chat = seed_rows(history, wire, "a question from an earlier build")
    before = snapshot(history, wire)
    host = await start_receiver(personal_home, history)
    try:
        use_mesh(host, wire)
        await host.bridge.scan()
        assert host.bridge._owner == owner
    finally:
        await node_mesh_chat.stop_all()
    assert (personal_home / node_identity.OWNER_FILE).read_bytes() == stored
    assert snapshot(history, wire) == before and before[0][0] == chat
    assert snapshot(history, owner) == []


PERSONAL_MODULES = (
    "orchestrator/node_api.py",
    "orchestrator/node_mesh.py",
    "orchestrator/node_mesh_chat.py",
    "orchestrator/node_mesh_admission.py",
    "orchestrator/node_mesh_calls.py",
    "orchestrator/node_mesh_keys.py",
    "orchestrator/node_mailbox.py",
    "orchestrator/mesh_push_client.py",
    "orchestrator/push_wake.py",
    "shared/dev_mesh_keys.py",
    "devtools/dev_mesh.py",
)
PERSONAL_FUNCTIONS = {
    "shared/node_identity.py": (
        "owner_id_for_mesh", "mesh_id_for_owner", "_stored_owner", "_owner_file", "bound_owner", "bound_mesh_owner",
        "_bind_owner_file", "_require_owner", "bind_mesh_owner", "enforce_owner",
    ),
    "orchestrator/boot_posture.py": ("_personal_node_problems",),
    "shared/a2a_security.py": ("_validate_node_token",),
}
ISSUER_NAMES = frozenset({
    "canonical_owner_id", "with_owner_identity", "accepted_issuers", "issuer_digest", "normalize_issuer",
    "bind_owner", "ISSUERS_ENV",
})


def issuer_uses(tree):
    found = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Attribute):
            name = item.attr
        elif isinstance(item, ast.Name):
            name = item.id
        elif isinstance(item, ast.alias):
            name = item.name.rsplit(".", 1)[-1]
        elif isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.startswith("oidc."):
            name = item.value
        else:
            continue
        if name in ISSUER_NAMES or name.startswith("oidc."):
            found.append(name)
    return found


@pytest.mark.parametrize("module", PERSONAL_MODULES)
def test_no_personal_module_derives_or_writes_an_issuer_owner(module):
    assert issuer_uses(ast.parse((BACKEND / module).read_text())) == []


@pytest.mark.parametrize("module", sorted(PERSONAL_FUNCTIONS))
def test_no_personal_function_in_a_shared_module_derives_or_writes_an_issuer_owner(module):
    tree = ast.parse((BACKEND / module).read_text())
    functions = {item.name: item for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in PERSONAL_FUNCTIONS[module]:
        assert name in functions, f"{module} lost {name}"
        assert issuer_uses(functions[name]) == [], f"{module}:{name}"


def test_the_static_check_finds_each_issuer_derivation_form():
    source = '''
from shared.node_identity import canonical_owner_id
def a(): return node_identity.with_owner_identity(1, 2)
def b(): return accepted_issuers()
def c(): return f"oidc.{x}.{y}"
def d(): node_identity.bind_owner("x")
'''
    assert sorted(issuer_uses(ast.parse(source))) == sorted([
        "canonical_owner_id", "with_owner_identity", "accepted_issuers", "oidc.", "bind_owner",
    ])
