"""Runs an institution node with the real kernel binding and mailbox: two owners are served by separate peers that cannot see each other's chats, a device whose identity attestation is missing is refused with a dotted code until it attests again, and a fresh device is served.
It runs only where the pinned kernel wheel and the mailbox binary are installed, using test token issuers, software keys and loopback networking."""

from __future__ import annotations

import json
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import institution_runtime, node_mesh, node_mesh_chat  # noqa: E402
from orchestrator.institution_meshes import Owner  # noqa: E402
from orchestrator.node_mailbox import MailboxSettings  # noqa: E402
from shared import institution_profile as ip  # noqa: E402
from tests.test_node_mesh_kernel_integration import (  # noqa: E402
    ISS,
    NODE_TEXT,
    OWNER,
    History,
    ak,
    enrolled,
    free_port,
    model_turn,
    open_device,
    request_state,
    test_issuers as issuers_for_tests,
    token,
    until,
)
from tests.test_node_mesh_peer_tools import model_server, node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import turn_host  # noqa: F401

from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

KEY = ec.derive_private_key(0x1D2C3B4A5968778695A4B3C2D1E0F00F, ec.SECP256R1())


def make_runtime(tmp_path, *, runner=model_turn, calls=None):
    settings = node_mesh.MeshSettings(
        label="institution node",
        relay=("none",),
        loopback_only=True,
        local_discovery=False,
        internet_lookup=False,
        sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",), max_meshes=10),
    )
    config = ip.InstitutionConfig(key=ip.key_text(KEY), issuers=(ISS,))
    return institution_runtime.InstitutionRuntime(
        config=config,
        base_dir=tmp_path / "node",
        history=History(),
        runner=runner,
        calls=calls,
        settings=settings,
        issuers=issuers_for_tests,
        phi_active=lambda owner: False,
    )


async def join_owner(runtime, tmp_path, subject, marker):
    owner = Owner.verified(ISS, subject)
    node = await runtime.serve_owner(owner)
    mailbox = runtime.meshes._mailbox.address
    laptop = open_device(tmp_path / f"{subject}-laptop", f"{subject}-laptop", marker, mailbox)
    created = laptop.begin_mesh_creation()
    laptop.complete_mesh_creation(token(created.nonce, subject))
    await laptop.start_network()
    offer = await node.pairing_offer()
    await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
    await until(lambda: enrolled(node), f"the node to join {subject}'s mesh")
    return node, laptop, (await node.status())["member_id"]


async def test_two_owners_are_served_by_separate_peers_and_never_see_each_others_chats(tmp_path):
    runtime = make_runtime(tmp_path)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    laptops = []
    try:
        alice_node, alice, alice_device = await join_owner(runtime, tmp_path, "alice", 1)
        bob_node, bob, bob_device = await join_owner(runtime, tmp_path, "bob", 2)
        laptops += [alice, bob]
        assert alice_node is not bob_node and alice_device != bob_device
        assert (await alice_node.status())["mesh_id"] != (await bob_node.status())["mesh_id"]
        assert runtime.meshes.owner_dir(Owner.verified(ISS, "alice")) != runtime.meshes.owner_dir(Owner.verified(ISS, "bob"))

        secret_chat = bob.create_chat("bob private")
        bob.send_message(secret_chat, "bob's note", None)
        chat = alice.create_chat("ask the node")
        alice.send_message(chat, "hello", None)
        request = alice.send_request(chat, "summarize", alice_device, None, None)
        await until(request_state(alice, request, "ANSWERED"), "the node to answer alice")
        await until(lambda: any((m.text or "") == NODE_TEXT + "summarize" for m in alice.chat(chat).view.messages), "alice to read the answer")
        await until(lambda: any(c.chat_id == secret_chat for c in bob_node.kernel.list_chats()), "bob's peer to hold bob's chat")
        assert all(c.chat_id != secret_chat for c in alice_node.kernel.list_chats())
        assert all(c.chat_id != chat for c in bob_node.kernel.list_chats())
        assert alice_node.kernel.pending_requests_for_me() == [] and bob_node.kernel.pending_requests_for_me() == []
    finally:
        node_mesh_chat.install(None)
        for laptop in laptops:
            await laptop.stop_network()
        await runtime.stop()


async def test_two_owners_use_separate_sealed_kernel_turns(tmp_path, monkeypatch, model_server):  # noqa: F811
    from tests.test_node_mesh_peer_tools import FakeModel, REPLY

    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "institution")
    FakeModel.tool = None
    runtime = make_runtime(tmp_path, runner=None)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    laptops = []
    try:
        for subject, marker in (("alice", 1), ("bob", 2)):
            node, laptop, device = await join_owner(runtime, tmp_path, subject, marker)
            laptops.append(laptop)
            subject_key = f"fixture-provider-{subject}"
            laptop.set_model_provider(ak.ModelProviderConfig(
                name="fake", base_url=f"http://127.0.0.1:{model_server}/v1", models=["fake-1"],
                api_keys=[subject_key], fallback_order=0, self_hosted=True, strict_tools=False,
            ))
            await until(lambda: bool(node.kernel.model_providers()), f"{subject}'s sealed provider")
            chat = laptop.create_chat(subject)
            laptop.send_message(chat, f"hello {subject}", None)
            await until(lambda: bool(node.kernel.chat(chat).view.messages), f"{subject}'s chat")
            request = laptop.send_request(chat, "answer", device, None, None)
            await until(request_state(laptop, request, "ANSWERED"), f"{subject}'s native answer")
            await until(lambda: any(message.text == REPLY for message in laptop.chat(chat).view.messages),
                        f"{subject}'s native reply")
            assert all(item.chat_id != chat for other in runtime.meshes._meshes.values() if other is not node
                       for item in other.kernel.list_chats())
        assert len(FakeModel.seen) >= 2
        assert FakeModel.seen_auth == ["Bearer fixture-provider-alice", "Bearer fixture-provider-bob"]
    finally:
        node_mesh_chat.install(None)
        for laptop in laptops:
            await laptop.stop_network()
        await runtime.stop()


@pytest.mark.parametrize("change", [None, "unattested", "stop", "revoke", "replay", "unknown", "audit-pre", "audit-post"])
async def test_institution_kernel_effect_uses_protected_receiver(
    tmp_path, monkeypatch, model_server, turn_host, change,  # noqa: F811
):
    from orchestrator import node_mesh_admission
    from orchestrator.node_mesh_calls import OrchestratorCalls
    from tests.test_node_mesh_composed_gate import CASES, install_effect
    from tests.test_node_mesh_peer_tools import API_KEY, FakeModel
    from tests.test_node_mesh_turn_tools import grant

    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "institution")
    host = turn_host
    monkeypatch.setattr(host.orch.delegation, "exchange_token_for_agent",
                        AsyncMock(return_value={"access_token": "scoped-institution-test-token"}))
    case = CASES["all-clear-dispatches"]
    granted = install_effect(host, case)
    FakeModel.tool, FakeModel.arguments = case["call"]["tool"], case["call"]["arguments"]
    runtime = make_runtime(tmp_path, runner=None, calls=OrchestratorCalls(host.orch))
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    laptop = None
    try:
        node, laptop, device = await join_owner(runtime, tmp_path, "user-1", 1)
        if change != "unattested":
            node.kernel.reattest(token(node.kernel.begin_attestation().nonce, "user-1"))
        laptop.set_model_provider(ak.ModelProviderConfig(
            name="fake", base_url=f"http://127.0.0.1:{model_server}/v1", models=["fake-1"],
            api_keys=[API_KEY], fallback_order=0, self_hosted=True, strict_tools=False,
        ))
        await until(lambda: bool(node.kernel.model_providers()), "the sealed institution provider")
        await runtime.bridges.get(Owner.verified(ISS, "user-1").partition)._publish_catalog()
        await grant(node, laptop, granted, "outside_effect")
        recorded = []
        record_turn = node.record_turn

        async def observed_record(*args, **kwargs):
            recorded.extend(kwargs["steps"])
            return await record_turn(*args, **kwargs)

        monkeypatch.setattr(node, "record_turn", observed_record)
        audit_events = []
        original_audit = host.orch.audit_recorder.record

        async def observed_audit(event):
            if event.action_type.startswith("mesh.kernel_effect_"):
                audit_events.append(event.action_type)
                if (change == "audit-pre" and event.action_type == "mesh.kernel_effect_started"
                        or change == "audit-post" and event.action_type == "mesh.kernel_effect_response_success"):
                    return None
            return await original_audit(event)

        monkeypatch.setattr(host.orch.audit_recorder, "record", observed_audit)
        unknown_attempts = []
        if change == "unknown":
            async def lost_response(*args, **kwargs):
                unknown_attempts.append(True)
                raise httpx.ReadTimeout("controlled executor response loss")

            monkeypatch.setattr(host.orch.vessel_executor._client, "post", lost_response)
        replayed = []
        if change in {"stop", "revoke", "replay"}:
            original_dispatch = node_mesh_admission.dispatch

            async def observed_dispatch(*args, **kwargs):
                if change == "stop":
                    node.kernel.raise_stop()
                elif change == "revoke":
                    node.kernel.revoke_tool(granted, "outside_effect")
                first = await original_dispatch(*args, **kwargs)
                if change == "replay":
                    replayed.append(await original_dispatch(*args, **kwargs))
                return first

            monkeypatch.setattr(node_mesh_admission, "dispatch", observed_dispatch)
        chat = laptop.create_chat("institution effect")
        laptop.send_message(chat, "send hello", None)
        await until(lambda: bool(node.kernel.chat(chat).view.messages), "the institution chat")
        request = laptop.send_request(chat, "send hello", device, None, None)
        await until(request_state(laptop, request, "ANSWERED"), "the institution effect answer", seconds=90)
        await until(lambda: any(item.call is not None for item in node.kernel.chat_requests(chat)),
                    "the signed institution child call")
        await runtime.bridges.get(OWNER).scan()
        terminal = {ak.RequestState.ANSWERED, ak.RequestState.REFUSED, ak.RequestState.CANCELLED}
        await until(lambda: all(item.state in terminal for item in node.kernel.chat_requests(chat)
                                if item.call is not None), "the institution child outcome")
        if change == "unknown":
            await until(lambda: bool(unknown_attempts) and "mesh.kernel_effect_uncertain" in audit_events,
                        "the uncertain institution effect")
            await runtime.bridges.get(OWNER).scan()
            assert len(unknown_attempts) == 1
        assert len(host.sent) == (1 if change in {None, "replay", "audit-post"} else 0), (
            [(step.tool, step.status, step.code) for step in recorded],
            [(item.state.name, item.code) for item in node.kernel.chat_requests(chat) if item.call is not None],
        )
        if change == "unattested":
            child = next(item for item in node.kernel.chat_requests(chat) if item.call is not None)
            assert child.code == "institution.attestation_stale"
            host.orch.delegation.exchange_token_for_agent.assert_not_awaited()
        else:
            host.orch.delegation.exchange_token_for_agent.assert_awaited()
        if change == "replay":
            assert len(replayed) == 1 and replayed[0].error["code"] == "node.ticket_used"
        if change == "audit-pre":
            assert audit_events == ["mesh.kernel_effect_started"]
        if change == "audit-post":
            assert audit_events == ["mesh.kernel_effect_started", "mesh.kernel_effect_response_success"]
        if change == "unknown":
            assert audit_events == ["mesh.kernel_effect_started", "mesh.kernel_effect_uncertain"]
    finally:
        node_mesh_chat.install(None)
        if laptop is not None:
            await laptop.stop_network()
        await runtime.stop()


async def test_a_device_without_a_current_attestation_is_refused_until_it_attests_again(tmp_path):
    runtime = make_runtime(tmp_path)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    running = []
    try:
        node, laptop, node_device = await join_owner(runtime, tmp_path, "alice", 1)
        running.append(laptop)
        tablet = open_device(tmp_path / "tablet", "tablet", 3, runtime.meshes._mailbox.address)
        await tablet.start_network()
        running.append(tablet)
        offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 3, "the tablet to join")
        tablet_device = (await tablet.mesh_status()).device_id
        await until(lambda: any(m.device_id == tablet_device and m.unattested for m in node.kernel.roster().members), "the node to see the tablet unattested")

        chat = tablet.create_chat("from the tablet")
        tablet.send_message(chat, "hello", None)
        first = tablet.send_request(chat, "run before attesting", node_device, None, None)
        await until(request_state(tablet, first, "REFUSED"), "the node to refuse the unattested tablet")
        assert tablet.request(first).code == "institution.attestation_stale"
        assert not any((m.text or "").startswith(NODE_TEXT) for m in tablet.chat(chat).view.messages)

        tablet.reattest(token(tablet.begin_attestation().nonce))
        await until(lambda: any(m.device_id == tablet_device and not m.unattested for m in node.kernel.roster().members), "the node to see the new attestation")
        second = tablet.send_request(chat, "run after attesting", node_device, None, None)
        await until(request_state(tablet, second, "ANSWERED"), "the node to serve the attested tablet")
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()


async def test_the_chat_bridge_entry_point_runs_an_institution_node_on_the_real_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "institution")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISS)
    configured = (await issuers_for_tests())[0]
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", configured["client_id"])
    monkeypatch.setattr("shared.jwks_cache.get_jwks", AsyncMock(return_value=json.loads(configured["jwks_json"])))
    monkeypatch.setenv(ip.KEY_ENV, ip.key_text(KEY))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "off")
    monkeypatch.setenv("ASTRAL_NODE_MESH_LOOPBACK", "1")
    await node_mesh_chat.start_for(SimpleNamespace(history=History()))
    try:
        runtime = institution_runtime.current()
        assert runtime is not None and runtime.descriptor_fields["profile"] == "institution"
        owner = Owner.verified(ISS, "alice")
        node = await runtime.serve_owner(owner)
        assert (await node.status())["state"] == "not_enrolled" and node.requests_supported
        assert runtime.bridges.get(owner.partition)._runner is None
    finally:
        await node_mesh_chat.stop_all()
