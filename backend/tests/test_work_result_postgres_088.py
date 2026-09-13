"""Owner-read excerpts from real completed research and authentic PG receipts.

Only external IAM/JWKS, page and provider replies are synthetic. These fixtures
execute the normal audited dispatcher, public Plane actions and final lifecycle.
No read test obtains an execution permit or opens provider configuration.
"""

import asyncio
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from llm_config.tests.test_research_profile_088 import reply
from persistent_agents.runtime_values import canonical, digest, thaw
from persistent_agents.tests.test_research_episode_postgres_088 import outcome
from persistent_agents.tests.test_research_execution_postgres_088 import (
    actions,
    current,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)
from tests.helpers.session_plane_runtime import get_session_record

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]


@pytest.fixture
async def completed(research):
    runner, result = await outcome(research)
    research.completed = await runner._finish_operation(research.executor, result)
    research.ledger = await actions(research)
    research.model = next(a for a in research.ledger if a.intent.request["kind"] == "model")
    research.source = next(a for a in research.ledger if a.action_id ==
                          research.completed.checkpoint["research_result"]["source"]["action_id"])
    return research


async def project(op, *, owner=None):
    from orchestrator.work_result import project_research_result

    def read(tx, repository):
        identity = op.owner if owner is None else owner
        snapshot = repository.get_operation(tx, owner_id=identity,
            assignment_id=op.completed.assignment_id)
        return project_research_result(tx, repository, owner_id=identity, read=snapshot)

    return await op.executor.store.transaction(read, bound_session_waits=True)


async def test_completed_result_is_rebuilt_from_actual_settled_receipts(completed):
    op = completed
    result = await project(op)
    assert set(result) == {"version", "available", "reason", "content"}
    assert result["version"] == 1 and result["available"] is True and result["reason"] is None
    assert result["content"] == thaw(op.completed.checkpoint)["research_result"]
    assert result["content"]["passages"] == [{"id": "p001", "text": "Public release 088"}]
    assert len(json.dumps(result).encode()) < 9000
    assert len(op.model_calls) == 1
    assert (await actions(op)) == op.ledger


async def _mutate_action(op, action, change):
    """Synthetic corruption only; shipping code reads through public repositories."""
    def mutate():
        with op.runtime.transaction() as tx:
            row = tx.fetch_one("SELECT data FROM persistent_assignment_action WHERE id=%s "
                "AND owner_user_id=%s", (action.action_id, op.owner))
            data = thaw(row["data"])
            change(data)
            data["intent_digest"] = digest(data["intent"])
            tx.execute("UPDATE persistent_assignment_action SET data=%s::jsonb,state=%s "
                "WHERE id=%s AND owner_user_id=%s",
                (canonical(data), data["state"], action.action_id, op.owner))
    await asyncio.to_thread(mutate)


async def _mutate_record(op, change):
    def mutate():
        with op.runtime.transaction() as tx:
            row = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s "
                "AND owner_user_id=%s", (op.completed.assignment_id, op.owner))
            data = thaw(row["data"])
            change(data)
            tx.execute("UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s "
                "AND owner_user_id=%s", (canonical(data), op.completed.assignment_id, op.owner))
    await asyncio.to_thread(mutate)


def unavailable(value):
    assert value == {"version": 1, "available": False, "reason": "unavailable", "content": None}


async def test_missing_and_foreign_owner_never_receive_content(completed):
    unavailable(await project(completed, owner="another-owner"))
    op = completed
    with op.runtime.transaction() as tx:
        op.runtime.repositories.assignments.delete_for_owner(tx, owner_id=op.owner,
            assignment_id=op.completed.assignment_id,
            expected_control_epoch=op.completed.control_epoch,
            expected_state_version=op.completed.state_version)
    unavailable(await project(op))


async def test_completed_excerpts_survive_config_and_original_session_retirement(completed):
    op = completed
    before = await project(op)
    assert await op.executor.orch._llm_store.clear(op.owner)
    session = get_session_record(op.runtime, op.sid)
    with op.runtime.transaction() as tx:
        op.runtime.repositories.history.sessions.delete(tx, owner_id=op.owner,
            session_id=op.sid, expected_incarnation_id=session.incarnation_id)
    assert await project(op) == before
    assert before["available"] is True
    assert len(op.model_calls) == 1 and len(op.physical) == 2


async def test_empty_selection_is_honest_insufficient_evidence(research):
    op = research
    op.model_response = reply([])
    runner, result = await outcome(op)
    op.completed = await runner._finish_operation(op.executor, result)
    value = await project(op)
    assert value["available"] is True
    assert value["content"]["disposition"] == "insufficient_evidence"
    assert value["content"]["passages"] == []


@pytest.mark.parametrize("key_change", ["missing", "changed", "historical", "conflicting"])
async def test_exact_named_key_required_without_current_key_fallback(completed, monkeypatch, key_change):
    secret = "synthetic-research-binding-key-" + "x" * 32
    if key_change == "missing":
        monkeypatch.delenv("AUDIT_HMAC_SECRET")
    elif key_change == "changed":
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-wrong-key-" + "y" * 40)
    elif key_change == "conflicting":
        monkeypatch.setenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", "synthetic-conflict-" + "z" * 40)
    else:
        monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "successor_test")
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-successor-key-" + "y" * 40)
        monkeypatch.setenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", secret)
    value = await project(completed)
    if key_change == "historical":
        assert value["available"] is True
    else:
        unavailable(value)


@pytest.mark.parametrize("change", ["text", "unknown", "reference", "source_digest"])
async def test_checkpoint_cannot_substitute_arbitrary_result_content(completed, change):
    def corrupt(data):
        page = data["checkpoint"]["research_result"]
        if change == "text":
            page["passages"][0]["text"] = "Private unsupported conclusion"
        elif change == "unknown":
            page["provider_configuration"] = "private-unrelated-field"
        elif change == "reference":
            data["operation"]["result_reference"] = completed.source.action_id
        else:
            page["source"]["result_digest"] = "a" * 64
    await _mutate_record(completed, corrupt)
    unavailable(await project(completed))


async def test_unrelated_private_checkpoint_fields_are_never_copied(completed):
    await _mutate_record(completed, lambda data: data["checkpoint"].update(
        provider_secret="private-provider-key", instructions="private-instructions",
        unrelated={"message": "private-body"}))
    value = await project(completed)
    assert value["available"] is True
    encoded = canonical(value)
    assert all(secret not in encoded for secret in (
        "private-provider-key", "private-instructions", "private-body",
        "synthetic-provider-key", "Read one page.", "payload_binding", "binding_key_id"))


@pytest.mark.parametrize("change", ["digest", "selection", "actual", "attempt", "unavailable", "unknown"])
async def test_model_receipt_tampering_cannot_create_readable_result(completed, change):
    def corrupt(data):
        result = data["result"]
        if change == "digest":
            result["result_digest"] = "a" * 64
        elif change == "selection":
            result["result"]["passage_ids"] = []
        elif change == "actual":
            result["actual"]["tokens"] += 1
        elif change == "attempt":
            data["attempts"][-1]["attempt_id"] = str(uuid4())
        elif change == "unavailable":
            result["result_available"] = False
            return
        else:
            result["result"]["private"] = "private-provider-key"
        # Alter both mirrors: equality alone is insufficient; the result MAC
        # must authenticate selection, attempt and consumption facts.
        data["attempts"][-1]["outcome"] = {key: value for key, value in result.items()
                                         if key != "result_available"}
    await _mutate_action(completed, completed.model, corrupt)
    unavailable(await project(completed))


@pytest.mark.parametrize("change", ["digest", "text", "url", "attempt", "unissued", "missing"])
async def test_source_requires_actual_available_settled_observation(completed, change):
    op = completed
    if change == "missing":
        with op.runtime.transaction() as tx:
            tx.execute("DELETE FROM persistent_assignment_action WHERE id=%s AND owner_user_id=%s",
                       (op.source.action_id, op.owner))
    else:
        def corrupt(data):
            result = data["result"]
            if change == "digest":
                result["result_digest"] = "b" * 64
            elif change == "text":
                result["result"]["text"] = "Unverified replacement source"
                result["result_digest"] = digest(result["result"])
            elif change == "url":
                data["intent"]["request"]["arguments"]["url"] = "https://93.184.216.34/other"
                data["intent"]["request_digest"] = digest(data["intent"]["request"])
            elif change == "attempt":
                data["attempts"][-1]["outcome"]["result_digest"] = "b" * 64
                return
            else:
                data["attempts"][-1]["dispatch_token"] = None
                return
            data["attempts"][-1]["outcome"] = dict(result)
        await _mutate_action(op, op.source, corrupt)
    unavailable(await project(op))


@pytest.mark.parametrize("kind", ["source", "model"])
@pytest.mark.parametrize("field", ["owner_id", "assignment_id", "instruction_revision", "control_epoch"])
async def test_action_identity_must_match_exact_completed_operation(completed, monkeypatch, kind, field):
    op = completed
    repository = op.runtime.repositories.assignments
    original = repository.get_action
    action_id = getattr(op, kind).action_id
    def substituted(tx, **kwargs):
        value = original(tx, **kwargs)
        if value is not None and value.action_id == action_id:
            replacement = (getattr(value, field) + 1 if field.endswith(("revision", "epoch"))
                           else "another-owner" if field == "owner_id" else str(uuid4()))
            return replace(value, **{field: replacement})
        return value
    monkeypatch.setattr(repository, "get_action", substituted)
    unavailable(await project(op))


@pytest.mark.parametrize("boundary", ["source", "model", "operation"])
async def test_second_reads_refuse_changes_in_same_supplied_transaction(completed, monkeypatch, boundary):
    op = completed
    repository = op.runtime.repositories.assignments
    action_read, operation_read = repository.get_action, repository.get_operation
    calls, transactions = {}, []
    def action(tx, **kwargs):
        value = action_read(tx, **kwargs)
        transactions.append(tx)
        key = kwargs["action_id"]
        calls[key] = calls.get(key, 0) + 1
        if boundary != "operation" and key == getattr(op, boundary).action_id and calls[key] == 2:
            return replace(value, state="failed")
        return value
    def operation(tx, **kwargs):
        value = operation_read(tx, **kwargs)
        transactions.append(tx)
        calls["operation"] = calls.get("operation", 0) + 1
        if boundary == "operation" and calls["operation"] == 3:
            return replace(value, assignment=replace(value.assignment,
                           state_version=value.assignment.state_version + 1))
        return value
    monkeypatch.setattr(repository, "get_action", action)
    monkeypatch.setattr(repository, "get_operation", operation)
    unavailable(await project(op))
    assert transactions and all(tx is transactions[0] for tx in transactions)


async def test_duck_typed_record_is_not_a_plane_receipt(completed):
    from orchestrator.work_result import project_research_result
    op = completed
    with op.runtime.transaction() as tx:
        repository = op.runtime.repositories.assignments
        read = repository.get_operation(tx, owner_id=op.owner, assignment_id=op.completed.assignment_id)
        fake = SimpleNamespace(**{name: getattr(read, name) for name in read.__dataclass_fields__})
        unavailable(project_research_result(tx, repository, owner_id=op.owner, read=fake))


async def test_boolean_disposition_version_is_not_version_one(completed):
    def corrupt(data):
        data["result"]["result_disposition"]["version"] = True
        data["attempts"][-1]["outcome"]["result_disposition"]["version"] = True
    await _mutate_action(completed, completed.model, corrupt)
    unavailable(await project(completed))


async def test_unfinished_operation_never_exposes_a_source_as_final_result(research):
    research.completed = await current(research)
    value = await project(research)
    assert value == {"version": 1, "available": False, "reason": "not_completed", "content": None}


@pytest.mark.parametrize("change,reason", [
    ("future_operation", "unsupported"), ("future_checkpoint", "unsupported"),
    ("nonretained", "not_retained"),
])
async def test_unknown_and_nonretained_operations_never_reuse_checkpoint(completed, change, reason):
    def mutate(data):
        if change == "future_operation":
            data["operation"]["version"] = 3
        elif change == "future_checkpoint":
            data["checkpoint"]["schema_version"] = 2
        else:
            data["operation"]["source_retention"] = "none"
    await _mutate_record(completed, mutate)
    assert await project(completed) == {
        "version": 1, "available": False, "reason": reason, "content": None}


async def test_actual_committed_retirement_between_reads_suppresses_content(completed, monkeypatch):
    op = completed
    repository = op.runtime.repositories.assignments
    # Retire after unlocked discovery but before the first metadata owner/row
    # lock. A synchronous second writer after that lock would wait on this test.
    original = repository.get_selected_input
    retired = False
    def read(tx, **kwargs):
        nonlocal retired
        if not retired:
            retired = True
            with op.runtime.transaction() as other:
                assert repository.delete_for_owner(other, owner_id=op.owner,
                    assignment_id=op.completed.assignment_id,
                    expected_control_epoch=op.completed.control_epoch,
                    expected_state_version=op.completed.state_version)
        return original(tx, **kwargs)
    monkeypatch.setattr(repository, "get_selected_input", read)
    unavailable(await project(op))
    assert retired


async def test_independent_retirement_waits_for_result_reader_then_commits(completed, monkeypatch):
    op = completed
    repository = op.runtime.repositories.assignments
    original = repository.get_selected_input
    entered, release, requesting = threading.Event(), threading.Event(), threading.Event()
    details = {}
    def locked(tx, **kwargs):
        value = original(tx, **kwargs)
        if not entered.is_set():
            details["reader"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            assert release.wait(5), "test did not release its bounded read gate"
        return value
    monkeypatch.setattr(repository, "get_selected_input", locked)
    def retire():
        with op.runtime.transaction() as tx:
            details["writer"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            requesting.set()
            assert repository.delete_for_owner(tx, owner_id=op.owner,
                assignment_id=op.completed.assignment_id,
                expected_control_epoch=op.completed.control_epoch,
                expected_state_version=op.completed.state_version)
        details["committed"] = True
    def blocked():
        with op.runtime.transaction() as tx:
            return tx.fetch_one("SELECT %s=ANY(pg_blocking_pids(%s)) AS waiting",
                (details["reader"], details["writer"]))["waiting"]
    reading = asyncio.create_task(project(op))
    writer = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        writer = asyncio.create_task(asyncio.to_thread(retire))
        assert await asyncio.to_thread(requesting.wait, 5)
        async with asyncio.timeout(3):
            while not await asyncio.to_thread(blocked):
                await asyncio.sleep(0.001)
        assert not details.get("committed", False)
        release.set()
        value = await reading
        assert value["available"] is True
        await writer
        assert details["committed"] is True
        unavailable(await project(op))
    finally:
        release.set()
        tasks = (reading,) if writer is None else (reading, writer)
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_availability_requires_boolean_true(completed):
    def corrupt(data):
        data["result"]["result_disposition"]["available"] = 1
        data["attempts"][-1]["outcome"]["result_disposition"]["available"] = 1
    await _mutate_action(completed, completed.model, corrupt)
    unavailable(await project(completed))


async def test_plane_and_projector_refuse_numeric_type_substitution(completed):
    op = completed
    cases = [
        (op.model, ("result", "actual", "tokens"), True),
        (op.model, ("intent", "maximum", "model_calls"), True),
        (op.model, ("intent", "transient_input", "version"), True),
        (op.model, ("intent", "transient_input", "references", 0, "revision"), True),
        (op.model, ("result", "result", "version"), True),
        (op.source, ("result", "result", "redacted"), 1),
        (op.source, ("result", "result", "excerpt_complete"), 1),
        (op.model, ("instruction_revision",), True),
        (op.model, ("control_epoch",), True),
        (op.source, ("instruction_revision",), True),
        (op.source, ("control_epoch",), True),
        (op.model, ("intent", "request", "max_output_tokens"), 1024.0),
        (op.model, ("intent", "transient_input", "payload_binding"), 1),
        (op.model, ("intent", "transient_input", "binding_key_id"), "Unknown Key"),
        (op.model, ("intent", "transient_input", "references", 0, "resource_id"), 1),
        (op.model, ("result", "actual", "spend_micro_units"), True),
        (op.model, ("result", "actual", "currency"), "USD"),
    ]
    for action, path, value in cases:
        with op.runtime.transaction() as tx:
            original = thaw(tx.fetch_one("SELECT data FROM persistent_assignment_action WHERE id=%s",
                                        (action.action_id,))["data"])
        def corrupt(data):
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            data["attempts"][-1]["outcome"] = {key: item for key, item in data["result"].items()
                                              if key != "result_available"}
        await _mutate_action(op, action, corrupt)
        value = await project(op)
        assert value["available"] is False, path
        unavailable(value)
        await _mutate_action(op, action, lambda data: data.update(thaw(original)))
    assert (await project(op))["available"] is True


async def test_transient_and_action_shapes_cannot_claim_supported_profile(completed, monkeypatch):
    op = completed
    repository = op.runtime.repositories.assignments
    original = repository.get_action
    transient = op.model.intent.transient_input
    variants = [
        replace(op.model, intent=replace(op.model.intent, transient_input=None)),
        replace(op.model, intent=replace(op.model.intent, transient_input=replace(transient, version=2))),
        replace(op.model, intent=replace(op.model.intent, transient_input=replace(transient, references=()))),
        replace(op.model, intent=replace(op.model.intent, transient_input=replace(transient,
            references=(replace(transient.references[0], revision=2),)))),
        replace(op.model, intent=replace(op.model.intent, transient_input=replace(transient,
            source_retention="none"))),
        replace(op.model, intent=replace(op.model.intent, request={**thaw(op.model.intent.request),
            "private": "private-injected"})),
        replace(op.model, intent=replace(op.model.intent, task_id=str(uuid4()))),
        replace(op.model, intent=replace(op.model.intent, boundary="read_only")),
        replace(op.model, intent=replace(op.model.intent, request_digest="a" * 64)),
        replace(op.model, attempts=()),
        replace(op.model, ever_started=False),
    ]
    for variant in variants:
        with monkeypatch.context() as scoped:
            def substitute(tx, **kwargs):
                value = original(tx, **kwargs)
                return variant if value is not None and value.action_id == op.model.action_id else value
            scoped.setattr(repository, "get_action", substitute)
            unavailable(await project(op))


async def test_discovery_is_rechecked_and_action_locks_follow_execution_order(completed, monkeypatch):
    op = completed
    repository = op.runtime.repositories.assignments
    original, peek = repository.get_action, repository.get_action_by_key
    calls = []
    def discovery(tx, **kwargs):
        calls.append(("peek", kwargs["action_key"], tx))
        return peek(tx, **kwargs)
    def action(tx, **kwargs):
        calls.append(("lock", kwargs["action_id"], tx))
        return original(tx, **kwargs)
    monkeypatch.setattr(repository, "get_action_by_key", discovery)
    monkeypatch.setattr(repository, "get_action", action)
    assert (await project(op))["available"] is True
    from persistent_agents.research_episode import research_action_keys
    assert calls[0][:2] == ("peek", "research-v1-" + research_action_keys(op.completed)[1])
    assert [call[1] for call in calls[1:3]] == sorted((op.source.action_id, op.model.action_id))
    assert len(calls) == 5 and all(call[2] is calls[0][2] for call in calls)


@pytest.mark.parametrize("change", ["missing", "identity", "source", "changed_locked_model"])
async def test_discovery_alone_cannot_authorize_content(completed, monkeypatch, change):
    op = completed
    repository = op.runtime.repositories.assignments
    original = repository.get_action_by_key
    def discover(tx, **kwargs):
        value = original(tx, **kwargs)
        if change == "missing":
            return None
        if change == "identity":
            return replace(value, action_id=str(uuid4()))
        if change == "source":
            transient = value.intent.transient_input
            return replace(value, intent=replace(value.intent, transient_input=replace(transient,
                references=(replace(transient.references[0], resource_id=op.source_action.action_id),))))
        # Same identities, different descriptor between unlocked peek and locked
        # read: the locked receipt may not silently replace the observed one.
        return replace(value, intent=replace(value.intent, precondition_digest="a" * 64))
    monkeypatch.setattr(repository, "get_action_by_key", discover)
    unavailable(await project(op))
