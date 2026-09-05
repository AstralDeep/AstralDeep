"""Real authenticated retirement route fences Plane assignments atomically."""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from astralplane.repositories import RepositoryConflictError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from orchestrator.attachments.purge import AttachmentPurgeCoordinator
from orchestrator.attachments.router import attachments_router
from orchestrator.auth import require_user_id
from persistent_agents.dispatch_context import current_dispatch
from persistent_agents.runtime_values import digest
from persistent_agents.tests.test_engine_postgres import (
    claim_and_run,
)
from persistent_agents.tests.test_engine_postgres import (
    engine as shared_engine,
)
from persistent_agents.tests.test_engine_postgres import (
    plane as shared_plane,
)

plane = shared_plane
engine = shared_engine


def client_app(plane, host):
    coordinator = AttachmentPurgeCoordinator(plane_runtime=plane, purge_repository=plane.repositories.purge,
                                            blobs=object(), executor=Mock())
    app = FastAPI()
    host.attachment_purge_coordinator = coordinator
    app.state.orchestrator = host
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.include_router(attachments_router)
    return app, coordinator


@pytest.mark.asyncio
async def test_safe_retirement_purges_assignments_and_schedules_blob_cleanup_same_request(plane, engine):
    host, _runner, store, identity = engine
    record = await store.call("get_assignment", owner_id="owner", assignment_id=identity)
    app, coordinator = client_app(plane, host)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post("/api/account/retirement", json={"confirmation": "retire-my-account"})
            assert response.status_code == 202
            assert response.headers["Cache-Control"] == "no-store"
            cleanup_id = response.json()["cleanup_id"]
            status = await client.get(f"/api/account/retirement/{cleanup_id}")
            assert status.status_code == 200 and status.json()["status"] != "purged"
        assert await store.call("get_assignment", owner_id="owner", assignment_id=identity) is None
        with pytest.raises(RepositoryConflictError, match="owner_retired"), plane.transaction() as tx:
            plane.repositories.assignments.create_assignment(tx, owner_id="owner", assignment_id=str(uuid4()),
                submission_id=str(uuid4()), submission_digest=digest("new after retirement"), definition=record.definition)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_uncertain_effect_retirement_commits_stop_without_claiming_account_purge(plane, engine):
    host, runner, store, identity = engine
    async def uncertain_model(socket, messages, **kwargs):
        async def failed_transport():
            raise ConnectionError("private provider diagnostic")
        return await current_dispatch().invoke_model(failed_transport, {"messages": messages})
    host._call_llm = uncertain_model
    await claim_and_run(runner, store)
    before = await store.call("get_assignment", owner_id="owner", assignment_id=identity)
    actions = await store.call("list_actions", owner_id="owner", assignment_id=identity)
    assert any(action.state == "uncertain" for action in actions)
    app, coordinator = client_app(plane, host)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post("/api/account/retirement", json={"confirmation": "retire-my-account"})
        assert response.status_code == 409
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["status"] == "reconciliation_required"
        assert response.json()["unresolved_action_count"] >= 1
        assert "cleanup_id" not in response.json() and "private" not in response.text
        stopped = await store.call("get_assignment", owner_id="owner", assignment_id=identity)
        assert stopped.lifecycle == "stopped" and stopped.control_epoch > before.control_epoch
        retained = await store.call("list_actions", owner_id="owner", assignment_id=identity)
        assert any(action.state == "uncertain" for action in retained)
        with plane.transaction() as tx:
            assert not plane.repositories.purge.has_incomplete_for_administration(tx)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_retirement_failure_rolls_back_assignment_changes_and_never_reports_accepted(plane, engine):
    host, _runner, store, identity = engine
    app, coordinator = client_app(plane, host)
    original = coordinator._repository
    coordinator._repository = SimpleNamespace(schedule_owner_namespace=Mock(side_effect=RuntimeError("storage unavailable")))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post("/api/account/retirement", json={"confirmation": "retire-my-account"})
        assert response.status_code == 503
        record = await store.call("get_assignment", owner_id="owner", assignment_id=identity)
        assert record.lifecycle == "active"
    finally:
        coordinator._repository = original
        await coordinator.close()
