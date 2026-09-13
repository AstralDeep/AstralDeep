"""Required Work control audit is committed with its real Plane mutation."""

import asyncio
from datetime import UTC, datetime
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralplane.async_runtime import AsyncPlaneRuntime

from tests.helpers.work_control_caller import current_control_caller
from tests.test_request_session_authority_088 import signing_key as signing_key

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate
from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
from persistent_agents.models import AssignmentError
from persistent_agents.tests import test_engine_postgres as engine_fixtures
from tests.test_work_service_postgres_088 import records as records
from persistent_agents.tests.test_engine_postgres import plane as plane

secondary_plane = engine_fixtures.plane


@pytest.fixture
async def audited(records, monkeypatch, signing_key):
    runtime, reads, identities, _legacy = records
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-control-audit-key")
    audit = AuditRepository(plane_runtime=runtime)
    reads.assignments.orch.audit_repo = audit
    # A secondary asynchronous audit sink must not be required or duplicated.
    legacy = AsyncMock()
    monkeypatch.setattr(reads.assignments, "_audit", legacy)
    async with current_control_caller(reads.assignments, monkeypatch, signing_key) as caller:
        yield SimpleNamespace(runtime=runtime, reads=reads, identity=identities[0],
            service=WorkControlService(reads.assignments), audit=audit, legacy=legacy, caller=caller)


def request(revision):
    return WorkControlRequest(expected_revision=revision, submission_id=str(uuid4()))


@pytest.mark.asyncio
async def test_pause_and_replay_have_one_atomic_audit_record(audited):
    value = audited
    body = request(1)
    response = await value.service.control("owner", value.caller.context.claims, value.identity, "pause", body, caller=value.caller)
    assert response["applied"] is True
    replay = await value.service.control("owner", value.caller.context.claims, value.identity, "pause", body, caller=value.caller)
    assert replay["applied"] is False
    events, cursor = await asyncio.to_thread(value.audit.list_for_user, "owner")
    assert cursor is None and len(events) == 1
    assert events[0].action_type == "assignment_pause"
    assert events[0].outputs_meta == {"assignment_id": value.identity,
        "instruction_revision": 1, "control_epoch": response["operation"]["control_epoch"]}
    assert await asyncio.to_thread(value.audit.verify_chain, "owner") is None
    value.legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_pause_and_command_receipt(audited, monkeypatch):
    value = audited
    body = request(1)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic private audit diagnostic")

    monkeypatch.setattr(value.audit, "insert_in_transaction", unavailable)
    with pytest.raises(AssignmentError) as failure:
        await value.service.control("owner", value.caller.context.claims, value.identity, "pause", body, caller=value.caller)
    assert failure.value.status_code == 503
    current = await value.reads.get("owner", {"sub": "owner"}, value.identity)
    assert current["revision"] == 1 and current["lifecycle"] == "active"
    assert (await asyncio.to_thread(value.audit.list_for_user, "owner"))[0] == []
    value.legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_cancel_delete_preserve_chain_and_do_not_copy_work_content(audited):
    value = audited
    revision = 1
    for command in ("pause", "cancel"):
        result = await value.service.control("owner", value.caller.context.claims, value.identity,
                                             command, request(revision), caller=value.caller)
        revision = result["operation"]["revision"]
    deleted = await value.service.delete("owner", value.caller.context.claims, value.identity,
                                         WorkDeleteRequest(expected_revision=revision), caller=value.caller)
    assert deleted == {"id": value.identity, "deleted": True}
    events, _ = await asyncio.to_thread(value.audit.list_for_user, "owner")
    assert {row.action_type for row in events} == {"assignment_pause", "assignment_stop", "assignment_delete"}
    assert len(events) == 3
    assert "private instruction" not in str(events) and "Public task title" not in str(events)
    assert await asyncio.to_thread(value.audit.verify_chain, "owner") is None
    value.legacy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel", "delete"])
@pytest.mark.parametrize("failure", ["before_append", "after_append", "bad_result", "coroutine"])
async def test_required_audit_failure_preserves_state_receipt_and_chain(audited, monkeypatch, command, failure):
    value = audited
    revision = 1
    if command == "delete":
        stopped = await value.service.control("owner", value.caller.context.claims, value.identity, "cancel", request(1), caller=value.caller)
        revision = stopped["operation"]["revision"]
    before = await value.reads.get("owner", {"sub": "owner"}, value.identity)
    prior, _ = await asyncio.to_thread(value.audit.list_for_user, "owner")
    original = value.audit.insert_in_transaction
    coroutines = []

    async def invalid_async():
        raise AssertionError("asynchronous audit must not run")

    def refused(*args, **kwargs):
        if failure == "coroutine":
            result = invalid_async()
            coroutines.append(result)
            return result
        if failure != "before_append":
            original(*args, **kwargs)
        if failure == "bad_result":
            return None
        raise RuntimeError("private unavailable audit detail")

    body = request(revision)
    monkeypatch.setattr(value.audit, "insert_in_transaction", refused)
    with pytest.raises(AssignmentError) as denied:
        if command == "delete":
            await value.service.delete("owner", value.caller.context.claims, value.identity,
                                       WorkDeleteRequest(expected_revision=revision), caller=value.caller)
        else:
            await value.service.control("owner", value.caller.context.claims, value.identity, command, body, caller=value.caller)
    assert denied.value.status_code == 503 and "private" not in str(denied.value)
    assert await value.reads.get("owner", {"sub": "owner"}, value.identity) == before
    assert (await asyncio.to_thread(value.audit.list_for_user, "owner"))[0] == prior
    assert all(coroutine.cr_frame is None for coroutine in coroutines)
    monkeypatch.setattr(value.audit, "insert_in_transaction", original)
    if command != "delete":
        # A rolled-back command did not leave a success receipt behind.
        assert (await value.service.control("owner", value.caller.context.claims, value.identity,
                                            command, body, caller=value.caller))["applied"] is True
    assert await asyncio.to_thread(value.audit.verify_chain, "owner") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "replaced", "wrong_runtime", "wrong_repository", "after_append"])
async def test_exact_audit_composition_is_required_at_commit(audited, monkeypatch, change):
    value = audited
    if change == "missing":
        value.service.audit.audit = None
    elif change == "replaced":
        monkeypatch.setattr(value.reads.assignments.orch, "audit_repo", object())
    elif change == "wrong_runtime":
        value.service.audit.runtime = object()
    elif change == "wrong_repository":
        monkeypatch.setattr(value.audit._audit, "repository", object())
    else:
        original = value.audit.insert_in_transaction

        def replace_after_append(*args, **kwargs):
            result = original(*args, **kwargs)
            value.reads.assignments.orch.audit_repo = object()
            return result

        monkeypatch.setattr(value.audit, "insert_in_transaction", replace_after_append)
    with pytest.raises(AssignmentError) as denied:
        await value.service.control("owner", value.caller.context.claims, value.identity, "pause", request(1), caller=value.caller)
    assert denied.value.status_code == 503
    assert (await value.reads.get("owner", {"sub": "owner"}, value.identity))["revision"] == 1


@pytest.mark.asyncio
async def test_accepted_replay_needs_no_new_audit_and_lost_ack_can_only_acknowledge(audited, monkeypatch):
    value = audited
    body = request(1)
    original = value.reads.store.transaction
    calls = 0

    async def lost_ack(callback, **kwargs):
        nonlocal calls
        calls += 1
        await original(callback, **kwargs)
        raise AssignmentError("assignment_transaction_unavailable", 503)

    monkeypatch.setattr(value.reads.store, "transaction", lost_ack)
    with pytest.raises(AssignmentError):
        await value.service.control("owner", value.caller.context.claims, value.identity, "pause", body, caller=value.caller)
    assert calls == 1
    monkeypatch.setattr(value.reads.store, "transaction", original)
    def no_new_audit(*_args, **_kwargs):
        raise AssertionError("accepted replay must not append another audit event")

    monkeypatch.setattr(value.audit, "insert_in_transaction", no_new_audit)
    replay = await value.service.control("owner", value.caller.context.claims, value.identity, "pause", body, caller=value.caller)
    assert replay["applied"] is False and replay["operation"]["revision"] == 2
    assert len((await asyncio.to_thread(value.audit.list_for_user, "owner"))[0]) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_commits_only_one_control_and_audit(audited):
    value = audited
    body = request(1)
    replies = await asyncio.gather(*(value.service.control("owner", value.caller.context.claims,
        value.identity, "pause", body, caller=value.caller) for _ in range(2)))
    assert sorted(reply["applied"] for reply in replies) == [False, True]
    assert len((await asyncio.to_thread(value.audit.list_for_user, "owner"))[0]) == 1
    assert await asyncio.to_thread(value.audit.verify_chain, "owner") is None


@pytest.mark.asyncio
async def test_audit_lock_contention_is_bounded_and_rolls_back_the_control(audited):
    value = audited
    entered, release = threading.Event(), threading.Event()

    def holder():
        now = datetime.now(UTC)
        with value.runtime.transaction() as transaction:
            value.audit.insert_in_transaction(AuditEventCreate(
                actor_user_id="owner", auth_principal="owner", event_class="settings",
                action_type="fixture.audit_lock", description="Synthetic lock fixture",
                correlation_id=str(uuid4()),
                outcome="success", started_at=now, completed_at=now,
            ), transaction=transaction, plane_runtime=value.runtime)
            entered.set()
            assert release.wait(5)

    pending = asyncio.create_task(asyncio.to_thread(holder))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        async with asyncio.timeout(3):
            with pytest.raises(AssignmentError) as denied:
                await value.service.control("owner", value.caller.context.claims, value.identity, "pause", request(1), caller=value.caller)
        assert denied.value.status_code == 503
    finally:
        release.set()
        await pending
    assert (await value.reads.get("owner", {"sub": "owner"}, value.identity))["revision"] == 1
    events, _ = await asyncio.to_thread(value.audit.list_for_user, "owner")
    assert [row.action_type for row in events] == ["fixture.audit_lock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["command", "record", "owner"])
async def test_audit_adapter_cannot_invent_an_owner_command(audited, change):
    value = audited
    record = (await value.reads.store.call("get_operation", owner_id="owner",
                                         assignment_id=value.identity)).assignment

    def refused(transaction, _repository):
        value.service.audit.append(transaction,
            owner_id="other" if change == "owner" else "owner",
            command="invented" if change == "command" else "pause",
            record=None if change == "record" else record)

    with pytest.raises(AssignmentError) as failure:
        await value.reads.store.transaction(refused, bound_session_waits=True)
    assert failure.value.status_code == 503
    assert (await asyncio.to_thread(value.audit.list_for_user, "owner"))[0] == []


@pytest.mark.asyncio
async def test_replaced_real_transaction_adapter_is_refused_before_resource_reads(
    audited, secondary_plane, monkeypatch,
):
    value = audited
    assert secondary_plane is not value.runtime
    wrong = AsyncPlaneRuntime(secondary_plane)
    store = value.reads.store
    original = store.async_runtime
    observed = []
    read_operation = store.repository.get_operation

    def observed_read(*args, **kwargs):
        observed.append(True)
        return read_operation(*args, **kwargs)

    monkeypatch.setattr(store.repository, "get_operation", observed_read)
    monkeypatch.setattr(store, "async_runtime", wrong)
    try:
        with pytest.raises(AssignmentError) as denied:
            await value.service.control("owner", value.caller.context.claims, value.identity, "pause", request(1), caller=value.caller)
        assert denied.value.status_code == 503 and observed == []
    finally:
        store.async_runtime = original
        wrong.close()
    assert (await value.reads.get("owner", {"sub": "owner"}, value.identity))["revision"] == 1
