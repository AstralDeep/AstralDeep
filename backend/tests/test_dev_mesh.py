"""Proves the seeded development mesh on the real kernel binding: its derived device ids are the roster ids the kernel produces, and its caller tickets and holder proofs verify on the node.
The node binds and serves it only with ASTRAL_ENV exactly development, through devtools/dev_mesh.py, tests/helpers/dev_mesh.py and orchestrator/node_mesh.py, and no orchestrator or shared module imports devtools."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS

import astral_kernel as ak
import pytest

from devtools import dev_mesh
from orchestrator import node_mesh, node_mesh_chat
from orchestrator.node_mailbox import MailboxSettings
from shared import dev_mesh_keys, node_identity
from tests import fake_astral_kernel as fake
from tests.helpers.dev_mesh import dev_node, publish_catalog, until, wait_for_caller_pause

BACKEND = Path(__file__).resolve().parents[1]
REFERENCE = BACKEND.parent / "specs/105-decentralized-personal-mesh/contracts/reference"
OUTSIDE_DEVELOPMENT = [None, "production", "Development", "DEVELOPMENT", "dev", " development", "development "]
MESH = "sha256:" + "c" * 64
ECHO = {
    "name": "echo", "description": "echoes text on the node", "effect": "mesh_read", "annotations": {},
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


def set_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", value)


def owner_file() -> Path:
    return node_identity.data_dir() / node_identity.OWNER_FILE


async def pending_on_node(node, request_id) -> bool:
    return any(request.id == request_id for request in await node.pending_requests())


def test_dev_keys_follow_the_mesh_reference_test_kit_from_public_labels(monkeypatch):
    monkeypatch.syspath_prepend(str(REFERENCE))
    from astral_mesh_ref import crypto, testkit

    for name in ("owner", "phone", "second-owner", "second-phone"):
        keys = dev_mesh_keys.device_keys(name)
        label = f"astral-dev-mesh/v1/{name}/"
        assert dev_mesh_keys.seed(label + "ops") == testkit.seed(label + "ops")
        assert dev_mesh_keys.scalar(label + "identity") == testkit.scalar(label + "identity")
        assert keys.identity_scalar == testkit.scalar(label + "identity")
        assert keys.presence_scalar == testkit.scalar(label + "presence")
        assert keys.identity_public_key == crypto.p256_public(testkit.scalar(label + "identity"))
        assert keys.presence_public_key == crypto.p256_public(testkit.scalar(label + "presence"))
        assert keys.operations_secret == testkit.seed(label + "ops")
        assert keys.wrap_secret == testkit.seed(label + "wrap")
        assert keys.restore_marker == testkit.seed(label + "restore")
        assert keys.device_id == crypto.device_id(keys.identity_public_key)
    ids = dev_mesh_keys.device_ids()
    assert tuple(ids) == dev_mesh_keys.NAMES and len(set(ids.values())) == 4
    assert ids["owner"] == dev_mesh_keys.device_keys("owner").device_id
    with pytest.raises(TypeError):
        ids["owner"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        dev_mesh_keys.device_keys("node")


async def test_dev_device_ids_are_the_roster_ids_the_real_kernel_produces(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    produced, meshes = {}, set()
    for owner_name, phone_name in (("owner", "phone"), ("second-owner", "second-phone")):
        owner = dev_mesh.DevOwner(owner_name, tmp_path / owner_name)
        phone = dev_mesh.DevDevice(phone_name, tmp_path / phone_name)
        try:
            kit = owner.create_mesh()
            owner.confirm_recovery()
            assert owner.mesh_id == kit.mesh_id == owner.kernel.mesh_principal().mesh_id
            assert kit.device_id == owner.device_id and kit.restore_marker == owner.keys.restore_marker
            await owner.start()
            await phone.start()
            approved = await owner.approve_offer(await phone.pairing_offer())
            await until(phone.enrolled, f"{phone_name} to join")
            assert approved.device_id == phone.device_id and approved.kind == "pocket"
            roster = owner.kernel.roster()
            assert roster.mesh_id == kit.mesh_id and len(roster.members) == 2
            for member in roster.members:
                keys = dev_mesh_keys.device_keys(member.label)
                produced[member.label] = member.device_id
                assert member.feeds == [ak.operations_public_key(keys.operations_secret)]
                assert member.kind == ("pocket" if member.label.endswith("phone") else "desktop")
            meshes.add(kit.mesh_id)
        finally:
            await phone.stop()
            await owner.stop()
    assert produced == dict(dev_mesh_keys.device_ids()) and len(meshes) == 2


async def test_a_development_node_binds_and_serves_the_dev_mesh_until_it_leaves_development(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node, owner = mesh.node, mesh.owner
        assert (await node.status())["owner_binding"] == {"state": "ready", "code": None}
        bound = node_identity.bound_owner()
        assert bound == node.personal_owner_id() and bound.endswith(owner.mesh_id.removeprefix("sha256:"))
        stored = owner_file().read_bytes()
        _, request_id = owner.send_request(mesh.node_device, "what is on my calendar?")
        await until(lambda: pending_on_node(node, request_id), "the request to reach the node")
        request = node_mesh.RequestReference.capture(await node.request(request_id))
        context = await node.personal_request_context(request, bound, node.kernel)
        assert (context.mesh_id, context.member_id) == (owner.mesh_id, mesh.node_device)
        assert request.from_device == owner.device_id == dev_mesh_keys.device_ids()["owner"]

        set_env(monkeypatch, "production")
        assert await node.refresh_personal_owner() is None
        assert node.personal_owner_id() is None
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
        with pytest.raises(node_mesh.NodeMeshError) as refused:
            await node.personal_request_context(request, bound, node.kernel)
        assert refused.value.code == "node.development_mesh"
        assert owner_file().read_bytes() == stored
        assert owner.kernel.request(request_id).state == ak.RequestState.PENDING


@pytest.mark.parametrize("value", [None, "production"])
async def test_outside_development_the_node_refuses_the_dev_mesh_and_writes_nothing(tmp_path, monkeypatch, value):
    async with dev_node(tmp_path, monkeypatch, bind=False) as mesh:
        node, owner = mesh.node, mesh.owner
        _, request_id = owner.send_request(mesh.node_device, "summarize my notes")
        await until(lambda: pending_on_node(node, request_id), "the request to reach the node")
        request = node_mesh.RequestReference.capture(await node.request(request_id))

        set_env(monkeypatch, value)
        bridge = node_mesh_chat.MeshChatBridge(
            node, owner_id=node.personal_owner_id, owner_setup=node.refresh_personal_owner,
            history=NS(), state_path=tmp_path / "chats.json",
        )
        assert await bridge.scan() == 0
        assert await node.refresh_personal_owner() is None
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
        assert node_identity.bound_owner() is None and not owner_file().exists()
        assert not (tmp_path / "chats.json").exists()
        for serve in (node.personal_request_context, node.personal_authority_context):
            with pytest.raises(node_mesh.NodeMeshError) as refused:
                await serve(request, owner.mesh_id, node.kernel)
            assert refused.value.code == "node.development_mesh"
        assert owner.kernel.request(request_id).state == ak.RequestState.PENDING
        with pytest.raises(dev_mesh.DevMeshRefused):
            owner.send_request(mesh.node_device, "try again")


async def test_dev_owner_mints_caller_tickets_and_signs_proofs_the_node_kernel_verifies(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        owner, node_kernel = mesh.owner, mesh.node.kernel
        await publish_catalog(mesh, [ECHO])
        holder = dev_mesh.CallerHolder.generate()
        assert holder.holder_text.startswith("astral-caller-holder:v1:")
        ticket = owner.mint_caller_ticket(mesh.node_device, holder, ["echo"], label="Dev script")
        assert (ticket.node, ticket.tools, ticket.label) == (mesh.node_device, ["echo"], "Dev script")
        assert ticket.expires_ms - ticket.issued_ms == dev_mesh.DEFAULT_LIFETIME_S * 1000
        assert [listed.id for listed in owner.kernel.caller_tickets()] == [ticket.id]
        await wait_for_caller_pause(mesh)
        body = b'{"jsonrpc":"2.0","method":"tools/call"}'

        def call(signer, tool="echo"):
            headers = owner.caller_headers(signer, ticket, "POST", "/mcp", body)
            assert set(headers) == {"Authorization", "Astral-Caller-Proof"}
            return ak.CallerRequest(
                authorization=headers["Authorization"], proof=headers["Astral-Caller-Proof"], method="POST",
                target="/mcp", body_sha256="sha256:" + hashlib.sha256(body).hexdigest(), tool=tool,
            )

        verified = node_kernel.verify_caller_request(call(holder))
        assert (verified.id, verified.mesh, verified.device, verified.tool, verified.label) == (
            ticket.id, owner.mesh_id, owner.device_id, "echo", "Dev script",
        )
        for signer, tool, code in (
            (dev_mesh.CallerHolder.generate(), "echo", "caller.proof_bad_signature"),
            (holder, "other", "caller.tool_not_covered"),
        ):
            with pytest.raises(ak.KernelError) as refused:
                node_kernel.verify_caller_request(call(signer, tool))
            assert refused.value.code == code
        proof = owner.sign_holder_proof(holder, ticket.id, "GET", "/mcp", ms=1, nonce=b"\x00" * 16)
        assert proof == owner.sign_holder_proof(holder, ticket.id, "GET", "/mcp", ms=1, nonce=b"\x00" * 16)


@pytest.mark.parametrize("value", OUTSIDE_DEVELOPMENT)
def test_devtools_refuse_to_start_outside_development(tmp_path, monkeypatch, value):
    set_env(monkeypatch, value)
    for build in (
        lambda: dev_mesh.DevOwner("owner", tmp_path / "owner"),
        lambda: dev_mesh.DevDevice("phone", tmp_path / "phone"),
        dev_mesh.CallerHolder.generate,
        dev_mesh.require_development,
    ):
        with pytest.raises(dev_mesh.DevMeshRefused):
            build()
    assert list(tmp_path.iterdir()) == []


async def test_a_dev_device_refuses_every_operation_once_it_leaves_development(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    holder = dev_mesh.CallerHolder.generate()
    set_env(monkeypatch, "production")
    try:
        for operation in (
            owner.create_mesh, owner.confirm_recovery, owner.start, owner.pairing_offer,
            lambda: owner.approve_offer("payload"), lambda: owner.send_request(MESH, "hi"),
            lambda: owner.mint_caller_ticket(MESH, holder, ["echo"]),
            lambda: owner.sign_holder_proof(holder, "ticket", "POST", "/mcp"),
        ):
            with pytest.raises(dev_mesh.DevMeshRefused):
                result = operation()
                if hasattr(result, "__await__"):
                    await result
    finally:
        await owner.stop()


async def test_caller_headers_take_only_the_ticket_text_form_and_waits_give_up(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    holder = dev_mesh.CallerHolder.generate()
    try:
        for text in ("AstralCaller e30", "astral-caller-key:v1:AAAA", "astral-caller-ticket:v2:e30"):
            with pytest.raises(ValueError):
                owner.caller_headers(holder, NS(id="ticket", text=text), "POST", "/mcp")
        with pytest.raises(AssertionError, match="never"):
            await until(lambda: False, "never", seconds=0.2)
    finally:
        await owner.stop()


def test_dev_devices_take_only_their_published_names(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    for build in (
        lambda: dev_mesh.DevOwner("phone", tmp_path / "x"),
        lambda: dev_mesh.DevOwner("node", tmp_path / "x"),
        lambda: dev_mesh.DevDevice("intruder", tmp_path / "x"),
    ):
        with pytest.raises(ValueError):
            build()
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
async def fake_node(tmp_path, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    node_identity.reset_cache()
    mesh = node_mesh.NodeMesh(
        data_dir=tmp_path, settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)), binding=fake,
    )
    await mesh.start()
    kernel = mesh.kernel
    kernel.join(MESH)
    kernel.mesh_principal = lambda: NS(mesh_id=MESH)
    state = NS(members=["sha256:" + "1" * 64], removed=[])
    kernel.roster = lambda: NS(
        mesh_id=MESH, stopped=False,
        members=[NS(device_id=kernel.device_id, is_local=True), *(NS(device_id=d, is_local=False) for d in state.members)],
        removed=[NS(device_id=d, cutoffs=[]) for d in state.removed],
    )
    request = kernel.add_request("request-1", "chat-1", author=state.members[0], state="CLAIMED")
    try:
        yield NS(mesh=mesh, kernel=kernel, state=state, request=node_mesh.RequestReference.capture(request))
    finally:
        await mesh.stop()
        node_identity.reset_cache()


@pytest.mark.parametrize("value", OUTSIDE_DEVELOPMENT)
@pytest.mark.parametrize("where", ["members", "removed"])
@pytest.mark.parametrize("name", ["owner", "phone", "second-owner", "second-phone"])
async def test_a_dev_device_anywhere_on_the_roster_is_refused_outside_development(fake_node, monkeypatch, value, where, name):
    set_env(monkeypatch, value)
    getattr(fake_node.state, where).append(dev_mesh_keys.device_ids()[name])
    assert await fake_node.mesh.refresh_personal_owner() is None
    assert (await fake_node.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
    assert node_identity.bound_owner() is None and not owner_file().exists()
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await fake_node.mesh.personal_authority_context(fake_node.request, MESH, fake_node.kernel)
    assert refused.value.code == "node.development_mesh"


@pytest.mark.parametrize("where", ["members", "removed"])
async def test_development_accepts_a_roster_that_lists_dev_devices(fake_node, monkeypatch, where):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    getattr(fake_node.state, where).append(dev_mesh_keys.device_ids()["phone"])
    assert await fake_node.mesh.refresh_personal_owner() == MESH == node_identity.bound_owner()
    context = await fake_node.mesh.personal_authority_context(fake_node.request, MESH, fake_node.kernel)
    assert context.mesh_id == MESH


@pytest.mark.parametrize("value", [None, "production", "development"])
async def test_a_roster_without_dev_devices_binds_in_every_environment(fake_node, monkeypatch, value):
    set_env(monkeypatch, value)
    fake_node.state.removed.append("sha256:" + "2" * 64)
    assert await fake_node.mesh.refresh_personal_owner() == MESH
    assert (await fake_node.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    context = await fake_node.mesh.personal_authority_context(fake_node.request, MESH, fake_node.kernel)
    assert context.member_id == fake_node.kernel.device_id


def devtools_imports(root: Path) -> list:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if callee in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                    names = [node.args[0].value]
            if any(name == "devtools" or name.startswith("devtools.") for name in names):
                found.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return found


def test_no_orchestrator_or_shared_module_imports_devtools():
    assert devtools_imports(BACKEND / "orchestrator") == []
    assert devtools_imports(BACKEND / "shared") == []


def test_the_import_scan_finds_every_form_of_a_devtools_import(tmp_path):
    (tmp_path / "pkg").mkdir()
    sources = {
        "a.py": "import devtools.dev_mesh\n",
        "b.py": "from devtools import dev_mesh\n",
        "c.py": "import importlib\nimportlib.import_module('devtools.dev_mesh')\n",
        "d.py": "__import__('devtools')\n",
        "pkg/e.py": "def f():\n    import os, devtools\n",
        "quiet.py": "BROWSER = '/devtools/browser/x'\nfrom .devtools import local\nimport devtoolsx\n",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source)
    assert devtools_imports(tmp_path) == ["a.py:1", "b.py:1", "c.py:2", "d.py:1", "pkg/e.py:2"]
