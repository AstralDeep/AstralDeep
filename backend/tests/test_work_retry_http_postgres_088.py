"""Real HTTP/supervisor/PG retry accounting with synthetic final transports.

A successful provider envelope with unusable selection has known consumption;
HTTP 503 does not. These cases must not share an invented zero-charge retry path.
No provider receives data, and no clock, claim, receipt or due time is rewritten.
"""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from llm_config import research_profile as profile
from llm_config.tests.test_research_profile_088 import reply
from persistent_agents.runtime_values import thaw
from tests.test_work_research_preflight_postgres_088 import research_command
from tests.test_work_runtime_postgres_088 import (
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]


@pytest.fixture(autouse=True)
def bounded_supervisor(monkeypatch):
    # Existing operator-supported bounds, preserving the actual 5-second retry.
    monkeypatch.setenv("PERSISTENT_AGENTS_TICK_SECONDS", "1")
    monkeypatch.setenv("PERSISTENT_AGENTS_LEASE_SECONDS", "15")


def retry_command(runner):
    source = runner.service.tool_bound("web-research-1:fetch_page")
    body = json.loads(research_command(
        SimpleNamespace(assignments=runner.service),
        model_calls=2 * (source["model_calls"] + 1),
        tool_calls=2 * source["tool_calls"],
        tokens=2 * (source["tokens"] + profile.RESERVED_TOKENS),
        elapsed_ms=2 * (source["elapsed_ms"] + profile.RESERVED_MILLISECONDS),
        max_retries=1,
    ))
    return {**body, "source_retention": "none"}


async def stored(op, identity):
    def read():
        with op.runtime.transaction() as tx:
            row = tx.fetch_one(
                "SELECT data FROM persistent_assignment WHERE id=%s", (identity,)
            )["data"]
            actions = [item["data"] for item in tx.fetch_all(
                "SELECT data FROM persistent_assignment_action WHERE assignment_id=%s",
                (identity,),
            )]
            now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
            return row, actions, now
    return await asyncio.to_thread(read)


async def wait_for(op, identity, predicate, *, seconds=35):
    async with asyncio.timeout(seconds):
        while True:
            value = await stored(op, identity)
            if predicate(*value):
                return value
            await asyncio.sleep(0.05)


async def test_known_usage_failure_retries_same_task_with_fresh_charged_sources(
    integrated, monkeypatch, record_testsuite_property,
):
    op, runner, client = integrated
    prior_reads = len(op.physical)
    sends = []

    async def transport(method, url, **kwargs):
        sends.append(time.monotonic())
        op.model_calls.append((method, url, kwargs))
        # One unusable but authentic, billed completion; the next reply is valid.
        response = reply(selection=["unavailable-passage"] if len(sends) == 1 else None)
        return SimpleNamespace(body=json.dumps(response).encode(), status_code=200)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    body = retry_command(runner)
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    scheduled, first_actions, now = await wait_for(
        op, identity, lambda row, _actions, _now: row["next_retry_at"] is not None
    )
    due = datetime.fromisoformat(scheduled["next_retry_at"].replace("Z", "+00:00"))
    assert 4 <= (due - now).total_seconds() <= 5
    assert scheduled["consecutive_failures"] == 1
    assert len(first_actions) == 2 and len(sends) == 1
    assert scheduled["usage"]["spent"]["tokens"] == 120
    assert scheduled["usage"]["outstanding"]["tokens"] == 0
    final, actions, _ = await wait_for(
        op, identity, lambda row, _actions, _now: row["lifecycle"] == "completed"
    )
    assert final["operation"]["terminal_outcome"] == "completed"
    assert final["consecutive_failures"] == 1
    assert len(sends) == 2 and sends[1] - sends[0] >= 5
    assert len(op.physical) == prior_reads + 2
    reads = [a for a in actions if a["intent"]["request"]["kind"] == "tool"]
    models = [a for a in actions if a["intent"]["request"]["kind"] == "model"]
    assert len(reads) == len(models) == 2
    assert len({a["action_id"] for a in reads}) == 2
    assert len({a["intent"]["action_key"] for a in reads}) == 2
    assert len({a["intent"]["action_key"] for a in models}) == 2
    assert all(len(a["attempts"]) == 1 for a in actions)
    assert sorted(a["state"] for a in models) == ["failed", "succeeded"]
    assert final["usage"]["spent"]["tokens"] == 240
    assert final["usage"]["spent"]["tool_calls"] == 2
    assert all(value == 0 for value in final["usage"]["outstanding"].values())
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.json()["result"]["content"] is None
    replay = await client.post("/api/work/v1/operations", json=body)
    assert replay.status_code == 200 and replay.json()["id"] == identity
    assert replay.json()["created"] is False
    await runner.tick()
    assert len(sends) == 2 and len(op.physical) == prior_reads + 2
    serialized = json.dumps(thaw([final, actions]))
    for text in ("Public release 088", "synthetic-provider-key", '"messages"'):
        assert text not in serialized
    assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None
    record_testsuite_property("first_retry_safe_error", scheduled["safe_error_code"])
    record_testsuite_property("seconds_between_model_sends", sends[1] - sends[0])


async def test_http_503_keeps_unknown_charge_and_never_retries_an_issued_effect(
    integrated, monkeypatch,
):
    op, runner, client = integrated
    prior_reads = len(op.physical)

    async def transport(method, url, **kwargs):
        op.model_calls.append((method, url, kwargs))
        return SimpleNamespace(
            body=b'{"error":{"message":"synthetic temporary outage"}}', status_code=503
        )

    monkeypatch.setattr("shared.isolated_http.request", transport)
    body = retry_command(runner)
    response = await client.post("/api/work/v1/operations", json=body)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    row, actions, _ = await wait_for(
        op, identity, lambda row, _actions, _now: (
            row["phase"] == "reconciliation" and row["claim_token"] is None
        )
    )
    assert row["lifecycle"] == "active" and row["next_retry_at"] is None
    assert row["safe_error_code"] == "assignment_action_uncertain"
    assert row["usage"]["outstanding"]["tokens"] == profile.RESERVED_TOKENS
    assert row["usage"]["spent"].get("tokens", 0) == 0
    assert len(actions) == 2
    model = next(a for a in actions if a["intent"]["request"]["kind"] == "model")
    assert model["state"] == "uncertain" and len(model["attempts"]) == 1
    assert model["attempts"][0]["outcome"]["actual"] is None
    replay = await client.post("/api/work/v1/operations", json=body)
    assert replay.status_code == 200 and replay.json()["id"] == identity
    await runner.tick()
    assert len(op.model_calls) == 1 and len(op.physical) == prior_reads + 1
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.json()["result"]["content"] is None
    serialized = json.dumps(thaw([row, actions]))
    assert "synthetic temporary outage" not in serialized
    assert "Public release 088" not in serialized
