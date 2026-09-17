"""Source-less one-shot chat through actual HTTP admission, supervision and PG.

Plane, encrypted configuration, JWT verification, reservations, the private
proof binding and audit are real. Only institutional replies and the final
model transport are synthetic. No source is read and none may be claimed.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from llm_config import research_profile as profile
from llm_config.tests.test_research_profile_088 import reply
from orchestrator.work_chat_result import project_chat_result
from orchestrator.work_result import project_research_result
from persistent_agents.chat_episode import CHAT_PROFILE
from persistent_agents.runtime_values import thaw
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_retry_http_postgres_088 import stored as _stored, wait_for as _wait_for
from tests.test_work_runtime_postgres_088 import (
    finished,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]
async def stored(op, identity):
    row, actions, now = await _stored(op, identity)
    return thaw(row), thaw(actions), now


async def wait_for(op, identity, predicate, *, seconds=35):
    row, actions, now = await _wait_for(op, identity, predicate, seconds=seconds)
    return thaw(row), thaw(actions), now


INSTRUCTION = "Explain in one sentence what a durable one-shot task is."
ANSWER = "A durable one-shot task keeps its identity and accounting until it settles."
SYSTEM_MARKER = "Answer the owner's request directly from your own knowledge"


def chat_command(**changes):
    values = {
        "version": 1, "caller_key": str(uuid4()), "kind": "chat", "name": "One question",
        "instructions": INSTRUCTION, "source": None,
        "limits": {"model_calls": 1, "tool_calls": 1, "tokens": profile.RESERVED_TOKENS,
                   "elapsed_ms": profile.RESERVED_MILLISECONDS, "max_retries": 0},
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        .replace("+00:00", "Z"),
        "conversation_id": None,
    }
    values.update(changes)
    return values


def chat_reply(text=ANSWER, **changes):
    value = reply(**changes)
    value["choices"][0]["message"]["content"] = json.dumps({"version": 1, "text": text})
    return value


@pytest.fixture(autouse=True)
def bounded_supervisor(monkeypatch):
    # Existing operator-supported bounds: fast discovery, one worker slot.
    monkeypatch.setenv("PERSISTENT_AGENTS_TICK_SECONDS", "1")
    monkeypatch.setenv("PERSISTENT_AGENTS_CONCURRENCY", "1")
    monkeypatch.setenv("PERSISTENT_AGENTS_LEASE_SECONDS", "15")


@pytest.fixture
def chat_transport(integrated, monkeypatch):
    op, _runner, _client = integrated
    op.chat_response = chat_reply()

    async def transport(method, url, **kwargs):
        op.model_calls.append((method, url, kwargs))
        return SimpleNamespace(body=json.dumps(op.chat_response).encode(), status_code=200)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    return op


async def test_chat_turn_is_admitted_executed_and_retained_without_any_source(
    integrated, chat_transport,
):
    op, runner, client = integrated
    prior_reads = len(op.physical)
    body = chat_command()
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    final = await finished(client, identity)
    assert final["kind"] == "chat" and final["disposition"] == "completed"
    assert final["usage"]["spent"]["tokens"] == 120
    assert final["usage"]["spent"]["model_calls"] == 1
    assert final["usage"]["spent"].get("tool_calls", 0) == 0
    assert all(value == 0 for value in final["usage"]["outstanding"].values())
    # No reader ran; exactly one fixed model send carried the owner's text.
    assert len(op.physical) == prior_reads and len(op.model_calls) == 1
    method, url, sent = op.model_calls[0]
    assert method == "POST" and url == profile.ENDPOINT
    assert sent["json_body"]["messages"][1] == {"role": "user", "content": INSTRUCTION}
    assert sent["json_body"]["store"] is False and sent["json_body"]["stream"] is False
    assert "passages" not in json.dumps(sent["json_body"])
    row, actions, _ = await stored(op, identity)
    assert row["operation"]["kind"] == "chat"
    assert row["definition"]["source"] == {} and row["definition"]["allowed_tools"] == []
    assert len(actions) == 1
    [model] = actions
    assert model["intent"]["request"]["kind"] == "model"
    assert model["intent"]["transient_input"]["references"] == []
    assert model["state"] == "succeeded" and len(model["attempts"]) == 1
    assert model["result"]["result"] == {"version": 1, "profile": CHAT_PROFILE, "text": ANSWER}
    assert model["result"]["result_available"] is True
    assert row["operation"]["result_reference"] == model["action_id"]
    assert row["checkpoint"]["chat_result"] == {
        "version": 1, "kind": "chat", "text": ANSWER, "model_action_id": model["action_id"]}
    serialized = json.dumps(thaw([row, actions]))
    for text in ('"messages"', SYSTEM_MARKER, "synthetic-provider-key", "payload_binding\": \"" + "x"):
        assert text not in serialized
    assert "research_result" not in row["checkpoint"] and "research_source" not in row["checkpoint"]
    events, _ = await asyncio.to_thread(op.audit._repo.list_for_user, op.owner)
    accepts = [event for event in events if event.action_type == "work.accept"]
    assert len(accepts) == 1 and accepts[0].inputs_meta["kind"] == "chat"
    assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None


async def test_chat_receipt_replay_and_tick_never_send_a_second_turn(integrated, chat_transport):
    op, runner, client = integrated
    body = chat_command()
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    await finished(client, identity)
    replay = await client.post("/api/work/v1/operations", json=body)
    assert replay.status_code == 200 and replay.json() == {
        "id": identity, "revision": replay.json()["revision"], "created": False}
    await runner.tick()
    await runner.tick()
    assert len(op.model_calls) == 1
    _, actions, _ = await stored(op, identity)
    assert len(actions) == 1 and len(actions[0]["attempts"]) == 1
    listing = await client.get("/api/work/v1/operations")
    assert listing.status_code == 200
    rows = [item for item in listing.json()["operations"] if item["id"] == identity]
    assert rows and rows[0]["kind"] == "chat" and "result" not in rows[0]


@pytest.mark.parametrize("change", [
    {"source": {"url": "https://93.184.216.34/releases"}},
    {"source": {}},
    {"source_retention": "none"},
    {"selection": {"version": 1, "agent": None, "skills": [{"skill_id": str(uuid4()), "revision": 1}],
                   "notes": []}},
    {"kind": "search"},
    {"limits": {"model_calls": 1, "tool_calls": 1, "tokens": profile.RESERVED_TOKENS - 1,
                "elapsed_ms": profile.RESERVED_MILLISECONDS, "max_retries": 0}},
])
async def test_chat_refuses_sources_selection_retention_and_unknown_kinds(
    integrated, chat_transport, change,
):
    op, runner, client = integrated
    before = len(op.model_calls), len(op.physical)
    response = await client.post("/api/work/v1/operations", json=chat_command(**change))
    assert response.status_code in {422, 503}, response.text
    assert response.json()["error"] in {"work_submit_invalid", "work_research_budget_insufficient"}
    listing = await client.get("/api/work/v1/operations")
    assert all(item["kind"] != "chat" for item in listing.json()["operations"])
    events, _ = await asyncio.to_thread(op.audit._repo.list_for_user, op.owner)
    assert not any(event.action_type == "work.accept" for event in events)
    await runner.tick()
    assert (len(op.model_calls), len(op.physical)) == before


@pytest.mark.parametrize("case", ["prose", "wrong-model", "missing-usage", "http-error", "phi"])
async def test_unusable_or_refused_answer_is_charged_and_never_retained(
    integrated, chat_transport, monkeypatch, case,
):
    op, runner, client = integrated
    if case == "prose":
        op.chat_response["choices"][0]["message"]["content"] = "private model prose"
    elif case == "wrong-model":
        op.chat_response["model"] = "another-model"
    elif case == "missing-usage":
        op.chat_response["usage"] = None
    elif case == "http-error":
        async def failing(method, url, **kwargs):
            op.model_calls.append((method, url, kwargs))
            return SimpleNamespace(body=b'{"error":"synthetic outage"}', status_code=500)
        monkeypatch.setattr("shared.isolated_http.request", failing)
    else:
        op.chat_response = chat_reply("Patient John Doe has a fever.")
        # Only the answer trips the gate; the owner's own prompt still passes.
        monkeypatch.setattr("persistent_agents.execution.get_phi_gate",
                            lambda: SimpleNamespace(contains_phi=lambda text: "John Doe" in text))
    accepted = await client.post("/api/work/v1/operations", json=chat_command())
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    known = case in {"prose", "wrong-model", "phi"}
    row, actions, _ = await wait_for(op, identity, lambda row, _a, _n: (
        row["claim_token"] is None and row["phase"] in {"failed", "reconciliation"}))
    [model] = actions
    assert len(op.model_calls) == 1 and len(model["attempts"]) == 1
    assert model["result"].get("result") in ({}, None) or "text" not in model["result"]["result"]
    if known:
        assert model["state"] == "failed" and row["phase"] == "failed"
        assert row["usage"]["spent"]["tokens"] == 120
        assert row["usage"]["outstanding"]["tokens"] == 0
        assert row["safe_error_code"] == "assignment_retry_exhausted"
    else:
        assert model["state"] == "uncertain" and row["phase"] == "reconciliation"
        assert row["usage"]["outstanding"]["tokens"] == profile.RESERVED_TOKENS
    serialized = json.dumps(thaw([row, actions]))
    for text in ("private model prose", "John Doe", "synthetic outage", "chat_result"):
        assert text not in serialized
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.json()["result"]["content"] is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing, cross-cutting defect in shared orchestrator/work_result.py::_amount "
        "(imported unchanged by orchestrator/work_chat_result.py -- not a D3/chat-specific bug and "
        "not fixable within this workstream's allowed files): AssignmentResourceAmount gained an "
        "additive `basis` field (components/AstralPlane assignment_models.py, FR-022/schema >=088.008), "
        "but `_amount`'s `_require(set(value) == counters | {\"spend_micro_units\", \"currency\"})` was "
        "never updated to admit it, so `thaw()`-ing ANY action's `intent.maximum` now raises "
        "ValueError('work_result_unavailable') inside `_action` -> `project_chat_result` returns the "
        "honest-but-wrong 'unavailable' envelope instead of the answer. Confirmed NOT chat-specific: "
        "backend/tests/test_work_result_postgres_088.py (project_research_result, D2's file, untouched "
        "by D3) shows the identical failure pattern on this same checkout (5+ failures of the first 11 "
        "tests run in isolation). Needs a fix in work_result.py::_amount, outside D3-deep-retry-and-"
        "chat-oneshot's allowed files; remove this marker once that lands."
    ),
)
async def test_chat_result_projection_is_honest_and_cites_nothing(integrated, chat_transport):
    op, runner, client = integrated
    accepted = await client.post("/api/work/v1/operations", json=chat_command())
    identity = accepted.json()["id"]
    await finished(client, identity)
    repository = op.runtime.repositories.assignments

    class _Discard(Exception):
        """Roll the tampering transaction back; nothing tampered is committed."""

    def project(projector, tamper=None):
        outcome = []
        try:
            with op.runtime.transaction() as tx:
                if tamper is not None:
                    tamper(tx)
                read = repository.get_operation(tx, owner_id=op.owner, assignment_id=identity)
                outcome.append(projector(tx, repository, owner_id=op.owner, read=read))
                if tamper is not None:
                    raise _Discard
        except _Discard:
            pass
        return outcome[0]

    envelope = project(project_chat_result)
    assert envelope == {"version": 1, "available": True, "reason": None, "content": {
        "version": 1, "kind": "chat", "scope": "model_only", "sources": [], "text": ANSWER}}
    # The research projection never lends a chat answer source attribution.
    assert project(project_research_result) == {
        "version": 1, "available": False, "reason": "unsupported", "content": None}
    with op.runtime.transaction() as tx:
        foreign = repository.get_operation(tx, owner_id=op.owner, assignment_id=identity)
        assert project_chat_result(tx, repository, owner_id="someone-else", read=foreign)["available"] is False

    def rewrite_checkpoint(tx):
        row = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (identity,))
        value = thaw(row["data"])
        value["checkpoint"]["chat_result"]["text"] = "an answer the model never gave"
        tx.execute("UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
                   (json.dumps(value), identity))

    assert project(project_chat_result, rewrite_checkpoint) == {
        "version": 1, "available": False, "reason": "unavailable", "content": None}

    def rewrite_answer(tx):
        row = tx.fetch_one("SELECT id,data FROM persistent_assignment_action WHERE assignment_id=%s",
                           (identity,))
        value = thaw(row["data"])
        value["result"]["result"]["text"] = "an answer the model never gave"
        tx.execute("UPDATE persistent_assignment_action SET data=%s::jsonb WHERE id=%s",
                   (json.dumps(value), row["id"]))

    assert project(project_chat_result, rewrite_answer)["available"] is False
    delivered = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert delivered.status_code == 200 and delivered.headers["cache-control"] == "no-store"
    assert "passages" not in delivered.text and "source_action_id" not in delivered.text


async def test_chat_original_session_fence_and_terminal_forget_are_the_shared_ones(
    integrated, chat_transport,
):
    op, runner, client = integrated
    # Hold the single worker slot so no claim can race the session replacement.
    runner._active[("blocker", 0)] = asyncio.create_task(asyncio.sleep(3600))
    accepted = await client.post("/api/work/v1/operations", json=chat_command())
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    original = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
    # The original incarnation is replaced before any claim: discovery must
    # refuse without a model send, exactly as the research profile does.
    replaced = await asyncio.to_thread(replace_session_record, op.runtime, original)
    assert replaced.incarnation_id != original.incarnation_id
    runner._active.pop(("blocker", 0)).cancel()
    await runner.tick()
    await runner.tick()
    row, actions, _ = await stored(op, identity)
    # Never claimed: the accepted turn stays due, unclaimed and unexecuted.
    assert row["lifecycle"] == "active" and row["phase"] == "waiting"
    assert row["claim_token"] is None and actions == [] and op.model_calls == []
    listing = await client.get("/api/work/v1/operations")
    assert any(item["id"] == identity and item["kind"] == "chat"
               for item in listing.json()["operations"])
    # A second, unfenced chat completes and is then forgotten through the
    # shared terminal delete: replay of its key is refused, its rows are gone.
    second = chat_command()
    accepted = await client.post("/api/work/v1/operations", json=second)
    assert accepted.status_code == 201, accepted.text
    other = accepted.json()["id"]
    final = await finished(client, other)
    deleted = await client.request("DELETE", f"/api/work/v1/operations/{other}",
                                   json={"expected_revision": final["revision"]})
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert (await client.get(f"/api/work/v1/operations/{other}")).status_code == 404
    replay = await client.post("/api/work/v1/operations", json=second)
    # The tombstoned key never creates another task; the admission route's
    # closed error allowlist keeps the deletion reason private over HTTP.
    assert replay.status_code == 503 and replay.json() == {"error": "work_submit_unavailable"}
    listing = await client.get("/api/work/v1/operations")
    assert all(item["id"] != other for item in listing.json()["operations"])
    with op.runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action "
                            "WHERE assignment_id=%s", (other,))["n"] == 0
    assert len(op.model_calls) == 1


async def test_chat_private_input_and_executor_entries_refuse_the_other_profile(
    integrated, chat_transport,
):
    from persistent_agents.dispatch_context import DispatchDenied
    from persistent_agents.research_input import ResearchInput, route
    from persistent_agents.research_recovery import EphemeralResearchSource

    op, runner, client = integrated
    accepted = await client.post("/api/work/v1/operations", json=chat_command())
    identity = accepted.json()["id"]
    await finished(client, identity)
    record = (await op.executor.store.call("get_operation", owner_id=op.owner,
                                           assignment_id=identity)).assignment
    model = await op.executor.store.call("get_action", owner_id=op.owner, assignment_id=identity,
                                         action_id=record.operation["result_reference"])
    private = await ResearchInput.capture_chat(record, config_store=op.executor.orch._llm_store,
                                               key_id=model.intent.transient_input.binding_key_id)
    assert private.kind == "chat" and private.passage_ids == () and private.source_action_id is None
    assert private.transient().references == ()
    # A completed operation's private input MAC is never reconstructed: the
    # binding covers the pre-completion operation, so even the chat reader
    # refuses it here, exactly as research does (delivery verifies by key).
    for denied in (lambda: private.chat_result(model), lambda: private.retained_result(model),
                   lambda: private.selection_result([]), lambda: private.ephemeral_result(model, {}),
                   lambda: private.assert_current(record, model, None)):
        with pytest.raises(DispatchDenied):
            denied()
    # The research fixture record is not a chat turn: no chat entry accepts it.
    research_executor = op.executor
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await research_executor.chat_turn("not-a-chat-key")
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        await research_executor.refresh(route(), _research=private)
    with pytest.raises(DispatchDenied):
        await ResearchInput.capture_chat(research_executor.record,
                                         config_store=op.executor.orch._llm_store)
    with pytest.raises(DispatchDenied):
        await ResearchInput.capture(record, model, config_store=op.executor.orch._llm_store)
    assert type(private._ephemeral) is not EphemeralResearchSource
    # The chat projection is honest about a research record and an unfinished turn.
    repository = op.runtime.repositories.assignments
    with op.runtime.transaction() as tx:
        research = repository.get_operation(tx, owner_id=op.owner,
                                            assignment_id=research_executor.record.assignment_id)
        assert project_chat_result(tx, repository, owner_id=op.owner, read=research) == {
            "version": 1, "available": False, "reason": "unsupported", "content": None}
    assert len(op.model_calls) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Cross-workstream defect outside D3's allowed files, not a chat-specific gap: "
        "a due-but-never-claimed one-shot record whose deadline has already lapsed makes "
        "runner._tick_operations -> _operation_authority -> "
        "session_authority.refresh_operation_execution_authority -> "
        "_refresh_operation_session raise SessionAuthorityUnavailable from its own "
        "`min(expiry, deadline) <= reference.state.observed_at` check "
        "(backend/orchestrator/session_authority.py, inside the `try` whose blanket "
        "`except Exception: raise SessionAuthorityUnavailable(...) from None` erases the "
        "deadline detail) before the claim transaction -- and Plane's deadline-conflict "
        "handling -- is ever reached. runner.py::_tick_operations's bare "
        "`except Exception: logger.warning('one_shot_claim_unavailable')` then drops that "
        "refusal every tick without writing any terminal state, and no other sweep "
        "revisits a record whose lease_expires_at was never set (recover_expired_operations_"
        "for_administration only reaps rows that WERE claimed and then lease-expired). "
        "Reproduced live (isolated DB, 27 passed / 1 failed in the rest of this file) with "
        "18x 'one_shot_claim_unavailable' warnings and a confirmed "
        "orchestrator.session_authority.SessionAuthorityUnavailable traceback. Shared with "
        "the research profile -- no prior test exercised 'deadline lapses while the single "
        "worker slot is occupied and the record is never claimed' for research either. "
        "Needs a fix in session_authority.py and/or runner.py::_tick_operations, both "
        "outside D3-deep-retry-and-chat-oneshot's allowed files; remove this marker once "
        "that lands."
    ),
)
async def test_chat_deadline_expiry_fence_is_the_shared_one(integrated, chat_transport):
    """A chat turn past its deadline is retired by the shared claim-time fence.

    No chat-specific expiry exists: Plane refuses the claim, records the closed
    deadline code and terminalizes the operation without any model send.
    """
    op, runner, client = integrated
    runner._active[("blocker", 0)] = asyncio.create_task(asyncio.sleep(3600))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=4)
    accepted = await client.post("/api/work/v1/operations", json=chat_command(
        deadline_at=deadline.isoformat().replace("+00:00", "Z")))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    await asyncio.sleep(4.5)
    runner._active.pop(("blocker", 0)).cancel()
    await runner.tick()
    row, actions, _ = await wait_for(
        op, identity, lambda row, _a, _n: row["lifecycle"] == "completed" and row["claim_token"] is None)
    assert row["operation"]["kind"] == "chat"
    assert row["operation"]["terminal_outcome"] == "failed"
    assert row["safe_error_code"] == "assignment_deadline_exceeded"
    assert actions == [] and op.model_calls == []
    assert row["usage"]["spent"].get("tokens", 0) == 0
    assert row["usage"]["spent"].get("model_calls", 0) == 0
    assert all(value == 0 for value in row["usage"]["outstanding"].values())
    assert "chat_result" not in row["checkpoint"]
    await runner.tick()
    assert op.model_calls == []
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.json()["result"]["content"] is None
    detail = await client.get(f"/api/work/v1/operations/{identity}")
    assert detail.status_code == 200
    operation = detail.json()["operation"]
    assert operation["kind"] == "chat" and operation["lifecycle"] == "completed"
    assert operation["disposition"] != "completed"
