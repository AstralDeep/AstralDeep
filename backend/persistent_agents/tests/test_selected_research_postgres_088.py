"""Tests for persistent_agents/research_input.py and
personalization/explicit_note_service.py through real HTTP and Postgres: selected
skills and notes reach the fixed request as a private proof without becoming retained
evidence.
"""

import asyncio
import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition

from persistent_agents.runtime_values import thaw
from tests.test_work_research_preflight_postgres_088 import research_command
from tests.test_work_runtime_postgres_088 import (
    fixture as fixture,
    finished,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


def add_skill(op):
    with op.runtime.transaction() as tx:
        return op.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
            op.owner, str(uuid4()), str(uuid4()), "create", 0, slug="selected-structure",
            definition=SkillDefinition("Private structure", "Prefer exact attributed excerpts.", ("always",)),
        )).head


async def test_selected_skill_reaches_fixed_model_with_new_private_proof(integrated):
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    command = json.loads(research_command(SimpleNamespace(assignments=runner.service)))
    command["selection"] = {"version": 1, "agent": None, "skills": [
        {"skill_id": skill.skill_id, "revision": skill.revision}], "notes": []}
    accepted = await client.post("/api/work/v1/operations", json=command)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    assert len(op.model_calls) == 1
    payload = json.loads(op.model_calls[0][2]["json_body"]["messages"][1]["content"])
    assert "Prefer exact attributed excerpts." in json.dumps(payload["owner_guidance"])
    ledger = await runner.store.call("list_actions", owner_id=op.owner, assignment_id=identity)
    model = next(action for action in ledger if action.intent.request["kind"] == "model")
    assert model.result["result"]["version"] == 3
    assert "Prefer exact attributed excerpts." not in json.dumps(thaw(ledger))
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.json()["result"]["available"] is True
    assert "Prefer exact attributed excerpts." not in result.text


@pytest.fixture
async def selected_notes(integrated, fixture, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from orchestrator.credential_manager import CredentialManager
    from orchestrator.human_request_authority import HumanRequestBoundary
    from personalization.explicit_note_service import ExplicitNoteService
    from personalization import phi_gate
    from tests.test_explicit_note_service_postgres_088 import apply as apply_note

    op, runner, client = integrated
    orch = runner.orch
    monkeypatch.setattr(phi_gate, "_GATE", runner.service.phi_gate)
    orch.credential_manager = CredentialManager(plane_runtime=op.runtime, data_dir=str(tmp_path))
    boundary = HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    orch.explicit_notes = ExplicitNoteService(orch)
    app = FastAPI()
    app.state.orchestrator = orch
    state = SimpleNamespace(api=SimpleNamespace(orch=orch, runtime=op.runtime, fixture=fixture,
        app=app, service=SimpleNamespace(assignments=runner.service)),
        boundary=boundary, service=orch.explicit_notes)
    try:
        note = await apply_note(state)
        yield op, runner, client, state, note
    finally:
        boundary.close()


def selected_body(runner, *, skill=None, note=None, agent=None, retention="operation"):
    body = json.loads(research_command(SimpleNamespace(assignments=runner.service)))
    body["source_retention"] = retention
    body["selection"] = {"version": 1, "agent": agent,
        "skills": [] if skill is None else [{"skill_id": skill.skill_id, "revision": skill.revision}],
        "notes": [] if note is None else [{"note_id": note.note_id, "revision": note.revision}]}
    return body


async def selected_ledger(op, runner, identity):
    ledger = await runner.store.call("list_actions", owner_id=op.owner, assignment_id=identity)
    return ledger, next((row for row in ledger if row.intent.request["kind"] == "model"), None)


async def settled_model(op, runner, identity):
    try:
        async with asyncio.timeout(15):
            while True:
                ledger, model = await selected_ledger(op, runner, identity)
                if model is not None and model.result is not None:
                    return ledger, model
                await asyncio.sleep(0.02)
    except TimeoutError:
        with op.runtime.transaction() as tx:
            waits = tx.fetch_all("SELECT pid,wait_event_type,wait_event,pg_blocking_pids(pid) AS blockers "
                "FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
        pytest.fail(f"settlement incomplete; phases={getattr(op, 'guidance_phases', ())}; waits={waits}")


@pytest.mark.parametrize("retention", ["operation", "none"])
async def test_encrypted_note_expands_only_in_actual_request_and_retention_is_preserved(selected_notes, retention):
    op, runner, client, state, note = selected_notes
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, note=note, retention=retention))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    body = op.model_calls[0][2]["json_body"]
    assert "Use concise paragraphs and quoted sources." in json.dumps(body)
    ledger, model = await selected_ledger(op, runner, identity)
    assert len(model.attempts) == 1
    assert "Use concise paragraphs and quoted sources." not in json.dumps(thaw(ledger))
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200
    assert result.json()["result"]["available"] is (retention == "operation")
    assert "Use concise paragraphs and quoted sources." not in result.text


async def test_forget_after_physical_send_preserves_one_authentic_charge_without_output(selected_notes):
    from personalization.explicit_note_service import ExplicitNoteCommand
    from tests.test_explicit_note_service_postgres_088 import apply as apply_note
    op, runner, client, state, note = selected_notes
    op.guidance_phases = []
    async def forget():
        op.guidance_phases.append("forget_entered")
        try:
            await apply_note(state, ExplicitNoteCommand("forget", note.note_id, note.revision))
        except BaseException as error:
            op.guidance_phases.append(type(error).__name__)
            raise
        op.guidance_phases.append("forget_committed")
    op.model_after = forget
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, note=note))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    _, model = await settled_model(op, runner, identity)
    assert len(op.model_calls) == len(model.attempts) == 1
    assert model.result["result_available"] is False and model.result["result"] == {}
    current = await runner.store.call("get_operation", owner_id=op.owner, assignment_id=identity)
    assert current.assignment.usage["spent"]["tokens"] == 120
    assert current.assignment.usage["outstanding"]["tokens"] == 0
    assert "research_result" not in current.assignment.checkpoint


async def test_passive_completed_result_survives_forget_without_reopening_notes(selected_notes, monkeypatch):
    from personalization.explicit_note_service import ExplicitNoteCommand
    from personalization.selected_guidance_boundary import SelectedGuidanceBoundary
    from tests.test_explicit_note_service_postgres_088 import apply as apply_note
    op, runner, client, state, note = selected_notes
    command = selected_body(runner, note=note)
    accepted = await client.post("/api/work/v1/operations", json=command)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    before = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert before.json()["result"]["available"] is True
    await apply_note(state, ExplicitNoteCommand("forget", note.note_id, note.revision))
    def forbidden(*args, **kwargs):
        pytest.fail("passive result read must not reopen forgotten selected values")
    monkeypatch.setattr(SelectedGuidanceBoundary, "capture", forbidden)
    monkeypatch.setattr(op.runtime.repositories.preferences.personalization, "get_explicit_note", forbidden)
    after = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert after.status_code == 200 and after.json() == before.json()
    replay = await client.post("/api/work/v1/operations", json=command)
    assert replay.status_code == 200 and replay.json()["created"] is False
    assert len(op.model_calls) == 1


async def test_no_selection_keeps_exact_legacy_input_transient_and_receipt_bytes(research):
    from audit.pii import private_binding_key
    from persistent_agents.research_input import ResearchInput
    from persistent_agents.runtime_values import canonical
    op = research
    current = (await op.executor.store.call("get_operation", owner_id=op.owner,
        assignment_id=op.executor.record.assignment_id)).assignment
    old = await ResearchInput.capture(current, op.source_action, config_store=op.executor.orch._llm_store)
    checked = await ResearchInput.capture(current, op.source_action, config_store=op.executor.orch._llm_store,
        guidance=op.executor._research_guidance)
    assert old._request.body == checked._request.body
    assert old.payload_binding == checked.payload_binding
    assert asdict(old.transient()) == asdict(checked.transient())
    selected = old.selection_result(["p001"])
    assert selected == checked.selection_result(["p001"]) and selected["version"] == 1
    values = dict(action_id=str(uuid4()), attempt_id=str(uuid4()), outcome="succeeded", result=selected, actual=None)
    from llm_config import research_profile as profile
    expected = private_binding_key(old.key_id).sign("result", canonical({
        "profile": profile.PROFILE, "payload_binding": old.payload_binding, **values}).encode())
    assert old.receipt_digest(**values) == checked.receipt_digest(**values) == expected


def add_agent(op, runner):
    from astralplane.repositories.agent_models import DeclarativeAgentCommand
    from tests.test_declarative_agent_lifecycle_postgres_088 import research_definition
    state = SimpleNamespace(api=SimpleNamespace(service=SimpleNamespace(assignments=runner.service)))
    with op.runtime.transaction() as tx:
        repo = op.runtime.repositories.agents
        command = DeclarativeAgentCommand(op.owner, "selected-reader-" + uuid4().hex,
            str(uuid4()), "create", revision_id=str(uuid4()), display_name="Private selected reader",
            definition=research_definition(state))
        created = repo.apply_declarative_command(tx,
            preparation=repo.prepare_declarative_command(tx, command=command))
        activate = DeclarativeAgentCommand(op.owner, command.agent_id, str(uuid4()), "activate",
            expected_revision=created.agent.state_revision, revision_id=command.revision_id)
        repo.apply_declarative_command(tx, preparation=repo.prepare_declarative_command(tx, command=activate))
    return {"agent_id": command.agent_id, "revision_id": command.revision_id}


async def test_selected_declarative_agent_has_separate_typed_proof_and_fixed_profile(integrated, monkeypatch):
    from shared.feature_flags import flags
    op, runner, client = integrated
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    agent = await asyncio.to_thread(add_agent, op, runner)
    response = await client.post("/api/work/v1/operations", json=selected_body(runner, agent=agent))
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    _, model = await selected_ledger(op, runner, identity)
    selected = model.result["result"]["selected_input"]
    assert selected["agent"]["kind"] == "declarative"
    assert selected["agent"]["agent_id"] == agent["agent_id"]
    assert selected["agent"]["revision_id"] == agent["revision_id"]
    assert [reference.kind for reference in model.intent.transient_input.references] == ["source"]
    body = json.loads(op.model_calls[0][2]["json_body"]["messages"][1]["content"])
    assert body["owner_guidance"]
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.json()["result"]["available"] is True


@pytest.mark.parametrize("change", ["named_key", "note_service", "config_store", "config_context", "config_cipher"])
async def test_local_selection_retirement_after_send_keeps_known_usage(selected_notes, monkeypatch, change):
    op, runner, client, state, note = selected_notes
    async def retire():
        if change == "named_key":
            monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-retired-key-" + "z" * 48)
        elif change == "note_service":
            runner.orch.explicit_notes = object()
        elif change == "config_store":
            runner.orch._llm_store = object()
        elif change == "config_context":
            store = runner.orch._llm_store
            context = store._repository
            store._repository = SimpleNamespace(plane_runtime=context.plane_runtime,
                                                 repository=context.repository)
        else:
            runner.orch._llm_store._fernet = object()
    op.model_after = retire
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, note=note))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    _, model = await settled_model(op, runner, identity)
    assert len(op.model_calls) == len(model.attempts) == 1
    assert model.result["actual"]["tokens"] == 120
    assert model.result["result_available"] is False and model.result["result"] == {}
    current = await runner.store.call("get_operation", owner_id=op.owner, assignment_id=identity)
    assert current.assignment.usage["spent"]["tokens"] == 120


async def test_selected_named_key_survives_unrelated_active_key_rotation(integrated, monkeypatch):
    import os
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    old_id, old_secret = os.environ["AUDIT_HMAC_KEY_ID"], os.environ["AUDIT_HMAC_SECRET"]
    async def rotate():
        monkeypatch.setenv("AUDIT_HMAC_SECRET_" + old_id.upper(), old_secret)
        monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "successor_selected")
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-unrelated-active-key-" + "n" * 48)
    op.hooks.after = rotate
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, skill=skill))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    _, model = await selected_ledger(op, runner, identity)
    assert model.intent.transient_input.binding_key_id == old_id
    assert model.result["result"]["selected_input"]["binding_key_id"] == old_id
    assert len(op.model_calls) == 1


@pytest.mark.parametrize("with_reference", [False, True])
async def test_legacy_header_is_never_silently_treated_as_absent(integrated, monkeypatch, with_reference):
    from astralplane.repositories.guidance_models import GuidanceReference
    from persistent_agents.execution import ActionExecutor
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    repo = op.runtime.repositories.assignments
    original = repo.create_operation
    def create(tx, **kwargs):
        record = original(tx, **kwargs)
        return repo.bind_guidance_references(tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch, expected_state_version=record.state_version,
            references=(GuidanceReference("skill", skill.skill_id, skill.revision),) if with_reference else ())
    monkeypatch.setattr(repo, "create_operation", create)
    refused = asyncio.Event()
    capture = ActionExecutor._capture_research_guidance
    async def observe(executor, authority):
        try:
            return await capture(executor, authority)
        except Exception:
            refused.set()
            raise
    monkeypatch.setattr(ActionExecutor, "_capture_research_guidance", observe)
    before = len(op.physical)
    accepted = await client.post("/api/work/v1/operations",
        content=research_command(SimpleNamespace(assignments=runner.service)))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await asyncio.wait_for(refused.wait(), 10)
    assert not await runner.store.call("list_actions", owner_id=op.owner, assignment_id=identity)
    assert len(op.physical) == before and op.model_calls == []


async def test_selected_unknown_response_keeps_authentic_unresolved_liability(integrated):
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    op.model_response["usage"] = None
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, skill=skill))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    _, model = await settled_model(op, runner, identity)
    assert model.state == "uncertain" and model.result["actual"] is None
    assert model.result["result_available"] is False
    assert len(model.attempts) == len(op.model_calls) == 1
    current = await runner.store.call("get_operation", owner_id=op.owner, assignment_id=identity)
    assert current.assignment.usage["outstanding"]["tokens"] == model.intent.maximum.tokens
    await runner.tick()
    assert len(op.model_calls) == 1


@pytest.mark.parametrize("retire", [False, True])
async def test_fresh_executor_reconstructs_only_original_current_selection(integrated, monkeypatch, retire):
    from orchestrator.session_authority import SessionAuthorityUnavailable
    from persistent_agents.dispatch_context import DispatchDenied
    from persistent_agents.execution import ActionExecutor
    from persistent_agents.models import AssignmentError
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ActionExecutor.research_selection
    captured = {}
    async def held(executor, key, **kwargs):
        result = await original(executor, key, **kwargs)
        captured.update(executor=executor, key=key, kwargs=kwargs, result=result)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(ActionExecutor, "research_selection", held)
    response = await client.post("/api/work/v1/operations", json=selected_body(runner, skill=skill))
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    runner.notify(identity)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        before = len(op.physical), len(op.model_calls)
        if retire:
            def change():
                with op.runtime.transaction() as tx:
                    op.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
                        op.owner, skill.skill_id, str(uuid4()), "replace", skill.revision,
                        definition=SkillDefinition("Changed structure", "A new unsupported adoption.", ("always",))))
            await asyncio.to_thread(change)
        first = captured["executor"]
        successor = ActionExecutor(first.runner, first.claim, first.operation_fence, object(),
            operation_sessions=first.operation_sessions, operation_authority_lock=first.operation_authority_lock)
        if retire:
            with pytest.raises((DispatchDenied, AssignmentError, SessionAuthorityUnavailable)):
                await original(successor, captured["key"], **captured["kwargs"])
        else:
            value = await original(successor, captured["key"], **captured["kwargs"])
            assert value == captured["result"]
            assert successor._research_guidance is not first._research_guidance
            assert successor._research_guidance.snapshot == first._research_guidance.snapshot
        assert (len(op.physical), len(op.model_calls)) == before
    finally:
        release.set()
    if not retire:
        await finished(client, identity)


@pytest.mark.parametrize("change", ["key", "note_service", "agent_flag"])
async def test_final_capture_database_wait_rechecks_local_selection_before_source(
    selected_notes, monkeypatch, change,
):
    from persistent_agents.execution import ActionExecutor
    from persistent_agents.research_input import ResearchGuidance
    from shared.feature_flags import flags
    op, runner, client, state, note = selected_notes
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    agent = await asyncio.to_thread(add_agent, op, runner) if change == "agent_flag" else None
    repo = op.runtime.repositories.assignments
    armed = False
    captured = ResearchGuidance.capture
    def capture(cls, *args, **kwargs):
        nonlocal armed
        value = captured(*args, **kwargs)
        if value.captured is not None:
            armed = True
        return value
    monkeypatch.setattr(ResearchGuidance, "capture", classmethod(capture))
    final = repo.assert_guidance_current
    observations = []
    def after_query(tx, **kwargs):
        result = final(tx, **kwargs)
        if armed:
            observations.append(True)
            if change == "key":
                monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-final-key-retirement-" + "r" * 48)
            elif change == "note_service":
                runner.orch.explicit_notes = object()
            else:
                monkeypatch.setitem(flags._flags, "byo_agents", False)
        return result
    monkeypatch.setattr(repo, "assert_guidance_current", after_query)
    refused = asyncio.Event()
    original = ActionExecutor._capture_research_guidance
    async def observe(executor, authority):
        try:
            return await original(executor, authority)
        except Exception:
            refused.set()
            raise
    monkeypatch.setattr(ActionExecutor, "_capture_research_guidance", observe)
    before = len(op.physical)
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, note=note, agent=agent))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await asyncio.wait_for(refused.wait(), 10)
    assert observations and len(op.physical) == before and not op.model_calls
    assert not await runner.store.call("list_actions", owner_id=op.owner, assignment_id=identity)


async def test_selected_note_expiry_during_real_config_row_wait_denies_model(selected_notes, monkeypatch):
    from persistent_agents.execution import ActionExecutor
    from persistent_agents.research_input import ResearchInput
    from tests.test_explicit_note_service_postgres_088 import apply as apply_note

    op, runner, client, state, _ = selected_notes
    expiry = time.time_ns() // 1_000_000 + 3500
    note = await apply_note(state, expires_at=expiry)
    repository = op.runtime.repositories.encrypted_llm_config
    original = repository.get_user_for_update
    locked, requesting = threading.Event(), threading.Event()
    shared, workers = {}, []
    armed = False

    def holder():
        with op.runtime.transaction() as tx:
            original(tx, owner_id=op.owner)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            locked.set()
            assert requesting.wait(8), "model never requested its USER row"
            end = time.monotonic() + 0.09
            while time.monotonic() < end:
                row = tx.fetch_one("SELECT %s=ANY(pg_blocking_pids(%s)) AS waiting, "
                    "floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now",
                    (blocker, shared["pid"]))
                shared["blocked"] = shared.get("blocked", False) or row["waiting"]
                if row["now"] > expiry:
                    shared["expired_at_release"] = row["now"]
                    break
                time.sleep(0.001)
            assert shared.get("blocked") and shared.get("expired_at_release", 0) > expiry

    captured = ResearchInput.capture
    async def capture(cls, *args, **kwargs):
        nonlocal armed
        value = await captured(*args, **kwargs)
        if value._guidance is not None and value._guidance.captured is not None:
            workers.append(asyncio.create_task(asyncio.to_thread(holder)))
            assert await asyncio.to_thread(locked.wait, 3)
            armed = True
        return value
    monkeypatch.setattr(ResearchInput, "capture", classmethod(capture))

    def config(tx, *, owner_id):
        nonlocal armed
        if armed:
            armed = False
            while True:
                now = tx.fetch_one("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now")["now"]
                if expiry - now <= 35:
                    assert now < expiry, "fixture failed to reach the config boundary before expiry"
                    break
                time.sleep(min(0.005, (expiry - now - 35) / 1000))
            shared["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            requesting.set()
        return original(tx, owner_id=owner_id)
    monkeypatch.setattr(repository, "get_user_for_update", config)
    refused = asyncio.Event()
    selection = ActionExecutor.research_selection
    async def observe(executor, *args, **kwargs):
        try:
            return await selection(executor, *args, **kwargs)
        except Exception:
            refused.set()
            raise
    monkeypatch.setattr(ActionExecutor, "research_selection", observe)
    try:
        accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, note=note))
        assert accepted.status_code == 201, accepted.text
        identity = accepted.json()["id"]
        runner.notify(identity)
        await asyncio.wait_for(refused.wait(), 10)
    finally:
        for worker in workers:
            await worker
    assert shared["blocked"] and shared["expired_at_release"] > expiry
    assert op.model_calls == []
    _, model = await selected_ledger(op, runner, identity)
    assert model is not None and not model.ever_started
    current = await runner.store.call("get_operation", owner_id=op.owner, assignment_id=identity)
    assert current.assignment.usage["outstanding"]["tokens"] == 0


@pytest.mark.parametrize("change", ["selection", "old_domain", "reference"])
async def test_selected_result_requires_exact_new_domain_and_references(integrated, change):
    from audit.pii import private_binding_key
    from persistent_agents.research_input import receipt_payload
    from tests.test_work_result_postgres_088 import _mutate_action, unavailable
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    accepted = await client.post("/api/work/v1/operations", json=selected_body(runner, skill=skill))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    await finished(client, identity)
    _, model = await selected_ledger(op, runner, identity)
    path = f"/api/work/v1/operations/{identity}/result"
    assert (await client.get(path)).json()["result"]["available"] is True
    def corrupt(data):
        result = data["result"]
        if change == "selection":
            result["result"]["selected_input"]["combined_binding"] = "0" * 64
        elif change == "reference":
            data["intent"]["transient_input"]["references"][-1]["revision"] += 1
        else:
            result["result_digest"] = private_binding_key(model.intent.transient_input.binding_key_id).sign(
                "result", receipt_payload(payload_binding=model.intent.request_digest,
                    action_id=model.action_id, attempt_id=data["attempts"][-1]["attempt_id"],
                    outcome=result["outcome"], result=result["result"], actual=result["actual"]))
        data["attempts"][-1]["outcome"] = {key: value for key, value in result.items()
                                         if key != "result_available"}
    await _mutate_action(op, model, corrupt)
    unavailable((await client.get(path)).json()["result"])
    assert len(op.model_calls) == 1


async def test_new_supervisor_reconstructs_selected_retry_without_private_state(selected_notes, monkeypatch):
    from llm_config.tests.test_research_profile_088 import reply
    from persistent_agents.runtime import start_assignment_runtime
    from tests.test_work_retry_http_postgres_088 import retry_command, wait_for
    op, runner, client, state, note = selected_notes
    monkeypatch.setenv("PERSISTENT_AGENTS_TICK_SECONDS", "1")
    body = retry_command(runner)
    body["selection"] = selected_body(runner, note=note)["selection"]
    async def response():
        op.model_response = reply(selection=["unavailable-passage"] if not op.model_calls else None)
    op.model_before = response
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    scheduled, first_actions, _ = await wait_for(op, identity,
        lambda row, _actions, _now: row["next_retry_at"] is not None)
    assert len(op.model_calls) == 1 and len(first_actions) == 2
    assert scheduled["usage"]["spent"]["tokens"] == 120
    await runner.stop()
    orch = runner.orch
    orch.persistent_assignments = orch.persistent_assignment_runner = None
    successor = start_assignment_runtime(orch)
    try:
        assert successor is not runner and successor.service is not runner.service
        final, actions, _ = await wait_for(op, identity,
            lambda row, _actions, _now: row["lifecycle"] == "completed")
        assert len(op.model_calls) == 2 and len(actions) == 4
        assert final["usage"]["spent"]["tokens"] == 240
        assert final["usage"]["spent"]["tool_calls"] == 2
        assert all(value == 0 for value in final["usage"]["outstanding"].values())
        models = [a for a in actions if a["intent"]["request"]["kind"] == "model"]
        assert len({a["intent"]["action_key"] for a in models}) == 2
        assert len({a["intent"]["transient_input"]["payload_binding"] for a in models}) == 2
        references = [a["intent"]["transient_input"]["references"] for a in models]
        assert references[0][1:] == references[1][1:]
        assert references[0][0]["resource_id"] != references[1][0]["resource_id"]
        for _, _, request in op.model_calls:
            payload = json.loads(request["json_body"]["messages"][1]["content"])
            assert "Use concise paragraphs and quoted sources." in json.dumps(payload["owner_guidance"])
        serialized = json.dumps(thaw([final, actions]))
        assert "Use concise paragraphs and quoted sources." not in serialized
        assert "Public release 088" not in serialized
        assert '"messages"' not in serialized
        replay = await client.post("/api/work/v1/operations", json=body)
        assert replay.status_code == 200 and replay.json()["id"] == identity
        assert len(op.model_calls) == 2
    finally:
        await successor.stop()
        successor.store.close()
