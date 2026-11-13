"""Covers orchestrator/institution_meshes.py: every owner gets a separate kernel data directory, keys and mesh, the same owner reuses its peer, a restart restores the owners already served, and invalid, duplicate-subject and over-capacity owners are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import node_mesh
from orchestrator.institution_meshes import InstitutionMeshes, Owner
from orchestrator.node_mesh import NodeMeshError
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_chat import no_issuers

ISS = "https://idp.example/realms/a"


@pytest.fixture(autouse=True)
def clean():
    fake.Kernel.instances.clear()
    yield


def meshes(tmp_path, **kwargs):
    return InstitutionMeshes(base_dir=tmp_path, binding=fake, issuers=no_issuers, **kwargs)


async def test_each_owner_gets_its_own_peer_data_directory_and_keys(tmp_path):
    registry = meshes(tmp_path)
    first = await registry.for_owner(Owner.verified(ISS, "alice"))
    second = await registry.for_owner(Owner.verified(ISS, "bob"))
    assert first is not second and len(fake.Kernel.instances) == 2
    dirs = {kernel.config.data_dir for kernel in fake.Kernel.instances}
    assert len(dirs) == 2 and all(str(tmp_path / "owners") in path for path in dirs)
    identities = {(Path(path).parent / "mesh" / "identity.pem").read_bytes() for path in dirs}
    assert len(identities) == 2
    await registry.stop()


async def test_the_same_owner_reuses_its_peer(tmp_path):
    registry = meshes(tmp_path)
    owner = Owner.verified(ISS + "/", "alice")
    assert await registry.for_owner(owner) is await registry.for_owner(Owner.verified(ISS, "alice"))
    assert len(fake.Kernel.instances) == 1 and registry.owners == [owner]
    await registry.stop()


async def test_the_same_subject_from_another_issuer_is_a_different_owner_but_is_refused_for_orchestrator_safety(tmp_path):
    registry = meshes(tmp_path)
    await registry.for_owner(Owner.verified(ISS, "alice"))
    other = Owner.verified("https://idp.example/realms/b", "alice")
    assert other.partition != Owner.verified(ISS, "alice").partition and other.slot != Owner.verified(ISS, "alice").slot
    with pytest.raises(NodeMeshError) as info:
        await registry.for_owner(other)
    assert info.value.code == "owner_subject_conflict"
    await registry.stop()


@pytest.mark.parametrize(("issuer", "subject"), [("", "alice"), (ISS, ""), (ISS, " alice"), (None, "alice"), (ISS, 7), (ISS, "a/b")])
def test_an_unusable_issuer_or_subject_is_refused(issuer, subject):
    with pytest.raises(NodeMeshError) as info:
        Owner.verified(issuer, subject)
    assert info.value.code == "owner_invalid"


async def test_capacity_is_enforced(tmp_path):
    registry = meshes(tmp_path, max_owners=1)
    await registry.for_owner(Owner.verified(ISS, "alice"))
    with pytest.raises(NodeMeshError) as info:
        await registry.for_owner(Owner.verified(ISS, "bob"))
    assert info.value.code == "institution_capacity" and info.value.status == 429
    await registry.stop()


async def test_a_restart_restores_every_owner_already_served(tmp_path):
    registry = meshes(tmp_path)
    await registry.for_owner(Owner.verified(ISS, "alice"))
    await registry.for_owner(Owner.verified(ISS, "bob"))
    await registry.stop()
    fake.Kernel.instances.clear()
    again = meshes(tmp_path)
    restored = await again.start()
    assert sorted(owner.subject for owner in restored) == ["alice", "bob"] and len(fake.Kernel.instances) == 2
    await again.stop()


async def test_a_stored_owner_record_that_does_not_match_its_directory_is_ignored(tmp_path):
    registry = meshes(tmp_path)
    owner = Owner.verified(ISS, "alice")
    await registry.for_owner(owner)
    await registry.stop()
    renamed = tmp_path / "owners" / ("0" * 32)
    (tmp_path / "owners" / owner.slot).rename(renamed)
    (tmp_path / "owners" / "junk").mkdir()
    fake.Kernel.instances.clear()
    again = meshes(tmp_path)
    assert await again.start() == [] and not fake.Kernel.instances
    await again.stop()


async def test_a_failing_start_hook_leaves_no_peer_behind(tmp_path):
    async def refuse(owner, mesh):
        raise node_mesh.MeshUnavailable("requests_unsupported")

    registry = meshes(tmp_path, on_started=refuse)
    owner = Owner.verified(ISS, "alice")
    with pytest.raises(node_mesh.MeshUnavailable):
        await registry.for_owner(owner)
    assert registry.owners == [] and registry.current(owner) is None
    assert fake.Kernel.instances[0].network is False
    await registry.stop()


async def test_the_hook_runs_once_per_owner(tmp_path):
    seen = []

    async def hook(owner, mesh):
        seen.append(owner.subject)

    registry = meshes(tmp_path, on_started=hook)
    owner = Owner.verified(ISS, "alice")
    await registry.for_owner(owner)
    await registry.for_owner(owner)
    assert seen == ["alice"]
    await registry.stop()


async def test_a_shared_mailbox_address_reaches_every_owners_kernel(tmp_path):
    class SharedMailbox:
        address = "shared-mailbox-address"

        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    import orchestrator.institution_meshes as module

    original = module.MailboxProcess
    module.MailboxProcess = SharedMailbox
    try:
        from orchestrator.node_mailbox import MailboxSettings

        registry = meshes(tmp_path, mailbox_settings=MailboxSettings(enabled=True, max_meshes=50))
        await registry.start()
        await registry.for_owner(Owner.verified(ISS, "alice"))
        await registry.for_owner(Owner.verified(ISS, "bob"))
        assert [kernel.config.network.mailbox for kernel in fake.Kernel.instances] == ["shared-mailbox-address"] * 2
        await registry.stop()
    finally:
        module.MailboxProcess = original


async def test_a_mailbox_that_cannot_start_leaves_the_owners_without_one(tmp_path):
    from orchestrator.node_mailbox import MailboxSettings

    registry = meshes(tmp_path, mailbox_settings=MailboxSettings(enabled=True, binary="/nonexistent/astral-mailbox"))
    await registry.start()
    await registry.for_owner(Owner.verified(ISS, "alice"))
    assert fake.Kernel.instances[0].config.network.mailbox is None
    await registry.stop()


def test_the_owner_capacity_comes_from_the_environment_and_ignores_nonsense(monkeypatch):
    from orchestrator import institution_meshes as module

    monkeypatch.setenv(module.MAX_OWNERS_ENV, "7")
    assert module._configured_capacity() == 7
    for value in ("", "0", "many", "-2"):
        monkeypatch.setenv(module.MAX_OWNERS_ENV, value)
        assert module._configured_capacity() == module.DEFAULT_MAX_OWNERS
