"""Tests for the bridge between the node's own turn loop and the owner's mesh log: requests claimed and answered, tool calls with tickets, refusals with dotted codes, PHI fail-closed behavior, label computation, crash recovery, mirrored thin-client turns and the machine-authority runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from orchestrator import vessel_executor, node_mesh, node_mesh_calls as calls_module, node_mesh_chat as nmc
from tests import fake_astral_kernel as fake

OWNER = "oidc.abc.owner"
PHONE = "dev-phone"


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


DEFINITION = "sha256:" + "d" * 64


class FakeCalls:
    def __init__(self, *, scope="tools:read", outcome=None, resolve_error=None, authorize_error=None):
        self.resolved = calls_module.ResolvedTool("agent-1", "lookup", scope, DEFINITION)
        self.outcome = outcome or calls_module.CallOutcome("ok", "sunny")
        self.resolve_error = resolve_error
        self.authorize_error = authorize_error
        self.ran = []
        self.tickets = []
        self.delay = 0

    async def resolve(self, owner_id, tool):
        if self.resolve_error:
            raise calls_module.CallRefused(self.resolve_error)
        return self.resolved

    async def authorize(self, owner_id, resolved):
        if self.authorize_error:
            raise calls_module.CallRefused(self.authorize_error)
        return "authority"

    async def run(self, owner_id, local_chat, resolved, authority, arguments, ticket=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.ran.append((owner_id, local_chat, resolved, authority, arguments))
        self.tickets.append(ticket)
        return self.outcome


@pytest.fixture(autouse=True)
def clean():
    fake.Kernel.instances.clear()
    yield
    nmc.install(None)
    node_mesh.install(None)


async def build(tmp_path, runner=None, *, policy=None, phi=False, start=True, turn_seconds=5.0, owner=OWNER, joined=True, calls=None, phi_member=False):
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, issuers=no_issuers, mailbox=StubMailbox())
    await mesh.start()
    kernel = fake.Kernel.instances[-1]
    kernel.phi_member = phi_member
    if joined:
        kernel.join()
    history = History()
    bridge = nmc.MeshChatBridge(
        mesh,
        owner_id=owner,
        history=history,
        runner=runner or (lambda *args: asyncio.sleep(0)),
        calls=calls,
        state_path=tmp_path / "mesh-chats.json",
        policy=policy or nmc.NodePolicy(),
        phi_active=lambda owner_id: phi,
        poll_seconds=0.05,
        turn_seconds=turn_seconds,
    )
    nmc.install(bridge)
    if start:
        await bridge.start()
    return mesh, kernel, history, bridge


async def settle(predicate, attempts=200):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def answering(text="the answer", steps=()):
    async def runner(owner_id, local_chat, prompt):
        for step in steps:
            nmc.observe_step(owner_id, local_chat, step)
        nmc.observe_message(owner_id, local_chat, "assistant", text)

    return runner


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_recorded_arguments_keep_non_ascii_paths_in_canonical_utf8():
    assert nmc._arguments({"path": "notes/café.md"}) == '{"path":"notes/café.md"}'


def call_for(*, tool="lookup", arguments=None, device=PHONE, digest=None, action_tool=None, turn="turn-9", tool_def=None):
    text = canonical(arguments if arguments is not None else {"q": "weather"})
    action = {
        "v": 1, "mesh": "mesh-1", "device": device, "turn": turn, "agent": "kernel", "tool": action_tool or tool,
        "tool_def": DEFINITION if tool_def is None else tool_def,
        "args_digest": digest or "sha256:" + hashlib.sha256(text.encode()).hexdigest(), "influence": [],
    }
    return fake.PeerCall(call_id="call-1", tool=tool, arguments_json=text, action_json=canonical(action), ticket_json="{}")


def only_turn(kernel):
    assert len(kernel.recorded) == 1
    return kernel.recorded[0]


async def serve(bridge, kernel, request_id):
    assert await bridge.scan() == 1
    await settle(lambda: request_id not in bridge._running)


async def test_a_thin_client_turn_is_recorded_into_the_owners_chat(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "What is the weather?\nplease")
    nmc.observe_step(OWNER, "local-a", {"name": "weather", "args": '{"q":1}', "status": "completed", "result": "sunny", "error": None})
    nmc.observe_step(OWNER, "local-a", {"name": "lookup", "args": None, "status": "errored", "result": None, "error": "denied"})
    nmc.observe_step(OWNER, "local-a", {"name": "raw", "args": "not json", "status": "completed", "result": {"k": 1}, "error": None})
    nmc.observe_message(OWNER, "local-a", "assistant", [{"type": "text", "content": "It is sunny."}])
    await settle(lambda: kernel.recorded)
    chat, title = kernel.created[0]
    assert title == "What is the weather?"
    assert kernel.sent[0][:3] == (chat, "What is the weather?\nplease", None)
    recorded_chat, turn = only_turn(kernel)
    assert recorded_chat == chat and turn.message == kernel.sent[0][3] and turn.answers is None and turn.continues is None
    assert (turn.provider, turn.reason, turn.text) == ("node", "completed", "It is sunny.")
    assert turn.labels == fake.peer_labels("PRIVATE", "TOOL_TRUSTED")
    first, second, third = turn.steps
    assert (first.tool, first.arguments_json, first.status, first.content, first.code) == ("weather", '{"q":1}', "ok", "sunny", None)
    assert first.tool_def.startswith("sha256:") and first.call_id == "node-step-1"
    assert (second.arguments_json, second.status, second.content, second.code) == ("{}", "error", "denied", "node.tool_failed")
    assert (third.arguments_json, third.content) == ('{"text":"not json"}', '{"k": 1}')
    assert bridge.state.local_for(chat) == "local-a"
    await bridge.stop()


async def test_the_second_turn_reuses_the_chat_and_continues_the_first(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    for text in ("one", "two"):
        nmc.observe_message(OWNER, "local-a", "user", text)
        nmc.observe_message(OWNER, "local-a", "assistant", f"re {text}")
        await settle(lambda: len(kernel.recorded) == (1 if text == "one" else 2))
    assert len(kernel.created) == 1
    first = kernel.recorded[0][1]
    assert kernel.recorded[1][1].continues == kernel.chats[kernel.created[0][0]].turns[0].id
    assert first.continues is None
    await bridge.stop()


async def test_other_owners_and_stray_events_are_ignored(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message("someone.else", "local-x", "user", "hi")
    nmc.observe_message("someone.else", "local-x", "assistant", "yo")
    nmc.observe_message(OWNER, "local-y", "assistant", "orphan answer")
    nmc.observe_message(OWNER, "local-z", "user", "blank")
    nmc.observe_message(OWNER, "local-z", "assistant", "   ")
    nmc.observe_step(OWNER, "local-unknown", {"name": "x"})
    await asyncio.sleep(0.2)
    assert not kernel.recorded and not kernel.created
    await bridge.stop()


async def test_phi_protection_blocks_mirroring(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, phi=True)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "secret thing")
    nmc.observe_message(OWNER, "local-a", "assistant", "reply")
    await asyncio.sleep(0.2)
    assert not kernel.recorded and not kernel.created
    await bridge.stop()


async def test_step_overflow_is_bounded(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    nmc.observe_message(OWNER, "local-a", "user", "many")
    for index in range(nmc.MAX_STEPS + 40):
        nmc.observe_step(OWNER, "local-a", {"name": f"t{index}", "args": "{}", "status": "completed", "result": "r", "error": None})
    nmc.observe_message(OWNER, "local-a", "assistant", "done")
    await settle(lambda: kernel.recorded)
    assert len(only_turn(kernel)[1].steps) == nmc.MAX_STEPS
    await bridge.stop()


async def test_a_request_is_claimed_run_and_answered_with_a_recorded_turn(tmp_path):
    prompts = []

    async def runner(owner_id, local_chat, prompt):
        prompts.append((owner_id, local_chat, prompt))
        nmc.observe_message(owner_id, local_chat, "user", prompt)
        nmc.observe_step(owner_id, local_chat, {"name": "search", "args": '{"q":"x"}', "status": "completed", "result": "hit", "error": None})
        nmc.observe_message(owner_id, local_chat, "assistant", "summary from the node")

    mesh, kernel, history, bridge = await build(tmp_path, runner)
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m1", "earlier question"), fake.message("m2", "earlier answer", role="ASSISTANT", author="dev-x")])
    kernel.add_chat("c", kernel.chats["c"].messages)
    kernel.add_request("r1", "c", "summarize the thread", continues="turn-0")
    kernel.chats["c"].turns.append(SimpleNamespace(id="turn-0", status=SimpleNamespace(name="COMPLETE"), author=PHONE, branch=None))
    await serve(bridge, kernel, "r1")
    assert prompts == [(OWNER, "local-1", "summarize the thread")]
    assert [(role, text) for _, role, text, _ in history.added] == [("user", "earlier question"), ("assistant", "earlier answer")]
    view = kernel.request("r1")
    assert view.state == fake.RequestState.ANSWERED and view.turn == kernel.chats["c"].turns[-1].id
    chat, turn = only_turn(kernel)
    assert (chat, turn.answers, turn.continues, turn.message, turn.text) == ("c", "r1", "turn-0", None, "summary from the node")
    assert turn.labels == fake.peer_labels("PRIVATE", "TOOL_TRUSTED")
    assert turn.steps[0].tool == "search" and turn.steps[0].content == "hit"
    assert "asst-" + view.turn in bridge.state.imported("c")
    assert kernel.claimed == ["r1"] and not kernel.refused
    assert await bridge.scan() == 0
    await bridge.stop()


async def test_a_second_request_in_the_chat_does_not_reimport_history(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m1", "earlier")])
    for name in ("r1", "r2"):
        kernel.add_request(name, "c")
        await serve(bridge, kernel, name)
    assert [text for _, _, text, _ in history.added] == ["earlier"]
    assert len(kernel.recorded) == 2 and len(history.created) == 1
    await bridge.stop()


async def test_request_labels_raise_the_labels_of_the_answer(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m1", "from a tool", integrity="untrusted")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PRIVATE", "UNTRUSTED"))
    await serve(bridge, kernel, "r1")
    assert only_turn(kernel)[1].labels == fake.peer_labels("PRIVATE", "UNTRUSTED")
    await bridge.stop()


@pytest.mark.parametrize("source", ["request", "chat", "owner"])
async def test_phi_work_is_refused_by_a_node_that_is_not_approved(tmp_path, source):
    ran = []

    async def runner(*args):
        ran.append(args)

    mesh, kernel, history, bridge = await build(tmp_path, runner, phi=source == "owner")
    kernel.add_chat("c", [fake.message("m1", "chart note", confidentiality="phi" if source == "chat" else "private")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI" if source == "request" else "PRIVATE", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")] and not ran and not kernel.claimed and not kernel.recorded
    assert history.created == []
    await bridge.stop()


@pytest.mark.parametrize("source", ["request", "chat", "owner"])
async def test_an_approved_node_runs_phi_work_and_labels_the_answer_phi(tmp_path, source):
    mesh, kernel, history, bridge = await build(
        tmp_path, answering(steps=[{"name": "t", "args": "{}", "status": "completed", "result": "r", "error": None}]),
        phi=source == "owner", phi_member=True,
    )
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m1", "chart note", confidentiality="phi" if source == "chat" else "private")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI" if source == "request" else "PRIVATE", "OWNER"))
    await serve(bridge, kernel, "r1")
    turn = only_turn(kernel)[1]
    assert turn.labels.confidentiality == fake.PeerConfidentiality.PHI
    assert turn.steps[0].labels.confidentiality == fake.PeerConfidentiality.PHI
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_phi_history_is_not_imported_by_an_unapproved_node_and_secret_is_never_imported(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    nmc.install(bridge)
    kernel.add_chat("c", [fake.message("m1", "plain"), fake.message("m2", "phi text", confidentiality="phi")])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")]
    assert nmc._importable(fake.message("m3", "x", confidentiality="phi"), True) is True
    assert nmc._importable(fake.message("m4", "x", confidentiality="secret"), True) is False
    assert nmc._importable(fake.message("m5", "x", confidentiality="phi"), False) is False
    await bridge.stop()


async def test_a_secret_request_is_refused(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering(), phi_member=True)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", labels=fake.peer_labels("SECRET", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.secret_label")] and not kernel.claimed
    await bridge.stop()


async def test_a_request_without_an_author_is_refused(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", author=None)
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "request.malformed")]
    await bridge.stop()


@pytest.mark.parametrize(
    "failure,code",
    [
        (lambda: node_mesh.NodeMeshError("authority_missing_consent"), "node.authority_missing_consent"),
        (lambda: RuntimeError("boom"), "node.turn_failed"),
        (lambda: asyncio.TimeoutError(), "node.turn_timeout"),
    ],
)
async def test_a_turn_that_cannot_run_is_refused_with_a_dotted_code(tmp_path, failure, code):
    async def runner(*args):
        raise failure()

    mesh, kernel, history, bridge = await build(tmp_path, runner)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", code)] and kernel.claimed == ["r1"] and not kernel.recorded
    assert kernel.request("r1").state == fake.RequestState.REFUSED
    await bridge.stop()


async def test_a_turn_that_never_finishes_times_out(tmp_path):
    async def runner(*args):
        await asyncio.sleep(30)

    mesh, kernel, history, bridge = await build(tmp_path, runner, turn_seconds=0.1)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.turn_timeout")]
    assert not bridge._sessions
    await bridge.stop()


async def test_a_turn_that_ends_without_an_answer_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(nmc, "ANSWER_GRACE_SECONDS", 0.05)
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.no_answer")]
    await bridge.stop()


async def test_the_kernel_refusing_the_recorded_labels_refuses_the_request(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    kernel.add_chat("c", [])
    kernel.floor = ("PHI", "OWNER")
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "peer_turn.labels_too_low")] and not kernel.recorded
    await bridge.stop()


async def test_a_request_cancelled_before_the_claim_is_left_alone(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c")
    original = kernel.claim_request

    def cancelled(request_id):
        request.state = fake.RequestState.CANCELLED
        return original(request_id)

    kernel.claim_request = cancelled
    await serve(bridge, kernel, "r1")
    assert not kernel.refused and not kernel.recorded
    await bridge.stop()


async def test_a_refusal_the_kernel_will_not_take_is_only_logged(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering(), phi=True)
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c")
    request.state = fake.RequestState.CLAIMED
    request.claim = "x"
    await bridge._refuse(request, "node.late")
    request.state = fake.RequestState.ANSWERED
    await bridge._refuse(request, "node.late")
    assert kernel.refused == [("r1", "node.late")]
    await bridge.stop()


async def test_a_request_claimed_before_a_crash_is_refused_once_on_the_first_scan(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    kernel.add_chat("c", [])
    mine = kernel.add_request("r1", "c", state="CLAIMED")
    kernel.add_request("r2", "c", state="CLAIMED", to="dev-other")
    kernel.add_request("r3", "c", state="ANSWERED")
    await bridge.scan()
    assert kernel.refused == [("r1", "node.interrupted")] and mine.state == fake.RequestState.REFUSED
    await bridge.scan()
    assert kernel.refused == [("r1", "node.interrupted")]
    await bridge.stop()


async def test_policy_off_and_unjoined_nodes_serve_nothing(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering(), policy=nmc.NodePolicy(serve_requests=False))
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    assert await bridge.scan() == 0 and kernel.request("r1").state == fake.RequestState.PENDING
    await bridge.stop()
    mesh, kernel, history, bridge = await build(tmp_path / "b", answering(), joined=False)
    assert await bridge.scan() == 0
    await bridge.stop()
    mesh, kernel, history, bridge = await build(tmp_path / "c", answering(), owner=None)
    kernel.add_request("r1", "c")
    assert await bridge.scan() == 0
    await bridge.stop()


async def test_a_pending_request_is_started_once_while_it_runs(tmp_path):
    release = asyncio.Event()

    async def runner(owner_id, local_chat, prompt):
        await release.wait()
        nmc.observe_message(owner_id, local_chat, "assistant", "late")

    mesh, kernel, history, bridge = await build(tmp_path, runner)
    nmc.install(bridge)
    kernel.add_chat("c", [])
    view = kernel.add_request("r1", "c")
    kernel.pending_requests_for_me = lambda: [view]
    assert await bridge.scan() == 1
    await settle(lambda: kernel.claimed)
    assert await bridge.scan() == 0
    release.set()
    await settle(lambda: kernel.recorded)
    await bridge.stop()


async def test_a_failure_before_any_write_leaves_the_request_pending_for_a_retry(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    original = kernel.chat

    def broken(chat_id):
        raise RuntimeError("store busy")

    kernel.chat = broken
    await serve(bridge, kernel, "r1")
    assert kernel.request("r1").state == fake.RequestState.PENDING
    kernel.chat = original
    await serve(bridge, kernel, "r1")
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_a_tool_call_is_verified_run_through_the_nodes_gates_and_recorded(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    call = call_for()
    kernel.add_request("r1", "c", "look it up", call=call, continues="turn-0")
    kernel.chats["c"].turns.append(SimpleNamespace(id="turn-0", status=SimpleNamespace(name="COMPLETE"), author=PHONE, branch=None))
    await serve(bridge, kernel, "r1")
    assert kernel.verified == [("{}", call.action_json, "outward_read")]
    assert calls.ran == [(OWNER, "local-1", calls.resolved, "authority", {"q": "weather"})]
    assert calls.tickets == [vessel_executor.CallTicket("{}", call.action_json, call.arguments_json)]
    chat, turn = only_turn(kernel)
    assert (turn.answers, turn.continues, turn.text, turn.reason) == ("r1", "turn-0", "", "completed")
    step = turn.steps[0]
    assert (step.call_id, step.tool, step.arguments_json, step.tool_def, step.status, step.content, step.code) == (
        "call-1", "lookup", call.arguments_json, "sha256:" + "d" * 64, "ok", "sunny", None,
    )
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_a_node_run_turn_mints_its_tool_and_records_the_real_receipt(tmp_path, monkeypatch):
    import httpx

    minted = []
    sent = []

    async def mint(request_id, **call):
        minted.append((request_id, call))
        action = json.loads(call_for(arguments={"q": "weather"}, device="dev-node", turn=request_id).action_json)
        return SimpleNamespace(ticket_json='{"signed":true}', action_json=canonical(action), receipt_event="receipt-1")

    def deliver(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"output": "sunny", "error": False})

    executor = vessel_executor.VesselExecutor(
        vessel_executor.ExecutorSettings("http://executor:8800", "catalog", agent_tokens={"agent-1": "agent"}),
        httpx.AsyncClient(transport=httpx.MockTransport(deliver)),
    )

    async def runner(owner, local, prompt):
        response = await executor.invoke("agent-1", "lookup", {"q": "weather"}, owner=owner)
        nmc.observe_step(owner, local, {"name": "agent-1__lookup", "args": '{"q":"weather"}', "status": "completed", "result": response.result})
        nmc.observe_message(owner, local, "assistant", "It is sunny.")

    mesh, kernel, history, bridge = await build(tmp_path, runner, calls=FakeCalls(scope="tools:write"))
    monkeypatch.setattr(mesh, "mint_ticket", mint, raising=False)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert len(sent) == 1 and sent[0]["ticket_json"] == '{"signed":true}'
    request_id, call = minted[0]
    assert request_id == "r1" and call["tool"] == "lookup" and call["tool_def"] == DEFINITION
    assert call["effect_kind"] == "outside_effect" and call["arguments_json"] == '{"q":"weather"}'
    step = only_turn(kernel)[1].steps[0]
    assert step.receipt == "receipt-1" and step.tool_def == DEFINITION and step.call_id == call["call_id"]
    assert step.tool == "lookup" and step.content == "sunny"
    assert vessel_executor.current_ticket() is None
    await bridge.stop()
    await executor.close()


async def test_a_node_mint_refusal_prevents_the_executor_request(tmp_path, monkeypatch):
    import httpx

    sent = []
    responses = []

    async def mint(*args, **kwargs):
        raise node_mesh.NodeMeshError("consent.required")

    def deliver(request):
        sent.append(request)
        return httpx.Response(200, json={"output": "must not run"})

    executor = vessel_executor.VesselExecutor(
        vessel_executor.ExecutorSettings("http://executor:8800", "catalog", agent_tokens={"agent-1": "agent"}),
        httpx.AsyncClient(transport=httpx.MockTransport(deliver)),
    )

    async def runner(owner, local, prompt):
        responses.append(await executor.invoke("agent-1", "lookup", {}, owner=owner))
        nmc.observe_message(owner, local, "assistant", "Consent is required.")

    mesh, kernel, history, bridge = await build(tmp_path, runner, calls=FakeCalls())
    monkeypatch.setattr(mesh, "mint_ticket", mint, raising=False)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert responses and responses[0].error["code"] == "consent.required" and not sent
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()
    await executor.close()


@pytest.mark.parametrize("case,code", [("owner", "node.owner_mismatch"), ("tool", "request.call_mismatch"), ("limit", "mint.limit")])
async def test_node_mint_context_refuses_a_foreign_owner_tool_or_excess_call(tmp_path, monkeypatch, case, code):
    import httpx

    results = []
    issued = []
    calls = FakeCalls()
    if case == "tool":
        calls.resolved = calls_module.ResolvedTool("different", "lookup", "tools:read", DEFINITION)
    monkeypatch.setattr(nmc, "MAX_STEPS", 1)

    async def mint(request, **kwargs):
        issued.append(kwargs)
        ticket = call_for(arguments={}, device="dev-node", turn=request)
        return SimpleNamespace(ticket_json=ticket.ticket_json, action_json=ticket.action_json, receipt_event=None)

    executor = vessel_executor.VesselExecutor(
        vessel_executor.ExecutorSettings("http://executor:8800", "catalog", agent_tokens={"agent-1": "agent"}),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"output": "done"}))),
    )

    async def runner(owner, local, prompt):
        if case == "limit":
            assert (await executor.invoke("agent-1", "lookup", {}, owner=owner)).result == "done"
        results.append(await executor.invoke("agent-1", "lookup", {}, owner="other" if case == "owner" else owner))
        nmc.observe_message(owner, local, "assistant", "Refused.")

    mesh, kernel, history, bridge = await build(tmp_path, runner, calls=calls)
    monkeypatch.setattr(mesh, "mint_ticket", mint)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert results and results[0].error["code"] == code
    assert len(issued) == (1 if case == "limit" else 0)
    await bridge.stop()
    await executor.close()


async def test_unrelated_tool_steps_never_inherit_a_minted_calls_receipt(tmp_path, monkeypatch):
    mesh, kernel, history, bridge = await build(tmp_path, answering())
    session = nmc._Session(OWNER, "local")
    session.minted.append({
        "call_id": "call", "tool": "lookup", "qualified": "agent-1__lookup", "arguments_json": '{"q":"x"}',
        "tool_def": DEFINITION, "receipt": "receipt-1", "labels": ("private", "untrusted"),
    })
    bridge._sessions["local"] = session
    for name, arguments in [("other", '{"q":"x"}'), ("lookup", '{"q":"y"}'), ("lookup", '{"q":"x"}'), ("lookup", '{"q":"x"}')]:
        bridge._handle_step(OWNER, "local", {"name": name, "args": arguments, "status": "completed", "result": "done"})
    recorded = nmc._step_records(session.steps, ("private", "tool_trusted"))
    assert [s.receipt for s in recorded] == [None, None, "receipt-1", None]
    await bridge.stop()


@pytest.mark.parametrize("cap,count,receipts,errors", [(1, 1, ["receipt-1"], ["mint.limit"]), (64, 2, ["receipt-2", "receipt-1"], [])])
async def test_parallel_node_calls_get_distinct_ids_and_keep_their_exact_receipts(tmp_path, monkeypatch, cap, count, receipts, errors):
    import httpx

    issued = []
    responses = []
    monkeypatch.setattr(nmc, "MAX_STEPS", cap)

    async def mint(request, **call):
        await asyncio.sleep(0)
        issued.append(call)
        arguments = json.loads(call["arguments_json"])
        ticket = call_for(arguments=arguments, device="dev-node", turn=request)
        return SimpleNamespace(ticket_json=ticket.ticket_json, action_json=ticket.action_json, receipt_event=f"receipt-{arguments['q']}")

    executor = vessel_executor.VesselExecutor(
        vessel_executor.ExecutorSettings("http://executor:8800", "catalog", agent_tokens={"agent-1": "agent"}),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"output": "done"}))),
    )

    async def runner(owner, local, prompt):
        responses.extend(await asyncio.gather(*(executor.invoke("agent-1", "lookup", {"q": n}, owner=owner) for n in [1, 2])))
        for n, response in reversed(list(zip([1, 2], responses))):
            if response.error is not None:
                continue
            nmc.observe_step(owner, local, {"name": "agent-1__lookup", "args": canonical({"q": n}), "status": "completed", "result": "done"})
        nmc.observe_message(owner, local, "assistant", "Both done.")

    mesh, kernel, history, bridge = await build(tmp_path, runner, calls=FakeCalls())
    monkeypatch.setattr(mesh, "mint_ticket", mint)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert len(issued) == count and len({call["call_id"] for call in issued}) == count
    assert [response.error["code"] for response in responses if response.error is not None] == errors
    assert sum(response.result == "done" for response in responses) == count
    steps = only_turn(kernel)[1].steps
    assert [step.receipt for step in steps] == receipts
    assert len({step.call_id for step in steps}) == count
    await bridge.stop()
    await executor.close()


async def test_a_write_tool_is_verified_as_an_outside_effect(tmp_path):
    calls = FakeCalls(scope="tools:write")
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.verified[0][2] == "outside_effect"
    await bridge.stop()


async def test_a_gate_refusal_is_recorded_as_an_error_step(tmp_path):
    calls = FakeCalls(outcome=calls_module.CallOutcome("error", "tool disabled", "tool.disabled"))
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    step = only_turn(kernel)[1].steps[0]
    assert (step.status, step.content, step.code) == ("error", "tool disabled", "tool.disabled")
    await bridge.stop()


@pytest.mark.parametrize(
    "call,code",
    [
        (lambda: call_for(action_tool="other"), "request.call_mismatch"),
        (lambda: call_for(digest="sha256:" + "0" * 64), "request.call_mismatch"),
        (lambda: call_for(device="dev-other"), "ticket.device_mismatch"),
        (lambda: call_for(tool_def="sha256:" + "e" * 64), "gate.tool_def_changed"),
        (lambda: call_for(tool_def=""), "gate.tool_def_changed"),
        (lambda: fake.PeerCall("call-1", "lookup", "[]", "{}", "{}"), "request.malformed"),
        (lambda: fake.PeerCall("call-1", "lookup", "{}", "[]", "{}"), "request.malformed"),
        (lambda: fake.PeerCall("call-1", "lookup", "{not json", "{}", "{}"), "request.malformed"),
    ],
)
async def test_a_call_that_does_not_match_its_action_is_refused_before_any_work(tmp_path, call, code):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", code)] and not calls.ran and not kernel.claimed and not kernel.recorded
    await bridge.stop()


@pytest.mark.parametrize("error", ["ticket.bad_root", "ticket.stopped", "ticket.consent_required", "ticket.device_removed"])
async def test_a_forged_or_refused_ticket_is_refused_with_the_kernel_code_and_nothing_runs(tmp_path, error):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.ticket_error = error
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", error)] and not calls.ran and not kernel.claimed and not kernel.recorded
    await bridge.stop()


async def test_a_ticket_for_another_device_or_turn_is_refused(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    original = kernel.verify_ticket

    def other_device(ticket, action, effect):
        verified = original(ticket, action, effect)
        verified.device = "dev-intruder"
        return verified

    kernel.verify_ticket = other_device
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "ticket.unauthorized")] and not calls.ran
    await bridge.stop()


@pytest.mark.parametrize("error", ["node.tool_unknown", "node.tool_ambiguous"])
async def test_an_unresolvable_tool_is_refused(tmp_path, error):
    calls = FakeCalls(resolve_error=error)
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", error)] and not kernel.verified
    await bridge.stop()


async def test_a_call_without_machine_authority_is_refused(tmp_path):
    calls = FakeCalls(authorize_error="node.authority_missing_consent")
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.authority_missing_consent")] and not calls.ran and not kernel.claimed
    await bridge.stop()


async def test_a_node_without_a_call_runner_refuses_calls(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.calls_unavailable")]
    await bridge.stop()


async def test_phi_labels_on_a_ticket_need_an_approved_node(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.ticket_labels = fake.peer_labels("PHI", "TOOL_TRUSTED")
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")] and not calls.ran and not kernel.claimed
    await bridge.stop()
    mesh, kernel, history, bridge = await build(tmp_path / "ok", calls=calls, phi_member=True)
    kernel.add_chat("c", [])
    kernel.ticket_labels = fake.peer_labels("PHI", "TOOL_TRUSTED")
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    turn = only_turn(kernel)[1]
    assert turn.labels.confidentiality == fake.PeerConfidentiality.PHI and turn.steps[0].labels.confidentiality == fake.PeerConfidentiality.PHI
    await bridge.stop()


async def test_a_tool_call_that_overruns_is_refused_as_a_timeout(tmp_path):
    calls = FakeCalls()
    calls.delay = 30
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls, turn_seconds=0.1)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.turn_timeout")]
    await bridge.stop()


async def test_start_refuses_without_the_kernel_export(tmp_path, monkeypatch):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    monkeypatch.delattr(fake.Kernel, "record_peer_turn")
    with pytest.raises(node_mesh.MeshUnavailable):
        await bridge.start()


async def test_chat_changes_wake_the_scan(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, answering(), start=False)
    nmc.install(bridge)
    bridge._poll_seconds = 60
    await bridge.start()
    await asyncio.sleep(0.1)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    kernel.notify()
    await settle(lambda: kernel.recorded)
    await bridge.stop()


def test_chat_state_persists_links_and_imports(tmp_path):
    path = tmp_path / "state" / "chats.json"
    state = nmc.ChatState(path)
    state.link("mesh-1", "local-1")
    state.mark_imported("mesh-1", ["a", "b", "a"])
    reloaded = nmc.ChatState(path)
    assert reloaded.local_for("mesh-1") == "local-1" and reloaded.mesh_for("local-1") == "mesh-1"
    assert reloaded.mesh_for("nope") is None and reloaded.local_for("nope") is None
    assert reloaded.imported("mesh-1") == {"a", "b"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


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


def test_policy_from_environment():
    assert nmc.NodePolicy.from_env({}) == nmc.NodePolicy(serve_requests=True)
    assert nmc.NodePolicy.from_env({nmc.REQUESTS_ENV: "OFF"}) == nmc.NodePolicy(serve_requests=False)


def test_labels_join_to_the_most_restrictive_and_unknown_values_fail_closed():
    assert nmc.join_labels(("public", "owner"), ("phi", "tool_trusted"), ("private", "untrusted")) == ("phi", "untrusted")
    source = type("L", (), {"confidentiality": "Strange", "integrity": "odd"})()
    assert nmc.labels_of(source) == ("secret", "untrusted")
    assert nmc.labels_of(fake.peer_labels("PHI", "OWNER")) == ("phi", "owner")


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
    assert nmc.active() is not None and node_mesh.current() is not None and nmc.active()._calls is not None
    await nmc.stop_all()
    assert nmc.active() is None and node_mesh.current() is None


async def test_start_for_without_the_export_keeps_the_mesh_but_no_bridge(tmp_path, monkeypatch):
    from shared import node_identity as ni

    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh, "default_issuers", no_issuers)
    monkeypatch.delattr(fake.Kernel, "record_peer_turn")
    await nmc.start_for(SimpleNamespace(history=History()))
    assert nmc.active() is None and node_mesh.current() is not None
    await nmc.stop_all()
    monkeypatch.delenv(ni.PROFILE_ENV)
    await nmc.start_for(SimpleNamespace(history=History()))
    assert node_mesh.current() is None


TOOLS = [{"name": "lookup", "description": "Looks up", "input_schema": {"type": "object"}, "annotations": {"agent": "guide"}, "effect": "outward_read"}]


class CatalogCalls(FakeCalls):
    def __init__(self, tools):
        super().__init__()
        self.tools = tools
        self.owners = []

    def published_tools(self, owner_id):
        self.owners.append(owner_id)
        return self.tools


async def test_the_vessel_tools_are_published_with_their_agent_annotation_once(tmp_path):
    mesh, kernel, _, bridge = await build(tmp_path, calls=CatalogCalls(TOOLS), start=False)
    await bridge.scan()
    await bridge.scan()
    assert len(kernel.catalogs) == 1
    assert bridge._calls.owners == [OWNER, OWNER]
    spec = kernel.catalogs[0][0]
    assert (spec.name, json.loads(spec.annotations_json), spec.effect.name, spec.confidentiality.name) == (
        "lookup", {"agent": "guide"}, "OUTWARD_READ", "PRIVATE")
    assert json.loads(spec.input_schema_json) == {"type": "object"}


async def test_a_changed_tool_list_is_published_again(tmp_path):
    calls = CatalogCalls(TOOLS)
    mesh, kernel, _, bridge = await build(tmp_path, calls=calls, start=False)
    await bridge.scan()
    calls.tools = [{**TOOLS[0], "effect": "outside_effect"}]
    await bridge.scan()
    assert len(kernel.catalogs) == 2


async def test_nothing_is_published_without_vessel_tools_or_a_mesh(tmp_path):
    mesh, kernel, _, bridge = await build(tmp_path, calls=CatalogCalls([]), start=False)
    await bridge.scan()
    assert kernel.catalogs == []
    mesh, kernel, _, bridge = await build(tmp_path / "b", calls=CatalogCalls(TOOLS), joined=False, start=False)
    await bridge.scan()
    assert kernel.catalogs == []
    mesh, kernel, _, bridge = await build(tmp_path / "c", calls=FakeCalls(), start=False)
    await bridge.scan()
    assert kernel.catalogs == []


async def test_a_refused_publish_is_retried_on_the_next_scan(tmp_path):
    mesh, kernel, _, bridge = await build(tmp_path, calls=CatalogCalls(TOOLS), start=False)
    kernel.catalog_error = "tools.malformed"
    await bridge.scan()
    assert kernel.catalogs == []
    kernel.catalog_error = None
    await bridge.scan()
    assert len(kernel.catalogs) == 1


async def test_a_kernel_without_catalog_publishing_is_reported_not_faked(tmp_path, monkeypatch):
    mesh, kernel, _, bridge = await build(tmp_path, calls=CatalogCalls(TOOLS), start=False)
    monkeypatch.delattr(fake.Kernel, "publish_tool_catalog")
    await bridge.scan()
    assert bridge._published is None
    with pytest.raises(node_mesh.MeshUnavailable) as caught:
        await mesh.publish_tool_catalog(TOOLS)
    assert caught.value.code == "catalog_unsupported"
