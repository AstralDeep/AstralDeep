"""Private declarative metadata over real IAM, Plane transactions and audit.

The production supervisor is running with a controlled no-dispatch tick. No
definition is wired into Work, and no provider, executable or UI is started.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.human_request_authority import (
    HumanRequestBoundary, authenticate_current_human_request,
)
from orchestrator.projection_surfaces import authoring
from orchestrator.user_agents import UserAgentRegistry
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import thaw
from tests.test_declarative_agent_definition_088 import definition
from tests.test_request_session_authority_088 import request
from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def declarations(api, monkeypatch):
    from personalization import phi_gate
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    monkeypatch.setattr(phi_gate, "_GATE", phi_gate.PHIGate(
        analyzer=SimpleNamespace(analyze=lambda **_: [])))
    api.orch.user_agent_registry = UserAgentRegistry(plane_runtime=api.runtime)
    boundary = HumanRequestBoundary(api.orch)
    api.orch.human_request_boundary = boundary
    result = SimpleNamespace(api=api, boundary=boundary,
                             service=authoring.DeclarativeAgentService(api.orch))
    try:
        yield result
    finally:
        boundary.close()
        api.service.store.async_runtime.close()


async def caller(state, *, method="POST", cookie=True, owner=None):
    fixture = state.api.fixture
    token = fixture[3](**({"sub": owner} if owner else {}))
    incoming = request(fixture[2] if cookie else None, method=method, headers=[
        (b"authorization", ("Bearer " + token).encode()),
        (b"content-type", b"application/json"), (b"origin", b"https://app.invalid"),
    ])
    incoming.scope["app"] = state.api.app
    return await authenticate_current_human_request(incoming, boundary=state.boundary)


def body(**changes):
    value = dict(command="create", command_id=str(uuid4()), agent_id=str(uuid4()),
                 revision_id=str(uuid4()), display_name="Public evidence", definition=definition())
    value.update(changes)
    return authoring.DeclarativeAgentRequest(**value)


def next_body(result, command, **changes):
    return authoring.DeclarativeAgentRequest(command=command, command_id=str(uuid4()),
        agent_id=result.agent.agent_id, expected_revision=result.agent.state_revision, **changes)


def research_definition(state):
    from llm_config import research_profile as profile
    value = definition()
    value["capabilities"] = [{"agent_id": "web-research-1", "tool_name": "fetch_page"}]
    minimum = state.api.service.assignments.tool_bound("web-research-1:fetch_page")
    minimum = {**minimum, "model_calls": minimum["model_calls"] + 1,
               "tokens": minimum["tokens"] + profile.RESERVED_TOKENS,
               "elapsed_ms": minimum["elapsed_ms"] + profile.RESERVED_MILLISECONDS}
    for period in ("daily", "lifetime"):
        value["limits"][period].update(minimum)
    value["limits"]["step_timeout_ms"] = profile.RESERVED_MILLISECONDS
    return value


async def apply(state, intent=None, **kwargs):
    return await state.service.command(caller=await caller(state), body=intent or body(**kwargs))


def counts(state):
    with state.api.runtime.transaction() as tx:
        return tuple(tx.fetch_one(sql)["n"] for sql in [
            "SELECT count(*) AS n FROM user_agent WHERE agent_kind='declarative'",
            "SELECT count(*) AS n FROM user_agent_revision WHERE revision_kind='declarative'",
            "SELECT count(*) AS n FROM user_agent_command_receipt",
            "SELECT count(*) AS n FROM audit_events WHERE action_type LIKE 'declarative_agent_%'",
        ])


async def test_tool_free_draft_and_real_history_without_work_flag(declarations, monkeypatch):
    state = declarations
    state.api.service.assignments.enabled = False
    monkeypatch.setenv("FF_PERSISTENT_AGENTS", "false")
    intent = body()
    result = await apply(state, intent)
    assert result.agent.agent_kind == result.revision.revision_kind == "declarative"
    assert result.agent.status == "draft" and result.agent.selected_definition_revision_id is None
    assert result.agent.active_revision_id is None and result.agent.host_client_id is None
    assert thaw(result.revision.definition_json) == definition()
    header, revisions = await state.service.history(caller=await caller(state, method="GET"),
        agent_id=result.agent.agent_id)
    assert header == result.agent and tuple(revisions) == (result.revision,)
    assert counts(state) == (1, 1, 1, 1) and state.api.fixture[-1] == []
    events, _ = state.api.service.audit.list_for_user(state.api.fixture[1])
    assert len(events) == 1
    with state.api.runtime.transaction() as tx:
        actor = tx.fetch_one("SELECT actor_user_id,auth_principal FROM audit_events")
    assert actor == {"actor_user_id": state.api.fixture[1], "auth_principal": state.api.fixture[1]}
    assert set(events[0].outputs_meta) == {
        "agent_id", "command_id", "state_revision", "definition_revision_id"}
    assert state.api.service.audit.verify_chain(state.api.fixture[1]) is None
    assert "Keep quotations" not in repr(result)


async def test_activate_revise_clone_archive_delete_preserve_real_history(declarations):
    state = declarations
    first = await apply(state, definition=research_definition(state))
    activated = await apply(state, next_body(first, "activate", revision_id=first.revision.revision_id))
    assert activated.agent.status == "active"
    assert activated.agent.selected_definition_revision_id == first.revision.revision_id
    assert activated.agent.active_revision_id is None
    second_definition = research_definition(state) | {"instructions": "Use exactly attributed excerpts."}
    revised = await apply(state, next_body(activated, "revise", revision_id=str(uuid4()),
        parent_revision_id=first.revision.revision_id, display_name="Public evidence revised",
        definition=second_definition))
    assert revised.agent.status == "draft" and revised.agent.selected_definition_revision_id is None
    assert revised.revision.revision_number == 2
    cloned = await apply(state, body(command="clone", definition=None,
        source_agent_id=revised.agent.agent_id, source_revision_id=first.revision.revision_id))
    assert cloned.agent.status == "draft" and cloned.agent.selected_definition_revision_id is None
    assert thaw(cloned.revision.definition_json) == research_definition(state)
    archived = await apply(state, next_body(revised, "archive"))
    deleted = await apply(state, next_body(archived, "delete"))
    assert deleted.agent.deleted_at is not None and deleted.agent.status == "archived"
    header, revisions = await state.service.history(caller=await caller(state, method="GET"),
        agent_id=first.agent.agent_id, limit=1)
    assert header == deleted.agent and len(revisions) == 1 and revisions[0] == revised.revision
    _, older = await state.service.history(caller=await caller(state, method="GET"),
        agent_id=first.agent.agent_id, before_revision_number=revised.revision.revision_number)
    assert tuple(older) == (first.revision,)
    assert counts(state) == (2, 3, 6, 6)
    with state.api.runtime.transaction() as tx:
        for table in ("persistent_assignment", "persistent_assignment_action", "agent_trust", "agent_ownership"):
            assert tx.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"] == 0


async def test_get_caller_cannot_mutate_metadata(declarations):
    with pytest.raises(AssignmentError, match="human_write_required") as caught:
        await declarations.service.command(caller=await caller(declarations, method="GET"), body=body())
    assert caught.value.status_code == 403 and counts(declarations) == (0, 0, 0, 0)


@pytest.mark.parametrize("selected", [None, "owner", SimpleNamespace(owner_id="owner")])
async def test_plain_owner_and_foreign_caller_types_do_not_authorize(declarations, selected):
    with pytest.raises(AssignmentError, match="declarative_authentication_required"):
        await declarations.service.command(caller=selected, body=body())
    assert counts(declarations) == (0, 0, 0, 0)


async def test_foreign_owner_cannot_clone_read_or_mutate(declarations):
    state = declarations
    result = await apply(state)
    other = await caller(state, cookie=False, owner="another-owner")
    for intent in [next_body(result, "archive"), body(command="clone", definition=None,
            source_agent_id=result.agent.agent_id, source_revision_id=result.revision.revision_id)]:
        with pytest.raises(AssignmentError, match="declarative_not_found"):
            await state.service.command(caller=other, body=intent)
    with pytest.raises(AssignmentError, match="declarative_not_found"):
        await state.service.history(caller=other, agent_id=result.agent.agent_id)
    assert counts(state) == (1, 1, 1, 1)


async def test_exact_receipt_precedes_config_runner_and_current_selection(declarations, monkeypatch):
    state = declarations
    first = await apply(state, definition=research_definition(state))
    intent = next_body(first, "activate", revision_id=first.revision.revision_id)
    original = await apply(state, intent)
    archived = await apply(state, next_body(original, "archive"))
    monkeypatch.setattr(state.api.orch, "persistent_assignment_runner", None)
    monkeypatch.delenv("AUDIT_HMAC_SECRET")
    replay = await apply(state, intent)
    assert replay.replayed and replay.receipt == original.receipt
    assert replay.agent == archived.agent and replay.revision is None
    assert counts(state) == (1, 1, 3, 3)
    changed = intent.model_copy(update={"expected_revision": archived.agent.state_revision})
    with pytest.raises(AssignmentError, match="declarative_conflict"):
        await apply(state, changed)


@pytest.mark.parametrize("change", ["revision", "parent", "unknown-field", "bool-version"])
async def test_closed_request_and_stale_fences_refuse(declarations, change):
    state = declarations
    first = await apply(state)
    intent = next_body(first, "revise", revision_id=str(uuid4()),
        parent_revision_id=first.revision.revision_id, display_name="Public evidence revised",
        definition=definition())
    if change == "revision":
        intent = intent.model_copy(update={"expected_revision": first.agent.state_revision + 1})
    elif change == "parent":
        intent = intent.model_copy(update={"parent_revision_id": str(uuid4())})
    elif change == "bool-version":
        intent = intent.model_copy(update={"version": True})
    else:
        intent = intent.model_copy(update={"definition": definition() | {"executable": "private"}})
    with pytest.raises(AssignmentError):
        await apply(state, intent)
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize("failure", ["throw", "awaitable", "wrong-return", "binding", "expiry"])
async def test_audit_or_final_caller_failure_rolls_back_everything(declarations, monkeypatch, failure):
    state = declarations
    original = state.api.service.audit.insert_in_transaction
    selected = await caller(state)

    async def wrong():
        raise AssertionError("coroutine must be closed without running")

    def insert(*args, **kwargs):
        value = original(*args, **kwargs)
        if failure == "throw":
            raise RuntimeError("private instruction must not escape")
        if failure == "awaitable":
            return wrong()
        if failure == "wrong-return":
            return object()
        if failure == "binding":
            state.api.orch.user_agent_registry = object()
        if failure == "expiry":
            object.__setattr__(selected, "_until", datetime.now(timezone.utc) - timedelta(seconds=1))
        return value

    monkeypatch.setattr(state.api.service.audit, "insert_in_transaction", insert)
    with pytest.raises(AssignmentError) as caught:
        await state.service.command(caller=selected, body=body())
    assert "private instruction" not in str(caught.value) and counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize("failure", ["shape", "tokens", "time", "step", "currency", "runner", "key", "config", "scope"])
async def test_unsupported_activation_leaves_inert_draft(declarations, monkeypatch, failure):
    state = declarations
    value = research_definition(state)
    if failure == "shape":
        value["capabilities"] = []
    elif failure in {"tokens", "time"}:
        value["limits"]["daily"]["tokens" if failure == "tokens" else "elapsed_ms"] -= 1
    elif failure == "step":
        value["limits"]["step_timeout_ms"] -= 1
    elif failure == "currency":
        for period in ("daily", "lifetime"):
            value["limits"][period]["spend_micro_units"] = 1
            value["limits"][period]["currency"] = "USD"
    first = await apply(state, definition=value)
    if failure == "runner":
        state.api.runner._stopping = True
    elif failure == "key":
        monkeypatch.delenv("AUDIT_HMAC_SECRET")
    elif failure == "config":
        with state.api.runtime.transaction() as tx:
            state.api.runtime.repositories.encrypted_llm_config.delete_user(tx, owner_id=state.api.fixture[1])
    elif failure == "scope":
        state.api.orch.tool_permissions.set_agent_scopes(state.api.fixture[1], "web-research-1", {"tools:read": False})
    with pytest.raises(AssignmentError):
        await apply(state, next_body(first, "activate", revision_id=first.revision.revision_id))
    header, _ = await state.service.history(caller=await caller(state), agent_id=first.agent.agent_id)
    assert header == first.agent and counts(state) == (1, 1, 1, 1)


async def test_mutable_body_after_first_await_never_changes_command(declarations, monkeypatch):
    state = declarations
    intent = body()
    original = state.service._privacy

    async def privacy(*args):
        intent.definition["instructions"] = "This was changed after capture."
        intent.agent_id = str(uuid4())
        return await original(*args)

    monkeypatch.setattr(state.service, "_privacy", privacy)
    result = await apply(state, intent)
    assert thaw(result.revision.definition_json) == definition()
    assert result.agent.agent_id != intent.agent_id


async def test_concurrent_identical_command_has_one_receipt_and_audit(declarations):
    state = declarations
    intent = body()
    first, second = await asyncio.gather(apply(state, intent), apply(state, intent))
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert first.receipt == second.receipt and counts(state) == (1, 1, 1, 1)


async def test_receipt_committed_during_failed_preflight_is_returned(declarations, monkeypatch):
    state = declarations
    intent = body()
    original = state.service._privacy
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def privacy(*args):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            raise authoring.DeclarativeAgentError("declarative_privacy_unavailable", 503)
        return await original(*args)

    monkeypatch.setattr(state.service, "_privacy", privacy)
    pending = asyncio.create_task(apply(state, intent))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        winner = await apply(state, intent)
    finally:
        release.set()
    replay = await pending
    assert replay.replayed and replay.receipt == winner.receipt
    assert counts(state) == (1, 1, 1, 1)


async def test_generated_revision_admission_refuses_declarative_before_codegen(declarations):
    from unittest.mock import AsyncMock
    from orchestrator import agent_authoring as aa
    from orchestrator.user_agents import StaleRuntimeGenerationError
    state = declarations
    first = await apply(state)
    draft_id = str(uuid4())
    draft = {"id": draft_id, "user_id": state.api.fixture[1],
        "target_agent_id": first.agent.agent_id, "revises_agent_id": first.agent.agent_id,
        "agent_name": "Executable revision"}
    generator = AsyncMock(side_effect=AssertionError("codegen must not run"))
    state.api.orch.lifecycle_manager = SimpleNamespace(
        draft_store=SimpleNamespace(get_draft_agent=lambda _: draft), generate_code=generator)
    with pytest.raises(StaleRuntimeGenerationError, match="executable"):
        await aa._generate_and_deliver(state.api.orch, user_id=state.api.fixture[1],
            agent_id=first.agent.agent_id, draft_id=draft_id, tool_names=[], declared_scopes=[])
    generator.assert_not_awaited()
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize("entry", ["revise", "start", "one-shot", "assist-existing"])
async def test_guided_and_one_shot_entry_refuse_before_drafting(declarations, entry):
    from unittest.mock import AsyncMock
    from orchestrator import agent_authoring as aa
    from orchestrator.user_agents import PersonalAgentNotFoundError
    state = declarations
    first = await apply(state)
    owner, identity = state.api.fixture[1], first.agent.agent_id
    create = AsyncMock(side_effect=AssertionError("draft creation must not run"))
    llm = AsyncMock(side_effect=AssertionError("drafting model must not run"))
    stored = {"id": str(uuid4()), "origin": "byo_client", "user_id": owner,
        "phase": "specify", "revises_agent_id": identity, "target_agent_id": identity,
        "description": "Describe an existing declaration", "agent_name": "Example"}
    state.api.orch.lifecycle_manager = SimpleNamespace(create_draft=create,
        draft_store=SimpleNamespace(get_owned_draft_agent=lambda *_: stored))
    state.api.orch._call_llm_json = llm
    if entry == "revise":
        assert await aa.revise(state.api.orch, owner, identity) == {"status": "unavailable"}
    elif entry == "assist-existing":
        ok, _ = await aa.draft_phase(state.api.orch, None, owner, stored["id"])
        assert not ok
    else:
        with pytest.raises(PersonalAgentNotFoundError):
            if entry == "start":
                await aa.start_session(state.api.orch, user_id=owner, agent_name="Example",
                    description="Describe an existing declaration", revises_agent_id=identity)
            else:
                await aa.author_and_deliver(state.api.orch, user_id=owner, agent_name="Example",
                    description="Describe an existing declaration", agent_id=identity)
    create.assert_not_awaited()
    llm.assert_not_awaited()
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize("field,value", [
    ("agent_id", "not-an-id"), ("limit", True), ("limit", 0), ("limit", 101),
    ("before_revision_number", True), ("before_revision_number", -1),
    ("before_revision_number", 2**63),
])
async def test_history_rejects_malformed_and_unbounded_cursors(declarations, field, value):
    kwargs = {"agent_id": str(uuid4()), field: value}
    with pytest.raises(AssignmentError) as caught:
        await declarations.service.history(caller=await caller(declarations), **kwargs)
    assert caught.value.status_code == 422 and counts(declarations) == (0, 0, 0, 0)


@pytest.mark.parametrize("failure", ["phi", "analyzer", "control-name"])
async def test_sensitive_or_unscannable_drafts_never_persist(declarations, monkeypatch, failure):
    from personalization.phi_gate import get_phi_gate
    state = declarations
    if failure == "analyzer":
        monkeypatch.setattr(get_phi_gate(), "contains_phi", lambda _: (_ for _ in ()).throw(RuntimeError("private")))
    elif failure == "phi":
        monkeypatch.setattr(get_phi_gate(), "contains_phi", lambda _: True)
    intent = body(display_name="bad\x00title") if failure == "control-name" else body()
    with pytest.raises(AssignmentError) as caught:
        await apply(state, intent)
    assert "private" not in str(caught.value) and counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", ["registry", "context", "audit", "catalog", "flag"])
async def test_replaced_domain_composition_cannot_accept(declarations, monkeypatch, change):
    from shared.feature_flags import flags
    state = declarations
    selected = await caller(state)
    if change == "registry":
        monkeypatch.setattr(state.api.orch, "user_agent_registry", object())
    elif change == "context":
        monkeypatch.setattr(state.service.registry, "_agents", object())
    elif change == "audit":
        monkeypatch.setattr(state.api.orch, "audit_repo", object())
    elif change == "catalog":
        monkeypatch.setattr(state.api.orch.runtime_composition.plane, "repositories", object())
    else:
        monkeypatch.setitem(flags._flags, "byo_agents", False)
    with pytest.raises(AssignmentError):
        await state.service.command(caller=selected, body=body())
    assert counts(state) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", ["config", "key", "runner"])
async def test_activation_cannot_adopt_new_snapshot_after_capture(declarations, monkeypatch, change):
    state = declarations
    first = await apply(state, definition=research_definition(state))
    original = state.service._prepare_activation

    async def capture(*args):
        observed = await original(*args)
        if change == "key":
            monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-replacement-" + "x" * 50)
        elif change == "runner":
            state.api.runner._stopping = True
        else:
            with state.api.runtime.transaction() as tx:
                repo = state.api.runtime.repositories.encrypted_llm_config
                row = repo.get_user(tx, owner_id=state.api.fixture[1])
                repo.upsert_user(tx, owner_id=row.owner_id, provider=row.provider,
                    base_url=row.base_url, model=row.model, api_key_ciphertext=row.api_key_ciphertext)
        return observed

    monkeypatch.setattr(state.service, "_prepare_activation", capture)
    with pytest.raises(AssignmentError):
        await apply(state, next_body(first, "activate", revision_id=first.revision.revision_id))
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize("change", ["kind", "digest", "owner"])
async def test_history_refuses_untrusted_repository_observation(declarations, monkeypatch, change):
    state = declarations
    first = await apply(state)
    row = replace(first.revision, **{
        "kind": {"revision_kind": "executable"},
        "digest": {"definition_digest": "0" * 64},
        "owner": {"owner_id": "another-owner"},
    }[change])
    repo = state.api.runtime.repositories.agents
    monkeypatch.setattr(repo, "list_revisions", lambda *_, **__: (row,))
    with pytest.raises(AssignmentError, match="declarative_unavailable"):
        await state.service.history(caller=await caller(state), agent_id=first.agent.agent_id)


async def test_lost_commit_ack_retries_exact_receipt_without_duplicate_audit(declarations, monkeypatch):
    state = declarations
    original = state.service._transaction
    count = 0

    async def ack(*args, **kwargs):
        nonlocal count
        value = await original(*args, **kwargs)
        count += 1
        if count == 2:
            raise AssignmentError("human_request_unavailable", 503)
        return value

    intent = body()
    with monkeypatch.context() as patch:
        patch.setattr(state.service, "_transaction", ack)
        with pytest.raises(AssignmentError, match="human_request_unavailable"):
            await apply(state, intent)
    assert counts(state) == (1, 1, 1, 1)
    replay = await apply(state, intent)
    assert replay.replayed and replay.revision is None
    assert counts(state) == (1, 1, 1, 1)


async def test_revoke_committed_during_config_wait_prevents_selection(declarations, monkeypatch):
    state = declarations
    first = await apply(state, definition=research_definition(state))
    runtime, owner = state.api.runtime, state.api.fixture[1]
    repo = runtime.repositories.encrypted_llm_config
    original = repo.get_user_for_update
    locked, requesting = threading.Event(), threading.Event()
    shared = {}

    def get_user(tx, *, owner_id):
        shared["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
        requesting.set()
        return original(tx, owner_id=owner_id)

    monkeypatch.setattr(repo, "get_user_for_update", get_user)

    def writer():
        with runtime.transaction() as tx:
            original(tx, owner_id=owner)
            pid = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            locked.set()
            assert requesting.wait(3)
            until = time.monotonic() + .08
            while time.monotonic() < until:
                if tx.fetch_one("SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                        (pid, shared["pid"]))["waiting"]:
                    shared["waiting"] = True
                    break
                time.sleep(.001)
            assert shared.get("waiting"), "actual config row-lock wait was not observed"
            runtime.repositories.tool_policy_state.set_scopes(tx, owner_id=owner,
                agent_id="web-research-1", scopes={"tools:read": False}, updated_at=int(time.time()*1000))

    worker = asyncio.create_task(asyncio.to_thread(writer))
    try:
        assert await asyncio.to_thread(locked.wait, 3)
        with pytest.raises(AssignmentError, match="declarative_permission_refused"):
            await apply(state, next_body(first, "activate", revision_id=first.revision.revision_id))
    finally:
        await worker
    assert shared["waiting"] and counts(state) == (1, 1, 1, 1)


async def test_cancelled_preflight_never_creates_a_command_or_background_retry(declarations, monkeypatch):
    state = declarations
    entered = asyncio.Event()

    async def hold(*_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(state.service, "_privacy", hold)
    task = asyncio.create_task(apply(state))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert counts(state) == (0, 0, 0, 0)


async def test_cancelled_worker_commit_remains_exactly_replayable(declarations, monkeypatch):
    state = declarations
    inserted, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = state.api.service.audit.insert_in_transaction
    intent = body()

    def insert(*args, **kwargs):
        value = original(*args, **kwargs)
        inserted.set()
        try:
            assert release.wait(3)
        finally:
            finished.set()
        return value

    with monkeypatch.context() as patch:
        patch.setattr(state.api.service.audit, "insert_in_transaction", insert)
        task = asyncio.create_task(apply(state, intent))
        assert await asyncio.to_thread(inserted.wait, 3)
        task.cancel()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 3)
    # The authentic worker may have committed despite lost acknowledgement.
    # Exact replay takes the same owner lock, settles that race, and never emits
    # two receipts/audits or performs a second semantic transition.
    await apply(state, intent)
    assert counts(state) == (1, 1, 1, 1)


async def test_typed_body_escape_hatches_remain_closed_and_private(declarations):
    state = declarations
    ordinary = body()
    cases = [None, ordinary.model_dump(),
        ordinary.model_copy(update={"command_id": "secret-not-a-uuid"}),
        ordinary.model_copy(update={"command": "publish"}),
        ordinary.model_copy(update={"expected_revision": 1}),
        ordinary.model_copy(update={"parent_revision_id": str(uuid4())}),
        ordinary.model_copy(update={"source_agent_id": str(uuid4())}),
        ordinary.model_copy(update={"revision_id": None}),
        ordinary.model_copy(update={"display_name": None}),
        ordinary.model_copy(update={"definition": None}),
        ordinary.model_copy(update={"command": "archive"}),
        ordinary.model_copy(update={"command": "clone"}),
    ]
    selected = await caller(state)
    for malformed in cases:
        with pytest.raises(AssignmentError) as caught:
            await state.service.command(caller=selected, body=malformed)
        assert caught.value.status_code == 422
        assert "secret" not in str(caught.value)
    assert counts(state) == (0, 0, 0, 0)


async def test_failed_preflight_without_receipt_preserves_original_refusal(declarations, monkeypatch):
    state = declarations

    async def failed(*_):
        raise authoring.DeclarativeAgentError("declarative_sensitive_content_refused", 422)

    monkeypatch.setattr(state.service, "_privacy", failed)
    with pytest.raises(AssignmentError) as caught:
        await apply(state)
    assert (caught.value.code, caught.value.status_code) == ("declarative_sensitive_content_refused", 422)
    assert counts(state) == (0, 0, 0, 0)
