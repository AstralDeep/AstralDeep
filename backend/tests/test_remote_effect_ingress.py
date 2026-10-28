"""Exercises qualified remote approvals through registered socket admission and real Plane.
External SSH alone is substituted; owner review, leases, reservations and audit stay observable.
"""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from audit.recorder import Recorder
from audit.repository import AuditRepository
from orchestrator import remote_confirmation, remote_machines
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.credential_manager import CredentialManager
from orchestrator.history import ConversationCommitRepository, HistoryManager
from orchestrator.human_request_authority import HumanRequestBoundary
from orchestrator.remote_effect_execution import select_effect, selected_effect, run_qualified
from orchestrator.remote_transport import ParamikoTransport
from orchestrator.work_admission import AdmissionClass, OwnerScope, OperationState, WorkAdmissionCoordinator
from orchestrator.workspace import WorkspaceManager
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.test_source_bound_ui_ingress import ApprovalButton, delivered, envelope
from tests.test_work_reconciliation_postgres import _ledger
from tests.test_work_surface_ingress_088 import (  # noqa: F401
    ingress, surface, command, context, fixture, runtime, service, signing_key, registered,
)
from webrender.renderer import render


@pytest.fixture
async def remote_ingress(ingress, runtime, fixture, monkeypatch, tmp_path):  # noqa: F811
    state = ingress
    await registered(state)
    orch, ws, owner = state.orch, state.socket, fixture[1]
    configs = orch.work_admission.repository.load_existing_configs()
    orch.work_admission = WorkAdmissionCoordinator(admission_classes=[replace(value, active_limit=1)
        for value in configs], repository=orch.work_admission.repository)
    ws.scope["headers"].append((b"origin", b"https://app.invalid"))
    monkeypatch.setenv("MEMORY_HMAC_KEY", "synthetic-remote-action-regression-key")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ=")
    history = HistoryManager(str(tmp_path), plane_runtime=runtime)
    chat = await asyncio.to_thread(history.create_chat, user_id=owner)
    orch.history = history
    orch.workspace = WorkspaceManager(history, plane_runtime=runtime)
    orch.conversation_commits = ConversationCommitRepository(plane_runtime=runtime,
        operation_coordinator=orch.work_admission)
    orch._conversation_scopes, orch._workspace_locks = {}, {}
    orch._ws_active_chat[id(ws)] = chat
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    source = SimpleNamespace(plane_runtime=runtime, plane_repositories=runtime.repositories)
    orch.plane_repository_source = source
    credentials = CredentialManager(db=source)
    machine = remote_machines.create_machine(source, owner, "fixture", "ssh.test", 22,
        "runner", "linux", "plain")
    credentials.set_machine_credential(machine, owner, "password", "fixture-secret")
    remote_machines.record_probe(source, owner, machine, "ok", {
        "type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "blob_b64": "fixture"})
    orch.credential_manager = credentials
    tools = {name: "tools:write" for name in ("make_directory", "remove_path")}
    orch.agent_cards["remote-compute-1"] = AgentCard(agent_id="remote-compute-1",
        name="Remote compute", description="Remote filesystem",
        skills=[AgentSkill(id=name, name=name, description=name, scope=scope,
            input_schema={"type": "object", "properties": {}}) for name, scope in tools.items()])
    orch.agents["remote-compute-1"] = object()
    orch.local_agents, orch.a2a_clients, orch.security_flags = {}, {}, {}
    orch.tool_permissions.register_tool_scopes("remote-compute-1", tools)
    orch.tool_permissions.set_agent_scopes(owner, "remote-compute-1", {"tools:write": True})
    orch._get_delegation_token = AsyncMock(return_value="synthetic-fixture-delegation")
    orch._delegation_required = lambda: False
    orch._delegation_denied_for_permissions = AsyncMock(return_value=False)
    orch._is_long_running_tool = lambda *_: False
    orch._tool_accepts_context_arg = lambda *_: False
    orch._auto_subscribe_stream_artifacts = AsyncMock()
    orch._map_file_paths = lambda _chat, args, **_kwargs: args
    orch.send_ui_render, orch.send_ui_upsert = AsyncMock(), AsyncMock()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
        lambda: SimpleNamespace(contains_phi=lambda _: False))
    repository = AuditRepository(plane_runtime=runtime)
    recorder = Recorder(repository, retry_queue=tmp_path / "remote-retry.jsonl")
    orch.audit_recorder = recorder
    orch.audit_repo = repository
    orch.human_request_boundary = HumanRequestBoundary(orch)
    orch.effect_attempts = _ledger(runtime, orch.work_admission)
    orch.work_admission.set_completion_guard(orch.effect_attempts.completion_uncertain)
    effects, selections = [], []
    hooks = SimpleNamespace(before_network=None, authenticated=None, selected=None, lost_reply=False)

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    orch.governed_final_dispatch = Adapter()

    async def dispatch(socket, agent, tool, args, **kwargs):
        selections.append(_CONNECTION_OPERATION_CONTEXT.get()["owner"])
        assert (await asyncio.to_thread(orch.work_admission.inspect_admission_class,
            AdmissionClass.GLOBAL)).active_count == 1
        selected = await select_effect(orch, socket, agent, tool, args, owner)
        hooks.selected = selected
        pinned = await asyncio.to_thread(remote_machines.build_effect_target, source, credentials, owner, machine)

        if hooks.before_network is not None:
            await hooks.before_network()

        class Channel:
            def recv(self, size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return 0

            def recv_stderr_ready(self):
                return False

        class Client:
            def exec_command(self, remote_command, *, timeout):
                attempts = orch.effect_attempts.list_for_operation(partition=owner,
                    operation_id=selected.fence.operation_id)
                assert len(attempts) == 1 and attempts[0].dispatch_id
                effects.append(remote_command)
                if hooks.lost_reply:
                    raise ConnectionError("reply lost after send")
                return None, SimpleNamespace(channel=Channel()), None

            def close(self):
                pass

        transport = ParamikoTransport()
        def connected(target, timeout):
            if hooks.authenticated is not None:
                hooks.authenticated()
            return Client(), None
        monkeypatch.setattr(transport, "_connect", connected)

        async def physical(_capabilities):
            result = await asyncio.to_thread(run_qualified, transport, pinned,
                (["mkdir", "-p", "/fixture"] if tool == "make_directory" else ["rm", "-f", "/fixture"]),
                tool=tool, owner_id=owner, timeout=1)
            return MCPResponse(result={"ok": True}) if result.ok else MCPResponse(error={
                "message": result.next_action, "retryable": False})

        with selected_effect(selected):
            return await orch._execute_governed_attempt(socket, agent, tool, args,
                user_id=owner, channel="web", audit_correlation_id=None,
                actor_user_id=owner, auth_principal=owner, conversation_id=chat, invoke=physical)

    orch._execute_with_retry = dispatch
    try:
        yield SimpleNamespace(state=state, orch=orch, ws=ws, owner=owner, chat=chat,
            machine=machine, effects=effects, selections=selections, runtime=runtime, hooks=hooks,
            enable=lambda: enable(orch))
    finally:
        orch.human_request_boundary.close()
        await recorder.close()


def enable(orch):
    orch.local_agents["remote-compute-1"] = object()
    orch._llm_store = SimpleNamespace(get=AsyncMock(return_value=None), get_system=AsyncMock(return_value=None))


async def propose(env, initial, tool="make_directory"):
    args = {"machine_id": env.machine, "path": "/fixture"}
    if initial == "direct":
        payload = {"agent_id": "remote-compute-1", "tool": tool, "args": args}
        action = "authorize_action"
    else:
        proposal, _ = remote_confirmation._create_proposal(env.orch, env.owner, env.chat,
            "remote-compute-1", tool, args)
        payload, action = {"proposal_id": proposal, "decision": "approve"}, "remote_op_decision"
    frame = envelope(env.state, env.chat, "", payload)
    frame["action"] = action
    frame["payload"].pop("component_id")
    await delivered(env.state, frame)
    assert env.selections == []
    offered = await env.orch.workspace.alive_components(env.chat, env.owner)
    cards = [value for value in offered if value.get("id", "").startswith("hitl_")
        and any(node.get("action") == "authorize_action" for node in value.get("content", []))]
    if not cards and initial == "direct" and tool == "remove_path":
        proposals = [node["payload"] for call in env.orch.send_ui_render.call_args_list
            for component in call.args[1] for node in component.get("content", [])
            if node.get("action") == "remote_op_decision" and node.get("payload", {}).get("decision") == "approve"]
        assert len(proposals) == 1
        decision = envelope(env.state, env.chat, "", proposals[0])
        decision["action"] = "remote_op_decision"
        decision["payload"].pop("component_id")
        await delivered(env.state, decision)
        assert env.selections == []
        offered = await env.orch.workspace.alive_components(env.chat, env.owner)
        cards = [value for value in offered if value.get("id", "").startswith("hitl_")
        and any(node.get("action") == "authorize_action" for node in value.get("content", []))]
    assert len(cards) == 1
    assert env.orch.send_ui_upsert.await_count > 0
    parser = ApprovalButton()
    parser.feed(render(cards))
    assert parser.payload is not None
    return envelope(env.state, env.chat, cards[0]["component_id"], parser.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,tool", [("direct", "make_directory"),
    ("direct", "remove_path"), ("proposal", "remove_path")])
async def test_normal_initial_event_delivers_review_then_final_approve_owns_user_operation(remote_ingress, initial, tool):
    env = remote_ingress
    approve = await propose(env, initial, tool)
    env.enable()
    await delivered(env.state, approve)
    assert len(env.selections) == 1
    assert env.selections[0].owner_scope is OwnerScope.USER
    assert env.selections[0].owner_user_id == env.owner
    assert len(env.effects) == 1
    await env.state.barrier()
    env.ws.feed(json.dumps(approve))
    await env.state.barrier()
    assert len(env.selections) == 1
    old = envelope(env.state, env.chat, approve["payload"]["component_id"], {
        "hitl_request_id": approve["payload"]["hitl_request_id"], "decision": "approve"})
    await delivered(env.state, old)
    assert len(env.effects) == len(env.selections) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["predecessor", "before_selection"])
async def test_foreground_user_disconnect_cancels_without_retaining_or_running(remote_ingress, monkeypatch, stage):
    env = remote_ingress
    approve = await propose(env, "direct")
    env.enable()
    connection = env.orch._connection_contexts[id(env.ws)]
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.orch._execute_with_retry
    predecessor = asyncio.get_running_loop().create_future()
    if stage == "predecessor":
        connection.mutation_tail = predecessor
    else:
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(env.orch, "_execute_with_retry", held)
    env.ws.feed(json.dumps(approve))
    accepted = await env.state.arrived(lambda value: value.get("type") == "operation_status" and value.get("state") == "accepted"
        and value.get("request_generation") == approve["request_generation"])
    if stage == "before_selection":
        await asyncio.wait_for(entered.wait(), 5)
    work = connection.operations[UUID(accepted["operation_id"])]
    assert work.owner.owner_scope is OwnerScope.USER
    assert work.operation_id not in env.orch._reconnectable_operations
    await env.orch._drain_connection_context(connection)
    release.set()
    assert not predecessor.cancelled()
    if not predecessor.done():
        predecessor.set_result(None)
    await asyncio.gather(work.task, return_exceptions=True)
    record = await asyncio.to_thread(env.orch.work_admission.repository.get_operation_for_administration,
        work.operation_id)
    assert record.state is OperationState.CANCELLED
    assert env.effects == [] and env.selections == []
    assert not env.orch._reconnectable_operations
    assert (await asyncio.to_thread(env.orch.work_admission.inspect_admission_class,
        AdmissionClass.GLOBAL)).active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["arguments", "tool", "decision", "component", "unknown"])
async def test_same_submission_cannot_change_captured_remote_intent(remote_ingress, change):
    env = remote_ingress
    approve = await propose(env, "direct")
    env.enable()
    await delivered(env.state, approve)
    assert len(env.effects) == 1
    altered = json.loads(json.dumps(approve))
    if change in {"arguments", "tool"}:
        frame = next(item for item in env.state.frames if str(item.request_generation) == approve["request_generation"])
        original = frame.remote_effect_call
        env.orch._hitl_pending_calls[original.request_id] = replace(original,
            **({"arguments": json.dumps({"machine_id": env.machine, "path": "/changed"})}
                if change == "arguments" else {"tool": "remove_path"}))
    elif change == "decision":
        altered["payload"]["decision"] = "decline"
    elif change == "component":
        altered["payload"]["component_id"] = "changed"
    else:
        altered["payload"]["capability"] = "forged"
    env.ws.feed(json.dumps(altered))
    rejected = await env.state.arrived(lambda value: value.get("type") == "error")
    assert rejected["code"] == "idempotency_conflict"
    assert len(env.effects) == len(env.selections) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["registration", "claims", "session", "lease", "target", "disconnect", "stop", "cancellation"])
async def test_registered_approval_rechecks_admitted_authority_before_network(remote_ingress, change):
    from datetime import datetime, timedelta, timezone
    from astralplane.repositories.work_admission import ExecutionFence as PlaneFence
    env = remote_ingress
    approve = await propose(env, "direct")
    env.enable()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await release.wait()

    env.hooks.before_network = held
    env.ws.feed(json.dumps(approve))
    await asyncio.wait_for(entered.wait(), 5)
    selected = env.hooks.selected
    connection = env.orch._connection_contexts[id(env.ws)]
    work = connection.operations[selected.fence.operation_id]
    if change == "registration":
        env.orch.ui_sessions[env.ws] = dict(env.orch.ui_sessions[env.ws])
    elif change == "claims":
        env.orch.ui_sessions[env.ws]["private_test_marker"] = "changed"
    elif change == "session":
        await asyncio.to_thread(env.orch.web_sessions.delete, selected.reference.state.credential.session_id)
    elif change == "lease":
        def reselect():
            with env.runtime.transaction() as tx:
                return env.runtime.repositories.work_admission.reselect_execution(tx,
                    PlaneFence(selected.fence.operation_id, selected.fence.execution_generation,
                        selected.fence.execution_lease_token), now=datetime.now(timezone.utc),
                    slot_lease=timedelta(seconds=30))
        await asyncio.to_thread(reselect)
    elif change == "target":
        await asyncio.to_thread(env.orch.credential_manager.delete_machine_credential, env.machine, env.owner)
    elif change == "stop":
        def stop():
            with env.runtime.transaction() as tx:
                repository = env.runtime.repositories.stop_epochs
                state = repository.get(tx, owner_id=env.owner)
                repository.engage(tx, owner_id=env.owner, expected_revision=0 if state is None else state.revision,
                    reason="fixture", actor_id=env.owner, at=datetime.now(timezone.utc))
        await asyncio.to_thread(stop)
    elif change == "cancellation":
        from orchestrator.hitl_confirmation import cancel_pending
        cancel_pending(env.orch, env.ws, env.owner, env.chat)
    else:
        await env.orch._drain_connection_context(connection)
    release.set()
    await asyncio.gather(work.task, return_exceptions=True)
    assert env.effects == []
    assert env.orch.effect_attempts.list_for_operation(partition=env.owner,
        operation_id=selected.fence.operation_id) == ()


@pytest.mark.asyncio
async def test_registered_lost_reply_preserves_one_uncertain_attempt(remote_ingress):
    env = remote_ingress
    approve = await propose(env, "proposal", "remove_path")
    env.enable()
    env.hooks.lost_reply = True
    await delivered(env.state, approve)
    selected = env.hooks.selected
    attempts = env.orch.effect_attempts.list_for_operation(partition=env.owner,
        operation_id=selected.fence.operation_id)
    assert len(env.effects) == len(attempts) == 1
    assert not attempts[0].settled
    record = env.orch.work_admission.repository.get_operation_for_administration(selected.fence.operation_id)
    assert record.state is OperationState.RETRYABLE and record.terminal_code == "effect_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["decline", "other_agent", "other_tool", "legacy", "expired", "foreign", "chat", "unknown"])
async def test_nonqualified_decision_never_borrows_user_placement(remote_ingress, change):
    env = remote_ingress
    approve = await propose(env, "direct")
    env.enable()
    request = approve["payload"]["hitl_request_id"]
    call = env.orch._hitl_pending_calls[request]
    if change == "decline":
        approve["payload"]["decision"] = "decline"
    elif change == "other_agent":
        env.orch._hitl_pending_calls[request] = replace(call, agent="external-fixture")
    elif change == "other_tool":
        env.orch._hitl_pending_calls[request] = replace(call, tool="list_directory")
    elif change == "legacy":
        env.orch._hitl_pending_calls[request] = replace(call, risks=("legacy",))
    elif change == "expired":
        env.orch._hitl_pending_calls[request] = replace(call, expires=0)
    elif change == "foreign":
        env.orch._hitl_pending_calls[request] = replace(call, owner="foreign")
    elif change == "chat":
        env.orch._ws_active_chat[id(env.ws)] = str(UUID(int=1, version=4))
    else:
        approve["payload"]["capability"] = "forged"
    await delivered(env.state, approve)
    admitted = next(item for item in env.state.frames if str(item.request_generation) == approve["request_generation"])
    assert not admitted.foreground and admitted.operation_kind == "connection_frame"
    assert env.effects == []
    assert all(value.owner_scope is OwnerScope.CONNECTION for value in env.selections)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["no_cookie", "no_origin", "wrong_origin", "no_boundary"])
async def test_final_approval_requires_original_cookie_origin_and_human_boundary(remote_ingress, change):
    env = remote_ingress
    approve = await propose(env, "direct")
    env.enable()
    boundary = env.orch.human_request_boundary
    if change == "no_cookie":
        env.ws.scope["headers"] = [(key, value) for key, value in env.ws.scope["headers"] if key != b"cookie"]
    elif change == "no_origin":
        env.ws.scope["headers"] = [(key, value) for key, value in env.ws.scope["headers"] if key != b"origin"]
    elif change == "wrong_origin":
        env.ws.scope["headers"] = [(key, b"https://foreign.invalid" if key == b"origin" else value)
            for key, value in env.ws.scope["headers"]]
    else:
        env.orch.human_request_boundary = None
    try:
        await delivered(env.state, approve)
        assert env.effects == env.selections == []
        assert approve["payload"]["hitl_request_id"] in env.orch._hitl_pending_calls
    finally:
        env.orch.human_request_boundary = boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "foreign", "arguments", "machine", "expired", "client_proof"])
async def test_dual_review_proof_cannot_be_substituted_or_expired(remote_ingress, monkeypatch, change):
    env = remote_ingress
    approve = await propose(env, "proposal", "remove_path")
    env.enable()
    identity = approve["payload"]["hitl_request_id"]
    call = env.orch._hitl_pending_calls[identity]
    assert call.remote_proposal is not None
    proposals = remote_confirmation._proposal_context(env.orch)
    row = proposals.call(proposals.repository.get, owner_id=env.owner, proposal_id=call.remote_proposal)
    assert row.status == "consumed"
    if change == "expired":
        monkeypatch.setattr(remote_confirmation.time, "time", lambda: row.expires_at + 1)
    elif change == "client_proof":
        approve["payload"]["remote_proposal"] = call.remote_proposal
    else:
        proof = "0" * 32
        if change != "missing":
            owner = "foreign" if change == "foreign" else env.owner
            args = json.loads(call.arguments)
            args["path"] = "/other" if change == "arguments" else args["path"]
            args["machine_id"] = str(UUID(int=1, version=4)) if change == "machine" else args["machine_id"]
            proof, _ = remote_confirmation._create_proposal(env.orch, owner, env.chat,
                "remote-compute-1", "remove_path", args)
            def consume():
                with proposals.transaction() as tx:
                    created = proposals.repository.get(tx, owner_id=owner, proposal_id=proof).created_at
                    proposals.repository.decide_if_pending(tx, owner_id=owner, proposal_id=proof,
                        decision="approved", decided_at=created)
                    proposals.repository.consume_if_valid(tx, owner_id=owner, proposal_id=proof,
                        expected_tool_name="remove_path", expected_args_fingerprint=remote_confirmation._fingerprint(args),
                        consumed_at=created)

            await asyncio.to_thread(consume)
        env.orch._hitl_pending_calls[identity] = replace(call, remote_proposal=proof)
    await delivered(env.state, approve)
    assert env.effects == env.selections == []


@pytest.mark.asyncio
async def test_dual_review_proof_created_in_a_later_second_is_still_rejected(remote_ingress, monkeypatch):
    env = remote_ingress
    approve = await propose(env, "proposal", "remove_path")
    env.enable()
    identity = approve["payload"]["hitl_request_id"]
    call = env.orch._hitl_pending_calls[identity]
    proposals = remote_confirmation._proposal_context(env.orch)
    row = proposals.call(proposals.repository.get, owner_id=env.owner, proposal_id=call.remote_proposal)
    assert row.status == "consumed"
    real_time = remote_confirmation.time.time
    monkeypatch.setattr(remote_confirmation.time, "time", lambda: real_time() + 5)
    args = json.loads(call.arguments)
    args["machine_id"] = str(UUID(int=1, version=4))
    proof, _ = remote_confirmation._create_proposal(env.orch, env.owner, env.chat,
        "remote-compute-1", "remove_path", args)
    monkeypatch.setattr(remote_confirmation.time, "time", real_time)
    crafted = proposals.call(proposals.repository.get, owner_id=env.owner, proposal_id=proof)
    assert crafted.created_at > row.created_at

    def consume():
        with proposals.transaction() as tx:
            proposals.repository.decide_if_pending(tx, owner_id=env.owner, proposal_id=proof,
                decision="approved", decided_at=crafted.created_at)
            proposals.repository.consume_if_valid(tx, owner_id=env.owner, proposal_id=proof,
                expected_tool_name="remove_path", expected_args_fingerprint=remote_confirmation._fingerprint(args),
                consumed_at=crafted.created_at)

    await asyncio.to_thread(consume)
    env.orch._hitl_pending_calls[identity] = replace(call, remote_proposal=proof)
    await delivered(env.state, approve)
    assert env.effects == env.selections == []


@pytest.mark.asyncio
async def test_distinct_server_remote_proposals_keep_distinct_pending_calls_and_attempts(remote_ingress):
    env = remote_ingress
    first = await propose(env, "proposal", "remove_path")
    second_id, _ = remote_confirmation._create_proposal(env.orch, env.owner, env.chat,
        "remote-compute-1", "remove_path", {"machine_id": env.machine, "path": "/fixture"})
    second = envelope(env.state, env.chat, "", {"proposal_id": second_id, "decision": "approve"})
    second["action"] = "remote_op_decision"
    second["payload"].pop("component_id")
    await delivered(env.state, second)
    calls = env.orch._hitl_pending_calls
    assert len(calls) == 2 and len({call.remote_proposal for call in calls.values()}) == 2
    env.enable()
    for call in tuple(calls.values()):
        assert call.remote_proposal in {second_id, calls[first["payload"]["hitl_request_id"]].remote_proposal}
        component = env.orch.workspace.resolve_identity({"id": f"hitl_{call.request_id}"})
        await delivered(env.state, envelope(env.state, env.chat, component,
            {"hitl_request_id": call.request_id, "decision": "approve"}))
    assert len(env.effects) == len(env.selections) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancellation", "proposal_expiry"])
async def test_current_review_retired_during_ssh_authentication_never_sends(remote_ingress, monkeypatch, change):
    from orchestrator.hitl_confirmation import cancel_pending

    env = remote_ingress
    approve = await propose(env, "proposal", "remove_path")
    call = env.orch._hitl_pending_calls[approve["payload"]["hitl_request_id"]]
    proposals = remote_confirmation._proposal_context(env.orch)
    row = proposals.call(proposals.repository.get, owner_id=env.owner, proposal_id=call.remote_proposal)
    env.enable()

    def authenticated():
        if change == "cancellation":
            cancel_pending(env.orch, env.ws, env.owner, env.chat)
        else:
            monkeypatch.setattr(remote_confirmation.time, "time", lambda: row.expires_at + 1)

    env.hooks.authenticated = authenticated
    await delivered(env.state, approve)
    selected = env.hooks.selected
    assert selected is not None and env.effects == []
    assert env.orch.effect_attempts.list_for_operation(partition=env.owner,
        operation_id=selected.fence.operation_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,tool", [("approval-fixture", "commit_fixture"),
    ("computer-use-1", "type_text")])
async def test_unrelated_approval_retains_original_frame_identity_and_connection_routing(remote_ingress, agent, tool):
    env = remote_ingress
    approve = await propose(env, "direct")
    request = approve["payload"]["hitl_request_id"]
    call = env.orch._hitl_pending_calls[request]
    env.orch._hitl_pending_calls[request] = replace(call, agent=agent, tool=tool)
    connection = env.orch._connection_contexts[id(env.ws)]
    original = env.orch._connection_frame(connection, json.dumps(approve), approve)
    await delivered(env.state, approve)
    admitted = next(value for value in env.state.frames if value.request_generation == original.request_generation)
    assert admitted.normalized_digest == original.normalized_digest
    assert admitted.operation_kind == "connection_frame" and not admitted.foreground
    assert env.effects == env.selections == []
