"""Exercises command output through governed dispatch, the worker bridge and native response serialization.
Only complete current root-cwd output may avoid the unchanged real PHI redactor.
"""
# ruff: noqa: F811

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import hitl_confirmation, project_authority, remote_confirmation, terminal_host_review
from personalization import phi_gate
from shared.project_workspaces import ProjectManifest
from shared.protocol import MCPResponse
from tests.test_computer_grant_flow_102 import _call, _fresh_pending_cards  # noqa: F401
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.test_project_standing_dispatch import PROJECT, project_runtime

PRIMARY = "/tmp/project-20261027"
SECONDARY = "/tmp/研究-20261027"


@pytest.fixture
async def command_world(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": [{"project_id": PROJECT,
        "grant_revision": 2, "roots": [{"name": "Primary", "path": PRIMARY}, {"name": "Second", "path": SECONDARY}]}]})
    host.project_manifest_highwater = 2
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    orch.ui_clients = [ws, host.websocket]
    orch.computer_hosts._orch = orch
    orch.computer_sessions._orch = orch
    host.descriptor["verbs"] = ["run_command", "read_output"]
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", loop)
    monkeypatch.setattr(terminal_host_review, "computer_use_review_current", lambda _: True)
    world = SimpleNamespace(orch=orch, ws=ws, session=session, host=host, runtime=task_runtime, delivered=[], results=[],
        reply=None, on_response=None, on_stderr=None, private_seen=[], finished=asyncio.Event())

    def analyze(*, text, **kwargs):
        world.private_seen.append(text)
        if text == "ordinary stderr" and world.on_stderr:
            scope = project_authority._CURRENT.get()
            async def mutate():
                world.on_stderr(scope)
            asyncio.run_coroutine_threadsafe(mutate(), loop).result(5)
        return []

    gate = phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=analyze))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    world.gate = gate

    async def send(socket, raw):
        frame = json.loads(raw)
        if frame.get("type") == "computer_request":
            world.delivered.append(frame)
            reply = world.reply
            if reply is None:
                stdout = frame["args"]["cwd"] + "\n"
                reply = {"stdout": stdout, "stderr": "", "stdout_bytes": len(stdout.encode()), "stderr_bytes": 0,
                    "exit_code": 0, "timed_out": False, "signal": None, "truncated": False}
            if world.on_response:
                world.on_response()
            orch.computer_hosts.handle_response(socket, "owner-a", {"request_id": frame["request_id"],
                "ok": True, "result": json.loads(json.dumps(reply))})
        return True

    orch._safe_send = send

    async def execute(websocket, agent, tool, args, **kwargs):
        def worker():
            try:
                result = getattr(mcp_tools, tool)(user_id="owner-a", session_id="chat-a", **args)
                world.results.append(result)
                return result
            finally:
                loop.call_soon_threadsafe(world.finished.set)
        async def invoke(_):
            return MCPResponse(result=await asyncio.to_thread(worker))
        return await orch._execute_governed_attempt(websocket, agent, tool, args, user_id="owner-a", channel="web",
            audit_correlation_id="output-test", actor_user_id="owner-a", auth_principal="owner-a",
            conversation_id="chat-a", invoke=invoke)

    orch._execute_with_retry = execute

    async def call(**args):
        if "command" not in args and "argv" not in args:
            args["argv"] = ["pwd"]
        return await _call(task_runtime, "run_command", **args)
    world.call = call
    yield world
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("cwd", [None, PRIMARY, SECONDARY])
@pytest.mark.parametrize("suffix", ["", "\n", "\r\n"])
async def test_complete_exact_current_root_output_survives_real_privacy_gate(command_world, cwd, suffix):
    world = command_world
    actual = (cwd or PRIMARY) + suffix
    world.reply = {"stdout": actual, "stderr": "", "stdout_bytes": len(actual.encode()), "stderr_bytes": 0,
        "exit_code": 0, "timed_out": False, "signal": None, "truncated": False}
    with pytest.raises(ValueError, match="phi_redaction_refused"):
        world.gate.redact_for_storage(actual)
    response = await world.call(**({"cwd": cwd} if cwd else {}))
    assert response.error is None, response
    data = response.result["_data"]
    assert data["stdout"] == actual and data["stdout_bytes"] == len(actual.encode())
    assert data["untrusted_output"] is True and data["approval_source"] == "project_grant"
    assert "privacy" not in data
    assert len(world.delivered) == 1 and world.delivered[0]["args"]["cwd"] == (cwd or PRIMARY)
    assert world.delivered[0]["project_context"] == world.session.project_origin.context.to_dict()
    assert not getattr(world.orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS


def _reply(stdout=PRIMARY + "\n", **changes):
    return {"stdout": stdout, "stderr": "", "stdout_bytes": len(stdout.encode()), "stderr_bytes": 0,
        "exit_code": 0, "timed_out": False, "signal": None, "truncated": False, **changes}


@pytest.mark.parametrize("field,value", [
    ("stdout_bytes", None), ("stdout_bytes", True), ("stdout_bytes", 22.0), ("stdout_bytes", -1),
    ("stdout_bytes", 1), ("stdout_bytes", 999999), ("truncated", None), ("truncated", True),
    ("truncated", 0), ("exit_code", None), ("exit_code", True), ("exit_code", 0.0), ("exit_code", 1),
    ("timed_out", None), ("timed_out", True), ("timed_out", 0), ("signal", "TERM"), ("signal", False),
])
async def test_incomplete_or_unsuccessful_metadata_never_releases_the_root(command_world, field, value):
    world = command_world
    world.reply = _reply(**{field: value})
    if value is None:
        world.reply.pop(field)
    response = await world.call()
    assert response.error is None, response
    assert response.result["_data"]["stdout"] == ""
    assert response.result["_data"]["privacy"] == "withheld"
    assert len(world.delivered) == 1


@pytest.mark.parametrize("stdout", [
    "prefix " + PRIMARY, PRIMARY + " suffix", PRIMARY + "\n\n", PRIMARY + "\r", " " + PRIMARY,
    "\x1b[31m" + PRIMARY, PRIMARY + "\x00", PRIMARY + "\t", PRIMARY + "/child", SECONDARY,
    "x" * 20000 + PRIMARY, "123456789", "MRN 123456789\n" + PRIMARY,
])
async def test_unknown_mixed_or_control_decorated_stdout_stays_private(command_world, stdout):
    world = command_world
    world.reply = _reply(stdout)
    response = await world.call()
    assert response.error is None, response
    assert response.result["_data"]["stdout"] == "" and response.result["_data"]["privacy"] == "withheld"


async def test_arbitrary_subdirectory_and_forged_result_cwd_do_not_extend_exception(command_world):
    world = command_world
    world.reply = _reply(PRIMARY + "/child\n", cwd=PRIMARY)
    response = await world.call(cwd=PRIMARY + "/child")
    assert response.result["_data"]["stdout"] == ""
    world.reply = _reply(SECONDARY + "\n", cwd=SECONDARY)
    response = await world.call(cwd=PRIMARY)
    assert response.result["_data"]["stdout"] == ""


async def test_unicode_counts_use_bytes_and_existing_tail_bound_is_preserved(command_world, monkeypatch):
    world = command_world
    actual = SECONDARY + "\n"
    world.reply = _reply(actual, stdout_bytes=len(actual))
    response = await world.call(cwd=SECONDARY)
    assert response.result["_data"]["stdout"] == ""
    world.reply = _reply()
    monkeypatch.setattr(mcp_tools, "_TAIL_BYTES", len(PRIMARY.encode()) - 1)
    response = await world.call()
    assert response.result["_data"]["stdout"] == ""


@pytest.mark.parametrize("unavailable", [False, True])
async def test_all_stderr_and_unknown_stdout_still_use_real_privacy_gate(command_world, unavailable):
    world = command_world
    if unavailable:
        world.on_response = lambda: setattr(world.gate, "_analyzer", None)
    world.reply = _reply(stderr="123456789", stderr_bytes=9)
    response = await world.call()
    assert response.result["_data"]["stdout"] == PRIMARY + "\n"
    assert response.result["_data"]["stderr"] == "" and response.result["_data"]["privacy"] == "withheld"
    if unavailable:
        world.gate._analyzer = SimpleNamespace(analyze=lambda **_: [])
        world.reply = _reply("ordinary unknown output")
        response = await world.call()
        assert response.result["_data"]["stdout"] == "" and response.result["_data"]["privacy"] == "withheld"


def _invalidate(world, change, scope=None):
    if change == "grant":
        world.host.project_manifest = ProjectManifest.from_dict({"generation": 3, "projects": []})
    elif change == "session":
        world.session.authority_generation += 1
    elif change == "controller":
        world.session.controller_ws_id += 1
    elif change == "connection":
        world.orch._connection_contexts[id(world.ws)].closing = True
    elif change == "owner":
        world.orch.ui_sessions[world.ws]["sub"] = "foreign"
    elif change == "task":
        hitl_confirmation.begin_task(world.orch, world.ws, "owner-a", "chat-a")
    elif change == "stop":
        world.orch.cancelled_sessions[id(world.ws)] = True
    elif change == "expiry":
        approval = hitl_confirmation._APPROVAL.get()
        approval.call = replace(approval.call, expires=0)
    elif change == "cancel":
        scope[2].cancel()


@pytest.mark.parametrize("change", ["grant", "session", "controller", "connection", "owner"])
async def test_authority_loss_during_rpc_never_claims_pre_effect_refusal(command_world, change):
    world = command_world
    world.on_response = lambda: _invalidate(world, change)
    response = await world.call()
    assert len(world.delivered) == 1
    failure = response.error or response.result["_data"]
    assert failure.get("effect_state") != "not_started", response
    assert failure.get("stdout", "") == "" and failure.get("code") in {"outcome_uncertain", "stale_project_grant"}


@pytest.mark.parametrize("change", ["grant", "session", "controller", "connection", "owner", "cancel", "task", "stop", "expiry"])
async def test_authority_is_rechecked_after_stderr_privacy_work(command_world, change):
    world = command_world
    world.reply = _reply(stderr="ordinary stderr", stderr_bytes=15)
    def mutate(scope):
        world.on_stderr = None
        _invalidate(world, change, scope)
    world.on_stderr = mutate
    task = asyncio.create_task(world.call())
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        response = await task
        if response.error:
            assert response.error.get("effect_state") != "not_started", response
    await asyncio.wait_for(world.finished.wait(), 5)
    assert len(world.delivered) == 1 and len(world.results) == 1
    data = world.results[0]["_data"]
    assert data["stdout"] == "" and data["privacy"] == "withheld"
    assert data["exit_code"] == 0 and data["untrusted_output"] is True


@pytest.mark.parametrize("change", ["grant", "controller", "connection", "owner", "unbound"])
async def test_missing_current_origin_never_acquires_an_output_exception(command_world, change):
    world = command_world
    if change == "unbound":
        world.session.project_origin = None
    else:
        _invalidate(world, change)
    response = await world.call()
    assert not world.delivered
    assert response.error or response.result["_data"].get("code"), response


async def test_legacy_command_output_has_no_known_project_root_exception(command_world):
    world = command_world
    world.host.project_mode = False
    world.host.descriptor["workspace_roots"] = [{"name": "Plain", "path": "/tmp/plain"}]
    world.session.project_origin = None
    await world.orch.computer_sessions.set_bypass(world.host, True)
    world.reply = _reply()
    response = await world.call(cwd="/tmp/plain")
    assert response.error is None, response
    assert len(world.delivered) == 1
    assert response.result["_data"]["stdout"] == "" and response.result["_data"]["privacy"] == "withheld"


async def test_background_chunks_do_not_inherit_foreground_root_exception(command_world):
    world = command_world
    job = "12345678-1234-4234-9234-123456789abc"
    world.reply = {"job_id": job, "started_at": "2026-10-28T00:00:00Z", "stdout": PRIMARY + "\n"}
    response = await world.call(background=True)
    assert response.error is None and response.result["_data"]["background"] is True, response
    world.reply = {"state": "completed", "exit_code": 0, "truncated": False,
        "chunks": [{"stream": "stdout", "offset": 0, "data": PRIMARY + "\n"},
                   {"stream": "stderr", "offset": 0, "data": "123456789"}], "next_offset": 99}
    response = await _call(world.runtime, "read_output", job_id=job)
    assert response.error is None, response
    assert [chunk["data"] for chunk in response.result["_data"]["chunks"]] == ["", ""]
    assert response.result["_data"]["privacy"] == "withheld"
    assert [frame["verb"] for frame in world.delivered] == ["run_command", "read_output"]


async def test_authority_loss_after_bridge_and_before_privacy_loses_exception(command_world, monkeypatch):
    world = command_world
    touch = world.orch.computer_sessions.touch
    def expire(session):
        touch(session)
        _invalidate(world, "controller")
    monkeypatch.setattr(world.orch.computer_sessions, "touch", expire)
    response = await world.call()
    assert len(world.delivered) == 1
    data = world.results[0]["_data"]
    assert data["stdout"] == "" and data["privacy"] == "withheld" and data["exit_code"] == 0
    if response.error:
        assert response.error.get("effect_state") != "not_started"


@pytest.mark.parametrize("stdout", [None, 0, False, {}, []])
async def test_missing_or_nontext_stdout_cannot_be_fabricated_from_cwd(command_world, stdout):
    world = command_world
    world.reply = _reply(stdout=PRIMARY + "\n")
    world.reply["stdout"] = stdout
    response = await world.call(command="pwd")
    assert response.error is None, response
    assert response.result["_data"]["stdout"] == ""
    assert len(world.delivered) == 1


async def test_actual_stdout_identity_does_not_depend_on_command_name(command_world):
    world = command_world
    world.reply = _reply()
    response = await world.call(command="true")
    assert response.result["_data"]["stdout"] == PRIMARY + "\n"
    assert world.delivered[0]["args"]["command"] == "true"


async def test_authority_loss_after_shaping_is_a_true_pre_delivery_refusal(command_world, monkeypatch):
    world = command_world
    shape = mcp_tools._command_args
    def supersede(args, host):
        result = shape(args, host)
        _invalidate(world, "controller")
        return result
    monkeypatch.setattr(mcp_tools, "_command_args", supersede)
    response = await world.call()
    assert not world.delivered
    failure = response.error or response.result["_data"]
    assert failure["code"] == "stale_project_grant" and failure["effect_state"] == "not_started"


async def test_real_sensitive_output_remains_redacted(command_world):
    world = command_world
    world.reply = _reply("SSN 123-45-6789")
    response = await world.call()
    data = response.result["_data"]
    assert "123-45-6789" not in data["stdout"] and data["privacy"] in {"redacted", "withheld"}


async def test_foreground_serialized_chat_keeps_origin_through_command_output(command_world):
    world = command_world
    responses = []
    async def chat(websocket, message, chat_id, display, **kwargs):
        responses.append(await world.call())
    world.orch.handle_chat_message = chat
    await world.orch._serialized_chat(world.ws, "Inspect the current directory", "chat-a", "Inspect the current directory",
                                      user_id="owner-a")
    assert len(responses) == 1 and responses[0].error is None, responses
    assert responses[0].result["_data"]["stdout"] == PRIMARY + "\n"
    assert len(world.delivered) == 1 and world.delivered[0]["project_context"]["chat_id"] == "chat-a"
