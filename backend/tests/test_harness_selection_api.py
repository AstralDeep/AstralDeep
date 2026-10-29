"""Harness selection HTTP uses the current human boundary and rejects ambiguous authority fields.
The strict action changes only the authenticated owner's CAS preference and atomic audit.
"""

import httpx
import pytest

from orchestrator.harness.selection_api import harness_router
from tests.test_harness_selection_postgres import (
    harness as harness, human as human, bound as bound, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


def headers(fixture):
    return {"Authorization": "Bearer " + fixture[3](), "Origin": "https://app.invalid",
            "Content-Type": "application/json"}


async def test_harness_routes_preserve_owner_cas_and_private_capture_boundary(harness, bound, fixture):
    bound[1].include_router(harness_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url="https://app.invalid") as client:
        response = await client.get("/api/harness/profiles", headers=headers(fixture))
        assert response.status_code == 200
        assert response.json()["selection"] == {"harness_id": None, "revision": 0}
        reset = await client.post("/api/harness/selection", headers=headers(fixture),
            json={"harness_id": None, "expected_revision": 0})
        assert reset.status_code == 200 and reset.json()["selection"]["revision"] == 1
        stale = await client.post("/api/harness/selection", headers=headers(fixture),
            json={"harness_id": None, "expected_revision": 0})
        assert stale.status_code == 409 and stale.json()["error"] == "harness_selection_conflict"
        assert "no-store" in reset.headers["cache-control"]
        assert "config_revision" not in reset.text and "config_binding_key_id" not in reset.text


@pytest.mark.parametrize("raw", [
    '{"harness_id":null,"expected_revision":true}',
    '{"harness_id":null,"expected_revision":0.0}',
    '{"harness_id":null,"expected_revision":"0"}',
    '{"harness_id":null,"expected_revision":-1}',
    '{"harness_id":null,"expected_revision":9007199254740992}',
    '{"harness_id":null,"expected_revision":0,"qualified":true}',
    '{"harness_id":true,"expected_revision":0}',
    '{"harness_id":null}',
    '{"harness_id":null,"expected_revision":0,"expected_revision":1}',
    '{"harness_id":null,"expected_revision":0,"owner_id":"other"}',
])
async def test_http_selection_rejects_unknown_duplicate_and_noninteger_fields(
        harness, bound, fixture, runtime, raw):
    bound[1].include_router(harness_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url="https://app.invalid") as client:
        response = await client.post("/api/harness/selection", headers=headers(fixture), content=raw)
    assert response.status_code == 422
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.harness.get(tx, owner_id=fixture[1]).revision == 0


async def test_http_framework_or_missing_iam_cannot_select(harness, bound, fixture):
    bound[1].include_router(harness_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url="https://app.invalid") as client:
        absent = await client.post("/api/harness/selection",
            json={"harness_id": None, "expected_revision": 0})
        delegated = headers(fixture)
        delegated["Authorization"] = "Bearer " + fixture[3](act={"sub": "agent"})
        denied = await client.post("/api/harness/selection", headers=delegated,
            json={"harness_id": None, "expected_revision": 0})
    assert absent.status_code in {401, 403}
    assert denied.status_code == 401
