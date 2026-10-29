"""Continuity recheck through the mounted HTTP boundary (backend/orchestrator/work_api.py):
recovery of a wake lost to node loss, and denials that hide foreign operations and stale
views without leaking private values.
"""

from uuid import uuid4

import httpx
import pytest

from orchestrator.work_api import work_router

from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)
from tests.test_work_completion_wake_postgres_088 import (
    paired as paired, subscribe as subscribe,
)
from tests.test_work_continuation_authority_088 import current
from tests.test_work_continuity_postgres import (
    body as body, complete_without_wake as complete_without_wake,
    service_for as service_for,
)
from tests.test_work_resume_postgres_088 import caller as caller
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mounted(api):
    api.app.include_router(work_router, prefix="/api")
    source, waiter = await paired(api)
    return api, source, waiter


def route(record):
    return f"/api/work/v1/operations/{record.assignment_id}/continuity"


async def client_for(api):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                             base_url="https://app.invalid")


async def test_mounted_recheck_recovers_a_wait_lost_to_node_loss(mounted):
    api, source, waiter = mounted
    await subscribe(api, waiter, source)
    await complete_without_wake(api, source)
    stuck = current(api.runtime, waiter)
    async with await client_for(api) as client:
        recovered = await client.post(route(waiter), json={
            "expected_revision": stuck.state_version, "submission_id": str(uuid4())},
            headers=headers(api.fixture))
        assert recovered.status_code == 200
        assert recovered.json()["applied"] is True
        assert current(api.runtime, waiter).phase == "waiting"
        again = await client.post(route(waiter), json={
            "expected_revision": stuck.state_version + 1, "submission_id": str(uuid4())},
            headers=headers(api.fixture))
        assert again.status_code == 200 and again.json()["applied"] is False
        assert again.json()["continuation"] is None
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_mounted_recheck_denials_hide_values_and_preserve_state(mounted):
    api, source, waiter = mounted
    await subscribe(api, waiter, source)
    stale = body(waiter)
    foreign = {"Authorization": "Bearer " + api.fixture[3](sub="different-owner"),
               "Content-Type": "application/json"}
    async with await client_for(api) as client:
        foreign_response = await client.post(route(waiter), json={
            "expected_revision": 1, "submission_id": str(uuid4())}, headers=foreign)
        assert foreign_response.status_code == 404
        assert foreign_response.json() == {"error": "work_not_found"}
        stale_response = await client.post(route(waiter), json={
            "expected_revision": stale.expected_revision,
            "submission_id": stale.submission_id}, headers=headers(api.fixture))
        assert stale_response.status_code == 409
        assert stale_response.json() == {"error": "assignment_revision_conflict"}
        malformed = await client.post(route(waiter), json={"expected_revision": 1},
                                      headers=headers(api.fixture))
        assert malformed.status_code == 422
        assert malformed.json() == {"error": "work_control_invalid"}
    assert current(api.runtime, waiter).phase == "awaiting_event"
