"""Builds a sealed result database through the authenticated research fixtures.
Each projection consumer receives an isolated clone, fresh reader credentials and real repository services without reusing execution authority.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
import json
import os
from types import MappingProxyType, SimpleNamespace

import pytest

from tests.helpers.plane_template import TemplateDatabase, engine_clone, populated_engine_template


def _immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(item) for item in value)
    return value


@dataclass(frozen=True)
class CompletedSeed:
    template: TemplateDatabase
    owner: str
    original_sid: str
    assignment_id: str
    source_action_id: str
    credential_key: str = field(repr=False)
    observations: str = field(repr=False)


async def _produce(runtime, patch, directory):
    from persistent_agents.tests.test_operation_reader_postgres_088 import operation
    from persistent_agents.tests.test_research_execution_postgres_088 import research
    from persistent_agents.tests.test_research_episode_postgres_088 import outcome
    from persistent_agents.runtime_values import canonical
    from tests.test_lets_gate_ordering import gate_orchestrator
    from tests.test_request_session_authority_088 import session_fixture, signing_key

    session = session_fixture(runtime, patch, signing_key.__wrapped__())
    producer = operation.__wrapped__(runtime, session, gate_orchestrator.__wrapped__(patch),
                                    patch, directory, SimpleNamespace(param={"tokens": 300_000}))
    op = await anext(producer)
    try:
        await research.__wrapped__(op, patch, directory)
        runner, result = await outcome(op)
        completed = await runner._finish_operation(op.executor, result)
        assert completed.lifecycle == "completed"
        assert len(op.model_calls) == 1 and len(op.physical) == 2
        assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None
        return dict(owner=op.owner, original_sid=op.sid, assignment_id=completed.assignment_id,
                    source_action_id=op.source_action.action_id,
                    credential_key=os.environ["CREDENTIAL_ENCRYPTION_KEY"],
                    observations=canonical({"physical": op.physical, "model_calls": op.model_calls}))
    finally:
        await producer.aclose()


@pytest.fixture(scope="session")
def completed_seed(plane_template, tmp_path_factory):
    metadata = {}
    directory = tmp_path_factory.mktemp("completed-seed")

    def populate(runtime):
        with pytest.MonkeyPatch.context() as patch:
            metadata.update(asyncio.run(_produce(runtime, patch, directory)))

    with populated_engine_template(plane_template, populate) as template:
        yield CompletedSeed(template=template, **metadata)


def _uses_completed(request):
    return "completed" in request.fixturenames


@pytest.fixture
def plane(plane_template, completed_seed, request):
    template = completed_seed.template if _uses_completed(request) else plane_template
    with engine_clone(template) as (runtime, _database):
        assert runtime.health().ready
        yield runtime


@pytest.fixture
def fixture(runtime, monkeypatch, signing_key, completed_seed, request):
    from tests.test_request_session_authority_088 import session_fixture

    owner = completed_seed.owner if _uses_completed(request) else None
    value = session_fixture(runtime, monkeypatch, signing_key, owner=owner)
    yield value
    value[0].delete(value[2])


@asynccontextmanager
async def completed_reader(runtime, session, orch, patch, directory, seed):
    from audit import recorder
    from audit.recorder import Recorder
    from audit.repository import AuditRepository
    from llm_config.user_store import UserLLMConfigStore
    from orchestrator.tool_permissions import ToolPermissionManager
    from orchestrator.work_admission import WorkAdmissionCoordinator
    from personalization.phi_gate import PHIGate
    from persistent_agents.service import AssignmentService
    from persistent_agents.store import AssignmentStore

    patch.setenv("CREDENTIAL_ENCRYPTION_KEY", seed.credential_key)
    patch.setenv("AUDIT_HMAC_KEY_ID", "research_test")
    patch.setenv("AUDIT_HMAC_SECRET", "synthetic-research-binding-key-" + "x" * 32)
    patch.delenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", raising=False)
    store = AssignmentStore(plane_runtime=runtime)
    audit = Recorder(AuditRepository(plane_runtime=runtime), retry_queue=directory / "audit-retry")
    try:
        patch.setattr(recorder, "_RECORDER", audit)
        orch.tool_permissions = ToolPermissionManager(plane_runtime=runtime)
        orch.tool_permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:read"})
        orch._llm_store = UserLLMConfigStore(plane_runtime=runtime, data_dir=str(directory))
        orch.audit_recorder = audit
        orch.work_admission = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
            plane_runtime=runtime, slot_lease=timedelta(seconds=60))
        service = AssignmentService(orch, store, enabled=True,
                                   phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
        read = await store.call("get_operation", owner_id=seed.owner, assignment_id=seed.assignment_id)
        ledger = await store.call("list_actions", owner_id=seed.owner, assignment_id=seed.assignment_id)
        observations = _immutable(json.loads(seed.observations))
        op = SimpleNamespace(runtime=runtime, sessions=session[0], owner=seed.owner, sid=session[2],
            original_sid=seed.original_sid, refreshes=session[-1], audit=audit, completed=read.assignment,
            ledger=ledger, seed_observations=observations, model_calls=[], physical=[],
            executor=SimpleNamespace(store=store, service=service, orch=orch, record=read.assignment))
        op.model = next(a for a in ledger if a.intent.request["kind"] == "model")
        op.source = next(a for a in ledger if a.action_id ==
                         read.assignment.checkpoint["research_result"]["source"]["action_id"])
        op.source_action = next(a for a in ledger if a.action_id == seed.source_action_id)

        async def model(*args, **kwargs):
            op.model_calls.append((args, kwargs))
            raise AssertionError("completed-result read attempted a new model send")

        async def physical(*args, **kwargs):
            op.physical.append((args, kwargs))
            raise AssertionError("completed-result read attempted a new source read")

        patch.setattr("shared.isolated_http.request", model)
        orch._execute_via_websocket = physical
        yield op
        assert op.model_calls == [] and op.physical == []
    finally:
        await audit.close()
        store.close()


@pytest.fixture
async def operation(runtime, fixture, gate_orchestrator, monkeypatch, tmp_path, request, completed_seed):
    if _uses_completed(request):
        async with completed_reader(runtime, fixture, gate_orchestrator[0], monkeypatch,
                                    tmp_path, completed_seed) as op:
            yield op
    else:
        from persistent_agents.tests.test_operation_reader_postgres_088 import operation as original

        producer = original.__wrapped__(runtime, fixture, gate_orchestrator, monkeypatch, tmp_path, request)
        try:
            yield await anext(producer)
        finally:
            await producer.aclose()


@pytest.fixture
async def completed(operation):
    return operation
