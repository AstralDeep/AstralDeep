"""Tests for the bridge between the node's own turn loop and the owner's mesh log: mirrored turns, picked-up requests, refusals, PHI fail-closed behavior, crash recovery and the machine-authority runner."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import node_mesh, node_mesh_chat as nmc
from tests import fake_astral_kernel as fake

OWNER = "oidc.abc.owner"


class StubMailbox:
    async def start(self):
        pass

    async def stop(self):
        pass

    def snapshot(self):
        return {"running": True, "address": None}


async def no_issuers():
    return []


class History:
    def __init__(self):
        self.created = []
        self.added = []

    def create_chat(self, user_id):
        self.created.append(user_id)
        return f"local-{len(self.created)}"

    def add_message(self, chat_id, role, content, user_id):
        self.added.append((chat_id, role, content, user_id))


@pytest.fixture(autouse=True)
def clean():
    fake.Kernel.instances.clear()
    yield
    nmc.install(None)
    node_mesh.install(None)


async def build(tmp_path, runner=None, *, policy=None, phi=False, start=True, turn_seconds=5.0, owner=OWNER, joined=True):
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, issuers=no_issuers, mailbox=StubMailbox())
    await mesh.start()
    kernel = fake.Kernel.instances[0]
    if joined:
        kernel.join()
    history = History()
    bridge = nmc.MeshChatBridge(
        mesh,
        owner_id=owner,
        history=history,
        runner=runner or (lambda *args: asyncio.sleep(0)),
        state_path=tmp_path / "mesh-chats.json",
        policy=policy or nmc.RequestPolicy("addressed"),
        phi_active=lambda owner_id: phi,
        poll_seconds=0.05,
        turn_seconds=turn_seconds,
    )
    if start:
        await bridge.start()
    return mesh, kernel, history, bridge


async def settle(predicate, attempts=200):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def kinds(kernel):
    return [kind for _, events in kernel.appended for kind, _ in events]


def bodies(kernel, kind):
    return [json.loads(body) for _, events in kernel.appended for k, body in events if k == kind]


async def test_a_thin_client_turn_is_written_as_events(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "What is the weather?\nplease")
    nmc.observe_step(OWNER, "local-a", {"name": "weather", "args": '{"q":1}', "status": "completed", "result": "sunny", "error": None})
    nmc.observe_step(OWNER, "local-a", {"name": "lookup", "args": None, "status": "errored", "result": None, "error": "denied"})
    nmc.observe_message(OWNER, "local-a", "assistant", [{"type": "text", "content": "It is sunny."}])
    await settle(lambda: kernel.appended)
    chat, title = kernel.created[0]
    assert title == "What is the weather?"
    assert kernel.sent[0][:3] == (chat, "What is the weather?\nplease", None)
    assert kinds(kernel) == [
        "chat.turn_start", "chat.tool_proposed", "chat.tool_result",
        "chat.tool_proposed", "chat.tool_result", "chat.assistant_message", "chat.turn_end",
    ]
    assert bodies(kernel, "chat.turn_start")[0] == {"continues": None, "message": kernel.sent[0][3]}
    assert bodies(kernel, "chat.assistant_message")[0] == {"text": "It is sunny."}
    results = bodies(kernel, "chat.tool_result")
    assert results[0] == {"tool": "weather", "status": "completed", "result": "sunny"}
    assert results[1]["error"] == "denied"
    assert bridge.state.local_for(chat) == "local-a"
    await bridge.stop()


async def test_the_second_turn_reuses_the_mesh_chat(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    for text in ("one", "two"):
        nmc.observe_message(OWNER, "local-a", "user", text)
        nmc.observe_message(OWNER, "local-a", "assistant", "ok " + text)
    await settle(lambda: len(kernel.appended) == 2)
    assert len(kernel.created) == 1 and {s[0] for s in kernel.sent} == {kernel.created[0][0]}
    await bridge.stop()


async def test_other_owners_and_stray_events_are_ignored(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message("someone-else", "local-a", "user", "x")
    nmc.observe_message(OWNER, "local-b", "assistant", "answer without a question")
    nmc.observe_message(OWNER, "local-c", "user", "q")
    nmc.observe_message(OWNER, "local-c", "assistant", "   ")
    nmc.observe_step(OWNER, "no-session", {"name": "t"})
    await asyncio.sleep(0.2)
    assert not kernel.appended and not kernel.sent
    await bridge.stop()


async def test_phi_protection_blocks_mirroring(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, phi=True)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "patient data")
    nmc.observe_message(OWNER, "local-a", "assistant", "answer")
    await asyncio.sleep(0.2)
    assert not kernel.appended and not kernel.created
    await bridge.stop()


async def test_step_overflow_is_bounded(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "q")
    for index in range(300):
        nmc.observe_step(OWNER, "local-a", {"name": f"t{index}", "status": "completed", "result": "r"})
    nmc.observe_message(OWNER, "local-a", "assistant", "done")
    await settle(lambda: kernel.appended)
    assert kinds(kernel).count("chat.tool_result") == 256
    await bridge.stop()


async def test_an_addressed_request_is_run_and_answered(tmp_path):
    seen = {}

    async def runner(owner_id, local_chat, text):
        seen.update(owner=owner_id, local=local_chat, text=text)
        nmc.observe_message(owner_id, local_chat, "user", text)
        nmc.observe_step(owner_id, local_chat, {"name": "calc", "args": "1+1", "status": "completed", "result": "2"})
        nmc.observe_message(owner_id, local_chat, "assistant", "It is 2.")

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    nmc.install(bridge)
    earlier = fake.message("m0", "earlier question", status="COMPLETE")
    earlier_answer = fake.message("m00", "earlier answer", role="ASSISTANT", status="COMPLETE", author="dev-node")
    request = fake.message("m1", "@node what is 1+1?")
    kernel.add_chat("chat-x", [earlier, earlier_answer, request], turns=[fake.turn("t0", status="ENDED", branch=None)])
    await bridge.start()
    await settle(lambda: "chat.turn_end" in kinds(kernel))
    assert seen == {"owner": OWNER, "local": "local-1", "text": "what is 1+1?"}
    assert history.added == [
        ("local-1", "user", "earlier question", OWNER),
        ("local-1", "assistant", "earlier answer", OWNER),
    ]
    start = bodies(kernel, "chat.turn_start")
    assert start == [{"continues": "t0", "message": "m1"}]
    assert kinds(kernel) == ["chat.turn_start", "chat.tool_proposed", "chat.tool_result", "chat.assistant_message", "chat.turn_end"]
    assert not kernel.sent
    assert bridge.state.claimed("m1")
    await bridge.scan()
    assert kinds(kernel).count("chat.turn_start") == 1
    await bridge.stop()


async def test_requests_the_node_should_not_run_are_left_alone(tmp_path):
    ran = []

    async def runner(*args):
        ran.append(args)

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    kernel.add_chat(
        "chat-x",
        [
            fake.message("a", "no address here"),
            fake.message("b", "@node mine", author="dev-node"),
            fake.message("c", "@node done", status="COMPLETE"),
            fake.message("d", "@node assistant", role="ASSISTANT"),
            fake.message("e", "", ),
        ],
    )
    await bridge.start()
    await asyncio.sleep(0.3)
    assert not ran and not kernel.appended
    await bridge.stop()


async def test_all_mode_runs_every_queued_message_and_off_mode_none(tmp_path):
    ran = []

    async def runner(owner_id, local_chat, text):
        ran.append(text)
        nmc.observe_message(owner_id, local_chat, "assistant", "a")

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False, policy=nmc.RequestPolicy("all"))
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m", "plain text")])
    await bridge.start()
    await settle(lambda: ran)
    assert ran == ["plain text"]
    await bridge.stop()
    off = nmc.RequestPolicy("off")
    assert off.accepts("@node x") is False and off.prompt("@node x") == "@node x"
    assert nmc.RequestPolicy("all").accepts("x") and nmc.RequestPolicy("addressed").accepts("@Node: hi")
    assert nmc.RequestPolicy("addressed").prompt("@node: hi") == "hi"


async def test_off_policy_never_scans(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False, policy=nmc.RequestPolicy("off"))
    kernel.add_chat("c", [fake.message("m", "@node hi")])
    assert await bridge.scan() == 0


async def test_a_node_not_in_a_mesh_or_without_an_owner_runs_nothing(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False, joined=False)
    kernel.add_chat("c", [fake.message("m", "@node hi")])
    assert await bridge.scan() == 0
    kernel.join()
    unowned = nmc.MeshChatBridge(
        mesh, owner_id=lambda: None, history=history, runner=lambda *a: asyncio.sleep(0),
        state_path=tmp_path / "other.json",
    )
    assert await unowned.scan() == 0


async def test_phi_protection_leaves_requests_queued(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False, phi=True)
    kernel.add_chat("c", [fake.message("m", "@node hi")])
    assert await bridge.scan() == 0
    assert not kernel.appended and not bridge.state.claimed("m")


async def test_a_refusal_is_written_as_an_answer_not_a_success(tmp_path):
    async def runner(owner_id, local_chat, text):
        raise node_mesh.NodeMeshError("authority_missing_consent")

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    kernel.add_chat("c", [fake.message("m", "@node do it")])
    await bridge.start()
    await settle(lambda: "chat.turn_end" in kinds(kernel))
    assert kinds(kernel) == ["chat.turn_start", "chat.assistant_message", "chat.turn_end"]
    answer = bodies(kernel, "chat.assistant_message")[0]
    assert answer["refused"] == "authority_missing_consent" and "did not run" in answer["text"]
    await bridge.stop()


async def test_a_failing_turn_is_recorded_as_outcome_unknown(tmp_path):
    async def runner(owner_id, local_chat, text):
        raise RuntimeError("model down")

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    kernel.add_chat("c", [fake.message("m", "@node do it")])
    await bridge.start()
    await settle(lambda: "chat.turn_outcome_unknown" in kinds(kernel))
    assert bodies(kernel, "chat.turn_outcome_unknown")[0]["reason"] == "turn_failed"
    await bridge.stop()


async def test_a_turn_that_never_answers_times_out_as_outcome_unknown(tmp_path):
    async def runner(owner_id, local_chat, text):
        await asyncio.sleep(30)

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False, turn_seconds=0.1)
    kernel.add_chat("c", [fake.message("m", "@node slow")])
    await bridge.start()
    await settle(lambda: "chat.turn_outcome_unknown" in kinds(kernel))
    assert bodies(kernel, "chat.turn_outcome_unknown")[0]["reason"] == "turn_timeout"
    await bridge.stop()


async def test_a_runner_that_finishes_without_an_answer_is_outcome_unknown(tmp_path, monkeypatch):
    async def runner(owner_id, local_chat, text):
        return None

    monkeypatch.setattr(nmc, "TURN_SECONDS", 0.1)
    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    original = asyncio.wait_for

    async def quick(awaitable, timeout):
        return await original(awaitable, 0.1 if timeout == 5.0 else timeout)

    monkeypatch.setattr(nmc.asyncio, "wait_for", quick)
    kernel.add_chat("c", [fake.message("m", "@node silent")])
    await bridge.start()
    await settle(lambda: "chat.turn_outcome_unknown" in kinds(kernel))
    await bridge.stop()


async def test_a_turn_left_running_by_a_crash_is_closed_on_the_next_scan(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    kernel.add_chat("c", [], turns=[fake.turn("t-old", status="RUNNING", author="dev-node"), fake.turn("t-other", status="RUNNING", author="dev-phone")])
    await bridge.scan()
    assert bodies(kernel, "chat.turn_outcome_unknown") == [{"turn": "t-old"}]


async def test_a_failed_write_releases_the_request_for_a_retry(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    kernel.add_chat("c", [fake.message("m", "@node hi")])
    original = kernel.append_chat_events

    def failing(chat_id, events):
        raise node_mesh.NodeMeshError("kernel_busy")

    kernel.append_chat_events = failing
    await bridge.scan()
    await settle(lambda: "m" not in bridge._running)
    assert not bridge.state.claimed("m")
    kernel.append_chat_events = original


async def test_start_refuses_without_the_kernel_export(tmp_path, monkeypatch):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    monkeypatch.delattr(fake.Kernel, "append_chat_events")
    with pytest.raises(node_mesh.MeshUnavailable):
        await bridge.start()


async def test_chat_changes_wake_the_scan(tmp_path):
    ran = []

    async def runner(owner_id, local_chat, text):
        ran.append(text)
        nmc.observe_message(owner_id, local_chat, "assistant", "a")

    mesh, kernel, history, bridge = await build(tmp_path, runner, start=False)
    nmc.install(bridge)
    bridge._poll_seconds = 60
    await bridge.start()
    await asyncio.sleep(0.1)
    kernel.add_chat("c", [fake.message("m", "@node later")])
    kernel.notify()
    await settle(lambda: ran)
    await bridge.stop()


def test_chat_state_persists_and_bounds_claims(tmp_path, monkeypatch):
    path = tmp_path / "state" / "chats.json"
    state = nmc.ChatState(path)
    state.link("mesh-1", "local-1")
    state.mark_imported("mesh-1", ["a", "b", "a"])
    assert state.claim("m1", "t1") and not state.claim("m1", "t2")
    reloaded = nmc.ChatState(path)
    assert reloaded.local_for("mesh-1") == "local-1" and reloaded.mesh_for("local-1") == "mesh-1"
    assert reloaded.mesh_for("nope") is None and reloaded.local_for("nope") is None
    assert reloaded.imported("mesh-1") == {"a", "b"} and reloaded.claimed("m1")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(nmc, "MAX_CLAIMS", 2)
    for index in range(4):
        reloaded.claim(f"x{index}", "t")
    assert not reloaded.claimed("m1") and reloaded.claimed("x3")
    reloaded.release("x3")
    reloaded.release("x3")
    assert not reloaded.claimed("x3")


def test_chat_state_ignores_a_corrupt_file(tmp_path):
    path = tmp_path / "chats.json"
    path.write_text("{not json")
    assert nmc.ChatState(path).local_for("x") is None
    path.write_text("[]")
    assert nmc.ChatState(path).imported("x") == set()


def test_message_text_extraction():
    assert nmc.message_text("plain") == "plain"
    assert nmc.message_text([{"type": "card", "children": [{"type": "text", "content": "inner"}]}, {"type": "markdown", "content": "md"}]) == "inner\n\nmd"
    assert nmc.message_text({"type": "image"}) == ""
    deep = {"a": {}}
    node = deep
    for _ in range(20):
        node["a"] = {"a": {}}
        node = node["a"]
    assert nmc.message_text(deep) == ""


def test_policy_from_environment(monkeypatch):
    monkeypatch.setenv(nmc.REQUESTS_ENV, "ALL")
    assert nmc.RequestPolicy.from_env().mode == "all"
    monkeypatch.setenv(nmc.REQUESTS_ENV, "weird")
    assert nmc.RequestPolicy.from_env().mode == "addressed"


def test_module_observers_do_nothing_without_a_bridge():
    nmc.install(None)
    nmc.observe_message(OWNER, "c", "user", "x")
    nmc.observe_step(OWNER, "c", {})
    assert nmc.active() is None


async def test_protection_check_uses_the_phi_policy(monkeypatch):
    import personalization.phi_policy as policy

    monkeypatch.setattr(policy, "protection_enabled", lambda owner: owner == "protected")
    assert nmc._protection_enabled("protected") is True
    assert nmc._protection_enabled("other") is False


async def test_runner_uses_machine_authority_without_tool_scopes():
    from orchestrator.chain_authority import AuthoritySkip

    calls = {}

    class Orch:
        async def derive_machine_authority(self, **kwargs):
            calls["derive"] = kwargs
            return "authority"

        def _bind_machine_turn(self, vws, authority):
            calls["bound"] = authority

        def _unbind_machine_turn(self, vws):
            calls["unbound"] = True

        async def handle_chat_message(self, vws, text, chat_id, user_id=None):
            calls["handled"] = (text, chat_id, user_id)

    await nmc.orchestrator_runner(Orch())(OWNER, "local-1", "hello")
    assert calls["derive"] == {"user_id": OWNER, "agent_id": None, "turn_class": "mesh_request", "consented_scopes": []}
    assert calls["handled"] == ("hello", "local-1", OWNER) and calls["unbound"] and calls["bound"] == "authority"

    class Denied(Orch):
        async def derive_machine_authority(self, **kwargs):
            return AuthoritySkip("missing_consent", "no grant")

    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await nmc.orchestrator_runner(Denied())(OWNER, "local-1", "hello")
    assert refused.value.code == "authority_missing_consent"


async def test_start_for_wires_the_bridge_and_stop_all_tears_it_down(tmp_path, monkeypatch):
    from shared import node_identity as ni

    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh, "default_issuers", no_issuers)
    await nmc.start_for(SimpleNamespace(history=History()))
    assert nmc.active() is not None and node_mesh.current() is not None
    await nmc.stop_all()
    assert nmc.active() is None and node_mesh.current() is None


async def test_start_for_without_the_export_keeps_the_mesh_but_no_bridge(tmp_path, monkeypatch):
    from shared import node_identity as ni

    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh, "default_issuers", no_issuers)
    monkeypatch.delattr(fake.Kernel, "append_chat_events")
    await nmc.start_for(SimpleNamespace(history=History()))
    assert nmc.active() is None and node_mesh.current() is not None
    await nmc.stop_all()
    monkeypatch.delenv(ni.PROFILE_ENV)
    await nmc.start_for(SimpleNamespace(history=History()))
    assert node_mesh.current() is None
