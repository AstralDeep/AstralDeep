"""Concurrent one-shot acceptance of one caller key against the live runtime.

The integrated runtime fixture supplies real Plane, encrypted configuration,
JWT verification, audit and a running supervisor. Only external IAM replies and
the (unused here) source/model transports are synthetic. Two concurrent
WorkSubmitService.submit calls with the same caller key must add exactly one
persistent_assignment row, one operation receipt and one work.accept audit row.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.work_submit import FixedResearchPreflight, WorkSubmitService
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
from tests.test_work_submit_postgres_088 import context, totals

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]


def submit_service(op, runner):
    """A source-only acceptance service over the integrated runtime's stores."""
    return WorkSubmitService(
        runner.service,
        op.audit._repo,
        op.sessions,
        research_preflight=FixedResearchPreflight(op.executor.orch._llm_store),
    )


def added(base, after):
    """The per-owner (assignment, receipt, audit) delta above the fixture baseline."""
    return tuple(after[index] - base[index] for index in range(3))


async def test_concurrent_same_key_admits_one_receipt_and_audit_atomically(
    integrated, fixture, runtime, monkeypatch
):
    op, runner, client = integrated
    service = submit_service(op, runner)
    base = totals(runtime, fixture[1])
    # One body, submitted twice under two current sessions: the same caller key,
    # source and limits. Two sessions avoid a same-incarnation authority conflict
    # so the race is decided purely by admission idempotency.
    body = research_command(SimpleNamespace(assignments=runner.service))
    second_sid = uuid4().hex
    fixture[0].create(
        second_sid,
        user_id=fixture[1],
        access_token=fixture[3](),
        refresh_token="synthetic-second-refresh",
        hard_max_seconds=3600,
    )
    try:
        first = await context(fixture, runtime, bearer=True)
        alternate = (fixture[0], fixture[1], second_sid, fixture[3], fixture[4])
        second = await context(alternate, runtime, bearer=True)
        barrier, arrived = asyncio.Event(), []
        original = service._definition

        async def definition(*args):
            value = await original(*args)
            arrived.append(True)
            if len(arrived) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 5)
            return value

        monkeypatch.setattr(service, "_definition", definition)
        values = await asyncio.gather(
            service.submit(first, body), service.submit(second, body)
        )
        # Both expanded the intent, but only one admission created durable state.
        assert arrived == [True, True]
        assert sum(value.created for value in values) == 1
        assert values[0].record.assignment_id == values[1].record.assignment_id
        assert values[0].record.operation["version"] == 2
        assert added(base, totals(runtime, fixture[1])) == (1, 1, 1)
        rows, _ = service.audit.list_for_user(fixture[1])
        assert sum(row.action_type == "work.accept" for row in rows) == 1
        assert service.audit.verify_chain(fixture[1]) is None
    finally:
        fixture[0].delete(second_sid)


async def test_replayed_key_after_commit_reuses_the_single_receipt(
    integrated, fixture, runtime
):
    op, runner, client = integrated
    service = submit_service(op, runner)
    base = totals(runtime, fixture[1])
    body = research_command(SimpleNamespace(assignments=runner.service))
    accepted = await service.submit(await context(fixture, runtime), body)
    assert accepted.created
    # A later duplicate acceptance replays the same receipt without a new row.
    replay = await service.submit(
        await context(fixture, runtime, cookie=False, bearer=True), body
    )
    assert not replay.created
    assert replay.record.assignment_id == accepted.record.assignment_id
    assert added(base, totals(runtime, fixture[1])) == (1, 1, 1)
    assert service.audit.verify_chain(fixture[1]) is None
