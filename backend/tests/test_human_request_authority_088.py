"""Current metadata callers retain normal IAM without requiring Work service."""
import importlib
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
import threading
from types import SimpleNamespace

import pytest

from orchestrator import auth
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record

from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, incoming,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("cookie", [True, False])
async def test_human_caller_is_independent_of_work_flag(bound, fixture, runtime, monkeypatch, cookie):
    module = importlib.import_module("orchestrator.human_request_authority")
    orch = bound[0].orch
    monkeypatch.setenv("FF_PERSISTENT_AGENTS", "false")
    monkeypatch.setattr(orch, "persistent_assignments", None)
    boundary = module.HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    try:
        caller = await module.authenticate_current_human_request(
            incoming(bound, fixture, cookie=cookie), boundary=boundary)
        assert type(caller) is module.CurrentHumanCaller
        assert caller.owner_id == fixture[1] and caller.plane_runtime is runtime
        assert caller.require_write() == "POST"
        claims = caller.claims
        claims["sub"] = "different-owner"
        assert caller.claims["sub"] == fixture[1]
        found = await caller.transaction(
            lambda tx, repositories: repositories is runtime.repositories,
            expected_orchestrator=orch)
        assert found is True
        await caller.verify_delivery()
        assert not fixture[-1]
    finally:
        boundary.close()


@pytest.fixture
def human(bound):
    module = importlib.import_module("orchestrator.human_request_authority")
    orch = bound[0].orch
    boundary = module.HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    yield module, boundary, orch
    boundary.close()


async def selected(human, bound, fixture, **kwargs):
    return await human[0].authenticate_current_human_request(
        incoming(bound, fixture, **kwargs), boundary=human[1])


@pytest.mark.parametrize("bearer", [True, False])
async def test_metadata_get_keeps_normal_iam_without_json_or_write_origin(human, bound, fixture, runtime, bearer):
    request = incoming(bound, fixture, method="GET", bearer=bearer)
    request.scope["headers"] = [(k, v) for k, v in request.scope["headers"]
                                 if k not in {b"content-type", b"origin"}]
    caller = await human[0].authenticate_current_human_request(request, boundary=human[1])
    assert caller.runtime is runtime and caller.repositories is runtime.repositories
    assert caller.audit_repo is human[2].audit_repo
    assert caller.require_session().credential.owner_id == fixture[1]
    request.scope["method"] = "POST"
    with pytest.raises(AssignmentError, match="human_write_required"):
        caller.require_write()
    await caller.verify_delivery()
    assert fixture[1] not in repr(caller) and fixture[3]() not in repr(caller)


async def test_metadata_get_does_not_accept_query_token(human, bound, fixture):
    request = incoming(bound, fixture, method="GET")
    request.scope["query_string"] = b"token=never-a-metadata-credential"
    with pytest.raises(AssignmentError) as caught:
        await human[0].authenticate_current_human_request(request, boundary=human[1])
    assert caught.value.status_code == 403
    assert "never-a-metadata-credential" not in str(caught.value)


@pytest.mark.parametrize("options", [
    {"cookie": False, "bearer": False},
    {"extra": [(b"cookie", b"astral_session=forged")]},
    {"method": "PATCH"},
])
async def test_invalid_transport_never_produces_a_caller(human, bound, fixture, options):
    with pytest.raises(AssignmentError):
        await selected(human, bound, fixture, **options)
    assert not fixture[-1]


async def test_bare_bearer_does_not_invent_a_session(human, bound, fixture):
    caller = await selected(human, bound, fixture, cookie=False)
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        caller.require_session()


async def test_same_sid_replacement_cannot_be_adopted(human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    old = get_session_record(runtime, fixture[2])
    replacement = replace_session_record(runtime, old)
    assert replacement.incarnation_id != old.incarnation_id
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.transaction(lambda tx, _: None, expected_orchestrator=human[2])
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.verify_delivery()


async def test_final_caller_loss_rolls_back_actual_session_mutation(human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    old = get_session_record(runtime, fixture[2])

    def mutation(tx, repositories):
        repositories.history.sessions.delete(tx, owner_id=fixture[1], session_id=fixture[2],
            expected_incarnation_id=old.incarnation_id)

    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.transaction(mutation, expected_orchestrator=human[2])
    assert get_session_record(runtime, fixture[2]) == old


@pytest.mark.parametrize("part", ["host", "boundary", "runtime", "catalog", "sessions", "audit", "adapter"])
async def test_rebound_composition_never_runs_metadata_callback(human, bound, fixture, monkeypatch, part):
    caller = await selected(human, bound, fixture)
    boundary, orch = human[1:]
    objects = {
        "host": (bound[1].state, "orchestrator"),
        "boundary": (orch, "human_request_boundary"),
        "runtime": (boundary, "plane_runtime"),
        "catalog": (boundary, "repositories"),
        "sessions": (orch, "web_sessions"),
        "audit": (orch, "audit_repo"),
        "adapter": (boundary, "adapter"),
    }
    touched = []
    # Restore the boundary before its fixture closes its own adapter.
    with monkeypatch.context() as patch:
        patch.setattr(*objects[part], object())
        with pytest.raises(AssignmentError, match="human_request_unavailable"):
            await caller.transaction(lambda tx, _: touched.append(True), expected_orchestrator=orch)
        with pytest.raises(AssignmentError, match="human_request_unavailable"):
            await caller.verify_delivery()
    assert touched == []


async def test_other_host_and_closed_boundary_refuse(human, bound, fixture):
    caller = await selected(human, bound, fixture)
    with pytest.raises(AssignmentError, match="human_request_unavailable"):
        await caller.transaction(lambda tx, _: None, expected_orchestrator=object())
    human[1].close()
    with pytest.raises(AssignmentError, match="human_request_unavailable"):
        await caller.verify_delivery()


@pytest.mark.parametrize("loss", ["monotonic", "absolute", "jwt", "mock"])
async def test_original_expiry_and_auth_posture_remain_current(human, bound, fixture, monkeypatch, loss):
    caller = await selected(human, bound, fixture)
    if loss == "monotonic":
        caller = replace(caller, _deadline=time.monotonic() - 1)
    elif loss == "absolute":
        caller = replace(caller, _until=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif loss == "jwt":
        caller = replace(caller, context=replace(caller.context,
            principal_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    else:
        monkeypatch.setenv("USE_MOCK_AUTH", "true")
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.transaction(lambda tx, _: None, expected_orchestrator=human[2])


async def test_delivery_refuses_a_different_verified_identity_snapshot(human, bound, fixture, monkeypatch):
    caller = await selected(human, bound, fixture)
    verify = auth.verify_production_token
    other = fixture[3](different_bound_claim="new-value")

    async def changed(token):
        assert token == caller._token
        return await verify(other)

    monkeypatch.setattr(auth, "verify_production_token", changed)
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.verify_delivery()


@pytest.mark.parametrize("kind", ["not_callable", "coroutine", "exception"])
async def test_metadata_callbacks_are_sync_and_errors_are_private(human, bound, fixture, kind):
    caller = await selected(human, bound, fixture)
    executed = []

    async def forbidden():
        executed.append(True)

    def failed(tx, _):
        raise RuntimeError("private-draft-must-not-escape")

    callback = None if kind == "not_callable" else (lambda tx, _: forbidden()) if kind == "coroutine" else failed
    with pytest.raises(AssignmentError, match="human_request_unavailable") as caught:
        await caller.transaction(callback, expected_orchestrator=human[2])
    assert executed == [] and "private-draft-must-not-escape" not in str(caught.value)


async def test_boundary_requires_real_composition_and_existing_services():
    module = importlib.import_module("orchestrator.human_request_authority")
    with pytest.raises(AssignmentError, match="human_request_unavailable"):
        module.HumanRequestBoundary(SimpleNamespace())


async def test_session_deleted_during_iam_is_an_authentication_failure(human, bound, fixture, monkeypatch):
    verify = auth.verify_production_token

    async def deleted(token):
        claims = await verify(token)
        await asyncio.to_thread(fixture[0].delete, fixture[2])
        return claims

    monkeypatch.setattr(auth, "verify_production_token", deleted)
    with pytest.raises(AssignmentError) as caught:
        await selected(human, bound, fixture)
    assert caught.value.status_code == 401
    assert not fixture[-1]


@pytest.mark.parametrize("cookie", [True, False])
async def test_audit_lock_wait_cannot_commit_after_original_expiry(human, bound, fixture, runtime, cookie):
    caller = await selected(human, bound, fixture, cookie=cookie)
    caller = replace(caller, _until=datetime.now(timezone.utc) + timedelta(seconds=.08))
    entered = threading.Event()

    def mutation(tx, _):
        tx.execute("UPDATE web_session SET resumed=true WHERE sid=%s", (fixture[2],))
        entered.set()
        tx.fetch_one("SELECT count(*) AS count FROM audit_events")

    with runtime.transaction() as blocker:
        blocker.execute("LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE")
        pending = asyncio.create_task(caller.transaction(mutation, expected_orchestrator=human[2]))
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(max(0, caller._until.timestamp() - time.time()) + .01)
        assert not pending.done()
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await pending
    assert get_session_record(runtime, fixture[2]).resumed is False


async def test_cancelled_waiter_keeps_capacity_until_worker_rolls_back(human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    caller = replace(caller, _until=datetime.now(timezone.utc) + timedelta(seconds=.08))
    entered, release = threading.Event(), threading.Event()

    def mutation(tx, _):
        tx.execute("UPDATE web_session SET resumed=true WHERE sid=%s", (fixture[2],))
        entered.set()
        assert release.wait(2)

    pending = asyncio.create_task(caller.transaction(mutation, expected_orchestrator=human[2]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert human[1].adapter.snapshot().active == 1
        await asyncio.sleep(max(0, caller._until.timestamp() - time.time()) + .01)
    finally:
        release.set()
    async with asyncio.timeout(3):
        while human[1].adapter.snapshot().active:
            await asyncio.sleep(.01)
    assert get_session_record(runtime, fixture[2]).resumed is False
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await caller.verify_delivery()
