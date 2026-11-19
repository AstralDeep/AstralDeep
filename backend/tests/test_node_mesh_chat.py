"""Tests for the bridge between the node and the owner's mesh log: Kernel-answered text requests, ticketed tool calls with tickets and refusals, PHI fail-closed behavior, label computation and crash recovery."""

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
        self.listed = self.resolved
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

    def catalog(self, owner_id):
        tool = self.listed
        return {(tool.agent_id, tool.tool): calls_module.CatalogTool(tool.definition, tool.effect_kind)}

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


class KernelAnswers:
    def __init__(self, kernel, mesh):
        self.kernel = kernel
        self.answered = []
        self.contexts = []
        self.interrupts = []
        self.interrupt_script = [True]
        self.context_error = None
        self.error = None
        self.state_on_error = None
        self.hold = None
        self.released = asyncio.Event()
        kernel.supports_node_request_tools = lambda: True
        kernel.answer_request = self.answer
        kernel.interrupt_request = self.interrupt
        mesh.personal_request_context = self.context

    async def context(self, reference, owner, kernel):
        self.contexts.append((reference.id, owner, kernel is self.kernel))
        if self.context_error is not None:
            raise self.context_error

    async def answer(self, request_id):
        self.answered.append(request_id)
        if self.hold is not None:
            await self.released.wait()
        view = self.kernel.request(request_id)
        if self.error is not None:
            if self.state_on_error is not None:
                view.state = self.state_on_error
            raise self.error
        view.state = fake.RequestState.ANSWERED
        return SimpleNamespace(status="completed")

    async def interrupt(self, request_id):
        self.interrupts.append(request_id)
        step = self.interrupt_script.pop(0) if len(self.interrupt_script) > 1 else self.interrupt_script[0]
        if isinstance(step, Exception):
            raise step
        if step is True:
            self.released.set()
        return step


async def build(tmp_path, *, policy=None, phi=False, start=True, turn_seconds=5.0, owner=OWNER, joined=True, calls=None, phi_member=False):
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, mailbox=StubMailbox())
    await mesh.start()
    kernel = fake.Kernel.instances[-1]
    kernel.phi_member = phi_member
    if joined:
        kernel.join()
    kernel.answers = KernelAnswers(kernel, mesh)
    history = History()
    bridge = nmc.MeshChatBridge(
        mesh,
        owner_id=owner,
        history=history,
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


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
    await bridge.scan()
    await settle(lambda: request_id not in bridge._running)


async def test_a_text_request_is_answered_by_the_kernel_engine_and_the_bridge_writes_nothing(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [fake.message("m1", "earlier question")])
    kernel.add_request("r1", "c", "summarize the thread")
    await serve(bridge, kernel, "r1")
    assert kernel.answers.answered == ["r1"] and kernel.answers.contexts == [("r1", OWNER, True)]
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    assert not kernel.claimed and not kernel.refused and not kernel.recorded
    assert history.created == [] and history.added == []
    assert await bridge.scan() == 0
    await bridge.stop()


async def test_a_pending_text_request_is_started_once_while_the_kernel_answers_it(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    kernel.answers.hold = True
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    assert await bridge.scan() == 1
    await settle(lambda: kernel.answers.answered)
    assert await bridge.scan() == 0
    kernel.answers.released.set()
    await settle(lambda: "r1" not in bridge._running)
    assert kernel.answers.answered == ["r1"]
    await bridge.stop()


async def test_the_admission_check_sees_the_joined_request_and_chat_labels(tmp_path):
    seen = []

    async def admission(request, floor):
        seen.append((request.id, floor))

    mesh, kernel, history, bridge = await build(tmp_path)
    bridge._admission = admission
    kernel.add_chat("c", [fake.message("m1", "from a tool", integrity="untrusted")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PRIVATE", "TOOL_TRUSTED"))
    await serve(bridge, kernel, "r1")
    assert seen == [("r1", ("private", "untrusted"))]
    assert kernel.answers.answered == ["r1"]
    await bridge.stop()


@pytest.mark.parametrize("source", ["request", "chat", "owner"])
async def test_phi_work_is_refused_by_a_node_that_is_not_approved(tmp_path, source):
    mesh, kernel, history, bridge = await build(tmp_path, phi=source == "owner")
    kernel.add_chat("c", [fake.message("m1", "chart note", confidentiality="phi" if source == "chat" else "private")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI" if source == "request" else "PRIVATE", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")] and not kernel.answers.answered
    assert not kernel.claimed and not kernel.recorded and history.created == []
    await bridge.stop()


@pytest.mark.parametrize("source", ["request", "chat", "owner"])
async def test_an_approved_node_hands_phi_work_to_the_kernel_with_a_phi_floor(tmp_path, source):
    floors = []

    async def admission(request, floor):
        floors.append(floor)

    mesh, kernel, history, bridge = await build(tmp_path, phi=source == "owner", phi_member=True)
    bridge._admission = admission
    kernel.add_chat("c", [fake.message("m1", "chart note", confidentiality="phi" if source == "chat" else "private")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI" if source == "request" else "PRIVATE", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert [floor[0] for floor in floors] == ["phi"]
    assert kernel.answers.answered == ["r1"] and kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_phi_history_is_not_imported_by_an_unapproved_node_and_secret_is_never_imported(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [fake.message("m1", "plain"), fake.message("m2", "phi text", confidentiality="phi")])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")]
    assert nmc._importable(fake.message("m3", "x", confidentiality="phi"), True) is True
    assert nmc._importable(fake.message("m4", "x", confidentiality="secret"), True) is False
    assert nmc._importable(fake.message("m5", "x", confidentiality="phi"), False) is False
    await bridge.stop()


async def test_a_secret_request_is_refused(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, phi_member=True)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", labels=fake.peer_labels("SECRET", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.secret_label")] and not kernel.claimed and not kernel.answers.answered
    await bridge.stop()


async def test_a_request_without_an_author_is_refused(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", author=None)
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "request.malformed")] and not kernel.answers.answered
    await bridge.stop()


@pytest.mark.parametrize(
    "error,state,code",
    [
        (lambda: node_mesh.NodeMeshError("authority_missing_consent"), None, "node.authority_missing_consent"),
        (lambda: RuntimeError("boom"), None, "node.turn_failed"),
        (lambda: node_mesh.NodeMeshError("phi.group_not_ready"), fake.RequestState.CLAIMED, "phi.group_not_ready"),
        (lambda: node_mesh.NodeMeshError("peer_turn.labels_too_low"), fake.RequestState.CLAIMED, "node.outcome_unknown"),
        (lambda: RuntimeError("boom"), fake.RequestState.CLAIMED, "node.outcome_unknown"),
    ],
)
async def test_a_kernel_answer_that_fails_is_refused_with_a_dotted_code_while_the_request_is_open(tmp_path, error, state, code):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.answers.error = error()
    kernel.answers.state_on_error = state
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", code)] and kernel.request("r1").state == fake.RequestState.REFUSED
    await bridge.stop()


@pytest.mark.parametrize("state", [fake.RequestState.ANSWERED, fake.RequestState.REFUSED])
@pytest.mark.parametrize("error", [lambda: node_mesh.NodeMeshError("phi.device_excluded"), lambda: RuntimeError("late")])
async def test_a_kernel_answer_that_fails_after_the_request_closed_is_only_logged(tmp_path, error, state):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.answers.error = error()
    kernel.answers.state_on_error = state
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [] and kernel.request("r1").state == state
    await bridge.stop()


async def test_a_kernel_answer_that_overruns_is_interrupted_until_the_kernel_acknowledges(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, turn_seconds=0.1)
    kernel.answers.hold = True
    kernel.answers.interrupt_script = [node_mesh.NodeMeshError("node.busy"), False, True]
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.answers.interrupts == ["r1", "r1", "r1"] and not kernel.refused
    await bridge.stop()


async def test_an_interrupted_kernel_answer_that_then_fails_leaves_the_request_to_the_kernel(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, turn_seconds=0.1)
    kernel.answers.hold = True
    kernel.answers.error = RuntimeError("late")
    kernel.answers.state_on_error = fake.RequestState.CLAIMED
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.answers.interrupts == ["r1"] and not kernel.refused and kernel.request("r1").state == fake.RequestState.CLAIMED
    await bridge.stop()


async def test_cancelling_a_kernel_answer_interrupts_it_and_propagates(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.answers.hold = True
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c")
    task = asyncio.create_task(bridge._answer_kernel(request))
    await settle(lambda: kernel.answers.answered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.answers.interrupts == ["r1"] and not kernel.refused
    await bridge.stop()


async def test_a_request_context_the_kernel_will_not_vouch_for_is_refused_before_any_answer(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.answers.context_error = node_mesh.NodeMeshError("gate.mesh_stopped")
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "gate.mesh_stopped")] and not kernel.answers.answered
    await bridge.stop()


async def test_an_owner_change_while_the_request_context_is_checked_refuses_the_answer(tmp_path):
    current = [OWNER]
    mesh, kernel, history, bridge = await build(tmp_path, owner=lambda: current[0])

    async def switching(reference, owner, kernel_):
        current[0] = "someone.else"

    mesh.personal_request_context = switching
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.owner_mismatch")] and not kernel.answers.answered
    await bridge.stop()


async def test_an_answered_request_whose_turn_is_still_running_is_served_again_on_recovery(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    kernel.add_chat("c", [])
    mine = kernel.add_request("r1", "c", state="ANSWERED")
    mine.turn = "turn-1"
    kernel.chats["c"].turns.append(SimpleNamespace(id="turn-1", status=SimpleNamespace(name="RUNNING"), author="dev-node", branch=None))
    kernel.add_request("r2", "c", state="ANSWERED").turn = "turn-2"
    kernel.chats["c"].turns.append(SimpleNamespace(id="turn-2", status=SimpleNamespace(name="COMPLETE"), author="dev-node", branch=None))
    await bridge.scan()
    await settle(lambda: not bridge._running)
    assert kernel.answers.answered == ["r1"]
    await bridge.stop()


async def test_a_call_request_cancelled_before_the_claim_is_left_alone(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", call=call_for())
    original = kernel.claim_request

    def cancelled(request_id):
        request.state = fake.RequestState.CANCELLED
        return original(request_id)

    kernel.claim_request = cancelled
    await serve(bridge, kernel, "r1")
    assert not kernel.refused and not kernel.recorded and not calls.ran
    await bridge.stop()


async def test_a_refusal_the_kernel_will_not_take_is_only_logged(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, phi=True)
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c")
    request.state = fake.RequestState.CLAIMED
    request.claim = "x"
    await bridge._refuse(request, "node.late")
    request.state = fake.RequestState.ANSWERED
    await bridge._refuse(request, "node.late")
    assert kernel.refused == [("r1", "node.late")]
    await bridge.stop()


async def test_a_text_request_claimed_before_a_crash_is_refused_once_on_the_first_scan(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
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
    mesh, kernel, history, bridge = await build(tmp_path, policy=nmc.NodePolicy(serve_requests=False))
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    assert await bridge.scan() == 0 and kernel.request("r1").state == fake.RequestState.PENDING
    await bridge.stop()
    mesh, kernel, history, bridge = await build(tmp_path / "b", joined=False)
    assert await bridge.scan() == 0
    await bridge.stop()
    mesh, kernel, history, bridge = await build(tmp_path / "c", owner=None)
    kernel.add_request("r1", "c")
    assert await bridge.scan() == 0
    await bridge.stop()


async def test_a_failure_before_any_write_leaves_the_request_pending_for_a_retry(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
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
    assert (turn.provider, turn.model) == ("node", "tool-runner")
    step = turn.steps[0]
    assert (step.call_id, step.tool, step.arguments_json, step.tool_def, step.status, step.content, step.code) == (
        "call-1", "lookup", call.arguments_json, "sha256:" + "d" * 64, "ok", "sunny", None,
    )
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_peer_calls_import_completed_signed_history_once_without_creating_another_mesh_chat(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, calls=FakeCalls())
    kernel.add_chat("c", [
        fake.message("m1", "résumé"),
        fake.message("m2", "answer", role="ASSISTANT"),
        fake.message("m3", "unfinished", role="ASSISTANT", status="PENDING"),
    ])
    for request_id in ("r1", "r2"):
        kernel.add_request(request_id, "c", call=call_for())
    await bridge.scan()
    await settle(lambda: not bridge._running)
    assert sorted(record[1].answers for record in kernel.recorded) == ["r1", "r2"]
    assert history.created == [OWNER]
    assert history.added == [("local-1", "user", "résumé", OWNER), ("local-1", "assistant", "answer", OWNER)]
    assert bridge.state.imported("c") >= {"m1", "m2"}
    assert not kernel.created and not kernel.sent
    await bridge.stop()


async def test_a_failed_history_import_reuses_already_stored_messages(tmp_path, monkeypatch):
    _, kernel, history, bridge = await build(tmp_path, calls=FakeCalls())
    kernel.add_chat("c", [fake.message("m1", "first"), fake.message("m2", "second")])
    append = history.add_message

    def fail_second(chat_id, role, text, owner):
        if text == "second":
            raise OSError("history unavailable")
        append(chat_id, role, text, owner)

    monkeypatch.setattr(history, "add_message", fail_second)
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.request("r1").state == fake.RequestState.CLAIMED and not bridge._calls.ran
    assert bridge.state.imported("c") == {"m1"}

    monkeypatch.setattr(history, "add_message", append)
    kernel.add_request("r2", "c", call=call_for())
    await serve(bridge, kernel, "r2")
    assert kernel.request("r2").state == fake.RequestState.ANSWERED
    assert [m[2] for m in history.added] == ["first", "second"]
    await bridge.stop()


async def test_a_local_history_checkpoint_failure_after_the_peer_reply_never_repeats_the_tool(tmp_path, monkeypatch):
    _, kernel, _, bridge = await build(tmp_path, calls=FakeCalls())
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())

    def fail_mark(_chat, _messages):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(bridge.state, "mark_imported", fail_mark)
    await serve(bridge, kernel, "r1")
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    assert await bridge.scan() == 0
    assert len(bridge._calls.ran) == len(kernel.recorded) == 1
    await bridge.stop()


async def test_a_write_tool_is_verified_as_an_outside_effect(tmp_path):
    calls = FakeCalls(scope="tools:write")
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.verified[0][2] == "outside_effect"
    await bridge.stop()


def test_output_labels_raise_integrity_for_an_outward_read_and_keep_every_other_floor():
    assert nmc.output_labels(("private", "owner"), "outward_read") == ("private", "untrusted")
    assert nmc.output_labels(("private", "tool_trusted"), "outward_read") == ("private", "untrusted")
    assert nmc.output_labels(("phi", "tool_trusted"), "outward_read") == ("phi", "untrusted")
    assert nmc.output_labels(("phi", "untrusted"), "outward_read") == ("phi", "untrusted")
    for floor in [("private", "owner"), ("phi", "tool_trusted"), ("private", "untrusted")]:
        assert nmc.output_labels(floor, "outside_effect") == floor


@pytest.mark.parametrize("scope,integrity", [("tools:read", "UNTRUSTED"), ("tools:write", "TOOL_TRUSTED")])
async def test_a_called_tools_step_is_labelled_by_its_effect_kind(tmp_path, scope, integrity):
    mesh, kernel, history, bridge = await build(tmp_path, calls=FakeCalls(scope=scope))
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    turn = only_turn(kernel)[1]
    assert turn.labels == fake.peer_labels("PRIVATE", "TOOL_TRUSTED")
    assert turn.steps[0].labels == fake.peer_labels("PRIVATE", integrity)
    await bridge.stop()


@pytest.mark.parametrize(
    "scope,ticket,expected",
    [
        ("tools:read", ("PHI", "TOOL_TRUSTED"), ("PHI", "UNTRUSTED")),
        ("tools:write", ("PHI", "TOOL_TRUSTED"), ("PHI", "TOOL_TRUSTED")),
        ("tools:read", ("PRIVATE", "UNTRUSTED"), ("PRIVATE", "UNTRUSTED")),
        ("tools:write", ("PRIVATE", "UNTRUSTED"), ("PRIVATE", "UNTRUSTED")),
    ],
)
async def test_a_called_tools_step_keeps_the_phi_and_untrusted_floor_the_ticket_raised(tmp_path, scope, ticket, expected):
    mesh, kernel, history, bridge = await build(tmp_path, calls=FakeCalls(scope=scope), phi_member=True)
    kernel.ticket_labels = fake.peer_labels(*ticket)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    turn = only_turn(kernel)[1]
    assert turn.labels == fake.peer_labels(ticket[0], "UNTRUSTED" if ticket[1] == "UNTRUSTED" else "TOOL_TRUSTED")
    assert turn.steps[0].labels == fake.peer_labels(*expected)
    await bridge.stop()


async def test_an_owner_change_after_the_claim_refuses_the_call_before_it_runs(tmp_path):
    current = [OWNER]
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls, owner=lambda: current[0])
    kernel.add_chat("c", [fake.message("m1", "owner history")])
    kernel.add_request("r1", "c", call=call_for())
    original = kernel.claim_request

    def claim(request_id):
        current[0] = "someone.else"
        return original(request_id)

    kernel.claim_request = claim
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.owner_mismatch")] and not calls.ran and not kernel.recorded
    assert not history.created and not history.added
    await bridge.stop()


async def test_a_tool_that_raises_leaves_the_claimed_call_open_instead_of_closing_it(tmp_path):
    class Exploding(FakeCalls):
        async def run(self, *args, **kwargs):
            raise RuntimeError("boom")

    mesh, kernel, history, bridge = await build(tmp_path, calls=Exploding())
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert request.state == fake.RequestState.CLAIMED and not kernel.refused and not kernel.recorded
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


@pytest.mark.parametrize(("after_sync", "expected_runs"), [
    (None, 1), ("ticket.stopped", 0), ("ticket.consent_sync_pending", 0),
])
async def test_consent_evidence_can_sync_before_claiming_a_call(tmp_path, monkeypatch, after_sync, expected_runs):
    calls = FakeCalls()
    mesh, kernel, _history, bridge = await build(tmp_path, calls=calls, start=False)
    monkeypatch.setattr(nmc, "CONSENT_SYNC_DELAYS", (0,))
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", call=call_for())
    verify = kernel.verify_ticket
    results = iter(("ticket.consent_sync_pending", after_sync))

    def verify_after_sync(*args):
        assert not kernel.claimed and not calls.ran
        kernel.ticket_error = next(results)
        return verify(*args)

    monkeypatch.setattr(kernel, "verify_ticket", verify_after_sync)
    await bridge._answer(request)
    assert len(calls.ran) == len(kernel.claimed) == expected_runs
    assert kernel.refused == ([] if after_sync is None else [("r1", after_sync)])
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
    assert kernel.refused == [("r1", "node.authority_missing_consent")] and not calls.ran
    assert kernel.claimed == ["r1"]
    await bridge.stop()


async def test_institution_policy_change_after_claim_refuses_without_a_tool_effect(tmp_path):
    from orchestrator import node_mesh_admission

    class CurrentCalls(FakeCalls):
        async def authorize(self, owner_id, resolved):
            bound = node_mesh_admission.current_admission()
            try:
                await bound.check_current()
            except node_mesh.NodeMeshError as exc:
                raise calls_module.CallRefused(exc.code) from exc
            return await super().authorize(owner_id, resolved)

    calls = CurrentCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    checks = []

    async def current_policy(request, floor):
        checks.append((request.id, floor))
        if len(checks) == 3:
            raise nmc.Refusal("institution.attestation_stale")

    bridge._admission = current_policy
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert len(checks) == 3
    assert kernel.refused == [("r1", "institution.attestation_stale")]
    assert kernel.claimed == ["r1"] and not calls.ran and not kernel.recorded
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


async def test_a_tool_call_that_overruns_preserves_the_claimed_unknown_outcome(tmp_path):
    calls = FakeCalls()
    calls.delay = 30
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls, turn_seconds=0.1)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [] and kernel.request("r1").state == fake.RequestState.CLAIMED
    await bridge.stop()


async def test_start_refuses_without_the_kernel_export(tmp_path, monkeypatch):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    monkeypatch.delattr(fake.Kernel, "record_peer_turn")
    with pytest.raises(node_mesh.MeshUnavailable):
        await bridge.start()


async def test_chat_changes_wake_the_scan(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, start=False)
    nmc.install(bridge)
    bridge._poll_seconds = 60
    await bridge.start()
    await asyncio.sleep(0.1)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    kernel.notify()
    await settle(lambda: kernel.answers.answered)
    await bridge.stop()


def test_chat_state_persists_links_and_imports(tmp_path):
    path = tmp_path / "state" / "chats.json"
    state = nmc.ChatState(path)
    state.link("mesh-1", "local-1")
    state.mark_imported("mesh-1", ["a", "b", "a"])
    reloaded = nmc.ChatState(path)
    assert reloaded.local_for("mesh-1") == "local-1"
    assert reloaded.local_for("nope") is None
    assert reloaded.imported("mesh-1") == {"a", "b"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_chat_state_ignores_a_corrupt_file(tmp_path):
    path = tmp_path / "chats.json"
    path.write_text("{not json")
    assert nmc.ChatState(path).local_for("x") is None
    path.write_text("[]")
    assert nmc.ChatState(path).imported("x") == set()


def test_policy_from_environment():
    assert nmc.NodePolicy.from_env({}) == nmc.NodePolicy(serve_requests=True)
    assert nmc.NodePolicy.from_env({nmc.REQUESTS_ENV: "OFF"}) == nmc.NodePolicy(serve_requests=False)


def test_labels_join_to_the_most_restrictive_and_unknown_values_fail_closed():
    assert nmc.join_labels(("public", "owner"), ("phi", "tool_trusted"), ("private", "untrusted")) == ("phi", "untrusted")
    source = type("L", (), {"confidentiality": "Strange", "integrity": "odd"})()
    assert nmc.labels_of(source) == ("secret", "untrusted")
    assert nmc.labels_of(fake.peer_labels("PHI", "OWNER")) == ("phi", "owner")


async def test_protection_check_uses_the_phi_policy(monkeypatch):
    import personalization.phi_policy as policy

    monkeypatch.setattr(policy, "protection_enabled", lambda owner: owner == "protected")
    assert nmc._protection_enabled("protected") is True
    assert nmc._protection_enabled("other") is False


async def test_start_for_wires_the_bridge_and_stop_all_tears_it_down(tmp_path, monkeypatch):
    from shared import node_identity as ni

    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
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


class SelectionCalls(CatalogCalls):
    def __init__(self, tools, omitted):
        super().__init__(tools)
        self.omitted = omitted

    def published_selection(self, owner_id):
        return self.tools, self.omitted


async def test_a_truncated_catalog_logs_the_omitted_count_once(tmp_path, caplog):
    mesh, kernel, _, bridge = await build(tmp_path, calls=SelectionCalls(TOOLS, 26), start=False)
    with caplog.at_level("WARNING", logger="Orchestrator.NodeMeshChat"):
        await bridge.scan()
        await bridge.scan()
    lines = [r.getMessage() for r in caplog.records if "node_mesh_catalog_truncated" in r.getMessage()]
    assert lines == ["node_mesh_catalog_truncated: omitted 26 tools"]
    assert len(kernel.catalogs) == 1


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


@pytest.mark.parametrize("source", [PHONE, "dev-node"])
async def test_recovery_preserves_claimed_peer_effects_without_synthetic_refusal(tmp_path, source):
    mesh, kernel, history, bridge = await build(tmp_path, calls=FakeCalls())
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", state="CLAIMED", call=call_for())
    request.from_device = source
    await bridge.scan()
    assert request.state == fake.RequestState.CLAIMED and not kernel.refused and not kernel.recorded
    assert not bridge._running
    await bridge.stop()


async def test_a_peer_effect_without_a_committed_reply_stays_claimed_and_is_not_retried(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", call=call_for())
    kernel.record_error = "store.reply_unwritten"
    await serve(bridge, kernel, "r1")
    assert len(calls.ran) == 1 and request.state == fake.RequestState.CLAIMED
    assert not kernel.refused and not kernel.recorded
    await bridge.scan()
    assert len(calls.ran) == 1 and request.state == fake.RequestState.CLAIMED
    assert not kernel.refused
    await bridge.stop()


async def test_an_unknown_dispatch_result_does_not_close_or_replay_the_claimed_call(tmp_path):
    calls = FakeCalls(outcome=calls_module.CallOutcome("error", "No committed reply", "node.outcome_unknown"))
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    request = kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    await bridge.scan()
    assert len(calls.ran) == 1 and request.state == fake.RequestState.CLAIMED
    assert not kernel.refused and not kernel.recorded
    await bridge.stop()
