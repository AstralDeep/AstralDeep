"""Exercises shared continuity affordances and original-caller rejection using real Work and Plane state."""

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
import pytest

from orchestrator.projection_surfaces import work
from orchestrator.work_surface_authority import WorkSurfaceRead
from tests.test_work_surface_postgres_088 import (
    surface as surface, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, plane as plane, context as context, command,
)
from tests.test_work_completion_wake_postgres_088 import subscribe
from tests.test_work_continuation_authority_088 import current

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def waiting(surface, service, fixture, runtime):
    orch, socket, identity = surface
    socket.scope["headers"].append((b"origin", b"https://app.invalid"))
    orch.ui_sessions[socket]["_client_capabilities"].append("work_continuity_v1")
    source = (await service.submit(await context(fixture, runtime), command(name="Completion source"))).record
    owner = fixture[1]
    waiter = current(runtime, SimpleNamespace(owner_id=owner, assignment_id=identity))
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    orch.audit_repo = service.audit
    app = FastAPI()
    app.state.orchestrator = orch
    api = SimpleNamespace(service=service, fixture=fixture, runtime=runtime, app=app)
    await subscribe(api, waiter, source)
    return orch, socket, current(runtime, waiter), source


async def test_waiting_detail_supplies_only_current_captured_continuity_bindings(waiting):
    orch, socket, waiter, _ = waiting
    read = WorkSurfaceRead(orch, socket, waiter.owner_id)
    try:
        await read.authenticate()
        state = await work._state(read, {"mode": "detail", "operation_id": waiter.assignment_id})
        payload = state["continuity"]
        assert set(payload) == {"version", "operation_id", "expected_revision", "submission_id"}
        assert payload["version"] == 1 and payload["operation_id"] == waiter.assignment_id
        assert payload["expected_revision"] == state["operation"]["revision"] == waiter.state_version
        assert str(UUID(payload["submission_id"])) == payload["submission_id"]
        assert UUID(payload["submission_id"]).version == 4
    finally:
        read.close()


async def test_legacy_client_or_nonwaiting_source_has_no_mutating_affordance(waiting):
    orch, socket, waiter, source = waiting
    orch.ui_sessions[socket]["_client_capabilities"] = ["work_read_v1"]
    read = WorkSurfaceRead(orch, socket, waiter.owner_id)
    try:
        await read.authenticate()
        assert "continuity" not in await work._state(read, {"mode": "detail", "operation_id": waiter.assignment_id})
    finally:
        read.close()
    orch.ui_sessions[socket]["_client_capabilities"].append("work_continuity_v1")
    read = WorkSurfaceRead(orch, socket, waiter.owner_id)
    try:
        await read.authenticate()
        assert "continuity" not in await work._state(read, {"mode": "detail", "operation_id": source.assignment_id})
    finally:
        read.close()


async def test_continuity_without_the_original_accepted_request_is_rejected(waiting):
    orch, socket, waiter, _ = waiting
    payload = {"version": 1, "operation_id": waiter.assignment_id,
               "expected_revision": waiter.state_version, "submission_id": str(uuid4())}
    assert not await work.deliver_continuity(orch, socket, waiter.owner_id, [], payload,
                                           request_generation=str(uuid4()))


@pytest.mark.parametrize("change", ["composition", "repositories", "adapter", "session_adapter", "audit"])
async def test_current_write_binding_rejects_replaced_composition(waiting, change):
    orch, socket, waiter, _ = waiting
    from persistent_agents.models import AssignmentError

    caller = await work._ws_publication_caller(orch, socket, waiter.owner_id)
    targets = {
        "composition": (orch.runtime_composition.plane, "runtime"),
        "repositories": (orch.runtime_composition.plane, "repositories"),
        "adapter": (orch.persistent_assignments.store, "async_runtime"),
        "session_adapter": (orch.web_sessions, "_sessions"),
        "audit": (orch, "audit_repo"),
    }
    target, name = targets[change]
    original = getattr(target, name)
    try:
        setattr(target, name, object())
        with pytest.raises(AssignmentError) as error:
            caller._assert_local(orch.persistent_assignments)
        assert error.value.code == "work_control_unavailable"
    finally:
        setattr(target, name, original)
