"""Proves the personal node's loopback pairing offer: only a loopback caller with no forwarding header and the node's current pairing secret gets the kernel's offer, each served offer replaces the secret, and enrollment deletes it.
Requests go through the app Orchestrator.build_http_app serves into orchestrator/node_api.py and node_mesh.py, on the fake kernel for the edges and on the real kernel with a seeded development owner for the end to end run, kept in this one file because those devices share fixed identities."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
from types import SimpleNamespace as NS

import httpx
import pytest

from devtools import dev_mesh
from orchestrator import node_mesh
from orchestrator.node_mailbox import MailboxSettings
from shared import node_identity as ni
from tests import fake_astral_kernel as fake
from tests.helpers import orchestrator_app
from tests.helpers.owner_file import seed_owner
from tests.helpers.dev_mesh import node_settings, until
from tests.test_dev_mesh import CLEAN, stored_node

OFFER = "/api/node/mesh/offer"
HEADER = "Astral-Pairing-Secret"
MESH = "sha256:" + "e" * 64
LEGACY = "oidc.0123456789abcdef.owner-1"


def _secret_path(home):
    return home / node_mesh.PAIRING_SECRET_FILE


def _secret(home) -> str:
    return _secret_path(home).read_text(encoding="utf-8")


def _client(app, host="127.0.0.1"):
    transport = httpx.ASGITransport(app=app, client=(host, 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8001")


async def _post(app, secret=None, *, host="127.0.0.1", headers=None):
    sent = dict(headers or {})
    if secret is not None:
        sent[HEADER] = secret
    async with _client(app, host) as http:
        return await http.post(OFFER, headers=sent)


@pytest.fixture
async def node(tmp_path, monkeypatch):
    home = tmp_path / "node"
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(home))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    fake.Kernel.instances.clear()
    mesh = node_mesh.NodeMesh(data_dir=home, binding=fake,
                              settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
    await mesh.start()
    node_mesh.install(mesh)
    app = orchestrator_app.build(monkeypatch, orchestrator_app.host(), surfaces=())
    yield NS(mesh=mesh, kernel=mesh.kernel, app=app, home=home)
    node_mesh.install(None)
    await mesh.stop()
    ni.reset_cache()


async def test_the_node_writes_a_private_secret_while_not_enrolled(node):
    path = _secret_path(node.home)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(node.home.stat().st_mode) == 0o700
    secret = _secret(node.home)
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32 and "=" not in secret


async def test_loopback_and_the_right_secret_return_the_offer_and_replace_the_secret(node):
    first = _secret(node.home)
    response = await _post(node.app, first)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"state": "offer", "code": "123-456-789", "payload": "payload-1", "expires_ms": 1234}
    second = _secret(node.home)
    assert second != first and stat.S_IMODE(_secret_path(node.home).stat().st_mode) == 0o600
    replayed = await _post(node.app, first)
    assert (replayed.status_code, replayed.json()["detail"]) == (401, "node.pairing_secret_required")
    assert (await _post(node.app, second)).json()["payload"] == "payload-2"
    assert node.kernel.offers == 2


@pytest.mark.parametrize("host", ["::1", "::ffff:127.0.0.1"])
async def test_every_loopback_form_is_accepted(node, host):
    assert (await _post(node.app, _secret(node.home), host=host)).status_code == 200


@pytest.mark.parametrize("host,headers", [
    ("192.0.2.7", {}),
    ("testclient", {}),
    ("127.0.0.1", {"Forwarded": "for=192.0.2.7"}),
    ("127.0.0.1", {"X-Forwarded-For": "192.0.2.7"}),
])
async def test_a_remote_or_forwarded_caller_is_refused(node, host, headers):
    secret = _secret(node.home)
    response = await _post(node.app, secret, host=host, headers=headers)
    assert (response.status_code, response.json()["detail"]) == (403, "node.loopback_required")
    assert response.headers["cache-control"] == "no-store"
    assert _secret(node.home) == secret and node.kernel.offers == 0


@pytest.mark.parametrize("presented", [None, "", "wrong", "two"])
async def test_a_missing_or_wrong_secret_is_refused(node, presented):
    secret = _secret(node.home)
    if presented == "two":
        async with _client(node.app) as http:
            response = await http.post(OFFER, headers=[(HEADER, secret), (HEADER, secret)])
    else:
        response = await _post(node.app, presented)
    assert (response.status_code, response.json()["detail"]) == (401, "node.pairing_secret_required")
    assert _secret(node.home) == secret and node.kernel.offers == 0


async def test_a_secret_file_anyone_else_can_read_is_not_trusted(node):
    path = _secret_path(node.home)
    path.chmod(0o644)
    response = await _post(node.app, _secret(node.home))
    assert response.status_code == 401 and node.kernel.offers == 0


async def test_a_deleted_secret_is_written_again_while_not_enrolled(node):
    old = _secret(node.home)
    _secret_path(node.home).unlink()
    assert (await _post(node.app, old)).status_code == 401
    fresh = _secret(node.home)
    assert fresh != old and (await _post(node.app, fresh)).status_code == 200


@pytest.mark.parametrize("source", ["file", "env"])
async def test_a_node_with_a_legacy_owner_retires_it_and_accepts_the_offer(node, monkeypatch, source):
    if source == "file":
        before = seed_owner(LEGACY)
    else:
        monkeypatch.setenv(ni.OWNER_ENV, LEGACY)
    secret = _secret(node.home)
    response = await _post(node.app, secret)
    assert response.status_code == 200
    assert ni.bound_owner() is None
    if source == "file":
        assert not (node.home / ni.OWNER_FILE).exists()
        (kept,) = node.home.glob(ni.OWNER_FILE + ".legacy-*")
        assert kept.read_bytes() == before


@pytest.mark.parametrize("configured", [MESH, "mesh." + "e" * 64])
@pytest.mark.parametrize("stored,code", [("mesh." + "f" * 64, "node.owner_mismatch")])
async def test_a_configured_mesh_owner_cannot_hide_a_different_owner_file_from_the_offer(node, monkeypatch, configured, stored, code):
    before = seed_owner(stored)
    monkeypatch.setenv(ni.OWNER_ENV, configured)
    secret = _secret(node.home)
    response = await _post(node.app, secret)
    assert (response.status_code, response.json()["detail"]) == (409, code)
    assert _secret(node.home) == secret and node.kernel.offers == 0
    assert (node.home / ni.OWNER_FILE).read_bytes() == before


async def test_an_unreadable_owner_binding_is_unavailable(node):
    (node.home / ni.OWNER_FILE).write_text("")
    response = await _post(node.app, _secret(node.home))
    assert (response.status_code, response.json()["detail"]) == (503, "node.owner_binding_unavailable")


async def test_a_loose_data_directory_is_closed_when_the_secret_is_written(node):
    node.home.chmod(0o755)
    _secret_path(node.home).unlink()
    assert (await _post(node.app, "stale")).status_code == 401
    assert stat.S_IMODE(node.home.stat().st_mode) == 0o700
    assert stat.S_IMODE(_secret_path(node.home).stat().st_mode) == 0o600


@pytest.mark.parametrize("member,code", [("development", node_mesh.DEVELOPMENT_MESH), ("clean", "mesh.not_enrolled")])
async def test_reopening_an_enrolled_store_deletes_a_leftover_secret(tmp_path, monkeypatch, member, code):
    from shared import dev_mesh_keys

    device = dev_mesh_keys.device_ids()["owner"] if member == "development" else CLEAN
    record = stored_node(tmp_path, monkeypatch, "production", device, supervised=False)
    leftover = _secret_path(tmp_path)
    leftover.write_text("left behind")
    leftover.chmod(0o600)
    await record.mesh.start()
    try:
        assert not leftover.exists()
        assert (await record.mesh.status())["owner_binding"]["code"] == code
    finally:
        await record.mesh.stop()
        ni.reset_cache()


@pytest.mark.parametrize("owner", [MESH, "mesh." + "e" * 64])
async def test_a_mesh_owner_does_not_block_the_offer(node, owner):
    seed_owner(owner)
    assert (await _post(node.app, _secret(node.home))).status_code == 200


@pytest.mark.parametrize("value,mesh", [
    (MESH, True), ("mesh." + "0" * 64, True), (LEGACY, False), ("sha256:" + "E" * 64, False),
    ("mesh." + "0" * 63, False), ("mesh-" + "0" * 64, False), (None, False), (7, False),
])
def test_one_predicate_tells_a_mesh_owner_from_a_legacy_one(value, mesh):
    assert ni.is_mesh_owner(value) is mesh


async def test_offers_are_limited_to_five_a_minute(node, monkeypatch):
    clock = NS(now=1000.0)
    monkeypatch.setattr(node_mesh, "time", NS(monotonic=lambda: clock.now))
    for _ in range(5):
        assert (await _post(node.app, _secret(node.home))).status_code == 200
    secret = _secret(node.home)
    limited = await _post(node.app, secret)
    assert (limited.status_code, limited.json()["detail"]) == (429, "node.pairing_rate_limited")
    assert _secret(node.home) == secret and node.kernel.offers == 5
    clock.now += 61
    assert (await _post(node.app, secret)).status_code == 200


async def test_concurrent_requests_with_one_secret_serve_one_offer(node):
    secret = _secret(node.home)
    responses = await asyncio.gather(*(_post(node.app, secret) for _ in range(4)))
    assert sorted(response.status_code for response in responses) == [200, 401, 401, 401]
    assert node.kernel.offers == 1


async def test_a_kernel_refusal_is_reported_and_keeps_the_secret(node):
    async def refuse():
        raise fake.KernelError.State("pairing.busy")

    node.kernel.create_pairing_offer = refuse
    secret = _secret(node.home)
    response = await _post(node.app, secret)
    assert (response.status_code, response.json()["detail"]) == (409, "pairing.busy")
    assert _secret(node.home) == secret


async def test_a_kernel_status_failure_is_reported_only_to_the_secret_holder(node):
    calls = []

    async def fail():
        calls.append(1)
        raise fake.KernelError.State("store.unavailable")

    node.kernel.mesh_status = fail
    response = await _post(node.app, _secret(node.home))
    assert (response.status_code, response.json()["detail"]) == (409, "store.unavailable")
    guessed = await _post(node.app, "guess")
    assert (guessed.status_code, guessed.json()["detail"]) == (401, "node.pairing_secret_required")
    assert len(calls) == 2 and node.kernel.offers == 0


async def test_a_caller_without_the_secret_never_reaches_the_kernel_offer(node):
    for presented in ("guess", "x" * 43):
        assert (await _post(node.app, presented)).status_code == 401
    assert node.kernel.offers == 0


async def test_an_enrolled_node_reports_its_membership_and_deletes_the_secret(node):
    secret = _secret(node.home)
    node.kernel.join(MESH)
    enrolled = {"state": "enrolled", "mesh_id": MESH, "member_id": "dev-node"}
    assert (await _post(node.app, secret)).json() == enrolled
    assert not _secret_path(node.home).exists() and node.kernel.offers == 0
    again = await _post(node.app, secret)
    assert (again.status_code, again.json()) == (200, enrolled)
    assert not _secret_path(node.home).exists() and node.kernel.offers == 0


async def test_a_joined_node_answers_its_enrolled_state_once_the_secret_is_gone(node):
    secret = _secret(node.home)
    node.kernel.join(MESH)
    node.mesh._listener.dispatch(NS(progress=type("JOINED", (), {})()))
    assert not _secret_path(node.home).exists()
    response = await _post(node.app, secret)
    assert (response.status_code, response.json()["state"]) == (200, "enrolled")
    assert not _secret_path(node.home).exists()


async def test_joining_deletes_the_secret_and_no_later_offer_writes_one(node):
    node.kernel.join(MESH)
    node.mesh._listener.dispatch(NS(progress=type("JOINED", (), {})()))
    assert not _secret_path(node.home).exists()
    node.mesh._pairing.rotate()
    node.mesh._pairing.ensure()
    await _settled(node.mesh)
    assert not _secret_path(node.home).exists()


async def _settled(mesh):
    await asyncio.sleep(0)
    await asyncio.gather(*mesh._checks, *mesh._pairing_checks)


@pytest.mark.parametrize("change", ["RosterChanged", "ROSTER_CHANGED"])
async def test_leaving_the_mesh_at_runtime_writes_a_fresh_secret(node, monkeypatch, change):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    node.kernel.join(MESH)
    node.mesh._listener.dispatch(NS(progress=type("JOINED", (), {})()))
    await _settled(node.mesh)
    assert not _secret_path(node.home).exists()
    node.kernel.enrolled = False
    node.mesh._listener.dispatch(type(change, (), {})())
    await _settled(node.mesh)
    fresh = _secret(node.home)
    response = await _post(node.app, fresh)
    assert (response.status_code, response.json()["state"]) == (200, "offer")


async def test_a_pairing_check_that_cannot_read_the_kernel_changes_nothing(node, caplog):
    secret = _secret(node.home)

    async def fail():
        raise fake.KernelError.State("store.unavailable")

    node.kernel.mesh_status = fail
    node.mesh._listener.dispatch(type("RosterChanged", (), {})())
    await _settled(node.mesh)
    assert _secret(node.home) == secret
    assert "node_mesh_pairing_check_failed: store.unavailable" in caplog.text


async def test_a_pairing_check_after_stop_does_nothing(node):
    secret = _secret(node.home)
    await node.mesh.stop()
    await node.mesh._check_pairing()
    assert _secret(node.home) == secret


async def test_the_owner_refresh_deletes_the_secret_once_enrolled(node):
    node.kernel.join(MESH)
    node.kernel.mesh_principal = lambda: NS(mesh_id=MESH)
    node.kernel.roster = lambda: NS(mesh_id=MESH, stopped=False, removed=[],
                                    members=[NS(device_id=node.kernel.device_id, is_local=True)])
    assert await node.mesh.refresh_personal_owner() == ni.owner_id_for_mesh(MESH)
    assert not _secret_path(node.home).exists()


async def test_other_listener_changes_keep_the_secret(node):
    secret = _secret(node.home)
    node.mesh._listener.dispatch(NS(progress=type("OFFER_SEEN", (), {})()))
    node.mesh._listener.dispatch(type("ChatChanged", (), {})())
    assert _secret(node.home) == secret


async def test_without_a_started_mesh_the_offer_is_unavailable(node):
    secret = _secret(node.home)
    node_mesh.install(None)
    response = await _post(node.app, secret)
    assert (response.status_code, response.json()["detail"]) == (503, "mesh_unavailable")
    node_mesh.install(node.mesh)
    await node.mesh.stop()
    assert (await _post(node.app, secret)).status_code == 503


async def test_outside_the_personal_profile_there_is_no_offer(node, monkeypatch):
    secret = _secret(node.home)
    monkeypatch.delenv(ni.PROFILE_ENV)
    response = await _post(node.app, secret)
    assert (response.status_code, response.json()["detail"]) == (404, "node_profile_inactive")
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await node.mesh.serve_offer(secret)
    assert (refused.value.status, refused.value.code) == (404, "node_profile_inactive")
    assert _secret(node.home) == secret and node.kernel.offers == 0


async def test_a_node_mesh_without_the_personal_profile_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    fake.Kernel.instances.clear()
    mesh = node_mesh.NodeMesh(data_dir=tmp_path / "owner", binding=fake,
                              settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
    await mesh.start()
    try:
        assert not _secret_path(tmp_path / "owner").exists()
    finally:
        await mesh.stop()


async def test_a_secret_that_cannot_be_stored_fails_the_start(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    home = tmp_path / "node"
    home.mkdir(mode=0o700)
    (home / node_mesh.PAIRING_SECRET_FILE).mkdir()
    fake.Kernel.instances.clear()
    mesh = node_mesh.NodeMesh(data_dir=home, binding=fake,
                              settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)))
    with pytest.raises(OSError):
        await mesh.start()


async def test_a_real_owner_pairs_through_the_offer_and_the_secret_is_gone(tmp_path, monkeypatch):
    home = tmp_path / "node"
    monkeypatch.setenv(dev_mesh.ENV, dev_mesh.DEVELOPMENT)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(home))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    node = node_mesh.NodeMesh(data_dir=home, settings=node_settings())
    await node.start()
    node_mesh.install(node)
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    try:
        app = orchestrator_app.build(monkeypatch, orchestrator_app.host(), surfaces=())
        assert stat.S_IMODE(_secret_path(home).stat().st_mode) == 0o600
        response = await _post(app, _secret(home))
        assert response.status_code == 200 and response.json()["state"] == "offer"
        offer = response.json()
        assert set(offer) == {"state", "code", "payload", "expires_ms"}
        owner.create_mesh()
        owner.confirm_recovery()
        await owner.start()
        await owner.approve_offer(offer["payload"])
        await until(lambda: not _secret_path(home).exists(), "the joined node to delete its pairing secret")
        status = await node.status()
        assert status["enrolled"] and ni.bound_owner() is None
        await node.stop()
        _secret_path(home).write_text("left behind")
        os.chmod(_secret_path(home), 0o600)
        node = node_mesh.NodeMesh(data_dir=home, settings=node_settings())
        await node.start()
        node_mesh.install(node)
        assert not _secret_path(home).exists()
        answer = await _post(app, "left behind")
        assert answer.json() == {"state": "enrolled", "mesh_id": status["mesh_id"], "member_id": status["member_id"]}
        assert not _secret_path(home).exists()
    finally:
        node_mesh.install(None)
        await owner.stop()
        await node.stop()
        ni.reset_cache()
