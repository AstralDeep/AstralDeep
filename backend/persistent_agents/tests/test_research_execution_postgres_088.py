"""Real one-shot action ledger with synthetic final IAM and model transports.

The actual fixed page reader, USER configuration encryption, model dispatcher,
session/claim guards and PostgreSQL accounting run. No provider is contacted.
"""

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from llm_config import research_profile as profile
from llm_config.audit_events import record_llm_call
from llm_config.tests.test_research_profile_088 import reply
from llm_config.user_store import UserLLMConfigStore
from orchestrator.orchestrator import Orchestrator
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.dispatch_context import PersistentDispatchContext
from persistent_agents.models import AssignmentError
from persistent_agents.research_input import ResearchInput
from persistent_agents.runtime_values import thaw
from persistent_agents.tests.test_operation_reader_postgres_088 import (
    REQUEST,
    actions,
    current,
    control,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    signing_key as signing_key,
)
from tests.helpers.session_plane_runtime import (
    get_session_record,
    replace_session_record,
)

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]


@pytest.fixture
async def research(operation, monkeypatch, tmp_path):
    op = operation
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "research_test")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-research-binding-key-" + "x" * 32
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", raising=False)
    store = UserLLMConfigStore(plane_runtime=op.runtime, data_dir=str(tmp_path))
    await store.set(
        op.owner,
        provider="openai",
        base_url=profile.BASE_URL,
        model=profile.MODEL,
        api_key="synthetic-provider-key-not-used",
    )
    orch = op.executor.orch
    orch._llm_store = store
    orch.audit_recorder = op.audit
    orch._record_llm_call = record_llm_call
    orch._call_llm = Orchestrator._call_llm.__get__(orch)
    op.model_calls = []
    op.model_response = reply()
    op.model_status = 200
    op.model_before = None
    op.model_after = None

    async def physical(method, url, **kwargs):
        if op.model_before:
            await op.model_before()
        op.model_calls.append((method, url, kwargs))
        if op.model_after:
            await op.model_after()
        return SimpleNamespace(
            body=json.dumps(op.model_response).encode(), status_code=op.model_status
        )

    monkeypatch.setattr("shared.isolated_http.request", physical)
    op.source = await op.executor.action("page", REQUEST)
    [op.source_action] = await actions(op)
    return op


async def select(op):
    return await op.executor.research_selection(
        "selected", source_action_id=op.source_action.action_id
    )


async def model_action(op):
    return next(
        action
        for action in await actions(op)
        if action.intent.request["kind"] == "model"
    )


async def test_one_fixed_send_caches_only_bound_selection_and_accounts(research):
    op = research
    result = await select(op)
    assert result["passage_ids"] == ["p001"]
    assert result["source_action_id"] == op.source_action.action_id
    assert await select(op) == result
    assert len(op.model_calls) == 1
    method, url, sent = op.model_calls[0]
    assert method == "POST" and url == profile.ENDPOINT
    assert sent["api_key"] == "synthetic-provider-key-not-used"
    assert sent["json_body"]["store"] is False
    stored = await model_action(op)
    serialized = json.dumps(thaw(stored))
    assert op.source["text"] not in serialized
    assert "synthetic-provider-key-not-used" not in serialized
    assert "messages" not in stored.intent.request
    assert len(stored.attempts) == 1 and stored.state == "succeeded"
    assert (await current(op)).usage["spent"]["tokens"] == 120


async def test_revoke_committed_during_actual_config_lock_wait_prevents_send(
    research, monkeypatch
):
    op = research
    config = op.runtime.repositories.encrypted_llm_config
    original = config.get_user_for_update
    locked, requesting = threading.Event(), threading.Event()
    shared = {}

    def get_user(tx, *, owner_id):
        shared["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
        requesting.set()
        return original(tx, owner_id=owner_id)

    monkeypatch.setattr(config, "get_user_for_update", get_user)

    def writer():
        with op.runtime.transaction() as tx:
            original(tx, owner_id=op.owner)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            locked.set()
            assert requesting.wait(5), "permit never reached its config read"
            end = time.monotonic() + 0.08
            while time.monotonic() < end:
                waiting = tx.fetch_one(
                    "SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                    (blocker, shared["pid"]),
                )["waiting"]
                if waiting:
                    shared["observed_wait"] = True
                    break
                time.sleep(0.001)
            assert shared.get("observed_wait"), "actual row-lock wait was not observed"
            op.runtime.repositories.tool_policy_state.set_scopes(
                tx,
                owner_id=op.owner,
                agent_id="web-research-1",
                scopes={"tools:read": False},
                updated_at=int(time.time() * 1000),
            )

    worker = asyncio.create_task(asyncio.to_thread(writer))
    try:
        assert await asyncio.to_thread(locked.wait, 5)
        with pytest.raises((DispatchDenied, AssignmentError)):
            await select(op)
    finally:
        await worker
    assert shared["observed_wait"]
    assert op.model_calls == []
    stored = await model_action(op)
    assert not stored.ever_started and stored.state == "failed_not_started"
    assert (await current(op)).usage["outstanding"]["tokens"] == 0


@pytest.mark.parametrize(
    "case", ["wrong-model", "bad-selection", "missing-usage", "http-error"]
)
async def test_unusable_response_retains_known_or_uncertain_charge(research, case):
    op = research
    if case == "wrong-model":
        op.model_response["model"] = "another-model"
    elif case == "bad-selection":
        op.model_response["choices"][0]["message"]["content"] = "private model prose"
    elif case == "missing-usage":
        op.model_response["usage"] = None
    else:
        op.model_status = 500
    with pytest.raises(DispatchDenied):
        await select(op)
    stored = await model_action(op)
    assert len(op.model_calls) == len(stored.attempts) == 1
    known = case in {"wrong-model", "bad-selection"}
    assert stored.state == ("failed" if known else "uncertain")
    assert "private model prose" not in json.dumps(thaw(stored))
    usage = (await current(op)).usage
    assert usage["spent"]["tokens"] == (120 if known else 0)
    assert usage["outstanding"]["tokens"] == (0 if known else profile.RESERVED_TOKENS)


async def test_cached_output_refuses_changed_current_permission_binding(research):
    op = research
    await select(op)
    manager = op.executor.orch.tool_permissions
    manager.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
    await asyncio.to_thread(
        manager.set_agent_scopes,
        op.owner,
        "web-research-1",
        {"tools:read": True, "tools:search": True},
    )
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await select(op)
    assert len(op.model_calls) == 1


async def change_config(op):
    # Even a same-value save has a new encrypted row and full timestamp binding.
    await op.executor.orch._llm_store.set(
        op.owner,
        provider="openai",
        base_url=profile.BASE_URL,
        model=profile.MODEL,
        api_key="synthetic-replacement-key",
    )


async def lose(op, monkeypatch, kind):
    if kind == "config":
        await change_config(op)
    elif kind == "key":
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-new-binding-" + "y" * 32)
    elif kind == "session":
        old = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
        await asyncio.to_thread(replace_session_record, op.runtime, old)
    elif kind == "permission":
        await asyncio.to_thread(
            op.executor.orch.tool_permissions.set_agent_scopes,
            op.owner,
            "web-research-1",
            {"tools:read": False},
        )
    elif kind == "pause":
        await control(op, "pause")


@pytest.mark.parametrize("loss", ["config", "key", "session", "permission", "pause"])
async def test_post_effect_loss_settles_once_without_usable_content(
    research, monkeypatch, loss
):
    op = research

    async def change():
        await lose(op, monkeypatch, loss)

    op.model_after = change
    with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
        await select(op)
    stored = await model_action(op)
    assert stored.result["result_available"] is False
    assert stored.result["result"] == {}
    assert len(op.model_calls) == len(stored.attempts) == 1
    assert (await current(op)).usage["spent"]["tokens"] == 120


@pytest.mark.parametrize("loss", ["config", "key"])
async def test_cached_model_output_requires_current_private_binding(
    research, monkeypatch, loss
):
    op = research
    await select(op)
    await lose(op, monkeypatch, loss)
    with pytest.raises(DispatchDenied):
        await select(op)
    assert len(op.model_calls) == 1


@pytest.mark.parametrize("loss", ["config", "key", "nested-session"])
async def test_prepermit_binding_loss_does_not_send_and_releases_reservation(
    research, monkeypatch, loss
):
    op = research
    invoke = PersistentDispatchContext.invoke_model

    async def held(context, physical, kwargs):
        if loss == "nested-session":
            for session in op.executor.orch.ui_sessions.values():
                if session.get("sub") == op.owner:
                    session["realm_access"]["roles"].append("administrator")
        else:
            await lose(op, monkeypatch, loss)
        return await invoke(context, physical, kwargs)

    monkeypatch.setattr(PersistentDispatchContext, "invoke_model", held)
    with pytest.raises((DispatchDenied, AssignmentError)):
        await select(op)
    stored = await model_action(op)
    assert not stored.ever_started and op.model_calls == []
    assert all(
        value == 0 for value in (await current(op)).usage["outstanding"].values()
    )


@pytest.mark.parametrize("boundary", ["authorize", "start"])
async def test_final_body_mutation_across_wait_never_sends(
    research, monkeypatch, boundary
):
    op = research
    invoke = PersistentDispatchContext.invoke_model

    async def capture(context, physical, kwargs):
        original = getattr(context, boundary)

        async def changed():
            value = await original()
            kwargs["messages"][1]["content"] = "changed after awaited boundary"
            return value

        setattr(context, boundary, changed)
        return await invoke(context, physical, kwargs)

    monkeypatch.setattr(PersistentDispatchContext, "invoke_model", capture)
    with pytest.raises(DispatchDenied):
        await select(op)
    stored = await model_action(op)
    assert op.model_calls == []
    assert stored.ever_started is (boundary == "start")
    assert stored.state == (
        "uncertain" if boundary == "start" else "failed_not_started"
    )


async def test_unknown_permit_acknowledgement_never_sends_or_refunds(
    research, monkeypatch
):
    op = research
    transaction = op.executor._research_transaction

    async def lose_ack(private, authority, callback, **kwargs):
        result = await transaction(private, authority, callback, **kwargs)
        if callback.__name__ == "commit":
            raise OSError("synthetic acknowledgement loss")
        return result

    monkeypatch.setattr(op.executor, "_research_transaction", lose_ack)
    with pytest.raises(OSError, match="acknowledgement"):
        await select(op)
    stored = await model_action(op)
    assert stored.ever_started and stored.state == "started"
    assert op.model_calls == []
    assert (await current(op)).usage["outstanding"]["tokens"] == profile.RESERVED_TOKENS
    with pytest.raises(DispatchDenied, match="assignment_action_uncertain"):
        await select(op)
    assert len((await model_action(op)).attempts) == 1


async def test_lost_settlement_ack_uses_identical_receipt_without_double_charge(
    research, monkeypatch
):
    op = research
    transaction = op.executor._research_transaction

    async def lose_ack(private, authority, callback, **kwargs):
        result = await transaction(private, authority, callback, **kwargs)
        if callback.__name__ == "settle":
            raise OSError("synthetic settlement acknowledgement loss")
        return result

    monkeypatch.setattr(op.executor, "_research_transaction", lose_ack)
    with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
        await select(op)
    assert len(op.model_calls) == len((await model_action(op)).attempts) == 1
    assert (await current(op)).usage["spent"]["tokens"] == 120


async def test_cancelled_result_authority_refresh_still_settles(research, monkeypatch):
    op = research
    refresh = op.executor.refresh

    async def cancelled(*args, **kwargs):
        if kwargs.get("_research") is not None and kwargs.get("authority") is None:
            raise asyncio.CancelledError
        return await refresh(*args, **kwargs)

    monkeypatch.setattr(op.executor, "refresh", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await select(op)
    stored = await model_action(op)
    assert stored.result["result_available"] is False
    assert (await current(op)).usage["spent"]["tokens"] == 120


async def test_repeated_caller_cancellation_owns_exactly_one_settlement(
    research, monkeypatch
):
    op = research
    begun, observing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def physical_wait():
        begun.set()
        await asyncio.Event().wait()

    op.model_after = physical_wait
    transaction = op.executor._research_transaction
    observer_calls = []

    async def held(private, authority, callback, **kwargs):
        if callback.__name__ == "settle":
            observer_calls.append(callback)
            observing.set()
            await finish.wait()
        return await transaction(private, authority, callback, **kwargs)

    monkeypatch.setattr(op.executor, "_research_transaction", held)
    task = asyncio.create_task(select(op))
    await asyncio.wait_for(begun.wait(), 5)
    task.cancel()
    await asyncio.wait_for(observing.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(observer_calls) == len(op.model_calls) == 1
    assert (await model_action(op)).state == "uncertain"
    assert (await current(op)).usage["outstanding"]["tokens"] == profile.RESERVED_TOKENS


async def test_cached_receipt_refuses_contradictory_attempt(research):
    op = research
    await select(op)
    private = await ResearchInput.capture(
        await current(op), op.source_action, config_store=op.executor.orch._llm_store
    )
    action = await model_action(op)
    attempt = thaw(action.attempts[-1])
    attempt["outcome"]["result"] = {"passage_ids": []}
    contradictory = replace(action, attempts=(attempt,))
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        private.retained_result(contradictory)


@pytest.mark.parametrize(
    "mutation",
    ["foreign-owner", "failed", "unavailable", "no-attempt", "contradiction"],
)
async def test_private_input_refuses_unsettled_or_foreign_source(research, mutation):
    op = research
    action = op.source_action
    if mutation == "foreign-owner":
        action = replace(action, owner_id="foreign-owner")
    elif mutation == "failed":
        action = replace(action, state="failed")
    elif mutation == "unavailable":
        result = thaw(action.result)
        result["result_available"] = False
        action = replace(action, result=result)
    elif mutation == "no-attempt":
        action = replace(action, attempts=())
    else:
        attempt = thaw(action.attempts[-1])
        attempt["outcome"]["result"] = {}
        action = replace(action, attempts=(attempt,))
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await ResearchInput.capture(
            await current(op), action, config_store=op.executor.orch._llm_store
        )
    assert op.model_calls == []


@pytest.mark.parametrize("mutation", ["record", "owner", "body", "action", "receipt"])
async def test_private_input_rejects_substituted_owner_body_and_receipt(
    research, mutation
):
    op = research
    await select(op)
    record = await current(op)
    private = await ResearchInput.capture(
        record, op.source_action, config_store=op.executor.orch._llm_store
    )
    action = await model_action(op)
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        if mutation == "record":
            operation = thaw(record.operation)
            operation["kind"] = "chat"
            private.assert_record(replace(record, operation=operation))
        elif mutation == "owner":
            private.assert_body("foreign", private.body())
        elif mutation == "body":
            body = private.body()
            body["store"] = True
            private.assert_body(op.owner, body)
        elif mutation == "action":
            private.assert_action(
                replace(
                    action, intent=replace(action.intent, request={"kind": "model"})
                )
            )
        else:
            result = thaw(action.result)
            result["result_digest"] = "0" * 64
            attempt = thaw(action.attempts[-1])
            attempt["outcome"]["result_digest"] = "0" * 64
            private.retained_result(replace(action, result=result, attempts=(attempt,)))
    assert len(op.model_calls) == 1


@pytest.mark.parametrize(
    "mutation", ["key", "source-id", "missing-session-resolver", "interactive"]
)
async def test_unsupported_invocations_cannot_prepare_a_model_action(
    research, mutation
):
    op = research
    key, source = "selected", op.source_action.action_id
    if mutation == "key":
        key = "invalid/key"
    elif mutation == "source-id":
        source = "invalid-source"
    elif mutation == "missing-session-resolver":
        op.executor.operation_sessions = None
    else:
        op.executor.interactive = True
    with pytest.raises(DispatchDenied):
        await op.executor.research_selection(key, source_action_id=source)
    assert len(await actions(op)) == 1 and op.model_calls == []


async def test_provider_overrun_is_charged_without_clamping(research):
    op = research
    op.model_response["usage"] = {
        "prompt_tokens": 200_000,
        "completion_tokens": 10,
        "total_tokens": 200_010,
    }
    with pytest.raises(DispatchDenied, match="assignment_research_response_refused"):
        await select(op)
    assert (await current(op)).usage["spent"]["tokens"] == 200_010
    assert (await model_action(op)).state == "failed"


async def test_result_authority_lock_cancellation_still_settles(research, monkeypatch):
    from persistent_agents.execution import _OperationAuthorityWindow

    op = research
    acquire = _OperationAuthorityWindow.acquire

    async def cancelled(window):
        if op.model_calls:
            raise asyncio.CancelledError
        return await acquire(window)

    monkeypatch.setattr(_OperationAuthorityWindow, "acquire", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await select(op)
    assert (await model_action(op)).result["result_available"] is False
    assert (await current(op)).usage["spent"]["tokens"] == 120


async def test_uncertain_settlement_ack_never_starts_a_second_observer(
    research, monkeypatch
):
    op = research
    invoke = PersistentDispatchContext.invoke_model
    observed = []

    async def capture(context, physical, kwargs):
        original = context.observe

        async def observer(*args):
            observed.append(args[0])
            await original(*args)
            raise OSError("synthetic lost final observer acknowledgement")

        context.observe = observer
        return await invoke(context, physical, kwargs)

    monkeypatch.setattr(PersistentDispatchContext, "invoke_model", capture)
    with pytest.raises(OSError, match="acknowledgement"):
        await select(op)
    assert len(observed) == len(op.model_calls) == 1
    assert (await current(op)).usage["spent"]["tokens"] == 120
