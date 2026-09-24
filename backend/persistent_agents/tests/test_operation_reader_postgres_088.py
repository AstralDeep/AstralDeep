"""Tests for persistent_agents dispatch readers against real Postgres, session and JWT:
settlement, cached-result reload, scope-change and policy-rewrite refusal,
cancellation-after-permit charging, and encrypted-credential handling.
"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import pytest
import astralplane
import requests
from agents.web_research import mcp_tools as page_tools
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent, AssignmentActionOutcome, AssignmentDefinition,
    AssignmentOperationAuthority, AssignmentOperationSpec,
    AssignmentResourceAmount,
)
from astralplane.repositories.history import SessionExecutionObservation
from audit import recorder as audit_recorder
from audit.recorder import Recorder
from audit.repository import AuditRepository
from orchestrator import session_authority
from orchestrator.governed_dispatch import GovernedFinalDispatch
from orchestrator.orchestrator import Orchestrator
from orchestrator.tool_permissions import ToolPermissionManager
from orchestrator.work_admission import WorkAdmissionCoordinator
from personalization.phi_gate import PHIGate
from persistent_agents.dispatch_context import DispatchDenied, canonical
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.runner import AssignmentRunner
from persistent_agents.runtime_values import digest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from shared.protocol import MCPResponse
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import signing_key as signing_key

URL = "https://93.184.216.34/releases"
runtime = plane
REQUEST = {"kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
           "arguments": {"url": URL}}


@pytest.fixture
async def operation(runtime, fixture, gate_orchestrator, monkeypatch, tmp_path, request):
    sessions, owner, sid, _, refreshes = fixture
    orch, untouched = gate_orchestrator
    untouched_session = dict(orch.ui_sessions[untouched])
    orch.agents = {"web-research-1": object()}
    orch.agent_cards = {"web-research-1": SimpleNamespace(
        name="Synthetic public reader", skills=[SimpleNamespace(id="fetch_page")])}
    orch._is_draft_agent = lambda _: False
    orch._chat_recorders = {}
    orch.agent_urls = {}
    orch.tool_permissions = ToolPermissionManager(plane_runtime=runtime)
    orch.tool_permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:read"})
    await asyncio.to_thread(orch.tool_permissions.set_agent_scopes,
                            owner, "web-research-1", {"tools:read": True})
    orch._get_delegation_token = Orchestrator._get_delegation_token.__get__(orch)
    orch._delegation_required = lambda: True
    delegations = []
    hooks = SimpleNamespace(before=None, after=None, delegated=None, prepared=None)

    async def exchange(token, agent, tools, user, scopes):
        delegations.append((token, user, tuple(tools), tuple(scopes)))
        if hooks.delegated:
            await hooks.delegated()
        return {"access_token": "synthetic-reader-delegation"}

    orch.delegation = SimpleNamespace(exchange_token_for_agent=exchange)
    orch.governed_final_dispatch = GovernedFinalDispatch.off()
    physical = []
    async def prepared(*args):
        if hooks.prepared:
            await hooks.prepared()
    orch._auto_subscribe_stream_artifacts = prepared

    async def transport(agent, tool, args, timeout, **kwargs):
        if hooks.before:
            await hooks.before()
        physical.append((agent, tool, dict(args)))
        if hooks.after:
            await hooks.after()
        page = requests.Response()
        page.url, page.status_code = args["url"], 200
        page.headers["Content-Type"] = "text/plain; charset=utf-8"
        page.encoding, page._content = "utf-8", b"Public release 088"
        with patch.object(page_tools, "_fetch_url", return_value=page):
            return MCPResponse(result=page_tools.fetch_page(url=args["url"]))

    orch._execute_via_websocket = transport
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-operation-audit-key-088")
    audit = Recorder(AuditRepository(plane_runtime=runtime), retry_queue=tmp_path / "audit-retry")
    monkeypatch.setattr(audit_recorder, "_RECORDER", audit)
    phi = PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: []))
    monkeypatch.setattr("persistent_agents.execution.get_phi_gate", lambda: phi)
    store = AssignmentStore(plane_runtime=runtime)
    service = AssignmentService(orch, store, enabled=True, phi_gate=phi)
    orch.work_admission = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
        plane_runtime=runtime, slot_lease=timedelta(seconds=60))
    runner = SimpleNamespace(orch=orch, service=service)

    def create():
        state = sessions.capture_execution_reference(owner_id=owner, session_id=sid).state
        observation = SessionExecutionObservation(state.credential, state.observed_at,
            state.observed_at + timedelta(seconds=15))
        identity = str(uuid4())
        with runtime.transaction() as tx:
            return runtime.repositories.assignments.create_operation(tx,
                owner_id=owner, assignment_id=identity, origin_namespace="test", caller_key=identity,
                command_digest=digest(identity), authority=observation,
                definition=AssignmentDefinition(name="Synthetic durable read", instructions="Read one page.",
                    source={"profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page",
                            "arguments": {"url": URL}, "linked_document_urls": []},
                    allowed_tools=("web-research-1:fetch_page",), consented_scopes=("tools:read", "tools:search"),
                    offline_grant_id=None,
                    limits={"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1, "max_tasks": 2,
                            "model_calls": 1, "tool_calls": 4, "tokens": 1000, "elapsed_ms": 120_000,
                            **getattr(request, "param", {})}),
                operation=AssignmentOperationSpec("research", AssignmentOperationAuthority(
                    owner, "interactive", "session_incarnation", state.credential.incarnation_id,
                    state.observed_at + timedelta(minutes=5)),
                    state.observed_at + timedelta(minutes=5), "operation"))

    record = await asyncio.to_thread(create)
    authority = await session_authority.refresh_operation_execution_authority(
        owner_id=owner, assignment_id=record.assignment_id, sessions=sessions, plane_runtime=runtime)
    claim = await store.call("claim_operation_for_administration", owner_id=owner,
        assignment_id=record.assignment_id, expected_state_version=record.state_version,
        worker_id="synthetic-reader", authority=authority.observation, lease_seconds=60)
    selected = await AssignmentRunner._admit(runner, claim)
    executor = ActionExecutor(runner, claim, selected, object(), operation_sessions=sessions)
    await store.call("bind_operation", fence=claim.fence, binding=executor.binding)
    state = SimpleNamespace(executor=executor, runtime=runtime, sessions=sessions, owner=owner, sid=sid,
        refreshes=refreshes, delegations=delegations, physical=physical, hooks=hooks, audit=audit)
    try:
        yield state
    finally:
        await audit.close()
        store.close()
        assert orch.ui_sessions == {untouched: untouched_session}


async def actions(op):
    return await op.executor.store.call("list_actions", owner_id=op.owner,
                                       assignment_id=op.executor.record.assignment_id)


async def current(op):
    return (await op.executor.store.call("get_operation", owner_id=op.owner,
                                        assignment_id=op.executor.record.assignment_id)).assignment


async def prepare(op, *, key="read"):
    checks = await op.executor.refresh(REQUEST)
    return await op.executor.store.call_for_operation("put_action_for_execution",
        fence=op.executor.claim.fence, binding=op.executor.binding,
        authority=checks["authority"].observation,
        intent=AssignmentActionIntent(key, REQUEST, digest(REQUEST),
            AssignmentResourceAmount(tool_calls=1, elapsed_ms=30_000),
            checks["permission_digest"], checks["precondition_digest"], boundary="read_only"))


async def control(op, command):
    record = await current(op)
    return await op.executor.store.call("apply_control", owner_id=op.owner,
        assignment_id=record.assignment_id, expected_state_version=record.state_version,
        expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch, submission_id=str(uuid4()),
        submission_digest=digest(command), control=command)


@pytest.mark.asyncio
async def test_real_reader_guards_dispatch_settlement_and_cached_result(operation):
    op = operation
    before = await current(op)
    result = await op.executor.action("read", REQUEST)
    assert "Public release 088" in result["text"]
    [action] = await actions(op)
    assert action.state == "succeeded"
    assert await op.executor.execute(action) == result
    assert len(op.physical) == len(op.delegations) == 1
    assert op.delegations[0][0] in [item[1] for item in op.refreshes]
    assert op.physical[0][2]["_delegation_token"] == "synthetic-reader-delegation"
    record = await current(op)
    assert record.usage["spent"]["tool_calls"] == 1
    assert record.checkpoint == before.checkpoint and record.tasks == before.tasks
    assert record.lifecycle == before.lifecycle
    repository = AuditRepository(plane_runtime=op.runtime)
    events, _ = await asyncio.to_thread(repository.list_for_user, op.owner)
    assert {event.action_type for event in events} >= {"tool.fetch_page.start", "tool.fetch_page.end"}
    assert await asyncio.to_thread(repository.verify_chain, op.owner) is None
    assert all(op.delegations[0][0] not in str(event) for event in events)


@pytest.mark.asyncio
async def test_replaced_session_after_permit_charges_once_without_content(operation):
    op = operation

    async def replace_issued_session():
        original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
        await asyncio.to_thread(replace_session_record, op.runtime, original)

    op.hooks.after = replace_issued_session
    with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
        await op.executor.action("read", REQUEST)
    [action] = await actions(op)
    assert action.result["result_available"] is False and action.result["result"] == {}
    assert (await current(op)).usage["spent"]["tool_calls"] == 1
    with pytest.raises(session_authority.SessionAuthorityUnavailable):
        await op.executor.execute(action)
    assert len(op.physical) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["session", "permission", "source", "model", "missing-resolver", "fanout"])
async def test_preparation_refuses_without_new_actions(operation, loss):
    op = operation
    request = REQUEST
    if loss == "session":
        original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
        await asyncio.to_thread(replace_session_record, op.runtime, original)
    elif loss == "permission":
        await asyncio.to_thread(op.executor.orch.tool_permissions.set_agent_scopes,
                                op.owner, "web-research-1", {"tools:read": False})
    elif loss == "source":
        request = {**REQUEST, "arguments": {"url": "https://93.184.216.34/unapproved"}}
    elif loss == "model":
        request = {"kind": "model", "messages": [{"role": "user", "content": "Do not run"}]}
    elif loss == "missing-resolver":
        op.executor.operation_sessions = None
    if loss == "fanout":
        with pytest.raises(DispatchDenied):
            op.executor.fork(object())
    else:
        with pytest.raises((DispatchDenied, AssignmentError, session_authority.SessionAuthorityUnavailable)):
            await op.executor.action("read", request)
    assert await actions(op) == ()
    assert op.physical == [] and op.delegations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["session", "owner", "admission", "pause", "permission", "private-context"])
async def test_loss_after_delegation_refuses_permit_and_releases_unstarted(operation, loss):
    op = operation

    async def lose():
        if loss == "session":
            original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
            await asyncio.to_thread(replace_session_record, op.runtime, original)
        elif loss == "owner":
            await op.executor.store.call("retire_operations_for_owner", owner_id=op.owner)
        elif loss == "admission":
            await asyncio.to_thread(op.executor.orch.work_admission.reselect_execution,
                                    op.executor.operation_fence)
        elif loss == "pause":
            await control(op, "pause")
        elif loss == "private-context":
            for key, value in list(op.executor.orch.ui_sessions.items()):
                if value.get("sub") == op.owner:
                    op.executor.orch.ui_sessions[key] = {"sub": "replacement"}
        else:
            await asyncio.to_thread(op.executor.orch.tool_permissions.set_agent_scopes,
                                    op.owner, "web-research-1", {"tools:read": False})

    op.hooks.prepared = lose
    with pytest.raises((DispatchDenied, AssignmentError)):
        await op.executor.action("read", REQUEST)
    assert len(op.delegations) == 1 and op.physical == []
    if loss == "owner":
        assert await op.executor.store.call("get_operation", owner_id=op.owner,
            assignment_id=op.executor.record.assignment_id) is None
        return
    [action] = await actions(op)
    assert not action.ever_started
    assert all(attempt.get("dispatch_token") is None for attempt in action.attempts)
    assert all(value == 0 for value in (await current(op)).usage["outstanding"].values())
    if loss == "private-context":
        replacement = [key for key, value in op.executor.orch.ui_sessions.items()
                       if value == {"sub": "replacement"}]
        assert len(replacement) == 1
        op.executor.orch.ui_sessions.pop(replacement[0])


@pytest.mark.asyncio
async def test_actual_policy_rewrite_is_refused_before_physical_permit(operation, monkeypatch):
    from orchestrator import policy
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setattr(policy, "load_rules", lambda: [
        {"effect": "rewrite", "rewrite": {"redact_args": ["url"]}}])
    with pytest.raises(DispatchDenied, match="assignment_action_binding_changed"):
        await operation.executor.action("read", REQUEST)
    [action] = await actions(operation)
    assert not action.ever_started and action.state == "failed_not_started"
    assert operation.physical == []


@pytest.mark.asyncio
async def test_cached_result_reloads_stored_content_and_refuses_retired_authority(operation):
    op = operation
    expected = await op.executor.action("read", REQUEST)
    [action] = await actions(op)
    supplied = replace(action, result={"result": {"text": "fabricated caller result"}})
    assert await op.executor.execute(supplied) == expected
    original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
    await asyncio.to_thread(replace_session_record, op.runtime, original)
    with pytest.raises(session_authority.SessionAuthorityUnavailable):
        await op.executor.execute(action)
    assert len(op.physical) == 1


@pytest.mark.asyncio
async def test_cancellation_after_permit_charges_before_releasing_private_context(operation):
    op = operation
    begun = asyncio.Event()

    async def wait():
        begun.set()
        await asyncio.Event().wait()

    op.hooks.after = wait
    task = asyncio.create_task(op.executor.action("read", REQUEST))
    await asyncio.wait_for(begun.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        [action] = await actions(op)
        if action.state == "failed":
            break
        await asyncio.sleep(0.01)
    assert action.state == "failed" and len(action.attempts) == 1
    assert (await current(op)).usage["spent"]["tool_calls"] == 1
    assert len(op.physical) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_live_consented_scope_change_denies_old_intent_and_cached_content(operation, cached):
    op = operation
    if cached:
        await op.executor.action("read", REQUEST)
        [action] = await actions(op)
    else:
        action = await prepare(op)
    permissions = op.executor.orch.tool_permissions
    permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
    await asyncio.to_thread(permissions.set_agent_scopes, op.owner, "web-research-1",
                            {"tools:read": True, "tools:search": True})
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await op.executor.refresh(REQUEST)
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await op.executor.execute(action)
    assert len(op.physical) == int(cached)
    assert (await actions(op))[0] == action


@pytest.mark.asyncio
async def test_real_gate_encrypted_credentials_keep_closed_marker(operation):
    op = operation
    op.executor.orch.credential_manager.get_agent_credentials_encrypted = (
        lambda *_: {"API_KEY": "synthetic-encrypted-ciphertext"})
    assert "Public release 088" in (await op.executor.action("read", REQUEST))["text"]
    sent = op.physical[0][2]
    assert sent["_credentials"] == {"API_KEY": "synthetic-encrypted-ciphertext"}
    assert sent["_credentials_encrypted"] is True
    assert "synthetic-encrypted-ciphertext" not in str(await actions(op))


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["governed", "authorize", "start"])
async def test_argument_mutation_across_await_cannot_change_physical_request(operation, monkeypatch, boundary):
    op = operation
    entered, release = asyncio.Event(), asyncio.Event()
    captured = {}
    governed = op.executor.orch._execute_governed_attempt
    refresh = op.executor.refresh
    transaction = op.executor._reader_policy_transaction

    async def pause():
        entered.set()
        await release.wait()

    async def gate(*args, **kwargs):
        captured["arguments"] = args[3]
        if boundary == "governed":
            await pause()
        return await governed(*args, **kwargs)

    async def authorize(*args, **kwargs):
        result = await refresh(*args, **kwargs)
        if boundary == "authorize" and captured and kwargs.get("authority") is not None:
            await pause()
        return result

    async def start(authority, action_id, callback):
        result = await transaction(authority, action_id, callback)
        if boundary == "start" and callback.__name__ == "commit":
            await pause()
        return result

    monkeypatch.setattr(op.executor.orch, "_execute_governed_attempt", gate)
    monkeypatch.setattr(op.executor, "refresh", authorize)
    monkeypatch.setattr(op.executor, "_reader_policy_transaction", start)
    task = asyncio.create_task(op.executor.action("read", REQUEST))
    await asyncio.wait_for(entered.wait(), 10)
    captured["arguments"]["url"] = "https://93.184.216.34/changed-after-await"
    release.set()
    with pytest.raises(DispatchDenied, match="assignment_action_binding_changed"):
        await task
    [action] = await actions(op)
    assert op.physical == []
    assert action.ever_started is (boundary == "start")
    assert (await current(op)).usage["spent"].get("tool_calls", 0) == int(boundary == "start")
    assert all(value == 0 for value in (await current(op)).usage["outstanding"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["untyped", "runtime", "incarnation"])
async def test_service_refuses_unbound_private_authority_without_grant_fallback(operation, loss):
    op = operation
    bundle = (await op.executor.refresh(REQUEST))["authority"]
    claims = bundle.claims
    if loss == "untyped":
        bundle = SimpleNamespace(**{name: getattr(bundle, name) for name in
            ("record", "observation", "plane_runtime", "claims")})
    elif loss == "runtime":
        bundle = replace(bundle, plane_runtime=object())
    else:
        bundle = replace(bundle, observation=replace(bundle.observation,
            credential=replace(bundle.observation.credential, incarnation_id=str(uuid4()))))
    with pytest.raises(AssignmentError, match="assignment_authorization_required"):
        await op.executor.service.validate_execution(op.owner, claims, op.executor.record,
            SimpleNamespace(request=REQUEST), authority=bundle)
    assert await actions(op) == () and op.physical == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["caller-intent", "stored-bound"])
async def test_reader_refuses_substituted_intent_or_unreviewed_stored_time_bound(operation, mismatch):
    op = operation
    if mismatch == "caller-intent":
        action = await prepare(op)
        action = replace(action, intent=replace(action.intent,
            maximum=AssignmentResourceAmount(tool_calls=1, elapsed_ms=31_000)))
        code = "assignment_action_binding_changed"
    else:
        checks = await op.executor.refresh(REQUEST)
        action = await op.executor.store.call_for_operation("put_action_for_execution",
            fence=op.executor.claim.fence, binding=op.executor.binding,
            authority=checks["authority"].observation,
            intent=AssignmentActionIntent("larger-bound", REQUEST, digest(REQUEST),
                AssignmentResourceAmount(tool_calls=1, elapsed_ms=31_000),
                checks["permission_digest"], checks["precondition_digest"], boundary="read_only"))
        code = "assignment_tool_time_bound_exceeded"
    with pytest.raises(DispatchDenied, match=code):
        await op.executor.execute(action)
    [stored] = await actions(op)
    assert not stored.ever_started and not stored.attempts and op.physical == []


@pytest.mark.asyncio
async def test_cancelled_result_authority_refresh_still_settles_authentic_permit(operation, monkeypatch):
    from unittest.mock import AsyncMock
    op = operation
    async def cancel_refresh():
        monkeypatch.setattr(session_authority.web_auth, "_exchange_session_refresh",
                            AsyncMock(side_effect=asyncio.CancelledError))
    op.hooks.after = cancel_refresh
    with pytest.raises(asyncio.CancelledError):
        await op.executor.action("read", REQUEST)
    [stored] = await actions(op)
    assert stored.state == "succeeded" and len(stored.attempts) == 1
    assert stored.result["result_available"] is False and stored.result["result"] == {}
    assert (await current(op)).usage["spent"]["tool_calls"] == 1
    assert len(op.physical) == 1


@pytest.mark.asyncio
async def test_required_governed_adapter_refusal_prevents_reader_permit(operation):
    op = operation
    op.executor.orch.governed_final_dispatch = GovernedFinalDispatch.unavailable("enforce")
    with pytest.raises(DispatchDenied, match="assignment_tool_refused"):
        await op.executor.action("read", REQUEST)
    [stored] = await actions(op)
    assert not stored.ever_started and stored.state == "failed_not_started"
    assert op.physical == []


@pytest.mark.asyncio
async def test_genuine_v1_ledger_refuses_new_reader_but_authentic_settlement_charges_once(operation):
    op = operation
    relative = "tests/fixtures/session_incarnation_088001.json"
    candidates = [Path(__file__).parents[3] / "components/AstralPlane" / relative,
                  Path(astralplane.__file__).resolve().parents[2] / relative]
    data = next(path for path in candidates if path.is_file()).read_bytes()
    assert hashlib.sha256(data).hexdigest() == "36ef17d004c846209ab2f806c02ec33e487491d914f86062d9851cbc46849530"
    fixture = json.loads(data)
    assert fixture["source_commit"] == "718021ba019abcd3a04ffbd6b80b88e51395bda6"
    def install():
        with op.runtime.transaction() as tx:
            for table in ("persistent_assignment", "assignment_operation_receipt",
                          "persistent_assignment_action", "persistent_assignment_activity"):
                for row in fixture["tables"][table]:
                    columns = tuple(row)
                    assert all(key.replace("_", "").isalnum() for key in columns)
                    values = tuple(canonical(row[key]) if isinstance(row[key], dict) else row[key]
                                   for key in columns)
                    placeholders = ",".join("%s::jsonb" if isinstance(row[key], dict) else "%s"
                                            for key in columns)
                    tx.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values)
    await asyncio.to_thread(install)
    for permit in fixture["permits"]:
        identifiers = {key: permit[key] for key in ("assignment_id", "action_id")}
        stored = await op.executor.store.call("get_action", owner_id="owner", **identifiers)
        record = await op.executor.store.call("get_assignment", owner_id="owner",
                                             assignment_id=permit["assignment_id"])
        legacy = ActionExecutor(op.executor.runner, SimpleNamespace(assignment=record),
            op.executor.operation_fence, object(), operation_sessions=op.sessions)
        with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
            await legacy.execute(stored)
        args = dict(owner_id="owner", **identifiers, attempt_id=permit["attempt_id"],
            dispatch_token=permit["dispatch_token"], expected_request_digest=permit["request_digest"],
            outcome=AssignmentActionOutcome("succeeded", digest("legacy result"), {"text": "not released"}))
        retained = await op.executor.store.call_for_operation("record_action_outcome", **args)
        assert retained.result["result_available"] is False and retained.result["result"] == {}
        assert await op.executor.store.call_for_operation("record_action_outcome", **args) == retained
        after = await op.executor.store.call("get_assignment", owner_id="owner",
                                            assignment_id=permit["assignment_id"])
        assert after.usage["spent"]["tool_calls"] == 1
        assert after.usage["outstanding"]["tool_calls"] == 0
        assert after.checkpoint == record.checkpoint and after.wake_generation == record.wake_generation
    assert op.physical == [] and op.delegations == []
