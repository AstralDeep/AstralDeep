"""Framework source-less chat execution uses real Plane credential and operation fences.
Provider responses are synthetic transport fixtures; these tests do not qualify a live model.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from llm_config import research_profile as profile
from orchestrator import framework_runtime
from persistent_agents.tests.test_chat_one_shot_postgres_088 import (
    ANSWER,
    chat_reply,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
    stored,
)
from shared.feature_flags import flags
from tests.helpers.session_consent_088 import consent_from_store

runtime = plane
pytestmark = [pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def framework(integrated, monkeypatch):
    op, runner, _client = integrated
    orch = op.executor.orch
    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    credentials = framework_runtime.start_framework_runtime(orch)
    row, token = credentials.issue(owner_id=op.owner,
        caller=consent_from_store(op.sessions, op.owner, op.sid).observation,
        name="Synthetic framework execution", scopes=("operations.submit", "operations.read",
            "operations.control", "artifacts.read"), expires_in_seconds=3600, max_admissions=1)
    caller = credentials.resolve_bearer(token)
    operations = orch.framework_work_operations
    seen = []
    before = None
    after = None

    async def transport(method, url, **kwargs):
        if state.before is not None:
            await state.before()
        invocation = next(value for value in orch.ui_sessions.values()
                          if value.get("machine_class") == "framework")
        seen.append(dict(invocation))
        op.model_calls.append((method, url, kwargs))
        if state.after is not None:
            await state.after()
        return SimpleNamespace(body=json.dumps(chat_reply()).encode(), status_code=200)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    state = SimpleNamespace(op=op, runner=runner, operations=operations, credentials=credentials,
        caller=caller, row=row, seen=seen, before=before, after=after)
    try:
        yield state
    finally:
        framework_runtime.stop_framework_runtime(orch)


async def submit(state, **changes):
    return await state.operations.submit(state.caller, idempotency_key=str(uuid4()),
        name="Bounded framework turn", instructions="Explain one durable task in one sentence.",
        limits={"model_calls": 1, "tool_calls": 1, "tokens": profile.RESERVED_TOKENS,
                "elapsed_ms": profile.RESERVED_MILLISECONDS, "max_retries": 0},
        deadline_in_seconds=300, **changes)


async def settle(state, identity):
    async with asyncio.timeout(20):
        while True:
            record, actions, _ = await stored(state.op, identity)
            if record["lifecycle"] in {"completed", "stopped"}:
                return record, actions
            await asyncio.sleep(0.05)


async def test_framework_chat_runs_after_issuer_logout_without_jwt_or_tools(framework):
    state = framework
    accepted = await submit(state)
    with state.op.runtime.transaction() as tx:
        page = state.op.runtime.repositories.assignments.discover_due_operations_for_administration(tx)
    assert any(record.assignment_id == accepted["id"] for record in page)
    await asyncio.to_thread(state.op.sessions.delete, state.op.sid)
    record, actions = await settle(state, accepted["id"])
    assert record["operation"]["authority"]["origin"] == "framework"
    assert record["operation"]["terminal_outcome"] == "completed"
    assert record["checkpoint"]["chat_result"]["text"] == ANSWER
    assert len(state.seen) == 1 and "_raw_token" not in state.seen[0]
    assert state.seen[0]["machine_class"] == "framework"
    assert state.seen[0]["_framework_credential_id"] == state.caller.credential_id
    assert len(actions) == 1 and actions[0]["state"] == "succeeded"
    assert len(state.op.model_calls) == 1
    assert state.credentials.list(owner_id=state.op.owner)[0]["consumed_admissions"] == 1
    result = await state.operations.result(state.caller, accepted["id"])
    assert result["result"]["available"] is True
    events, _ = await asyncio.to_thread(state.op.audit._repo.list_for_user, state.op.owner)
    with state.op.runtime.transaction() as tx:
        calls = state.op.runtime.repositories.audit.list_for_chain(tx, chain_id=state.op.owner)
    assert any(event.event.auth_principal == "framework:" + state.caller.credential_id
               for event in calls if event.event.action_type.startswith("llm.call."))
    assert await asyncio.to_thread(state.op.audit._repo.verify_chain, state.op.owner) is None
    await state.runner.tick()
    assert len(state.op.model_calls) == 1
    assert not any(v.get("machine_class") == "framework" for v in state.op.executor.orch.ui_sessions.values())


@pytest.mark.parametrize("when", ["before_claim", "before_send", "after_send"])
async def test_credential_revocation_never_releases_a_retained_answer(framework, monkeypatch, when):
    state = framework
    original = state.runner._operation_authority
    revoked = False

    async def revoke():
        nonlocal revoked
        if not revoked:
            revoked = True
            await asyncio.to_thread(state.credentials.revoke, owner_id=state.op.owner,
                                    credential_id=state.caller.credential_id)

    if when == "before_claim":
        async def authority(record):
            await revoke()
            return await original(record)
        monkeypatch.setattr(state.runner, "_operation_authority", authority)
    elif when == "before_send":
        from persistent_agents.dispatch_context import PersistentDispatchContext
        original_invoke = PersistentDispatchContext.invoke_model
        async def invoke(context, *args, **kwargs):
            await revoke()
            return await original_invoke(context, *args, **kwargs)
        monkeypatch.setattr(PersistentDispatchContext, "invoke_model", invoke)
    else:
        state.after = revoke
    accepted = await submit(state)
    await state.runner._tick_operations()
    if when != "before_claim":
        async with asyncio.timeout(20):
            while state.runner._active:
                await asyncio.sleep(0.05)
    record, actions, _ = await stored(state.op, accepted["id"])
    assert "chat_result" not in record["checkpoint"]
    assert record["operation"].get("terminal_outcome") != "completed"
    assert len(state.op.model_calls) == (1 if when == "after_send" else 0)
    assert state.credentials.list(owner_id=state.op.owner)[0]["revoked"] is True
    assert all((action.get("result") or {}).get("result_available") is not True for action in actions)


@pytest.mark.parametrize("point", ["before_send", "after_send"])
async def test_normal_cancel_preserves_control_fence_and_suppresses_answer(framework, monkeypatch, point):
    state = framework
    identity = None
    import contextvars
    external_context = contextvars.copy_context()

    async def cancel():
        current = await state.operations.get(state.caller, identity)
        await state.operations.cancel(state.caller, identity,
            submission_id=str(uuid4()), expected_revision=current["revision"])

    async def external_cancel():
        return await asyncio.create_task(cancel(), context=external_context.copy())

    if point == "before_send":
        from persistent_agents.dispatch_context import PersistentDispatchContext
        original = PersistentDispatchContext.invoke_model
        async def invoke(context, *args, **kwargs):
            await external_cancel()
            return await original(context, *args, **kwargs)
        monkeypatch.setattr(PersistentDispatchContext, "invoke_model", invoke)
    else:
        state.after = external_cancel
    identity = (await submit(state))["id"]
    record, actions = await settle(state, identity)
    assert record["operation"]["terminal_outcome"] == "cancelled"
    async with asyncio.timeout(20):
        while state.runner._active:
            await asyncio.sleep(0.05)
    assert "chat_result" not in record["checkpoint"]
    assert len(state.op.model_calls) == (1 if point == "after_send" else 0)
    result = await state.operations.result(state.caller, identity)
    assert result["result"]["available"] is False
    assert not any(v.get("machine_class") == "framework" for v in state.op.executor.orch.ui_sessions.values())


@pytest.mark.parametrize("change", ["runtime", "owner", "credential", "scope", "untyped", "kind",
                                   "source", "tools", "offline", "selection", "origin"])
async def test_framework_authority_cannot_authorize_displaced_or_broader_work(framework, change):
    from dataclasses import replace
    from orchestrator.framework_operation_authority import (
        authority_repository, refresh_assignment_execution_authority,
    )
    from persistent_agents.runtime_values import thaw

    state = framework
    accepted = await submit(state)
    record = await state.runner.store.call("get_operation", owner_id=state.op.owner,
                                           assignment_id=accepted["id"])
    authority = await refresh_assignment_execution_authority(record.assignment,
        sessions=state.op.sessions, plane_runtime=state.op.runtime)
    current = authority.record
    if change == "runtime":
        authority = replace(authority, plane_runtime=object())
    elif change == "untyped":
        authority = object()
    elif change in {"owner", "credential", "scope"}:
        field = {"owner": "owner_id", "credential": "credential_id", "scope": "scopes"}[change]
        value = ("operations.read",) if change == "scope" else str(uuid4())
        authority = replace(authority, observation=replace(authority.observation,
            credential=replace(authority.observation.credential, **{field: value})))
    elif change in {"source", "tools", "offline"}:
        values = {"source": {"url": "https://example.invalid"},
                  "allowed_tools": ("web-research-1:fetch_page",), "offline_grant_id": str(uuid4())}
        field = {"source": "source", "tools": "allowed_tools", "offline": "offline_grant_id"}[change]
        authority = replace(authority, record=replace(current,
            definition=replace(current.definition, **{field: values[field]})))
    else:
        operation = thaw(current.operation)
        if change == "kind":
            operation["kind"] = "research"
        elif change == "origin":
            operation["authority"]["origin"] = "interactive"
        else:
            operation["selection"] = {"version": 1}
        authority = replace(authority, record=replace(current, operation=operation))
    from orchestrator.session_authority import SessionAuthorityUnavailable
    from persistent_agents.dispatch_context import DispatchDenied
    with pytest.raises((SessionAuthorityUnavailable, DispatchDenied)):
        authority_repository(state.op.runtime, authority)
    assert "token_hash" not in repr(authority)


@pytest.mark.parametrize("change", ["revise", "pause", "retire", "missing", "outage"])
async def test_refresh_refuses_changed_snapshot_or_unavailable_authority(framework, monkeypatch, change):
    from dataclasses import replace
    from orchestrator.framework_operation_authority import refresh_assignment_execution_authority
    from orchestrator.session_authority import SessionAuthorityUnavailable
    from persistent_agents.runtime_values import digest

    state = framework
    await state.runner.stop()
    accepted = await submit(state)
    read = await state.runner.store.call("get_operation", owner_id=state.op.owner,
                                        assignment_id=accepted["id"])
    record = read.assignment
    if change in {"revise", "pause"}:
        await state.runner.store.call("apply_control", owner_id=state.op.owner,
            assignment_id=record.assignment_id, expected_state_version=record.state_version,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch, submission_id=str(uuid4()),
            submission_digest=digest(change), control=change,
            replacement=replace(record.definition, instructions="A different bounded instruction.")
                if change == "revise" else None)
    elif change == "retire":
        await state.runner.store.call("retire_operations_for_owner", owner_id=state.op.owner)
    elif change == "missing":
        record = replace(record, assignment_id=str(uuid4()))
    else:
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("synthetic private authority diagnostic")
        monkeypatch.setattr(state.runner.store.repository,
            "capture_framework_operation_execution_observation", unavailable)
    with pytest.raises(SessionAuthorityUnavailable) as caught:
        await refresh_assignment_execution_authority(record, sessions=state.op.sessions,
                                                      plane_runtime=state.op.runtime)
    assert str(caught.value) == "framework_operation_authority_unavailable"
    assert "synthetic private authority diagnostic" not in repr(caught.value)
    assert state.op.model_calls == []


async def test_framework_turn_renews_and_cancellation_recovers_without_redispatch(framework, monkeypatch):
    from datetime import datetime
    from persistent_agents.runner import AssignmentRunner
    from tests.helpers.database_clock import wait_for_database_time

    state = framework
    entered, renewed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    original = state.runner.store.repository.renew_claim

    def renew(tx, **kwargs):
        result = original(tx, **kwargs)
        loop.call_soon_threadsafe(renewed.set)
        return result

    monkeypatch.setattr(state.runner.store.repository, "renew_claim", renew)

    async def hold():
        entered.set()
        await release.wait()

    state.after = hold
    identity = (await submit(state))["id"]
    await asyncio.wait_for(entered.wait(), 20)
    await asyncio.wait_for(renewed.wait(), 20)
    await state.runner.stop()
    record, actions, _ = await stored(state.op, identity)
    assert len(state.op.model_calls) == 1 and "chat_result" not in record["checkpoint"]
    assert not state.runner._active
    assert not any(v.get("machine_class") == "framework" for v in state.op.executor.orch.ui_sessions.values())
    if record["lease_expires_at"] is not None:
        await wait_for_database_time(state.op.runtime, datetime.fromisoformat(record["lease_expires_at"]))
    restarted = AssignmentRunner(state.op.executor.orch, state.runner.service,
        config=state.runner.config, one_shot=state.runner.one_shot)
    try:
        await restarted.tick()
        await restarted.tick()
        async with asyncio.timeout(20):
            while restarted._active:
                await asyncio.sleep(0.05)
        current, _, _ = await stored(state.op, identity)
        assert len(state.op.model_calls) == 1 and "chat_result" not in current["checkpoint"]
        assert state.credentials.list(owner_id=state.op.owner)[0]["consumed_admissions"] == 1
        result = await state.operations.result(state.caller, identity)
        assert result["result"]["available"] is False
    finally:
        release.set()
        await restarted.stop()


async def test_configuration_removal_after_send_quarantines_framework_answer(framework):
    state = framework

    async def clear():
        await state.op.executor.orch._llm_store.clear(state.op.owner)

    state.after = clear
    accepted = await submit(state)
    async with asyncio.timeout(20):
        while not state.op.model_calls or state.runner._active:
            await asyncio.sleep(0.05)
    record, actions, _ = await stored(state.op, accepted["id"])
    assert len(state.op.model_calls) == 1 and "chat_result" not in record["checkpoint"]
    assert all((action.get("result") or {}).get("result_available") is not True for action in actions)
    result = await state.operations.result(state.caller, accepted["id"])
    assert result["result"]["available"] is False


async def test_disabling_framework_credentials_stops_queued_execution_authority(framework, monkeypatch):
    from orchestrator.framework_operation_authority import refresh_assignment_execution_authority
    from orchestrator.session_authority import SessionAuthorityUnavailable

    state = framework
    await state.runner.stop()
    accepted = await submit(state)
    read = await state.runner.store.call("get_operation", owner_id=state.op.owner,
                                        assignment_id=accepted["id"])
    monkeypatch.setitem(flags._flags, "framework_credentials", False)
    with pytest.raises(SessionAuthorityUnavailable):
        await refresh_assignment_execution_authority(read.assignment, sessions=state.op.sessions,
                                                      plane_runtime=state.op.runtime)
    assert state.op.model_calls == []
