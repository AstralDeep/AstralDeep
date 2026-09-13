"""Resume through the mounted ordinary IAM/body boundary and actual Plane."""
import json
from uuid import uuid4

import httpx
import pytest

from orchestrator.work_api import work_router
from tests.test_work_resume_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
    paused,
)
from tests.test_work_continuation_authority_088 import current
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mounted(api):
    api.app.include_router(work_router, prefix="/api")
    return api, await paused(api)


def url(record):
    return f"/api/work/v1/operations/{record.assignment_id}/resume"


def body(record):
    return {"submission_id": str(uuid4()), "expected_revision": record.state_version}


async def test_mounted_resume_and_bearer_only_replay_keep_one_logical_control(mounted):
    api, record = mounted
    command = body(record)
    initial_refreshes = len(api.fixture[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://app.invalid") as client:
        accepted = await client.post(url(record), json=command, headers=headers(api.fixture))
        assert accepted.status_code == 200 and accepted.json()["applied"] is True
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json()["operation"]["lifecycle"] == "active"
        api.orch.persistent_assignment_runner = None
        api.orch._llm_store = None
        replay = await client.post(url(record), json=command, headers=headers(api.fixture, cookie=False))
        assert replay.status_code == 200 and replay.json()["applied"] is False
    assert len(api.fixture[-1]) == initial_refreshes + 1
    rows, _ = api.service.audit.list_for_user(record.owner_id)
    assert sum(row.action_type == "assignment_resume" for row in rows) == 1
    assert api.service.audit.verify_chain(record.owner_id) is None


@pytest.mark.parametrize("change", ["bare_bearer", "extra", "bool_revision", "duplicate_key"])
async def test_mounted_resume_refuses_missing_session_or_ambiguous_intent_without_changes(mounted, change):
    api, record = mounted
    command = body(record)
    if change == "extra":
        command["authority"] = "client-cannot-assert-authority"
    elif change == "bool_revision":
        command["expected_revision"] = True
    raw = json.dumps(command).encode()
    if change == "duplicate_key":
        raw = raw[:-1] + b', "expected_revision": 1}'
    before = len(api.fixture[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://app.invalid") as client:
        response = await client.post(url(record), content=raw,
            headers=headers(api.fixture, cookie=change != "bare_bearer"))
    assert response.status_code == (401 if change == "bare_bearer" else 400 if change == "duplicate_key" else 422)
    assert response.headers["cache-control"] == "no-store"
    assert "client-cannot-assert-authority" not in response.text
    assert current(api.runtime, record) == record and len(api.fixture[-1]) == before
    rows, _ = api.service.audit.list_for_user(record.owner_id)
    assert not any(row.action_type == "assignment_resume" for row in rows)
