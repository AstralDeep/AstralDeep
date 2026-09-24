"""Tests for mixed-profile account retirement: unresolved durable liabilities (including
action-id-less holds) block purge and commit an owner fence, and schedule failures
roll back without claiming acceptance.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from astralplane import create_streaming_blob_store
from astralplane.repositories.assignment_models import AssignmentActionIntent, AssignmentResourceAmount
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.attachments.purge import AttachmentPurgeCoordinator, AttachmentPurgeReadinessError
from orchestrator.attachments.router import attachments_router
from orchestrator.auth import require_user_id
from persistent_agents.runtime_values import digest
from tests.test_work_service_postgres_088 import plane as plane
from tests.test_work_service_postgres_088 import records as records
from tests.test_work_measurements_postgres_088 import claimed
from tests.attachments.test_purge_coordinator_074 import _coordinator


def client_app(runtime, service, blobs):
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime, purge_repository=runtime.repositories.purge, blobs=blobs)
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(attachment_purge_coordinator=coordinator)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.include_router(attachments_router)
    return app, coordinator


async def retire(client):
    return await client.post("/api/account/retirement", json={"confirmation": "retire-my-account"})


async def repository_snapshot(service, identity):
    def read(tx, repo):
        record = repo.get_assignment(tx, owner_id="owner", assignment_id=identity)
        tombstones = tx.fetch_all("SELECT * FROM astralplane_purge_tombstone WHERE owner_id=%s", ("owner",))
        retired = tx.fetch_one("SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s", ("owner",))
        receipts = tx.fetch_all("SELECT * FROM assignment_operation_receipt WHERE owner_id=%s", ("owner",))
        return record, tombstones, retired, receipts
    return await service.store.transaction(read)


@pytest.mark.asyncio
async def test_mixed_profile_retirement_schedules_only_after_safe_removal_and_converges(records, tmp_path):
    runtime, service, identities, legacy = records
    blob_root = tmp_path / "blobs"
    blobs = create_streaming_blob_store(root=blob_root)
    for owner in ("owner", "other"):
        path = blob_root / owner / "orphan" / "private.bin"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"private fixture payload")
    app, coordinator = client_app(runtime, service, blobs)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await retire(client)
            assert response.status_code == 202 and response.json()["status"] == "cleanup_pending"
            cleanup_id = response.json()["cleanup_id"]
            assert (await retire(client)).json()["cleanup_id"] == cleanup_id
            for identity in (*identities[:2], legacy):
                assert await service.store.call("get_assignment", owner_id="owner", assignment_id=identity) is None
            assert await service.store.call("get_assignment", owner_id="other", assignment_id=identities[2]) is not None
            await coordinator.areconcile_once(fail_on_incomplete=True)
            status = await client.get(f"/api/account/retirement/{cleanup_id}")
            assert status.json()["status"] == "purged"
        assert not (blob_root / "owner").exists()
        assert (blob_root / "other/orphan/private.bin").read_bytes() == b"private fixture payload"
        _, _, retired, receipts = await repository_snapshot(service, identities[0])
        assert retired["state"] == "retired" and not receipts
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["outstanding_usage", "reconciliation_task"])
async def test_retained_work_without_action_ids_blocks_purge_and_commits_owner_fence(records, tmp_path, hold):
    runtime, service, identities, _ = records
    identity = identities[0]
    def hold_fixture(tx, repo):
        if hold == "outstanding_usage":
            tx.execute("UPDATE persistent_assignment SET data=jsonb_set(data, '{usage,outstanding,tokens}', '1') WHERE id=%s", (identity,))
        else:
            task = {"task_id": "held-task", "plan_key": "fixture", "instruction_revision": 1,
                    "title": "Held task", "instruction": "private task details", "allowed_tools": [],
                    "state": "reconciliation"}
            tx.execute("UPDATE persistent_assignment SET data=jsonb_set(data, '{tasks}', %s::jsonb) WHERE id=%s", (json.dumps([task]), identity))
    await service.store.transaction(hold_fixture)
    app, coordinator = client_app(runtime, service, create_streaming_blob_store(root=tmp_path / "blobs"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            for _ in range(2):
                response = await retire(client)
                assert response.status_code == 409 and response.json()["status"] == "reconciliation_required"
                assert response.json()["retained_assignment_count"] == 1
                assert response.json()["unresolved_action_count"] == 0
                assert "cleanup_id" not in response.json() and "private" not in response.text
                assert response.headers["cache-control"] == "no-store"
        record, tombstones, retired, receipts = await repository_snapshot(service, identity)
        assert record.lifecycle == "stopped" and retired["state"] == "retired"
        assert not tombstones and receipts
        if hold == "outstanding_usage":
            assert record.usage["outstanding"]["tokens"] == 1
        else:
            assert record.tasks[0]["state"] == "reconciliation"
            assert record.tasks[0]["instruction"] == "private task details"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_opaque_action_bytes_and_reservation_survive_repeated_retirement(records, tmp_path):
    runtime, service, identities, _ = records
    fence, binding, authority = await claimed(service, identities[0])
    def seed(tx, repo):
        request = {"kind": "model", "messages": ["private fixture input"]}
        maximum = AssignmentResourceAmount(model_calls=1)
        action = repo.put_action_for_execution(
            tx, fence=fence, binding=binding, authority=authority, intent=AssignmentActionIntent(
            action_key=str(uuid4()), request=request, request_digest=digest(request), maximum=maximum,
            permission_digest=digest("permission"), precondition_digest=digest("precondition")))
        repo.reserve_action_for_execution(
            tx, fence=fence, binding=binding, authority=authority,
            action_id=action.action_id, attempt_id=str(uuid4()),
            expected_request_digest=action.intent.request_digest, maximum=maximum)
        tx.execute("UPDATE persistent_assignment_action SET data=jsonb_set(data, '{future_payload}', %s::jsonb) WHERE id=%s",
                   ('{"version":2,"private":"opaque metadata"}', action.action_id))
        return action.action_id, tx.fetch_one("SELECT data FROM persistent_assignment_action WHERE id=%s", (action.action_id,))["data"]
    action_id, before = await service.store.transaction(seed)
    app, coordinator = client_app(runtime, service, create_streaming_blob_store(root=tmp_path / "blobs"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            for _ in range(2):
                response = await retire(client)
                assert response.status_code == 409
                assert response.json()["unresolved_action_count"] == response.json()["retained_assignment_count"] == 1
                assert "private" not in response.text and "cleanup_id" not in response.json()
        after = await service.store.transaction(lambda tx, _: tx.fetch_one(
            "SELECT data FROM persistent_assignment_action WHERE id=%s", (action_id,))["data"])
        assert after == before
        record, tombstones, retired, receipts = await repository_snapshot(service, identities[0])
        assert record.lifecycle == "stopped" and record.usage["outstanding"]["model_calls"] == 1
        assert not tombstones and retired["state"] == "retired" and receipts
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_mixed_retirement_schedule_failure_rolls_back_and_reports_no_acceptance(records, tmp_path):
    runtime, service, identities, legacy = records
    app, coordinator = client_app(runtime, service, create_streaming_blob_store(root=tmp_path / "blobs"))
    real = coordinator._repository
    def fail_after_schedule(transaction, **values):
        real.schedule_owner_namespace(transaction, **values)
        raise RuntimeError("private storage failure")
    coordinator._repository = SimpleNamespace(schedule_owner_namespace=fail_after_schedule)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await retire(client)
            assert response.status_code == 503 and "private" not in response.text
        for identity in (*identities[:2], legacy):
            record, tombstones, retired, _ = await repository_snapshot(service, identity)
            assert record.lifecycle == "active" and not tombstones and retired is None
    finally:
        coordinator._repository = real
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["catalog", "repository", "method"])
async def test_unqualified_retirement_cannot_skip_durable_work_or_use_legacy_adapter(missing):
    coordinator, runtime, repository, executor, events = _coordinator()
    legacy = Mock()
    if missing == "catalog":
        del runtime.repositories
    elif missing == "repository":
        runtime.repositories.assignments = None
    else:
        runtime.repositories.assignments = SimpleNamespace(retire_owner=legacy)
    try:
        with pytest.raises(AttachmentPurgeReadinessError, match="account_retirement_repository_unavailable"):
            await coordinator.aschedule_owner(owner_id="owner")
        assert repository.owner_values is None and "executor.execute" not in events
        legacy.assert_not_called()
    finally:
        await coordinator.close()
