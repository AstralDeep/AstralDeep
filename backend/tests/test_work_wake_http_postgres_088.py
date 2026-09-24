"""Tests for manual owner wake through the mounted HTTP boundary
(backend/orchestrator/work_api.py): replay after cancel without a new continuation,
and that denials reveal no private values.
"""

from uuid import uuid4

import httpx
import pytest

from orchestrator.work_api import work_router
from tests.test_work_wake_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
    waiting, wake_audits,
)
from tests.test_work_continuation_authority_088 import control, current
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mounted(api):
    api.app.include_router(work_router, prefix="/api")
    record, command = await waiting(api)
    return api, record, command.model_dump()


def url(record):
    return f"/api/work/v1/operations/{record.assignment_id}/wake"


async def test_mounted_wake_replays_after_cancel_without_new_continuation(mounted):
    api, record, command = mounted
    before = len(api.fixture[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://app.invalid") as client:
        accepted = await client.post(url(record), json=command, headers=headers(api.fixture))
        assert accepted.status_code == 200 and accepted.json()["applied"] is True
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json()["operation"]["phase"] == "waiting"
        stopped = control(api.runtime, current(api.runtime, record), "stop")
        api.orch.persistent_assignment_runner = None
        api.orch._llm_store = None
        command["expected_revision"] = stopped.state_version
        replay = await client.post(url(record), json=command, headers=headers(api.fixture, cookie=False))
        assert replay.status_code == 200 and replay.json()["applied"] is False
        assert replay.headers["cache-control"] == "no-store"
    assert current(api.runtime, record) == stopped
    assert len(api.fixture[-1]) == before + 1 and len(wake_audits(api)) == 1
    assert api.service.audit.verify_chain(record.owner_id) is None


@pytest.mark.parametrize("change,status", [
    ("bare_bearer", 401), ("extra_authority", 422), ("stale_event", 409),
    ("different_event", 409), ("foreign_or_absent", 404),
])
async def test_mounted_wake_denials_preserve_wait_and_reveal_no_private_values(mounted, change, status):
    api, record, command = mounted
    target = url(record)
    if change == "extra_authority":
        command["authority"] = "client-cannot-assert-authority"
    elif change == "stale_event":
        command["owner_revision"] = 0
    elif change == "different_event":
        command["owner_event_id"] = str(uuid4())
    elif change == "foreign_or_absent":
        target = target.replace(record.assignment_id, str(uuid4()))
    before = len(api.fixture[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://app.invalid") as client:
        response = await client.post(target, json=command,
            headers=headers(api.fixture, cookie=change != "bare_bearer"))
        if change == "foreign_or_absent":
            foreign = {"Authorization": "Bearer " + api.fixture[3](sub="different-owner")}
            hidden = await client.post(url(record), json=command, headers=foreign)
            assert hidden.status_code == response.status_code and hidden.json() == response.json()
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert "client-cannot-assert-authority" not in response.text
    assert current(api.runtime, record) == record and len(api.fixture[-1]) == before
    assert wake_audits(api) == []
