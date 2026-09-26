"""Tests for work_continuation_authority.py's original-operation composition:
same-issuance refresh requires causal proof, a valid refresh without that proof is
not adopted, and distinct callers each keep their own required session.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
from uuid import uuid4

import pytest

from orchestrator.work_continuation_authority import refresh_operation_control_authority
from persistent_agents.models import AssignmentError
from tests.test_operation_session_authority_088 import create_operation
from tests.test_work_continuation_authority_088 import control
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, selected,
)

pytestmark = pytest.mark.asyncio


async def original(fixture, record, guard, *, causal=True):
    return await refresh_operation_control_authority(
        context=guard.context, original=record, command="resume", sessions=fixture[0],
        expected_request_credential=guard.require_session().credential if causal else None)


async def test_same_issuance_uses_only_causally_verified_refresh_and_keeps_deadline(bound, fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    guard = await selected(bound, fixture)
    authority = await original(fixture, paused, guard)
    combined = guard.with_original(authority)
    assert combined.caller.started_at == guard.caller.started_at
    assert combined.caller.valid_until <= guard.caller.valid_until
    assert combined._deadline == guard._deadline and combined._until == guard._until
    assert combined.caller.credential == authority.observation.credential
    assert combined.caller.credential != guard.caller.credential
    with runtime.transaction() as tx:
        combined.assert_current(tx, assignments=bound[0])
    with runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_authentication_required"):
        guard.assert_current(tx, assignments=bound[0])
    await combined.verify_delivery()
    assert len(fixture[-1]) == 1
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        combined.with_original(authority)


async def test_valid_same_session_refresh_without_causal_proof_is_not_adopted(bound, fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    guard = await selected(bound, fixture)
    authority = await original(fixture, paused, guard, causal=False)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        guard.with_original(authority)


async def test_proof_and_private_context_replacements_are_not_equal_by_coercion(bound, fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    guard = await selected(bound, fixture)
    authority = await original(fixture, paused, guard)
    for change in [
        {"request_context": replace(guard.context)},
        {"request_credential": None},
        {"request_credential": replace(guard.caller.credential,
            hard_expires_at=float(guard.caller.credential.hard_expires_at))},
        {"request_credential": replace(guard.caller.credential, session_id="another")},
        {"plane_runtime": object()}, {"command": "pause"},
        {"record": replace(paused, owner_id="other-owner")},
        {"record": replace(paused, operation={**paused.operation, "version": True})},
    ]:
        with pytest.raises(AssignmentError, match="work_authentication_required"):
            guard.with_original(replace(authority, **change))


async def separate(bound, fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    sid = uuid4().hex
    fixture[0].create(sid, user_id=fixture[1], access_token=fixture[3](),
                      refresh_token="synthetic-distinct-B", hard_max_seconds=3600)
    caller_fixture = (fixture[0], fixture[1], sid, fixture[3], fixture[4])
    guard = await selected(bound, caller_fixture)
    authority = await original(fixture, paused, guard, causal=False)
    return sid, guard, authority


async def test_distinct_caller_is_unchanged_and_both_sessions_remain_required(bound, fixture, runtime):
    sid, guard, authority = await separate(bound, fixture, runtime)
    try:
        combined = guard.with_original(authority)
        assert combined.caller is guard.caller
        with runtime.transaction() as tx:
            combined.assert_current(tx, assignments=bound[0])
        with pytest.raises(AssignmentError, match="work_authentication_required"):
            guard.with_original(replace(authority, request_credential=guard.caller.credential))
        fixture[0].delete(fixture[2])
        with runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_authentication_required"):
            combined.assert_current(tx, assignments=bound[0])
        with runtime.transaction() as tx:
            guard.assert_current(tx, assignments=bound[0])
    finally:
        fixture[0].delete(sid)


async def test_caller_observation_expiring_during_original_session_lock_wait_refuses(bound, fixture, runtime):
    sid, guard, authority = await separate(bound, fixture, runtime)
    combined = guard.with_original(authority)
    until = datetime.now(timezone.utc) + timedelta(seconds=.08)
    combined = replace(combined, caller=replace(combined.caller, valid_until=until))
    entered = threading.Event()
    def assertion():
        with runtime.transaction() as tx:
            runtime.repositories.history.sessions.bound_request_execution_waits(tx)
            assert tx.fetch_one("SHOW lock_timeout")["lock_timeout"] == "100ms"
            tx.execute("SET LOCAL lock_timeout = '1s'")
            entered.set()
            combined.assert_current(tx, assignments=bound[0])
    try:
        with runtime.transaction() as blocker:
            blocker.fetch_one("SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (fixture[2],))
            pending = asyncio.create_task(asyncio.to_thread(assertion))
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(max(0, until.timestamp() - time.time()) + .02)
            assert not pending.done()
        with pytest.raises(AssignmentError, match="work_authentication_required"):
            await pending
    finally:
        fixture[0].delete(sid)
