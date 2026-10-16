"""Tests completion and subscription listing beyond unrelated operation history.
Real PostgreSQL rows and the guarded Work interface preserve bounded source fanout.
"""

from uuid import uuid4

import pytest
from tests.test_work_subscriptions_postgres_088 import (
    _issue, _operation, _read, _subscribe, _env as _env,
    audit as audit, credentials as credentials, fixture as fixture, plane as plane,
    signing_key as signing_key, work as work,
)


runtime = plane

def unrelated_history(work, template):
    with work.runtime.transaction() as tx:
        tx.execute(
            "INSERT INTO persistent_assignment(id,owner_user_id,submission_id,submission_digest,"
            "lifecycle,next_wake_at,lease_expires_at,state_version,data,execution_profile) "
            "SELECT ('00000000-0000-4000-8000-'||lpad(to_hex(n),12,'0'))::uuid,owner_user_id,"
            "('10000000-0000-4000-8000-'||lpad(to_hex(n),12,'0'))::uuid,submission_digest,"
            "'completed',NULL,NULL,state_version,"
            "jsonb_set(jsonb_set(jsonb_set(jsonb_set(data,'{lifecycle}',to_jsonb('completed'::text)),"
            "'{next_wake_at}','null'::jsonb),'{lease_expires_at}','null'::jsonb),'{assignment_id}',to_jsonb("
            "'00000000-0000-4000-8000-'||lpad(to_hex(n),12,'0'))),execution_profile "
            "FROM persistent_assignment CROSS JOIN generate_series(1,1001) n WHERE id=%s",
            (template.assignment_id,))
        assert tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment WHERE owner_user_id=%s",
                            (work.owner,))["n"] > 1000


@pytest.mark.asyncio
async def test_unrelated_history_does_not_block_terminal_completion_without_subscribers(work):
    caller = _issue(work)
    template = _operation(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Unrelated completion", instructions="Read public data.")
    unrelated_history(work, template)
    result = await work.ops.cancel(caller, operation_id=source["id"],
                                   expected_revision=source["revision"], submission_id=str(uuid4()))
    assert result["applied"] is True
    assert (await _read(work, source["id"])).assignment.lifecycle == "stopped"


@pytest.mark.asyncio
async def test_distant_waiter_is_listed_and_woken_after_unrelated_history(work):
    caller = _issue(work)
    template = _operation(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Distant completion", instructions="Read public data.")
    target = _operation(work)
    unrelated_history(work, template)
    await _subscribe(work, caller, source["id"], target)
    listed = await work.ops.subscriptions(caller, limit=50)
    assert [row["target_operation_id"] for row in listed["subscriptions"]] == [target.assignment_id]
    assert listed["next_after_id"] is None
    await work.ops.cancel(caller, operation_id=source["id"],
                          expected_revision=source["revision"], submission_id=str(uuid4()))
    assert (await _read(work, target)).assignment.phase == "waiting"


@pytest.mark.asyncio
async def test_listing_returns_scan_cursor_after_full_page_of_unrecognized_completion_keys(work):
    from persistent_agents.runtime_values import digest

    caller = _issue(work)
    source, template, target = [_operation(work) for _ in range(3)]
    await _subscribe(work, caller, source, target)
    with work.runtime.transaction() as tx:
        work.runtime.repositories.assignments.set_owner_event_wait(
            tx, owner_id=work.owner, assignment_id=template.assignment_id,
            expected_state_version=template.state_version,
            expected_instruction_revision=template.instruction_revision,
            expected_control_epoch=template.control_epoch, submission_id=str(uuid4()),
            submission_digest=digest("Historical unrecognized source"),
            event_key="operation:unrecognized-source", source_revision=1)
    unrelated_history(work, template)
    first = await work.ops.subscriptions(caller, limit=50)
    assert first["subscriptions"] == []
    assert first["next_after_id"] is not None
    second = await work.ops.subscriptions(caller, limit=50, after_id=first["next_after_id"])
    assert [row["target_operation_id"] for row in second["subscriptions"]] == [target.assignment_id]
    assert second["next_after_id"] is None


@pytest.mark.asyncio
async def test_relevant_fanout_counts_both_completion_aliases_and_replays_do_not_consume_capacity(work, monkeypatch):
    from orchestrator import work_subscriptions
    from persistent_agents.models import AssignmentError
    from persistent_agents.runtime_values import digest

    monkeypatch.setattr(work_subscriptions, "_MAX_EVENT_WAITERS", 2, raising=False)
    caller = _issue(work)
    source, first, second, refused = [_operation(work) for _ in range(4)]
    await _subscribe(work, caller, source, first)
    with work.runtime.transaction() as tx:
        work.runtime.repositories.assignments.set_owner_event_wait(
            tx, owner_id=work.owner, assignment_id=second.assignment_id,
            expected_state_version=second.state_version,
            expected_instruction_revision=second.instruction_revision,
            expected_control_epoch=second.control_epoch, submission_id=str(uuid4()),
            submission_digest=digest("alternate owner-confirmed completion key"),
            event_key="completion:" + source.assignment_id, source_revision=1)
    with pytest.raises(AssignmentError, match="work_completion_capacity_exhausted"):
        await _subscribe(work, caller, source, refused)
    assert (await _read(work, refused)).assignment.operation.get("control", {}).get("wait") is None
    assert (await _subscribe(work, caller, source, first))["applied"] is False


@pytest.mark.asyncio
async def test_closed_matching_wait_history_cannot_block_source_or_new_subscriber(work):
    from persistent_agents.runtime_values import digest

    caller = _issue(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Open source", instructions="Read public data.")
    template, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source["id"], template)
    current = (await _read(work, template)).assignment
    with work.runtime.transaction() as tx:
        work.runtime.repositories.assignments.apply_control(
            tx, owner_id=work.owner, assignment_id=current.assignment_id,
            expected_state_version=current.state_version,
            expected_instruction_revision=current.instruction_revision,
            expected_control_epoch=current.control_epoch, submission_id=str(uuid4()),
            submission_digest=digest("Close synthetic prior wait"), control="stop")
    unrelated_history(work, template)
    assert (await _subscribe(work, caller, source["id"], target))["applied"] is True
    result = await work.ops.cancel(caller, operation_id=source["id"],
                                   expected_revision=source["revision"], submission_id=str(uuid4()))
    assert result["applied"] is True
    assert (await _read(work, target)).assignment.phase == "waiting"


@pytest.mark.asyncio
async def test_relevant_fanout_is_bounded_and_replays_do_not_consume_capacity(work, monkeypatch):
    from orchestrator import work_subscriptions
    from persistent_agents.models import AssignmentError

    monkeypatch.setattr(work_subscriptions, "_MAX_EVENT_WAITERS", 2, raising=False)
    caller = _issue(work)
    source, first, second, refused = [_operation(work) for _ in range(4)]
    await _subscribe(work, caller, source, first)
    await _subscribe(work, caller, source, second)
    with pytest.raises(AssignmentError, match="work_completion_capacity_exhausted"):
        await _subscribe(work, caller, source, refused)
    assert (await _read(work, refused)).assignment.operation.get("control", {}).get("wait") is None
    assert (await _subscribe(work, caller, source, first))["applied"] is False

