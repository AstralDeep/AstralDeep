"""Real Plane/PostgreSQL engine recovery with deterministic external boundaries.

The runtime, guarded migrations, action ledger and work admission are production
implementations. Only IAM/model/tool responses are controlled by the fixture.
Each test owns an isolated PostgreSQL schema; no application data is touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralplane.api import create_postgres_runtime
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.repositories.assignment_models import (
    AssignmentControl,
    AssignmentDefinition,
    AssignmentEpisodeCompletion,
)
from orchestrator.work_admission import WorkAdmissionCoordinator
from persistent_agents.config import RunnerConfig
from persistent_agents.dispatch_context import current_dispatch
from persistent_agents.models import AssignmentLimits, ControlRequest
from persistent_agents.runner import AssignmentRunner
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore


class _FixtureReconciler:
    name = "assignment-engine-test"
    version = "1"

    def reconcile(self, context):
        return {"fixture": "isolated-assignment-engine"}


@pytest.fixture
def plane():
    dsn = os.environ.get("ASTRALPLANE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("isolated ASTRALPLANE_TEST_POSTGRES_DSN required")
    import psycopg2
    from psycopg2.extensions import make_dsn
    from psycopg2.sql import SQL, Identifier
    # Schema creation/removal is fixture isolation only; all product tables and
    # changes are exclusively initialized by the public guarded Plane runtime.
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    schema = "engine_079_" + uuid4().hex
    with admin.cursor() as cursor:
        cursor.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    runtime = None
    try:
        runtime = create_postgres_runtime(
            make_dsn(dsn, options=f"-csearch_path={schema},pg_catalog"),
            identity=schema, reconcilers=(_FixtureReconciler(),), maximum_connections=8,
        )
        runtime.initialize(expected_revision=SCHEMA_REVISION)
        assert runtime.health().ready
        yield runtime
    finally:
        if runtime is not None:
            runtime.close()
        with admin.cursor() as cursor:
            cursor.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        admin.close()


class _Host:
    """Instrumented boundary exercising each real persistent dispatch permit."""

    def __init__(self, runtime):
        self.work_admission = WorkAdmissionCoordinator.from_plane(
            plane_runtime=runtime, slot_lease=timedelta(seconds=2))
        self.ui_sessions = {}
        self.tool_permissions = SimpleNamespace(get_tool_scope=lambda *args: "tools:read")
        self.physical_tools = 0
        self.source_urls = []
        self.physical_models = []
        self.model_contexts = []
        self.tool_before_send = None
        self.tool_after_send = None
        self.before_join = None
        self.join_completed = False
        self.join_text = "Version 2 was released."
        self.source_text = "Release version 2 published."

    async def derive_machine_authority(self, **kwargs):
        return SimpleNamespace(user_id=kwargs["user_id"])

    def _bind_machine_turn(self, socket, authority):
        self.ui_sessions[socket] = {"sub": authority.user_id}

    def _unbind_machine_turn(self, socket):
        self.ui_sessions.pop(socket, None)

    async def execute_single_tool(self, socket, call, tool_map, *, chat_id, user_id):
        context = current_dispatch()
        arguments = json.loads(call.function.arguments)
        await context.validate_tool(user_id, tool_map[call.function.name], call.function.name, arguments)
        if self.tool_before_send is not None:
            await self.tool_before_send()
        async def physical():
            self.physical_tools += 1
            self.source_urls.append(arguments["url"])
            if self.tool_after_send is not None:
                await self.tool_after_send()
            return SimpleNamespace(error=None, result={"_data": {"version": self.source_text}},
                                   ui_components=[{"type": "text", "text": self.source_text}])
        return await context.invoke_tool(physical)

    async def _call_llm(self, socket, messages, **kwargs):
        context = current_dispatch()
        system = messages[0]["content"]
        if system.startswith("You plan"):
            label = "plan"
            value = {"tasks": [{"id": "first", "instruction": "Identify the release.", "tools": [], "depends_on": []},
                               {"id": "second", "instruction": "Check the implications.", "tools": [], "depends_on": ["first"]}]}
        elif system.startswith("Perform"):
            label = "child"
            value = {"kind": "result", "text": "The release was verified."}
        else:
            label = "join"
            value = {"kind": "result", "text": self.join_text, "completed": self.join_completed}
            if self.before_join is not None:
                await self.before_join()
        async def physical():
            self.physical_models.append(label)
            self.model_contexts.append((label, json.loads(messages[1]["content"])))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],
                                   usage=SimpleNamespace(total_tokens=100))
        response = await context.invoke_model(physical, {"messages": messages})
        return response.choices[0].message, None


SOURCE_URL = "https://example.org/releases"
# A prior complete observation seeded through the public lifecycle API. Its
# digests name content the fixture host never serves, so the first governed
# read of a seeded assignment is a meaningful change assessed by the model.
PRIOR_REVISION = digest("fixture-prior-revision")
PRIOR_RESULT = digest("fixture-prior-result")


def prior_observation_record(definition):
    return {"version": 1, "kind": "initial", "reason": None, "observation_sequence": 1,
            "revision_digest": PRIOR_REVISION, "context_digest": PRIOR_RESULT,
            "prior_revision_digest": None, "prior_result_digest": None,
            "complete_source_set": [SOURCE_URL], "normalized_final_urls": [],
            "completeness": {"body_complete": None, "extraction_complete": None, "excerpt_complete": True},
            "source_configuration_digest": digest(thaw(definition.source))}


def _engine(plane, monkeypatch, *, seed_prior):
    from persistent_agents import execution
    from persistent_agents import runner as runner_module
    monkeypatch.setattr(execution, "safe_text", AsyncMock())
    monkeypatch.setattr(runner_module, "safe_text", AsyncMock())
    host = _Host(plane)
    store = AssignmentStore(plane_runtime=plane)
    service = AssignmentService(host, store=store, enabled=True, phi_gate=SimpleNamespace())
    service.validate_execution = AsyncMock(return_value={"permission_digest": digest("permission"), "precondition_digest": digest("precondition")})
    runner = AssignmentRunner(host, service, config=RunnerConfig(lease_seconds=5))
    grant = str(uuid4())
    assignment_id = str(uuid4())
    limits = AssignmentLimits(cadence_seconds=60).to_plane()
    now = int(datetime.now(UTC).timestamp() * 1000)
    with plane.transaction() as tx:
        plane.repositories.offline_grants.create_grant(tx, grant_id=grant, owner_id="owner", agent_id=None,
            encrypted_refresh_token=b"fixture-opaque-encrypted-token", issued_at=now, expires_at=now + 3600000)
        definition = AssignmentDefinition(name="Release monitor", instructions="Investigate meaningful release changes.",
            source={"profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page", "arguments": {"url": SOURCE_URL}, "linked_document_urls": []},
            allowed_tools=("web-research-1:fetch_page",), consented_scopes=("tools:read",),
            offline_grant_id=grant, limits=limits)
        plane.repositories.assignments.create_assignment(tx, owner_id="owner", assignment_id=assignment_id,
            submission_id=str(uuid4()), submission_digest=digest("create"), definition=definition)
        if seed_prior:
            [claim] = plane.repositories.assignments.claim_due_for_administration(
                tx, worker_id="prior-fixture", limit=1, lease_seconds=5)
            checkpoint = {**thaw(claim.assignment.checkpoint),
                          "observation": prior_observation_record(definition)}
            plane.repositories.assignments.finish_episode(tx, fence=claim.fence, completion=AssignmentEpisodeCompletion(
                expected_state_version=claim.assignment.state_version, checkpoint=checkpoint,
                completion_digest=digest(["prior-fixture", assignment_id]),
                next_wake_at=datetime.now(UTC) - timedelta(minutes=1)))
    yield host, runner, store, assignment_id
    store.close()


@pytest.fixture
def engine(plane, monkeypatch):
    """An assignment with one seeded prior observation: its first read is a change."""
    yield from _engine(plane, monkeypatch, seed_prior=True)


@pytest.fixture
def fresh_engine(plane, monkeypatch):
    """An assignment that has never observed its source."""
    yield from _engine(plane, monkeypatch, seed_prior=False)


async def claim_and_run(runner, store):
    claims = await store.call("claim_due_for_administration", worker_id=runner.worker_id, lease_seconds=5)
    assert len(claims) == 1
    await runner.run_claim(claims[0])
    return claims[0]


async def current(store, identity):
    return await store.call("get_assignment", owner_id="owner", assignment_id=identity)


async def control(store, identity, command):
    record = await current(store, identity)
    return await store.call("apply_control", owner_id="owner", assignment_id=identity,
        expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
        submission_id=str(uuid4()), submission_digest=digest([command, record.control_epoch]), control=AssignmentControl(command))


def test_source_plan_children_join_and_unchanged_check(engine):
    host, runner, store, identity = engine
    async def scenario():
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert record.checkpoint["last_finding"] == "Version 2 was released."
        assert [task["state"] for task in record.tasks] == ["completed", "completed"]
        assert all(task["incorporated_by"].get("__assignment__") == task["result_digest"] for task in record.tasks)
        assert host.physical_tools == 1
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert record.usage["spent"]["tool_calls"] == 1
        assert record.usage["spent"]["model_calls"] == 4
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        models = [action for action in actions if action.intent.request["kind"] == "model"]
        assert len(models) == 4
        assert all(action.intent.request["max_output_tokens"] == 4096 for action in models)
        assert all(action.intent.maximum.tokens > 4096 for action in models)
        before_activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
        await control(store, identity, "pause")
        await control(store, identity, "resume")
        await claim_and_run(runner, store)
        unchanged = await current(store, identity)
        assert unchanged.phase == "waiting", unchanged.safe_error_code
        assert host.physical_tools == 2
        assert host.physical_models == ["plan", "child", "child", "join"]
        activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
        assert sum(item.activity_type == "finding" for item in activity) == sum(item.activity_type == "finding" for item in before_activity) == 1
    asyncio.run(scenario())


def test_stop_committed_between_gate_and_physical_send_denies_effect(engine):
    host, runner, store, identity = engine
    async def scenario():
        host.tool_before_send = lambda: control(store, identity, "stop")
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.lifecycle == "stopped"
        assert host.physical_tools == 0
        assert host.physical_models == []
        assert all(amount == 0 for amount in record.usage["outstanding"].values())
    asyncio.run(scenario())


def test_completion_allowance_cannot_exceed_existing_owner_token_budget(engine):
    host, runner, store, identity = engine
    async def scenario():
        initial = await current(store, identity)
        limits = {**initial.definition.limits, "tokens": 4096, "daily_tokens": 4096}
        await store.call("apply_control", owner_id="owner", assignment_id=identity,
            expected_instruction_revision=initial.instruction_revision, expected_control_epoch=initial.control_epoch,
            submission_id=str(uuid4()), submission_digest=digest("limited-owner-token-budget"),
            control=AssignmentControl.REVISE, replacement=replace(initial.definition, limits=limits))
        await claim_and_run(runner, store)
        stopped = await current(store, identity)
        assert stopped.phase == "budget_exhausted", stopped.safe_error_code
        assert host.physical_tools == 1 and host.physical_models == []
        assert stopped.definition.limits == limits
        assert stopped.definition.offline_grant_id == initial.definition.offline_grant_id
        assert stopped.usage["spent"]["tokens"] == stopped.usage["spent"]["model_calls"] == 0
        assert all(amount == 0 for amount in stopped.usage["outstanding"].values())
    asyncio.run(scenario())


def test_configured_model_after_pause_resume_reuses_source_and_replaces_only_unstarted_intent(engine):
    host, runner, store, identity = engine
    configured_model = host._call_llm
    async def current_authorization(owner, claims, record, action):
        return {"permission_digest": digest(["permission", record.control_epoch]),
                "precondition_digest": digest("precondition")}
    runner.service.validate_execution = AsyncMock(side_effect=current_authorization)
    host._call_llm = AsyncMock(return_value=(None, None))
    async def actions():
        return await store.call("list_actions", owner_id="owner", assignment_id=identity)
    async def scenario():
        initial = await current(store, identity)
        await claim_and_run(runner, store)
        failed = await current(store, identity)
        assert failed.phase == "failed", failed.safe_error_code
        assert failed.safe_error_code == "assignment_model_unconfigured"
        before = await actions()
        source = next(action for action in before if action.intent.request["kind"] == "tool")
        planner = next(action for action in before if action.intent.request["kind"] == "model")
        assert source.state == "succeeded" and source.ever_started
        assert planner.state == "failed_not_started" and not planner.ever_started and planner.result is None
        assert host.physical_tools == 1 and host.physical_models == []
        await control(store, identity, "pause")
        await control(store, identity, "resume")
        host._call_llm = configured_model
        restarted = AssignmentRunner(host, runner.service, config=RunnerConfig(lease_seconds=5))
        await claim_and_run(restarted, store)
        resumed = await current(store, identity)
        assert resumed.phase == "waiting", resumed.safe_error_code
        assert resumed.checkpoint["last_finding"] == "Version 2 was released."
        assert host.physical_tools == 1
        assert host.physical_models == ["plan", "child", "child", "join"]
        after = await actions()
        assert next(action for action in after if action.action_id == source.action_id) == source
        assert next(action for action in after if action.action_id == planner.action_id) == planner
        successor = next(action for action in after if action.intent.action_key ==
            digest([planner.intent.action_key, "successor", planner.control_epoch]))
        assert successor.state == "succeeded" and successor.control_epoch == resumed.control_epoch
        assert successor.intent.request_digest == planner.intent.request_digest
        assert successor.intent.permission_digest != planner.intent.permission_digest
        assert successor.intent.maximum == planner.intent.maximum
        assert resumed.definition.offline_grant_id == initial.definition.offline_grant_id
        assert resumed.definition.limits == initial.definition.limits
        assert resumed.usage["spent"]["tool_calls"] == 1 and resumed.usage["spent"]["model_calls"] == 4
    asyncio.run(scenario())


def test_restart_reuses_completed_children_and_source_actions(engine):
    host, runner, store, identity = engine
    async def scenario():
        at_join = asyncio.Event()
        async def interrupt_at_join():
            at_join.set()
            await asyncio.Event().wait()
        host.before_join = interrupt_at_join
        task = asyncio.create_task(claim_and_run(runner, store))
        await asyncio.wait_for(at_join.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        retained = await current(store, identity)
        assert all(child["state"] == "completed" for child in retained.tasks)
        await asyncio.sleep(5.1)
        await asyncio.to_thread(host.work_admission.expire_execution_leases)
        await store.call("recover_expired_for_administration", limit=10)
        recovered = await current(store, identity)
        assert recovered.phase == "failed"
        # Respect the production retry backoff instead of changing database
        # clocks or writing scheduler state outside the public repository.
        delay = (recovered.next_wake_at - datetime.now(UTC)).total_seconds()
        assert 0 < delay <= 60
        await asyncio.sleep(delay + 0.1)
        host.before_join = None
        replacement = AssignmentRunner(host, runner.service, config=RunnerConfig(lease_seconds=5))
        await claim_and_run(replacement, store)
        finished = await current(store, identity)
        assert finished.phase == "waiting", finished.safe_error_code
        assert host.physical_tools == 1
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert all(child["incorporated_by"] for child in finished.tasks)
    asyncio.run(scenario())


def test_pause_before_join_then_resume_preserves_completed_work(engine):
    host, runner, store, identity = engine
    async def scenario():
        host.before_join = lambda: control(store, identity, "pause")
        await claim_and_run(runner, store)
        paused = await current(store, identity)
        assert paused.lifecycle == "paused"
        assert all(child["state"] == "completed" for child in paused.tasks)
        host.before_join = None
        await control(store, identity, "resume")
        await claim_and_run(runner, store)
        resumed = await current(store, identity)
        assert resumed.phase == "waiting", resumed.safe_error_code
        assert host.physical_tools == 1
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert all(child["incorporated_by"] for child in resumed.tasks)
    asyncio.run(scenario())


def test_heartbeat_between_snapshot_and_finish_does_not_lose_completed_join(engine, monkeypatch):
    host, runner, store, identity = engine
    original = runner._finish
    renewed = False
    async def interleaved(executor, record, **kwargs):
        nonlocal renewed
        if not renewed:
            renewed = True
            await store.call("renew_claim", fence=executor.claim.fence, lease_seconds=5)
        return await original(executor, record, **kwargs)
    monkeypatch.setattr(runner, "_finish", interleaved)
    async def scenario():
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert record.checkpoint["last_finding"] == "Version 2 was released."
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert all(child["incorporated_by"] for child in record.tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize("uncertain", [False, True])
def test_terminal_checkpoint_survives_renewal_during_notification(engine, monkeypatch, uncertain):
    host, runner, store, identity = engine

    async def scenario():
        committed = asyncio.Event()
        release_notification = asyncio.Event()
        renewal_finished = asyncio.Event()
        original_renew = runner._renew
        original_notify = runner._notify_activity
        host.notify_user = AsyncMock()

        async def observed_renew(executor, episode):
            try:
                await original_renew(executor, episode)
            finally:
                renewal_finished.set()

        async def delayed_notification(record):
            committed.set()
            await release_notification.wait()
            await original_notify(record)

        async def uncertain_model(socket, messages, **kwargs):
            async def failed_transport():
                raise ConnectionError("private provider diagnostic")
            return await current_dispatch().invoke_model(failed_transport, {"messages": messages})

        monkeypatch.setattr(runner, "_renew", observed_renew)
        monkeypatch.setattr(runner, "_notify_activity", delayed_notification)
        if uncertain:
            monkeypatch.setattr(host, "_call_llm", uncertain_model)
        task = asyncio.create_task(claim_and_run(runner, store))
        try:
            await asyncio.wait_for(committed.wait(), timeout=10)
            record = await current(store, identity)
            assert record.phase == ("reconciliation" if uncertain else "waiting")
            actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
            if uncertain:
                assert any(action.state == "uncertain" for action in actions)
            await asyncio.wait_for(renewal_finished.wait(), timeout=10)
            assert not task.done(), "a committed checkpoint must survive its retired renewal"
            release_notification.set()
            await asyncio.wait_for(task, timeout=10)
            host.notify_user.assert_awaited_once()
            assert (await current(store, identity)).phase == record.phase
            assert not host.ui_sessions
        finally:
            release_notification.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_lost_claim_before_terminal_commit_cancels_episode(engine, monkeypatch):
    host, runner, store, identity = engine

    async def scenario():
        entered = asyncio.Event()
        release_work = asyncio.Event()
        effects = []

        async def blocked_episode(executor):
            entered.set()
            await release_work.wait()
            effects.append("must not execute after lease loss")

        monkeypatch.setattr(runner, "episode", blocked_episode)
        task = asyncio.create_task(claim_and_run(runner, store))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            await control(store, identity, "pause")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            assert task.cancelled() and effects == []
            assert (await current(store, identity)).lifecycle == "paused"
            assert await store.call("list_actions", owner_id="owner", assignment_id=identity) == ()
            assert not host.ui_sessions
        finally:
            release_work.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_source_revision_does_not_investigate_old_pending_source_as_new_assignment(engine):
    host, runner, store, identity = engine
    async def scenario():
        async def revise_source():
            record = await current(store, identity)
            replacement = replace(record.definition,
                instructions="Investigate only the new project's releases.",
                source={"profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page",
                        "arguments": {"url": "https://example.org/another-project"}, "linked_document_urls": []})
            await store.call("apply_control", owner_id="owner", assignment_id=identity,
                expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
                submission_id=str(uuid4()), submission_digest=digest("new-source"),
                control=AssignmentControl.REVISE, replacement=replacement)
        host.before_join = revise_source
        await claim_and_run(runner, store)
        host.before_join = None
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert host.source_urls == ["https://example.org/releases", "https://example.org/another-project"]
        pending = await store.call("list_events", owner_id="owner", assignment_id=identity, disposition="pending")
        assert not pending
    asyncio.run(scenario())


def test_already_started_effect_gets_durable_outcome_after_stop_without_followup(engine):
    host, runner, store, identity = engine
    stopped = []
    async def scenario():
        async def stop_in_flight():
            stopped.append(await control(store, identity, "stop"))
        host.tool_after_send = stop_in_flight
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.lifecycle == "stopped"
        assert host.physical_tools == 1
        assert host.physical_models == []
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert len(actions) == 1 and actions[0].state == "succeeded"
        assert actions[0].action_id in stopped[0].begun_action_ids
        assert record.usage["spent"]["tool_calls"] == 1
        assert all(amount == 0 for amount in record.usage["outstanding"].values())
    asyncio.run(scenario())


def test_explicit_completion_condition_stops_future_checks(engine):
    host, runner, store, identity = engine
    async def scenario():
        record = await current(store, identity)
        await store.call("apply_control", owner_id="owner", assignment_id=identity,
            expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
            submission_id=str(uuid4()), submission_digest=digest("completion-condition"),
            control=AssignmentControl.REVISE,
            replacement=replace(record.definition, completion_condition="Complete after the first new version is verified."))
        host.join_completed = True
        await claim_and_run(runner, store)
        completed = await current(store, identity)
        assert completed.lifecycle == "completed", completed.safe_error_code
        assert completed.next_wake_at is None
        assert not await store.call("claim_due_for_administration", worker_id=runner.worker_id, lease_seconds=5)
        assert all(task["incorporated_by"] for task in completed.tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize("zero_cost", [False, True])
def test_trusted_currency_coverage_meters_real_action_reservations(engine, zero_cost):
    host, runner, store, identity = engine
    async def scenario():
        record = await current(store, identity)
        rate = 0 if zero_cost else 1
        cap = 0 if zero_cost else 1_000_000
        coverage = {"currency": "USD", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "model_call_micro_units": rate, "model_token_micro_units": rate,
                    "tool_call_micro_units": {"web-research-1:fetch_page": rate}}
        coverage["quote_digest"] = digest(coverage)
        limits = dict(record.definition.limits)
        limits.update(currency="USD", spend_micro_units=cap, daily_spend_micro_units=cap)
        await store.call("apply_control", owner_id="owner", assignment_id=identity,
            expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
            submission_id=str(uuid4()), submission_digest=digest("quoted-cap"), control=AssignmentControl.REVISE,
            replacement=replace(record.definition, limits=limits, cost_quote_coverage=coverage))
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert host.physical_tools == 1
        spent = record.usage["spent"]["spend_micro_units"]
        assert spent == 0 if zero_cost else spent > 0
        assert record.usage["spent"]["spend_micro_units"] <= cap
    asyncio.run(scenario())


def test_restart_after_source_receipt_before_source_batch_never_repeats_read(engine, monkeypatch):
    host, runner, store, identity = engine
    original = store.call
    async def scenario():
        at_batch = asyncio.Event()
        async def before_batch(method, **kwargs):
            if method == "record_source_batch":
                at_batch.set()
                await asyncio.Event().wait()
            return await original(method, **kwargs)
        monkeypatch.setattr(store, "call", before_batch)
        task = asyncio.create_task(claim_and_run(runner, store))
        await asyncio.wait_for(at_batch.wait(), timeout=10)
        assert host.physical_tools == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(store, "call", original)
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert len(actions) == 1 and actions[0].state == "succeeded"
        assert not await store.call("list_events", owner_id="owner", assignment_id=identity, disposition="pending")
        await asyncio.sleep(5.1)
        await asyncio.to_thread(host.work_admission.expire_execution_leases)
        await store.call("recover_expired_for_administration", limit=10)
        recovered = await current(store, identity)
        assert recovered.phase == "failed"
        delay = (recovered.next_wake_at - datetime.now(UTC)).total_seconds()
        assert 0 < delay <= 60
        await asyncio.sleep(delay + 0.1)
        replacement = AssignmentRunner(host, runner.service, config=RunnerConfig(lease_seconds=5))
        await claim_and_run(replacement, store)
        finished = await current(store, identity)
        assert finished.phase == "waiting", finished.safe_error_code
        assert host.physical_tools == 1
        assert host.physical_models == ["plan", "child", "child", "join"]
    asyncio.run(scenario())


def test_twenty_five_idle_assignments_use_no_model_and_controls_stay_responsive(engine, record_property):
    host, runner, store, identity = engine
    async def scenario():
        first = await current(store, identity)
        for index in range(24):
            await store.call("create_assignment", owner_id="owner", assignment_id=str(uuid4()),
                submission_id=str(uuid4()), submission_digest=digest(["idle", index]), definition=first.definition)
        # Seed valid idle checkpoints through the public lifecycle API. No
        # direct task/lease/clock SQL and no runner or admission mock is used.
        claims = await store.call("claim_due_for_administration", worker_id="idle-fixture", limit=25, lease_seconds=5)
        assert len(claims) == 25
        for claim in claims:
            await store.call("finish_episode", fence=claim.fence, completion=AssignmentEpisodeCompletion(
                expected_state_version=claim.assignment.state_version,
                checkpoint=thaw(claim.assignment.checkpoint), completion_digest=digest(["idle", claim.assignment.assignment_id]),
                next_wake_at=datetime.now(UTC) + timedelta(hours=1)))
        for _ in range(3):
            await runner.tick()
        assert not runner._active
        assert host.physical_models == [] and host.physical_tools == 0
        records = await store.call("list_assignments", owner_id="owner", limit=50)
        assert len(records) == 25 and all(row.phase == "waiting" for row in records)
        times = []
        for row in records:
            started = time.perf_counter()
            await runner.service.control("owner", {"sub": "owner"}, row.assignment_id, "pause", ControlRequest(
                submission_id=str(uuid4()), expected_instruction_revision=row.instruction_revision,
                expected_control_epoch=row.control_epoch))
            times.append(time.perf_counter() - started)
        assert max(times) < 2.0
        maximum_ms = round(max(times) * 1000, 2)
        record_property("idle_assignment_count", 25)
        record_property("idle_model_calls", 0)
        record_property("max_control_latency_ms", maximum_ms)
        print(f"25 idle assignments, zero model calls, maximum owner-control latency {maximum_ms} ms")
    asyncio.run(scenario())


def test_irrelevant_source_change_keeps_last_meaningful_finding(engine):
    host, runner, store, identity = engine
    async def scenario():
        await claim_and_run(runner, store)
        host.source_text = "Release version 2 published. Minor page spelling correction."
        host.join_text = "UNCHANGED"
        await control(store, identity, "pause")
        await control(store, identity, "resume")
        await claim_and_run(runner, store)
        record = await current(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert record.checkpoint["last_finding"] == "Version 2 was released."
        activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
        assert sum(item.activity_type == "finding" for item in activity) == 1
        assert host.physical_tools == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("initial_finding", ["Version 2 was released.", "UNCHANGED"])
def test_replacement_runner_delivers_committed_memory_to_every_model(engine, plane, initial_finding):
    host, runner, store, identity = engine
    async def scenario():
        host.join_text = initial_finding
        await claim_and_run(runner, store)
        baseline = await current(store, identity)
        assert baseline.phase == "waiting", baseline.safe_error_code
        observation = thaw(baseline.checkpoint["last_observation"])
        assert observation["text"] == host.source_text
        prior_finding = None if initial_finding == "UNCHANGED" else initial_finding
        assert baseline.checkpoint.get("last_finding") == prior_finding
        assert all(context["prior_observation"] is None and context["prior_finding"] is None
                   for _, context in host.model_contexts)
        await control(store, identity, "pause")
        await control(store, identity, "resume")

        # A new host/service/runner has no memory of prior provider calls. Its
        # actual dispatched prompts must reconstruct evidence from PostgreSQL.
        replacement_host = await asyncio.to_thread(_Host, plane)
        replacement_host.source_text = "Release version 2 published. Minor navigation spelling correction."
        replacement_host.join_text = "UNCHANGED"
        replacement_service = AssignmentService(replacement_host, store=store, enabled=True,
                                                 phi_gate=SimpleNamespace())
        replacement_service.validate_execution = AsyncMock(return_value={
            "permission_digest": digest("permission"), "precondition_digest": digest("precondition")})
        replacement = AssignmentRunner(replacement_host, replacement_service,
                                       config=RunnerConfig(lease_seconds=5))
        await claim_and_run(replacement, store)
        assert replacement_host.physical_models == ["plan", "child", "child", "join"]
        for _, context in replacement_host.model_contexts:
            assert context["prior_observation"] == {
                key: value for key, value in observation.items() if key != "revision_digest"}
            assert "revision_digest" not in context["prior_observation"]
            assert context["prior_finding"] == prior_finding
        finished = await current(store, identity)
        assert finished.phase == "waiting", finished.safe_error_code
        assert finished.checkpoint["last_observation"]["text"] == replacement_host.source_text
        assert finished.checkpoint.get("last_finding") == prior_finding
        activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
        assert sum(item.activity_type == "finding" for item in activity) == (initial_finding != "UNCHANGED")
    asyncio.run(scenario())


# --- feature 088 T042: typed monitoring outcomes (FR-013, FR-014) -----------


async def current_thawed(store, identity):
    """The current record with its checkpoint as plain JSON values."""
    return SimpleNamespace(**thaw(await current(store, identity)))


async def _findings(store, identity):
    activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
    return [item for item in activity if item.activity_type == "finding"]


async def _rewrite_checkpoint(store, identity, checkpoint):
    """Replace the retained checkpoint through the public lifecycle API only."""
    await control(store, identity, "pause")
    await control(store, identity, "resume")
    [claim] = await store.call("claim_due_for_administration", worker_id="checkpoint-fixture",
                               limit=1, lease_seconds=5)
    assert claim.assignment.assignment_id == identity
    await store.call("finish_episode", fence=claim.fence, completion=AssignmentEpisodeCompletion(
        expected_state_version=claim.assignment.state_version, checkpoint=checkpoint,
        completion_digest=digest(["checkpoint-fixture", identity, checkpoint]),
        wake_reason="checkpoint_fixture", next_wake_at=datetime.now(UTC) - timedelta(minutes=1)))


async def _pause_resume(store, identity):
    await control(store, identity, "pause")
    await control(store, identity, "resume")


def test_initial_observation_is_extractive_then_unchanged_then_changed(fresh_engine):
    host, runner, store, identity = fresh_engine
    async def scenario():
        await claim_and_run(runner, store)
        initial = await current_thawed(store, identity)
        assert initial.phase == "waiting", initial.safe_error_code
        assert host.physical_tools == 1 and host.physical_models == []
        assert initial.usage["spent"].get("model_calls", 0) == 0
        observation = initial.checkpoint["observation"]
        retained = initial.checkpoint["last_observation"]
        assert observation["kind"] == "initial" and observation["observation_sequence"] == 1
        assert observation["prior_result_digest"] is None and observation["prior_revision_digest"] is None
        assert observation["revision_digest"] == retained["revision_digest"]
        assert observation["context_digest"] == digest(retained)
        assert observation["complete_source_set"] == [SOURCE_URL]
        assert observation["completeness"] == {"body_complete": None, "extraction_complete": None,
                                               "excerpt_complete": True}
        assert "text" not in observation and host.source_text not in json.dumps(observation)
        assert initial.checkpoint["last_finding"] == host.source_text
        result = initial.checkpoint["extractive_result"]
        assert result["scope"] == "one_page_excerpts" and result["disposition"] == "evidence"
        assert result["passages"] == [{"id": "p001", "text": host.source_text}]
        [read] = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert result["source"]["action_id"] == read.action_id
        assert result["source"]["result_digest"] == read.result["result_digest"]
        assert result["source"]["revision_digest"] == retained["revision_digest"]
        findings = await _findings(store, identity)
        assert len(findings) == 1 and findings[0].summary == host.source_text
        assert findings[0].references["scope"] == "one_page_excerpts"
        assert not await store.call("list_events", owner_id="owner", assignment_id=identity, disposition="pending")

        await _pause_resume(store, identity)
        await claim_and_run(runner, store)
        unchanged = await current_thawed(store, identity)
        assert unchanged.phase == "waiting", unchanged.safe_error_code
        assert host.physical_tools == 2 and host.physical_models == []
        assert unchanged.usage["spent"].get("model_calls", 0) == 0
        assert unchanged.usage["spent"]["tool_calls"] == 2
        again = unchanged.checkpoint["observation"]
        assert again["kind"] == "unchanged" and again["observation_sequence"] == 1
        assert again["revision_digest"] == observation["revision_digest"]
        assert again["prior_result_digest"] == observation["context_digest"]
        assert unchanged.checkpoint["last_observation"] == retained
        assert unchanged.checkpoint["last_finding"] == host.source_text
        assert len(await _findings(store, identity)) == 1

        host.source_text = "Release version 3 published."
        await _pause_resume(store, identity)
        await claim_and_run(runner, store)
        changed = await current_thawed(store, identity)
        assert changed.phase == "waiting", changed.safe_error_code
        assert host.physical_tools == 3 and host.physical_models == ["plan", "child", "child", "join"]
        third = changed.checkpoint["observation"]
        assert third["kind"] == "changed" and third["observation_sequence"] == 2
        assert third["prior_revision_digest"] == observation["revision_digest"]
        assert third["prior_result_digest"] == observation["context_digest"]
        assert third["revision_digest"] == changed.checkpoint["last_observation"]["revision_digest"]
        assert third["revision_digest"] != observation["revision_digest"]
        [plan] = [context for label, context in host.model_contexts if label == "plan"]
        assert plan["prior_result_digest"] == observation["context_digest"]
        assert plan["prior_finding"] == "Release version 2 published."
        assert plan["prior_observation"] == {key: value for key, value in retained.items() if key != "revision_digest"}
        assert changed.checkpoint["last_finding"] == "Version 2 was released."
        assert len(await _findings(store, identity)) == 2
    asyncio.run(scenario())


def test_unchanged_observation_spends_no_model_allowance(engine):
    host, runner, store, identity = engine
    async def scenario():
        await claim_and_run(runner, store)
        first = await current_thawed(store, identity)
        assert first.phase == "waiting", first.safe_error_code
        assessed = first.checkpoint["observation"]
        assert assessed["kind"] == "changed" and assessed["observation_sequence"] == 2
        assert assessed["prior_revision_digest"] == PRIOR_REVISION
        assert assessed["prior_result_digest"] == PRIOR_RESULT
        [plan] = [context for label, context in host.model_contexts if label == "plan"]
        assert plan["prior_result_digest"] == PRIOR_RESULT
        assert first.usage["spent"]["model_calls"] == 4 and first.usage["spent"]["tool_calls"] == 1
        findings = len(await _findings(store, identity))
        await _pause_resume(store, identity)
        await claim_and_run(runner, store)
        second = await current_thawed(store, identity)
        assert second.phase == "waiting", second.safe_error_code
        assert host.physical_tools == 2 and host.physical_models == ["plan", "child", "child", "join"]
        assert second.usage["spent"]["model_calls"] == 4 and second.usage["spent"]["tool_calls"] == 2
        assert all(amount == 0 for amount in second.usage["outstanding"].values())
        unchanged = second.checkpoint["observation"]
        assert unchanged["kind"] == "unchanged" and unchanged["observation_sequence"] == 2
        assert unchanged["revision_digest"] == assessed["revision_digest"]
        assert unchanged["prior_result_digest"] == assessed["context_digest"]
        assert unchanged["prior_revision_digest"] == assessed["revision_digest"]
        assert second.checkpoint["last_observation"] == first.checkpoint["last_observation"]
        assert second.checkpoint["last_finding"] == first.checkpoint["last_finding"]
        assert len(await _findings(store, identity)) == findings
        assert not await store.call("list_events", owner_id="owner", assignment_id=identity, disposition="pending")
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert sum(action.intent.request["kind"] == "model" for action in actions) == 4
    asyncio.run(scenario())


def test_missing_prior_result_binding_is_insufficient_evidence_without_plan(fresh_engine):
    host, runner, store, identity = fresh_engine
    async def scenario():
        await claim_and_run(runner, store)
        initial = await current_thawed(store, identity)
        assert initial.checkpoint["observation"]["kind"] == "initial"
        # A legacy checkpoint whose cursor survived but whose retained bytes did not.
        legacy = {"schema_version": 1, "cursor": initial.checkpoint["cursor"],
                  "last_batch_key": initial.checkpoint["last_batch_key"],
                  "source_configuration_digest": initial.checkpoint["source_configuration_digest"],
                  "last_finding": initial.checkpoint["last_finding"]}
        await _rewrite_checkpoint(store, identity, legacy)
        await claim_and_run(runner, store)
        record = await current_thawed(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert host.physical_tools == 2 and host.physical_models == []
        assert record.usage["spent"].get("model_calls", 0) == 0
        observation = record.checkpoint["observation"]
        assert observation["kind"] == "insufficient_evidence"
        assert observation["reason"] == "prior_result_missing"
        assert observation["prior_revision_digest"] == initial.checkpoint["cursor"]["revision"]
        assert observation["prior_result_digest"] is None
        assert observation["observation_sequence"] == 1
        assert "last_observation" not in record.checkpoint
        assert record.checkpoint["last_finding"] == initial.checkpoint["last_finding"]
        assert record.checkpoint["cursor"] == initial.checkpoint["cursor"]
        assert not await store.call("list_events", owner_id="owner", assignment_id=identity, disposition="pending")
        assert len(await _findings(store, identity)) == 1
        # Honest recovery: with no comparable prior the next read starts over
        # as an initial extractive observation, never an invented comparison.
        host.source_text = "Release version 4 published."
        await _pause_resume(store, identity)
        await claim_and_run(runner, store)
        restarted = await current_thawed(store, identity)
        assert restarted.phase == "waiting", restarted.safe_error_code
        assert host.physical_models == []
        assert restarted.checkpoint["observation"]["kind"] == "initial"
        assert restarted.checkpoint["last_finding"] == "Release version 4 published."
    asyncio.run(scenario())


def test_legacy_checkpoint_upgrade_keeps_unchanged_and_changed_detection(fresh_engine):
    host, runner, store, identity = fresh_engine
    async def scenario():
        await claim_and_run(runner, store)
        initial = await current_thawed(store, identity)
        retained = initial.checkpoint["last_observation"]
        legacy = {key: value for key, value in thaw(initial.checkpoint).items()
                  if key not in {"observation", "extractive_result"}}
        await _rewrite_checkpoint(store, identity, legacy)
        assert "observation" not in (await current_thawed(store, identity)).checkpoint
        await claim_and_run(runner, store)
        unchanged = await current_thawed(store, identity)
        assert unchanged.phase == "waiting", unchanged.safe_error_code
        assert host.physical_tools == 2 and host.physical_models == []
        observation = unchanged.checkpoint["observation"]
        assert observation["kind"] == "unchanged" and observation["observation_sequence"] == 1
        assert observation["prior_revision_digest"] == retained["revision_digest"]
        assert observation["prior_result_digest"] == digest(retained)
        host.source_text = "Release version 3 published."
        await _pause_resume(store, identity)
        await claim_and_run(runner, store)
        changed = await current_thawed(store, identity)
        assert changed.phase == "waiting", changed.safe_error_code
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert changed.checkpoint["observation"]["kind"] == "changed"
        assert changed.checkpoint["observation"]["observation_sequence"] == 2
        assert changed.checkpoint["observation"]["prior_result_digest"] == digest(retained)
        [plan] = [context for label, context in host.model_contexts if label == "plan"]
        assert plan["prior_result_digest"] == digest(retained)
        assert plan["prior_observation"] == {key: value for key, value in retained.items() if key != "revision_digest"}
    asyncio.run(scenario())


def test_checkpoint_never_retains_discarded_source_text(engine):
    host, runner, store, identity = engine
    async def scenario():
        marker = "DISCARDED-TAIL-MARKER-" + uuid4().hex
        host.source_text = "Release notes line. " * 300 + marker
        await claim_and_run(runner, store)
        record = await current_thawed(store, identity)
        assert record.phase == "waiting", record.safe_error_code
        assert host.physical_models == ["plan", "child", "child", "join"]
        assert marker not in json.dumps(thaw(record.checkpoint))
        retained = record.checkpoint["last_observation"]
        assert retained["truncated"] is True and len(retained["text"]) == 4096
        observation = record.checkpoint["observation"]
        assert observation["kind"] == "changed"
        assert observation["completeness"]["excerpt_complete"] is False
        assert observation["complete_source_set"] == [SOURCE_URL]
        assert set(observation) == {"version", "kind", "reason", "observation_sequence", "revision_digest",
                                    "context_digest", "prior_revision_digest", "prior_result_digest",
                                    "complete_source_set", "normalized_final_urls", "completeness",
                                    "source_configuration_digest"}
        activity = await store.call("list_activity", owner_id="owner", assignment_id=identity)
        assert all(marker not in item.summary for item in activity)
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert all(marker not in json.dumps(thaw(action.intent.request)) for action in actions)
    asyncio.run(scenario())


def test_pre_upgrade_planner_intent_keeps_its_receipt(engine):
    """A planner intent reserved without the prior-result binding still matches."""
    host, runner, store, identity = engine
    from persistent_agents import runner as runner_module
    original_model = runner_module.AssignmentRunner._model
    async def legacy_model(self, executor, key, system, context, **kwargs):
        if key.endswith(":plan"):
            context = {name: value for name, value in context.items() if name != "prior_result_digest"}
            kwargs["previous_contexts"] = ()
        return await original_model(self, executor, key, system, context, **kwargs)
    async def scenario():
        configured = host._call_llm
        host._call_llm = AsyncMock(return_value=(None, None))
        runner._model = legacy_model.__get__(runner)
        await claim_and_run(runner, store)
        failed = await current_thawed(store, identity)
        assert failed.safe_error_code == "assignment_model_unconfigured"
        actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        planner = next(action for action in actions if action.intent.request["kind"] == "model")
        assert "prior_result_digest" not in planner.intent.request["messages"][1]["content"]
        host._call_llm = configured
        await _pause_resume(store, identity)
        upgraded = AssignmentRunner(host, runner.service, config=RunnerConfig(lease_seconds=5))
        await claim_and_run(upgraded, store)
        resumed = await current_thawed(store, identity)
        assert resumed.phase == "waiting", resumed.safe_error_code
        assert host.physical_models == ["plan", "child", "child", "join"]
        after = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        successor = next(action for action in after if action.intent.action_key ==
            digest([planner.intent.action_key, "successor", planner.control_epoch]))
        assert successor.state == "succeeded"
        assert successor.intent.request_digest == planner.intent.request_digest
        assert resumed.checkpoint["observation"]["kind"] == "changed"
    asyncio.run(scenario())
