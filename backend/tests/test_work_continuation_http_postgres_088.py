"""Tests that the mounted owner wait/reconciliation HTTP routes (work_api.py) change
state at most once, retain the manual namespace, and reject payloads carrying
authority fields the owner should not control.
"""

import httpx
import pytest

from orchestrator.work_api import work_router
from tests.test_work_continuations_postgres_088 import (
    plane as plane, read_records as read_records, signing_key as signing_key,
    value as value, current, uncertain, wait_body, reconcile_body,
)

pytestmark = pytest.mark.asyncio


def url(value, command, identity=None):
    return f"/api/work/v1/operations/{identity or value.identity}/{command}"


def headers(value):
    return {"Authorization": "Bearer " + value.caller._token, "Content-Type": "application/json"}


def client(value):
    app = value.caller._binding.app
    app.include_router(work_router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid")


async def test_mounted_owner_wait_changes_once_and_retains_manual_namespace(value):
    body = wait_body(value).model_dump()
    async with client(value) as http:
        accepted = await http.post(url(value, "wait"), json=body, headers=headers(value))
        assert accepted.status_code == 200 and accepted.json()["applied"] is True
        replay = await http.post(url(value, "wait"), json=body, headers=headers(value))
        assert replay.status_code == 200 and replay.json()["applied"] is False
    record = await current(value)
    assert record.phase == "awaiting_event" and record.next_wake_at is None
    assert record.operation["control"]["wait"] == {
        "event_key": "owner:" + body["owner_event_id"], "source_revision": body["owner_revision"]}
    assert accepted.headers["cache-control"] == replay.headers["cache-control"] == "no-store"
    assert len(value.audit.list_for_user("owner")[0]) == 1
    assert value.audit.verify_chain("owner") is None


@pytest.mark.parametrize("decision", ["confirmed_applied", "confirmed_not_applied"])
async def test_mounted_reconciliation_settles_once_without_refund_output_or_wake(value, decision):
    action, outcome = await uncertain(value)
    before = await current(value)
    body = (await reconcile_body(value, outcome, decision=decision)).model_dump()
    endpoint = url(value, f"actions/{action.action_id}/reconcile")
    async with client(value) as http:
        accepted = await http.post(endpoint, json=body, headers=headers(value))
        assert accepted.status_code == 200 and accepted.json()["applied"] is True
        body["expected_revision"] = (await current(value)).state_version
        replay = await http.post(endpoint, json=body, headers=headers(value))
        assert replay.status_code == 200 and replay.json()["applied"] is False
    after = await current(value)
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.usage["spent"]["tool_calls"] == action.intent.maximum.tool_calls
    assert after.wake_generation == before.wake_generation and after.next_wake_at is None
    assert "prior_result_digest" not in accepted.text and "synthetic" not in accepted.text
    assert accepted.headers["cache-control"] == replay.headers["cache-control"] == "no-store"
    assert len(value.audit.list_for_user("owner")[0]) == 1
    assert value.audit.verify_chain("owner") is None


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_mounted_safe_continuation_is_owner_scoped_and_rejects_authority_fields(value, kind):
    if kind == "wait":
        body = wait_body(value).model_dump()
        suffix = "wait"
    else:
        action, outcome = await uncertain(value)
        body = (await reconcile_body(value, outcome)).model_dump()
        suffix = f"actions/{action.action_id}/reconcile"
    before = await current(value)
    async with client(value) as http:
        denied = await http.post(url(value, suffix, value.foreign), json=body, headers=headers(value))
        assert denied.status_code == 404 and denied.json() == {"error": "work_not_found"}
        malformed = await http.post(url(value, suffix), json={**body, "authority": "untrusted-client-proof"},
                                    headers=headers(value))
        assert malformed.status_code == 422 and malformed.json() == {"error": "work_control_invalid"}
        assert denied.headers["cache-control"] == malformed.headers["cache-control"] == "no-store"
    assert await current(value) == before and value.audit.list_for_user("owner")[0] == []
