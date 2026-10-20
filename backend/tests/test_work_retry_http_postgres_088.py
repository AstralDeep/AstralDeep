"""Tests for work retry accounting (backend/llm_config/research_profile.py,
backend/shared/isolated_http.py): known-usage failures retry with fresh sources;
ambiguous or HTTP 503 loss keeps its charge and never retries.
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from llm_config import research_profile as profile
from llm_config.tests.test_research_profile_088 import reply
from persistent_agents.runtime_values import canonical, thaw
from shared.isolated_http import IsolatedHttpError
from tests.test_work_research_preflight_postgres_088 import research_command
from tests.test_work_runtime_postgres_088 import (
    fast_runner_cycles as fast_runner_cycles,
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
def controlled_runner(monkeypatch):
    from astralplane.repositories import assignments
    from persistent_agents.runner import AssignmentRunner

    origins = []
    clock = assignments._now

    def observed_clock(transaction):
        now = clock(transaction)
        origins.append(now)
        return now

    async def manually_scheduled(self):
        while not self._stopping:
            self._wake.clear()
            await self._wake.wait()

    monkeypatch.setattr(AssignmentRunner, "run", manually_scheduled)
    monkeypatch.setattr(assignments, "_now", observed_clock)
    return origins


async def tick_to_settled(runner):
    await runner.tick()
    await asyncio.gather(*tuple(runner._active.values()))
    await asyncio.sleep(0)
    assert not runner._active


async def settle_claim(op, runner, identity):
    await tick_to_settled(runner)
    row, _, _ = await stored(op, identity)
    if row["claim_token"] is not None:
        assert row["phase"] in {"checking", "failed", "reconciliation"}
        def expire():
            with op.runtime.transaction() as tx:
                current = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (identity,))
                data = thaw(current["data"])
                due = tx.fetch_one("SELECT clock_timestamp() - interval '1 second' AS due")["due"]
                data["lease_expires_at"] = due.isoformat()
                tx.execute("UPDATE persistent_assignment SET data=%s::jsonb,lease_expires_at=%s WHERE id=%s",
                           (canonical(data), due, identity))
        await asyncio.to_thread(expire)
        await tick_to_settled(runner)


async def schedule_retry(op, identity, *, due):
    def change():
        with op.runtime.transaction() as tx:
            row = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (identity,))
            data = thaw(row["data"])
            now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
            selected = now + (timedelta(seconds=-1) if due else timedelta(days=1))
            data.update(next_retry_at=selected.isoformat(), next_wake_at=selected.isoformat())
            tx.execute("UPDATE persistent_assignment SET data=%s::jsonb,next_wake_at=%s WHERE id=%s",
                       (canonical(data), selected, identity))
            return selected
    return await asyncio.to_thread(change)


async def retry_after_early_tick(op, runner, identity, sends, first_actions):
    await schedule_retry(op, identity, due=False)
    await tick_to_settled(runner)
    early, actions, _ = await stored(op, identity)
    assert len(sends) == 1 and actions == first_actions
    assert early["consecutive_failures"] == 1
    due = await schedule_retry(op, identity, due=True)
    await tick_to_settled(runner)
    return due


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


async def database_now(op):
    def read():
        with op.runtime.transaction() as tx:
            return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    return await asyncio.to_thread(read)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def wait_for(op, identity, predicate, *, seconds=35):
    async with asyncio.timeout(seconds):
        while True:
            value = await stored(op, identity)
            if predicate(*value):
                return value
            await asyncio.sleep(0.05)


async def test_known_usage_failure_retries_same_task_with_fresh_charged_sources(
    integrated, monkeypatch, record_testsuite_property, controlled_runner,
):
    op, runner, client = integrated
    assert runner._loop is not None and not runner._loop.done()
    prior_reads = len(op.physical)
    sends = []

    async def transport(method, url, **kwargs):
        sends.append(await database_now(op))
        op.model_calls.append((method, url, kwargs))
        response = reply(selection=["unavailable-passage"] if len(sends) == 1 else None)
        return SimpleNamespace(body=json.dumps(response).encode(), status_code=200)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    body = retry_command(runner)
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    await settle_claim(op, runner, identity)
    scheduled, first_actions, _ = await stored(op, identity)
    due = timestamp(scheduled["next_retry_at"])
    assert due - timedelta(seconds=5) in controlled_runner
    assert timestamp(scheduled["next_retry_at"]) == due
    assert scheduled["next_retry_at"] == scheduled["next_wake_at"]
    assert scheduled["consecutive_failures"] == 1
    assert len(first_actions) == 2 and len(sends) == 1
    assert scheduled["usage"]["spent"]["tokens"] == 120
    assert scheduled["usage"]["outstanding"]["tokens"] == 0
    due = await retry_after_early_tick(op, runner, identity, sends, first_actions)
    final, actions, _ = await stored(op, identity)
    assert final["lifecycle"] == "completed"
    assert final["operation"]["terminal_outcome"] == "completed"
    assert final["consecutive_failures"] == 1
    assert len(sends) == 2 and sends[1] >= due
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
    record_testsuite_property(
        "seconds_between_model_sends", (sends[1] - sends[0]).total_seconds())


async def test_pre_send_provider_unreachable_retries_with_fresh_source(
    integrated, monkeypatch, record_testsuite_property, controlled_runner,
):
    op, runner, client = integrated
    assert runner._loop is not None and not runner._loop.done()
    prior_reads = len(op.physical)
    sends = []

    async def transport(method, url, **kwargs):
        sends.append(await database_now(op))
        op.model_calls.append((method, url, kwargs))
        if len(sends) == 1:
            raise IsolatedHttpError("egress_blocked")
        return SimpleNamespace(body=json.dumps(reply()).encode(), status_code=200)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    body = retry_command(runner)
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    await settle_claim(op, runner, identity)
    scheduled, first_actions, _ = await stored(op, identity)
    due = timestamp(scheduled["next_retry_at"])
    assert due - timedelta(seconds=5) in controlled_runner
    assert timestamp(scheduled["next_retry_at"]) == due
    assert scheduled["next_retry_at"] == scheduled["next_wake_at"]
    assert scheduled["consecutive_failures"] == 1
    assert scheduled["lifecycle"] == "active" and scheduled["phase"] == "failed"
    assert scheduled["safe_error_code"] != "assignment_action_uncertain"
    assert len(first_actions) == 2 and len(sends) == 1
    assert scheduled["usage"]["spent"].get("tokens", 0) == 0
    assert scheduled["usage"]["spent"]["tool_calls"] == 1
    assert scheduled["usage"]["spent"].get("model_calls", 0) == 0
    assert all(value == 0 for value in scheduled["usage"]["outstanding"].values())
    first_model = next(a for a in first_actions if a["intent"]["request"]["kind"] == "model")
    assert first_model["state"] == "failed" and len(first_model["attempts"]) == 1
    settled = first_model["attempts"][0]["outcome"]
    assert settled["outcome"] == "failed"
    assert settled["actual"]["tokens"] == 0 and settled["actual"]["model_calls"] == 0
    due = await retry_after_early_tick(op, runner, identity, sends, first_actions)
    final, actions, _ = await stored(op, identity)
    assert final["lifecycle"] == "completed"
    assert final["operation"]["terminal_outcome"] == "completed"
    assert final["consecutive_failures"] == 1
    assert len(sends) == 2 and sends[1] >= due
    assert len(op.physical) == prior_reads + 2
    reads = [a for a in actions if a["intent"]["request"]["kind"] == "tool"]
    models = [a for a in actions if a["intent"]["request"]["kind"] == "model"]
    assert len(reads) == len(models) == 2
    assert len({a["action_id"] for a in reads}) == 2
    assert len({a["intent"]["action_key"] for a in reads}) == 2
    assert len({a["intent"]["action_key"] for a in models}) == 2
    assert all(len(a["attempts"]) == 1 for a in actions)
    assert sorted(a["state"] for a in models) == ["failed", "succeeded"]
    assert final["usage"]["spent"]["tokens"] == 120
    assert final["usage"]["spent"]["model_calls"] == 1
    assert final["usage"]["spent"]["tool_calls"] == 2
    assert all(value == 0 for value in final["usage"]["outstanding"].values())
    result = await client.get(f"/api/work/v1/operations/{identity}/result")
    assert result.status_code == 200 and result.json()["result"]["content"] is None
    replay = await client.post("/api/work/v1/operations", json=body)
    assert replay.status_code == 200 and replay.json()["id"] == identity
    await runner.tick()
    assert len(sends) == 2 and len(op.physical) == prior_reads + 2
    serialized = json.dumps(thaw([final, actions]))
    for text in ("Public release 088", "synthetic-provider-key", '"messages"', "egress_blocked"):
        assert text not in serialized
    assert await asyncio.to_thread(op.audit._repo.verify_chain, op.owner) is None
    record_testsuite_property("pre_send_safe_error", scheduled["safe_error_code"])
    record_testsuite_property(
        "seconds_between_model_sends", (sends[1] - sends[0]).total_seconds())


@pytest.mark.parametrize("code", ["unreachable", "deadline", "cleanup_uncertain", "child_failure"])
async def test_ambiguous_transport_loss_after_permit_stays_uncertain(integrated, monkeypatch, code):
    op, runner, client = integrated
    assert runner._loop is not None and not runner._loop.done()
    prior_reads = len(op.physical)

    async def transport(method, url, **kwargs):
        op.model_calls.append((method, url, kwargs))
        raise IsolatedHttpError(code)

    monkeypatch.setattr("shared.isolated_http.request", transport)
    body = retry_command(runner)
    response = await client.post("/api/work/v1/operations", json=body)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    await settle_claim(op, runner, identity)
    row, actions, _ = await stored(op, identity)
    assert row["phase"] == "reconciliation" and row["claim_token"] is None
    assert row["lifecycle"] == "active" and row["next_retry_at"] is None
    assert row["safe_error_code"] == "assignment_action_uncertain"
    assert row["usage"]["outstanding"]["tokens"] == profile.RESERVED_TOKENS
    assert row["usage"]["spent"].get("tokens", 0) == 0
    model = next(a for a in actions if a["intent"]["request"]["kind"] == "model")
    assert model["state"] == "uncertain" and len(model["attempts"]) == 1
    await runner.tick()
    assert len(op.model_calls) == 1 and len(op.physical) == prior_reads + 1


async def test_http_503_keeps_unknown_charge_and_never_retries_an_issued_effect(
    integrated, monkeypatch,
):
    op, runner, client = integrated
    assert runner._loop is not None and not runner._loop.done()
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
    await settle_claim(op, runner, identity)
    row, actions, _ = await stored(op, identity)
    assert row["phase"] == "reconciliation" and row["claim_token"] is None
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
