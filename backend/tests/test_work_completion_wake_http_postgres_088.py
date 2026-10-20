"""Tests for owner completion continuations through the mounted HTTP boundary
(backend/orchestrator/work_api.py): proposal/confirmation, bounded inspection,
revocation, and denials that reveal no private values.
"""

from uuid import uuid4

import httpx
import pytest

from orchestrator.work_api import work_router
from tests.test_work_completion_wake_postgres_088 import (
    api as api, fixture as fixture, module as module, paired as paired,
    plane as plane, research_service as research_service, service as service,
    signing_key as signing_key, source_service as source_service,
)
from tests.test_work_continuation_authority_088 import control, current
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mounted(api):
    api.app.include_router(work_router, prefix="/api")
    source, waiter = await paired(api)
    return api, source, waiter


def base(record):
    return f"/api/work/v1/operations/{record.assignment_id}/continuations"


async def client_for(api):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                             base_url="https://app.invalid")


async def test_mounted_proposal_confirm_inspect_and_revoke_round_trip(mounted):
    api, source, waiter = mounted
    async with await client_for(api) as client:
        proposed = await client.post(base(waiter) + "/proposals", json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id}, headers=headers(api.fixture))
        assert proposed.status_code == 200
        proposal = proposed.json()
        assert proposal["source_operation_id"] == source.assignment_id
        assert proposal["waiter_operation_id"] == waiter.assignment_id
        confirmed = await client.post(base(waiter), json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id,
            "proposal_id": proposal["proposal_id"],
            "proposal_digest": proposal["proposal_digest"]}, headers=headers(api.fixture))
        assert confirmed.status_code == 200 and confirmed.json()["applied"] is True
        inspected = await client.get(base(waiter), headers=headers(api.fixture))
        assert inspected.status_code == 200
        view = inspected.json()["continuation"]
        assert view["source_operation_id"] == source.assignment_id
        assert view["source_disposition"] == "queued" and view["awaiting_event"] is True
        subscribed = current(api.runtime, waiter)
        revoked = await client.request("DELETE", base(waiter) + "/" + source.assignment_id,
            json={"expected_revision": subscribed.state_version, "submission_id": str(uuid4())},
            headers=headers(api.fixture))
        assert revoked.status_code == 200 and revoked.json()["applied"] is True
        assert revoked.json()["continuation"] is None
        emptied = await client.get(base(waiter), headers=headers(api.fixture))
        assert emptied.json() == {"continuation": None}
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_mounted_confirmation_denials_preserve_state_and_hide_values(mounted):
    api, source, waiter = mounted
    async with await client_for(api) as client:
        foreign = {"Authorization": "Bearer " + api.fixture[3](sub="different-owner"),
                   "Content-Type": "application/json"}
        proposed = await client.post(base(waiter) + "/proposals", json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id}, headers=headers(api.fixture))
        proposal = proposed.json()
        missing = await client.post(base(waiter), json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id, "proposal_id": uuid4().hex,
            "proposal_digest": "0" * 64}, headers=headers(api.fixture))
        assert missing.status_code == 409 and missing.json() == {"error": "work_proposal_expired"}
        changed = await client.post(base(waiter), json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id, "proposal_id": proposal["proposal_id"],
            "proposal_digest": "1" * 64}, headers=headers(api.fixture))
        assert changed.status_code == 409
        assert changed.json() == {"error": "assignment_proposal_changed"}
        hidden = await client.post(base(waiter), json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id, "proposal_id": uuid4().hex,
            "proposal_digest": "0" * 64}, headers=foreign)
        assert hidden.status_code == 404
        assert hidden.json() == {"error": "work_not_found"}
        bearer_only = await client.post(base(waiter) + "/proposals", json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id},
            headers=headers(api.fixture, cookie=False))
        assert bearer_only.status_code == 200
        unauthenticated = await client.post(base(waiter) + "/proposals", json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id},
            headers={"Content-Type": "application/json"})
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"error": "work_authentication_required"}
    assert current(api.runtime, waiter).phase == waiter.phase
    rows, _ = api.service.audit.list_for_user(waiter.owner_id)
    assert [row.action_type for row in rows
            if row.action_type.startswith("assignment_subscribe")
            or row.action_type.startswith("assignment_unsubscribe")] == []


async def test_mounted_source_terminal_refusal_is_truthful(mounted):
    api, source, waiter = mounted
    control(api.runtime, source, "stop")
    async with await client_for(api) as client:
        proposed = await client.post(base(waiter) + "/proposals", json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id}, headers=headers(api.fixture))
        assert proposed.status_code == 409
        assert proposed.json() == {"error": "work_continuation_source_terminal"}
        forged = await client.post(base(waiter), json={
            "expected_revision": waiter.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source.assignment_id, "proposal_id": uuid4().hex,
            "proposal_digest": "0" * 64}, headers=headers(api.fixture))
        assert forged.status_code == 409
        assert forged.json() == {"error": "work_proposal_expired"}
    assert current(api.runtime, waiter).phase == waiter.phase


async def test_mounted_unknown_continuation_errors_are_partitioned(mounted):
    api, source, waiter = mounted
    async with await client_for(api) as client:
        absent = await client.get(base(waiter), headers=headers(api.fixture))
        assert absent.status_code == 200 and absent.json() == {"continuation": None}
        missing_operation = await client.get(
            f"/api/work/v1/operations/{uuid4()}/continuations", headers=headers(api.fixture))
        assert missing_operation.status_code == 404
        assert missing_operation.json() == {"error": "work_not_found"}
        foreign = {"Authorization": "Bearer " + api.fixture[3](sub="different-owner"),
                   "Content-Type": "application/json"}
        hidden = await client.get(base(waiter), headers=foreign)
        assert hidden.status_code == 404 and hidden.json() == {"error": "work_not_found"}
        revoked_absent = await client.request("DELETE",
            base(waiter) + "/" + source.assignment_id,
            json={"expected_revision": waiter.state_version, "submission_id": str(uuid4())},
            headers=headers(api.fixture))
        assert revoked_absent.status_code == 404
        assert revoked_absent.json() == {"error": "work_not_found"}
