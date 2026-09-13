"""Non-retained source custody through real IAM verification, dispatch and PG.

Only external source/model/JWKS responses are synthetic. No provider is called.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from llm_config import research_profile as profile
from llm_config.audit_events import record_llm_call
from llm_config.tests.test_research_profile_088 import reply
from llm_config.user_store import UserLLMConfigStore
from orchestrator.orchestrator import Orchestrator
from orchestrator.session_authority import SessionAuthorityUnavailable
from orchestrator.work_admission import OperationState
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runtime_values import thaw
from persistent_agents.tests import test_operation_reader_postgres_088 as reader
from persistent_agents.tests.test_research_episode_postgres_088 import (
    actions, attach_runner, current,
    fixture as fixture, gate_orchestrator as gate_orchestrator,
    plane as plane, signing_key as signing_key,
)
from persistent_agents.tests.test_research_continuation_postgres_088 import fresh_executor

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def operation(runtime, fixture, gate_orchestrator, monkeypatch, tmp_path, request):
    # Construct the real supported Plane operation with the existing none
    # retention policy; no stored-row mutation or runtime admission bypass.
    constructor = reader.AssignmentOperationSpec
    monkeypatch.setattr(reader, "AssignmentOperationSpec",
        lambda *a, **kw: replace(constructor(*a, **kw), source_retention="none"))
    iterator = reader.operation.__wrapped__(runtime, fixture, gate_orchestrator,
        monkeypatch, tmp_path,
        SimpleNamespace(param={"tokens": 300_000, "model_calls": 2,
                               **getattr(request, "param", {})}))
    op = await anext(iterator)
    try:
        yield op
    finally:
        await iterator.aclose()


@pytest.fixture
async def ephemeral(operation, monkeypatch, tmp_path):
    op = operation
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "research_test")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-recovery-key-" + "x" * 40)
    monkeypatch.delenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", raising=False)
    store = UserLLMConfigStore(plane_runtime=op.runtime, data_dir=str(tmp_path))
    await store.set(op.owner, provider="openai", base_url=profile.BASE_URL,
                   model=profile.MODEL, api_key="synthetic-provider-key-not-used")
    orch = op.executor.orch
    orch._llm_store, orch.audit_recorder = store, op.audit
    orch._record_llm_call = record_llm_call
    orch._call_llm = Orchestrator._call_llm.__get__(orch)
    op.model_calls, op.model_response, op.model_status = [], reply(), 200

    async def physical(method, url, **kwargs):
        op.model_calls.append((method, url, kwargs))
        return SimpleNamespace(body=json.dumps(op.model_response).encode(),
                               status_code=op.model_status)

    monkeypatch.setattr("shared.isolated_http.request", physical)
    return op


async def test_none_retention_completes_without_durable_source_or_model_text(ephemeral):
    op = ephemeral
    runner = attach_runner(op, run_research_episode)
    result = await run_research_episode(op.executor)
    final = await runner._finish_operation(op.executor, result)
    assert final.lifecycle == "completed"
    assert len(op.physical) == len(op.model_calls) == 1
    assert final.usage["spent"]["tool_calls"] == 1
    assert final.usage["spent"]["model_calls"] == 1
    assert final.usage["spent"]["tokens"] == 120
    ledger = await actions(op)
    assert len(ledger) == 2
    for action in ledger:
        assert action.state == "succeeded"
        assert action.result["result_available"] is False
        assert action.result["result"] == {}
        assert action.result["result_disposition"]["reason"] == "retention_discarded"
    serialized = json.dumps([thaw(final), *map(thaw, ledger)])
    assert "Public release 088" not in serialized
    assert "synthetic-provider-key-not-used" not in serialized
    assert "research_result" not in final.checkpoint
    assert final.checkpoint["research_source"]["text_available"] is False
    assert final.checkpoint["research_source"]["reacquisition"]["required"] is False
    assert final.operation["result_reference"] in {a.action_id for a in ledger}
    assert all(value == 0 for value in final.usage["outstanding"].values())


@pytest.mark.parametrize("transition", ["restart", "pause_resume"])
async def test_discarded_source_is_reacquired_and_charged_after_real_claim_recovery(
    ephemeral, monkeypatch, transition
):
    op = ephemeral
    runner = attach_runner(op, run_research_episode)
    old_executor = op.executor
    captured = []

    async def process_exit(*args, **kwargs):
        assert not op.model_calls
        captured.append(kwargs["ephemeral"])
        raise InterruptedError("synthetic exit before model preparation")

    monkeypatch.setattr(old_executor, "research_selection", process_exit)
    with pytest.raises(InterruptedError):
        await run_research_episode(old_executor)
    [first] = await actions(op)
    assert first.state == "succeeded" and first.result["result_available"] is False
    assert first.result["result"] == {}
    if transition == "restart":
        expiry = datetime.now(UTC) - timedelta(seconds=1)
        with op.runtime.transaction() as tx:
            tx.execute("UPDATE persistent_assignment SET lease_expires_at=%s, "
                "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
                (expiry, expiry.isoformat(), first.assignment_id))
        recovered = await runner._recover_operations()
        assert recovered.reclaimed_assignment_ids == (first.assignment_id,)
        waiting = await current(op)
        delay = (waiting.next_wake_at - datetime.now(UTC)).total_seconds()
        assert 3 <= delay <= 5
        await asyncio.sleep(max(0, delay) + .02)
    else:
        await reader.control(op, "pause")
        await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
            old_executor.operation_fence, state=OperationState.CANCELLED,
            terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
        await reader.control(op, "resume")
    # Complete reconstruction of the host executor, with a real new claim.
    runner = attach_runner(op, run_research_episode)
    executor = await fresh_executor(op, runner)
    from persistent_agents.research_recovery import model_key
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await executor.research_selection(model_key(executor.record, captured[0]),
            source_action_id=first.action_id, ephemeral=captured[0])
    result = await run_research_episode(executor)
    final = await runner._finish_operation(executor, result)
    ledger = await actions(op)
    sources = [a for a in ledger if a.intent.request["kind"] == "tool"]
    assert first in sources
    assert len(sources) == len(op.physical) == 2
    assert sources[0].action_id != sources[1].action_id
    assert sources[0].intent.action_key != sources[1].intent.action_key
    assert sources[0].result["result_digest"] != sources[1].result["result_digest"]
    assert len(op.model_calls) == 1
    assert final.usage["spent"]["tool_calls"] == 2
    assert final.usage["spent"]["model_calls"] == 1
    assert final.lifecycle == "completed"
    model = next(a for a in ledger if a.intent.request["kind"] == "model")
    used = model.intent.transient_input.references[0].resource_id
    assert used != first.action_id
    assert final.checkpoint["research_source"]["action_id"] == used
    assert "Public release 088" not in json.dumps(thaw(final))


@pytest.mark.parametrize("current_result", [True, False])
async def test_unknown_model_consumption_cannot_escape_by_reacquiring_source(ephemeral, current_result, monkeypatch):
    op = ephemeral
    runner = attach_runner(op, run_research_episode)
    op.model_status = 503
    if not current_result:
        from shared import isolated_http
        original = isolated_http.request

        async def changed_config(*a, **kw):
            response = await original(*a, **kw)
            await op.executor.orch._llm_store.set(op.owner, provider="openai",
                base_url=profile.BASE_URL, model=profile.MODEL, api_key="replacement-synthetic-key")
            return response

        monkeypatch.setattr(isolated_http, "request", changed_config)
    with pytest.raises(DispatchDenied, match="assignment_action_uncertain"):
        await run_research_episode(op.executor)
    before = await current(op)
    ledger = await actions(op)
    assert before.phase == ("reconciliation" if current_result else "checking")
    assert before.usage["outstanding"]["model_calls"] == 1
    with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
        await run_research_episode(op.executor)
    await reader.control(op, "pause")
    await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
        op.executor.operation_fence, state=OperationState.CANCELLED,
        terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
    await reader.control(op, "resume")
    if current_result:
        with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
            await fresh_executor(op, runner)
    else:
        # Older Plane can re-claim this stale-result phase; the host must still
        # refuse effects. The qualified successor refuses the claim itself.
        try:
            executor = await fresh_executor(op, runner)
        except (AssignmentError, DispatchDenied, SessionAuthorityUnavailable):
            assert (await current(op)).phase == "reconciliation"
        else:
            with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
                await run_research_episode(executor)
    after = await actions(op)
    assert {a.action_id for a in after} == {a.action_id for a in ledger}
    unknown = next(a for a in after if a.intent.request["kind"] == "model")
    original = next(a for a in ledger if a.action_id == unknown.action_id)
    assert unknown.state == "uncertain"
    assert unknown.intent == original.intent
    assert len(unknown.attempts) == 1
    assert unknown.attempts[0]["attempt_id"] == original.attempts[0]["attempt_id"]
    assert unknown.attempts[0]["outcome"] == original.attempts[0]["outcome"]
    assert (await current(op)).usage == before.usage
    assert len(op.physical) == len(op.model_calls) == 1


async def test_generic_action_paths_cannot_retain_none_source(ephemeral):
    op = ephemeral
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await op.executor.action("unqualified", reader.REQUEST)
    assert not await actions(op) and op.physical == []
    source = await op.executor.acquire_research_source()
    [action] = await actions(op)
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await op.executor.execute(action)
    assert len(op.physical) == 1 and source.source_action_id == action.action_id


async def test_repeated_generation_never_reuses_a_source_action(ephemeral, monkeypatch):
    from uuid import UUID

    op = ephemeral
    source = await op.executor.acquire_research_source()
    before = await actions(op)
    monkeypatch.setattr("persistent_agents.research_recovery.uuid4", lambda: UUID(source.generation))
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await op.executor.acquire_research_source()
    assert await actions(op) == before
    assert len(op.physical) == 1


@pytest.mark.parametrize("change", ["missing_source", "wrong_key", "used_selection"])
async def test_source_identity_is_required_and_discarded_selection_is_never_replayed(ephemeral, change):
    from persistent_agents.research_recovery import model_key

    op = ephemeral
    source = await op.executor.acquire_research_source()
    key = model_key(op.executor.record, source)
    if change == "used_selection":
        await op.executor.research_selection(key, source_action_id=source.source_action_id, ephemeral=source)
    with pytest.raises((DispatchDenied, AssignmentError)):
        await op.executor.research_selection("arbitrary" if change == "wrong_key" else key,
            source_action_id=source.source_action_id,
            ephemeral=None if change == "missing_source" else source)
    assert len(op.physical) == 1
    assert len(op.model_calls) == (1 if change == "used_selection" else 0)


async def test_key_retirement_during_read_charges_without_persisting_source(ephemeral, monkeypatch):
    op = ephemeral

    async def change_key():
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "retired-synthetic-key-" + "y" * 40)

    op.hooks.after = change_key
    with pytest.raises(DispatchDenied):
        await op.executor.acquire_research_source()
    [action] = await actions(op)
    assert action.state == "failed" and action.ever_started
    assert action.result["result"] == {} and action.result["result_available"] is False
    assert (await current(op)).usage["spent"]["tool_calls"] == 1
    assert op.model_calls == []


async def test_cancellation_after_read_permit_still_settles_and_propagates(ephemeral):
    op = ephemeral
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    op.hooks.after = hold
    task = asyncio.create_task(op.executor.acquire_research_source())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [action] = await actions(op)
    assert action.state == "failed" and len(action.attempts) == 1
    assert action.result["result"] == {} and action.result["result_available"] is False
    usage = (await current(op)).usage
    assert usage["spent"]["tool_calls"] == 1
    assert all(value == 0 for value in usage["outstanding"].values())
    assert len(op.physical) == 1 and op.model_calls == []


@pytest.mark.parametrize("boundary", ["commit", "settle"])
async def test_lost_model_acknowledgement_never_redispatches_or_forgets_charge(
    ephemeral, monkeypatch, boundary
):
    op = ephemeral
    original = op.executor._research_transaction

    async def lose_ack(private, authority, callback, **kwargs):
        result = await original(private, authority, callback, **kwargs)
        if callback.__name__ == boundary:
            raise OSError("synthetic model acknowledgement loss")
        return result

    monkeypatch.setattr(op.executor, "_research_transaction", lose_ack)
    with pytest.raises((OSError, DispatchDenied, AssignmentError)):
        await run_research_episode(op.executor)
    before = await current(op)
    model = next(a for a in await actions(op) if a.intent.request["kind"] == "model")
    assert model.ever_started and len(model.attempts) == 1
    assert len(op.model_calls) == (0 if boundary == "commit" else 1)
    if boundary == "commit":
        assert model.state == "started"
        assert before.usage["outstanding"]["model_calls"] == 1
        with pytest.raises((AssignmentError, DispatchDenied)):
            await op.executor.acquire_research_source()
        assert len(op.physical) == 1
    else:
        assert model.state == "succeeded" and model.result["result"] == {}
        assert before.usage["spent"]["tokens"] == 120
        assert all(value == 0 for value in before.usage["outstanding"].values())
    assert "research_source" not in before.checkpoint


async def test_cancelled_model_keeps_unknown_consumption_and_no_source_payload(ephemeral, monkeypatch):
    from shared import isolated_http

    op = ephemeral
    entered = asyncio.Event()

    async def held(method, url, **kwargs):
        op.model_calls.append((method, url, kwargs))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(isolated_http, "request", held)
    task = asyncio.create_task(run_research_episode(op.executor))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    before = await current(op)
    model = next(a for a in await actions(op) if a.intent.request["kind"] == "model")
    assert model.state == "uncertain" and len(model.attempts) == 1
    assert model.result["result"] == {} and model.result["result_available"] is False
    assert before.usage["outstanding"]["model_calls"] == 1
    assert before.usage["spent"]["tool_calls"] == 1
    with pytest.raises((AssignmentError, DispatchDenied)):
        await op.executor.acquire_research_source()
    assert len(op.physical) == len(op.model_calls) == 1


async def test_rejected_model_prose_is_charged_and_never_retained(ephemeral):
    op = ephemeral
    private_prose = "UNRETAINED_PROVIDER_RESPONSE_CANARY_088"
    op.model_response["choices"][0]["message"]["content"] = private_prose
    with pytest.raises(DispatchDenied):
        await run_research_episode(op.executor)
    ledger = await actions(op)
    model = next(a for a in ledger if a.intent.request["kind"] == "model")
    assert model.state == "failed" and model.ever_started
    assert model.result["result_available"] is False and model.result["result"] == {}
    assert (await current(op)).usage["spent"]["tokens"] == 120
    assert all(value == 0 for value in (await current(op)).usage["outstanding"].values())
    assert private_prose not in json.dumps(list(map(thaw, ledger)))
    assert "Public release 088" not in json.dumps(list(map(thaw, ledger)))
    assert len(op.physical) == len(op.model_calls) == 1


@pytest.mark.parametrize("case", ["bytes", "generation", "record", "intent", "receipt", "key", "type"])
async def test_changed_live_source_cannot_prepare_a_model(ephemeral, case):
    from persistent_agents.research_input import ResearchInput
    from persistent_agents.research_recovery import model_key

    op = ephemeral
    proof = await op.executor.acquire_research_source()
    [action] = await actions(op)
    if case == "bytes":
        page = json.loads(proof._observation_json)
        page["text"] = "Changed evidence"
        proof = replace(proof, _observation_json=json.dumps(page))
    elif case == "generation":
        from uuid import uuid4
        proof = replace(proof, generation=str(uuid4()))
    elif case == "record":
        proof = replace(proof, _record_json="{}")
    elif case == "intent":
        proof = replace(proof, _intent_json="{}")
    elif case == "receipt":
        result = thaw(action.result)
        result["result_digest"] = "0" * 64
        action = replace(action, result=result)
    elif case == "key":
        from audit.pii import PrivateBindingKey
        proof = replace(proof, _key=PrivateBindingKey(proof.key_id, b"wrong" * 12))
    else:
        proof = SimpleNamespace(**{field: getattr(proof, field)
            for field in ("source_action_id", "generation", "key_id")})
    with pytest.raises((DispatchDenied, ValueError)):
        await ResearchInput.capture(await current(op), action,
            config_store=op.executor.orch._llm_store, ephemeral=proof)
    if case not in {"receipt", "type"}:
        with pytest.raises((DispatchDenied, AssignmentError)):
            await op.executor.research_selection(model_key(op.executor.record, proof),
                source_action_id=proof.source_action_id, ephemeral=proof)
    assert len(op.physical) == 1 and op.model_calls == []
    assert len(await actions(op)) == 1


@pytest.mark.parametrize("boundary", ["read", "model", "completion"])
async def test_retirement_settles_issued_effect_without_discarded_content(ephemeral, boundary):
    from tests.helpers.session_plane_runtime import get_session_record, replace_session_record

    op = ephemeral
    runner = attach_runner(op, run_research_episode)

    async def retire():
        original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
        await asyncio.to_thread(replace_session_record, op.runtime, original)

    if boundary == "read":
        op.hooks.after = retire
    elif boundary == "model":
        from shared import isolated_http
        original = isolated_http.request

        async def replace_after(*a, **kw):
            response = await original(*a, **kw)
            await retire()
            return response

        with patch.object(isolated_http, "request", replace_after):
            with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
                await run_research_episode(op.executor)
    if boundary != "model":
        with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
            result = await run_research_episode(op.executor)
            await retire()
            await runner._finish_operation(op.executor, result)
    final = await current(op)
    ledger = await actions(op)
    assert final.usage["spent"]["tool_calls"] == 1
    assert final.usage["spent"]["model_calls"] == (0 if boundary == "read" else 1)
    assert "research_source" not in final.checkpoint
    assert final.operation.get("result_reference") is None
    assert all(a.result["result_available"] is False and a.result["result"] == {} for a in ledger)
    assert all(value == 0 for value in final.usage["outstanding"].values())


@pytest.mark.parametrize("operation", [{"tool_calls": 1}], indirect=True)
async def test_spent_read_budget_is_not_reset_by_fresh_observation(ephemeral):
    op = ephemeral
    await op.executor.acquire_research_source()
    before = await current(op)
    with pytest.raises((AssignmentError, DispatchDenied)):
        await op.executor.acquire_research_source()
    assert len(op.physical) == 1 and op.model_calls == []
    assert (await current(op)).usage["spent"] == before.usage["spent"]
    assert all(value == 0 for value in (await current(op)).usage["outstanding"].values())


async def test_new_acquisition_retires_older_live_observation(ephemeral):
    from persistent_agents.research_recovery import model_key

    op = ephemeral
    old = await op.executor.acquire_research_source()
    fresh = await op.executor.acquire_research_source()
    with pytest.raises(DispatchDenied, match="assignment_research_binding_changed"):
        await op.executor.research_selection(model_key(op.executor.record, old),
            source_action_id=old.source_action_id, ephemeral=old)
    assert op.model_calls == []
    selected = await op.executor.research_selection(model_key(op.executor.record, fresh),
        source_action_id=fresh.source_action_id, ephemeral=fresh)
    assert selected["source_action_id"] == fresh.source_action_id
    assert len(op.physical) == 2 and len(op.model_calls) == 1


async def test_reacquisition_uses_new_extracted_bytes_and_source_facts(ephemeral):
    import requests
    from shared.protocol import MCPResponse

    op = ephemeral
    prior = await op.executor.acquire_research_source()
    new_text = "New observation after process reconstruction."
    final_url = "https://93.184.216.34/updated-release"

    async def transport(agent, tool, args, timeout, **kwargs):
        op.physical.append((agent, tool, dict(args)))
        response = requests.Response()
        response.url, response.status_code = final_url, 200
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        response.encoding, response._content = "utf-8", new_text.encode()
        with patch.object(reader.page_tools, "_fetch_url", return_value=response):
            return MCPResponse(result=reader.page_tools.fetch_page(url=args["url"]))

    op.executor.orch._execute_via_websocket = transport
    runner = attach_runner(op, run_research_episode)
    result = await run_research_episode(op.executor)
    final = await runner._finish_operation(op.executor, result)
    sent = json.dumps(op.model_calls[0][2]["json_body"])
    assert new_text in sent and "Public release 088" not in sent
    metadata = final.checkpoint["research_source"]
    assert metadata["source"]["final_url"] == final_url
    assert metadata["source"]["body_complete"] is True
    assert metadata["action_id"] != prior.source_action_id
    assert metadata["observation_generation"] != prior.generation
    assert metadata["receipt"] != prior.receipt
    assert final.usage["spent"]["tool_calls"] == 2


async def test_no_source_or_prompt_canary_in_any_private_database_row(ephemeral, caplog):
    import re

    op = ephemeral
    runner = attach_runner(op, run_research_episode)
    final = await runner._finish_operation(op.executor, await run_research_episode(op.executor))
    # This is an isolated synthetic schema. Inspect every table, not just the
    # projected DTO, so accidental checkpoint/attempt/audit persistence is caught.
    with op.runtime.transaction() as tx:
        tables = tx.fetch_all("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        for table in tables:
            name = table["tablename"]
            assert re.fullmatch(r"[a-z_][a-z_0-9]*", name)
            rows = tx.fetch_all(f'SELECT to_jsonb(t)::text AS value FROM "{name}" AS t')
            for row in rows:
                assert "Public release 088" not in row["value"], name
                assert "synthetic-provider-key-not-used" not in row["value"], name
                assert '"messages"' not in row["value"], name
    assert "Public release 088" not in caplog.text
    assert "synthetic-provider-key-not-used" not in caplog.text
    from orchestrator.work_result import project_research_result
    assert final.lifecycle == "completed"
    with op.runtime.transaction() as tx:
        repository = op.runtime.repositories.assignments
        read = repository.get_operation(tx, owner_id=op.owner, assignment_id=final.assignment_id)
        result = project_research_result(tx, repository, owner_id=op.owner, read=read)
    assert result["available"] is False and result["reason"] == "not_retained"
    assert result["content"] is None


@pytest.mark.parametrize("change", ["checkpoint", "selection", "reference", "proof"])
async def test_ephemeral_completion_cannot_incorporate_unbound_content(ephemeral, change):
    op = ephemeral
    runner = attach_runner(op, run_research_episode)
    result = await run_research_episode(op.executor)
    if change == "checkpoint":
        result = replace(result, completion=replace(result.completion,
            checkpoint={"schema_version": 1, "private_text": "must not persist"}))
    elif change == "selection":
        selected = json.loads(result.research._selection_json)
        selected["passage_ids"] = []
        result = replace(result, research=replace(result.research, _selection_json=json.dumps(selected)))
    elif change == "reference":
        result = replace(result, completion=replace(result.completion,
            result_reference=result.research.private.source_action_id))
    else:
        result = replace(result, research=None)
    with pytest.raises((AssignmentError, DispatchDenied)):
        await runner._finish_operation(op.executor, result)
    assert "research_source" not in (await current(op)).checkpoint
    assert len(op.physical) == len(op.model_calls) == 1


async def test_source_only_claim_cannot_validate_model_completion(ephemeral):
    from astralplane.repositories import RepositoryConflictError

    op = ephemeral
    result = await run_research_episode(op.executor)
    proof = result.research
    checks = await proof.refresh(op.executor)
    repository = op.runtime.repositories.assignments
    with op.runtime.transaction() as tx:
        authority = checks["model"]["authority"].observation
        repository.assert_current_assignment_execution(tx, fence=op.executor.claim.fence,
            binding=op.executor.binding, authority=authority)
        # A persisted source-only restriction must be enforced even if a
        # malformed host executor forgot to copy its approved_action_id field.
        tx.execute("UPDATE persistent_assignment SET data=jsonb_set(data, "
            "'{approved_action_id}',to_jsonb(%s::text)) WHERE id=%s",
            (proof.private.source_action_id, op.executor.record.assignment_id))
        selected = repository.assert_current_assignment_execution(tx,
            fence=op.executor.claim.fence, binding=op.executor.binding, authority=authority,
            action_id=proof.private.source_action_id)
        with pytest.raises(RepositoryConflictError, match="assignment_action_claim_restricted"):
            proof.rebuild(op.executor, tx, repository, selected, checks)
    assert "research_source" not in (await current(op)).checkpoint
