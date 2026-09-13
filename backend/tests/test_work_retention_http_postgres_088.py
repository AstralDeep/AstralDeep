"""Explicit non-retention through HTTP, real supervision and governed dispatch.

External IAM/source/model responses are synthetic; no provider receives data.
The actual runtime, PostgreSQL, encrypted selection, action ledger and audit run.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from persistent_agents.runtime_values import thaw
from tests.test_work_runtime_postgres_088 import (
    fixture as fixture, finished, gate_orchestrator as gate_orchestrator,
    integrated as integrated, operation as operation, plane as plane,
    research as research, signing_key as signing_key,
)
from tests.test_work_research_preflight_postgres_088 import research_command

runtime = plane
pytestmark = [pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


async def test_explicit_nonretained_http_task_completes_without_durable_source_or_model_text(integrated):
    op, runner, client = integrated
    previous_reads = len(op.physical)
    body = json.loads(research_command(SimpleNamespace(assignments=runner.service)))
    body["source_retention"] = "none"
    response = await client.post("/api/work/v1/operations", json=body)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    final = await finished(client, identity)
    assert final["disposition"] == "completed"
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["result"]["available"] is False
    assert result.json()["result"]["content"] is None
    assert "Public release 088" not in result.text
    assert len(op.physical) == previous_reads + 1 and len(op.model_calls) == 1
    assert final["usage"]["spent"]["tokens"] == 120

    def stored():
        with op.runtime.transaction() as tx:
            row = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (identity,))
            actions = tx.fetch_all("SELECT data FROM persistent_assignment_action WHERE assignment_id=%s", (identity,))
            assert row["data"]["operation"]["source_retention"] == "none"
            return json.dumps(thaw([row, actions]))

    serialized = await asyncio.to_thread(stored)
    assert '"source_retention": "none"' in serialized
    for text in ("Public release 088", "synthetic-provider-key", '"messages"'):
        assert text not in serialized
    replay = await client.post("/api/work/v1/operations", json=body)
    assert replay.status_code == 200 and replay.json()["id"] == identity
    assert replay.json()["created"] is False
    await runner.tick()
    assert len(op.physical) == previous_reads + 1 and len(op.model_calls) == 1
    changed = {**body, "source_retention": "operation"}
    conflict = await client.post("/api/work/v1/operations", json=changed)
    assert conflict.status_code == 409
    assert "Public release 088" not in conflict.text
    assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None


async def test_invalid_retention_shapes_never_refresh_or_dispatch(integrated, fixture):
    op, runner, client = integrated
    before = len(op.physical), len(op.model_calls), len(fixture[-1])
    base = json.loads(research_command(SimpleNamespace(assignments=runner.service)))
    for value in (None, True, 1, [], {}, "persistent", "NONE"):
        response = await client.post("/api/work/v1/operations", json={**base, "source_retention": value})
        assert response.status_code == 422, response.text
        assert response.json() == {"error": "work_submit_invalid"}
    extra = await client.post("/api/work/v1/operations", json={
        **base, "source_retention": "none", "retained_text": "forbidden-private-body"})
    assert extra.status_code == 422
    assert "forbidden-private-body" not in extra.text
    assert (len(op.physical), len(op.model_calls), len(fixture[-1])) == before
    events, _ = await asyncio.to_thread(op.audit._repo.list_for_user, op.owner)
    assert not any(event.action_type == "work.accept" for event in events)
