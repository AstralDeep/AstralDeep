"""HTTP acceptance through actual supervision, governed dispatch and result read.

Plane, encrypted configuration, JWT verification, reservations and audit are real.
Institutional replies and the final source/model network responses are synthetic;
the inherited dispatch fixture isolates unrelated policy analyzers. This is not
live staging or a claim about all provider/clinical policy configurations.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from orchestrator import web_auth
from orchestrator.api import operation_router
from persistent_agents.runtime import start_assignment_runtime
from persistent_agents.tests.test_research_execution_postgres_088 import (
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)
from shared.feature_flags import flags
from tests.test_work_research_preflight_postgres_088 import research_command

runtime = plane
pytestmark = [pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def integrated(research, fixture, monkeypatch):
    op = research
    orch = op.executor.orch
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=op.runtime, repositories=op.runtime.repositories))
    orch.web_sessions = op.sessions
    orch.audit_repo = op.audit._repo
    orch.history = SimpleNamespace(get_chat=lambda *_args, **_kwargs: None)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.invalid")
    monkeypatch.setitem(flags._flags, "persistent_agents", True)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: op.executor.service.phi_gate)
    runner = start_assignment_runtime(orch)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(operation_router)
    headers = {"cookie": "astral_session=" + web_auth._sign(fixture[2]),
               "origin": "https://app.invalid", "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://app.invalid", headers=headers) as client:
        try:
            yield op, runner, client
        finally:
            await runner.stop()
            runner.store.close()


async def finished(client, identity, *, seconds=20):
    async with asyncio.timeout(seconds):
        while True:
            response = await client.get("/api/work/v1/operations/" + identity)
            assert response.status_code == 200, response.text
            operation = response.json()["operation"]
            if operation["lifecycle"] == "completed":
                return operation
            await asyncio.sleep(0.05)


async def test_registered_send_runs_one_charged_episode_and_delivers_only_attributed_result(integrated):
    op, runner, client = integrated
    previous_reads = len(op.physical)
    body = research_command(SimpleNamespace(assignments=runner.service))
    accepted = await client.post("/api/work/v1/operations", content=body)
    assert accepted.status_code == 201, accepted.text
    value = accepted.json()
    assert set(value) == {"id", "revision", "created"}
    final = await finished(client, value["id"])
    assert final["disposition"] == "completed"
    result = await client.get("/api/work/v1/operations/" + value["id"] + "/result")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    envelope = result.json()
    assert set(envelope) == {"id", "revision", "result"}
    assert envelope["id"] == value["id"] and envelope["revision"] == final["revision"]
    assert envelope["result"]["available"] is True
    page = envelope["result"]["content"]
    assert page["scope"] == "one_page_excerpts"
    assert page["passages"] == [{"id": "p001", "text": "Public release 088"}]
    assert len(op.physical) == previous_reads + 1 and len(op.model_calls) == 1
    assert final["usage"]["spent"]["tokens"] == 120
    assert final["usage"]["outstanding"]["tokens"] == 0
    replay = await client.post("/api/work/v1/operations", content=body)
    assert replay.status_code == 200 and replay.json()["id"] == value["id"]
    assert replay.json()["created"] is False
    await runner.tick()
    assert len(op.model_calls) == 1 and len(op.physical) == previous_reads + 1
    assert "result" not in final and "checkpoint" not in result.text
    assert "synthetic-provider-key" not in result.text and "payload_binding" not in result.text
    events, _ = await asyncio.to_thread(op.audit._repo.list_for_user, op.owner)
    assert sum(event.action_type == "work.accept" for event in events) == 1
    assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None


async def test_registered_result_preserves_metadata_and_unavailable_disposition(integrated):
    op, runner, client = integrated
    # Existing source-only fixture work has no completed result or matching fixed
    # capability; observing it must neither execute it nor expose its checkpoint.
    identity = op.executor.record.assignment_id
    before = len(op.physical), len(op.model_calls)
    response = await client.get("/api/work/v1/operations/" + identity + "/result")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result == {"version": 1, "available": False, "reason": "not_completed", "content": None}
    listing = await client.get("/api/work/v1/operations")
    assert listing.status_code == 200
    assert all("result" not in row for row in listing.json()["operations"])
    assert (len(op.physical), len(op.model_calls)) == before


async def test_registered_submission_refuses_unqualified_user_configuration_without_work(integrated):
    op, runner, client = integrated
    await op.executor.orch._llm_store.clear(op.owner)
    before = len(op.physical), len(op.model_calls)
    response = await client.post("/api/work/v1/operations",
        content=research_command(SimpleNamespace(assignments=runner.service)))
    assert response.status_code == 503, response.text
    assert response.json() == {"error": "work_research_profile_unavailable"}
    events, _ = await asyncio.to_thread(op.audit._repo.list_for_user, op.owner)
    assert not any(event.action_type == "work.accept" for event in events)
    assert (len(op.physical), len(op.model_calls)) == before
    assert "synthetic" not in json.dumps(response.json())


@pytest.mark.parametrize("change", ["config_clear", "foreign", "logout_during_delivery"])
async def test_completed_result_requires_current_read_identity_without_reusing_execution_authority(
    integrated, fixture, monkeypatch, change,
):
    op, runner, client = integrated
    accepted = await client.post("/api/work/v1/operations",
        content=research_command(SimpleNamespace(assignments=runner.service)))
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    await finished(client, identity)
    path = "/api/work/v1/operations/" + identity + "/result"
    if change == "config_clear":
        await op.executor.orch._llm_store.clear(op.owner)
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["result"]["available"] is True
    elif change == "foreign":
        response = await client.get(path, headers={
            "authorization": "Bearer " + fixture[3](sub="different-owner")})
        assert response.status_code == 404
        assert response.json() == {"error": "work_not_found"}
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        original = runner.store.transaction

        async def held(callback, **kwargs):
            value = await original(callback, **kwargs)
            if isinstance(value, dict) and value.get("id") == identity and "result" in value:
                entered.set()
                await release.wait()
            return value

        monkeypatch.setattr(runner.store, "transaction", held)
        pending = asyncio.create_task(client.get(path))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.to_thread(op.sessions.delete, op.sid)
            release.set()
            response = await asyncio.wait_for(pending, 5)
            assert response.status_code == 401
            assert response.json() == {"error": "work_authentication_required"}
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    if change != "config_clear":
        assert "Public release 088" not in response.text and identity not in response.text
    assert len(op.model_calls) == 1
