"""Removes a lost device while every other device is off and shows the removal reaches a device that comes online afterwards through the node's own mailbox: the notice is deposited, the node and the removing device go offline, and the late device learns of the removal from the mailbox alone. With the mailbox stopped it does not learn, which shows the notice path is the mailbox.
It runs only where the pinned kernel wheel and the mailbox binary are installed, using a test token issuer, software keys and loopback networking."""

from __future__ import annotations

import asyncio
import shutil

import pytest

pytest.importorskip("astral_kernel")
pytestmark = pytest.mark.skipif(shutil.which("astral-mailbox") is None, reason="mailbox binary not installed")

from orchestrator import node_mesh  # noqa: E402
from orchestrator.node_mailbox import MailboxSettings  # noqa: E402
from tests.test_node_mesh_kernel_integration import (  # noqa: E402
    ak,
    enrolled,
    free_port,
    open_device,
    test_issuers as issuers_for_tests,
    token,
    until,
)


def removed_ids(kernel):
    return [item.device_id for item in kernel.roster().removed]


@pytest.mark.parametrize("mailbox_up", [True, False])
async def test_a_removal_notice_reaches_a_device_that_comes_online_after_every_other_device_is_off(tmp_path, monkeypatch, mailbox_up):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "personal")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    settings = node_mesh.MeshSettings(
        label="node", relay=("none",), loopback_only=True, local_discovery=False, internet_lookup=False, sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",)),
    )
    node = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=settings, issuers=issuers_for_tests)
    await node.start()
    running = []
    try:
        mailbox = (await node.status())["mailbox"]["address"]
        laptop = open_device(tmp_path / "laptop", "laptop", 1, mailbox)
        request = laptop.begin_mesh_creation()
        laptop.complete_mesh_creation(token(request.nonce))
        await laptop.start_network()
        running.append(laptop)
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        await until(lambda: enrolled(node), "the node to join")

        joined = {}
        for name, marker in (("tablet", 2), ("lost", 3)):
            device = open_device(tmp_path / name, name, marker, mailbox)
            await device.start_network()
            running.append(device)
            device_offer = await device.create_pairing_offer()
            await laptop.approve_pairing(device_offer.payload, ak.HistoryGrant.ALL)
            await until(lambda device=device: len(device.roster().members) >= 3, f"the {name} to join")
            joined[name] = (device, (await device.mesh_status()).device_id)
        tablet, tablet_id = joined["tablet"]
        lost, lost_id = joined["lost"]
        await until(lambda: len(tablet.roster().members) == 4 and len(lost.roster().members) == 4, "every device to see the full roster")

        await tablet.stop_network()
        await lost.stop_network()
        laptop.remove_device(lost_id)
        assert lost_id in removed_ids(laptop)
        await until(lambda: laptop.mailbox_state().pending_notices == 0 and laptop.mailbox_state().status == "ok", "the notice to reach the mailbox")
        assert lost_id not in removed_ids(tablet)

        await laptop.stop_network()
        running.remove(laptop)
        await node.kernel.stop_network()

        if not mailbox_up:
            await node.mailbox.stop()
        await tablet.start_network()
        if mailbox_up:
            await until(lambda: lost_id in removed_ids(tablet), "the tablet to learn of the removal from the mailbox alone")
            assert sorted(item.device_id for item in tablet.roster().members).count(lost_id) == 0
            assert len(tablet.roster().members) == 3
        else:
            await asyncio.sleep(4)
            assert lost_id not in removed_ids(tablet) and len(tablet.roster().members) == 4
    finally:
        for kernel in running:
            await kernel.stop_network()
        await node.stop()
