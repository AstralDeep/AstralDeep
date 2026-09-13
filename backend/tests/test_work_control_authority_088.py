"""Private Work caller fences against real Plane and normal signed JWT policy."""
import importlib
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from tests.test_work_submit_postgres_088 import (
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_request_session_authority_088 import request
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from persistent_agents.models import AssignmentError
from orchestrator import auth, work_control_authority as module

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bound(service, fixture, runtime):
    orch = service.assignments.orch
    orch.persistent_assignments = service.assignments
    orch.web_sessions = fixture[0]
    orch.audit_repo = service.audit
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    app = FastAPI()
    app.state.orchestrator = orch
    yield service.assignments, app
    service.store.async_runtime.close()


def incoming(bound, fixture, *, cookie=True, bearer=True, method="POST", extra=()):
    headers = [(b"authorization", ("Bearer " + fixture[3]()).encode())] if bearer else []
    value = request(fixture[2] if cookie else None, method=method, headers=[
        (b"content-type", b"application/json"), (b"origin", b"https://app.invalid"),
        *headers, *extra])
    value.scope["app"] = bound[1]
    return value


@pytest.mark.parametrize("cookie", [True, False])
async def test_real_caller_guard_checks_selected_session_without_refresh(bound, fixture, runtime, cookie):
    module = importlib.import_module("orchestrator.work_control_authority")
    guard = await module.authenticate_work_control_request(
        incoming(bound, fixture, cookie=cookie), assignments=bound[0], sessions=fixture[0])
    with runtime.transaction() as tx:
        observed = guard.assert_current(tx, assignments=bound[0])
    assert (observed is not None) is cookie
    assert not fixture[-1]
    await guard.verify_delivery()


async def selected(bound, fixture, **kwargs):
    return await module.authenticate_work_control_request(
        incoming(bound, fixture, **kwargs), assignments=bound[0], sessions=fixture[0])


async def test_delete_and_cookie_iam_use_original_selected_session(bound, fixture):
    guard = await selected(bound, fixture, method="DELETE", bearer=False)
    assert guard.context.cookie_session == (fixture[2], guard.require_session().credential.incarnation_id)
    assert fixture[-1] == []
    assert fixture[1] not in repr(guard) and fixture[3]() not in repr(guard)


async def test_bare_bearer_cannot_satisfy_new_continuation_session(bound, fixture):
    guard = await selected(bound, fixture, cookie=False)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        guard.require_session()


@pytest.mark.parametrize("extra", [
    [(b"cookie", b"astral_session=forged")],
    [(b"cookie", b"astral_session=")],
])
async def test_ambiguous_cookie_never_downgrades_to_bare_bearer(bound, fixture, extra):
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await selected(bound, fixture, extra=extra)
    assert not fixture[-1]


async def test_identical_sid_replacement_after_capture_refuses_transaction_and_delivery(bound, fixture, runtime):
    guard = await selected(bound, fixture)
    old = get_session_record(runtime, fixture[2])
    new = replace_session_record(runtime, old)
    assert new.incarnation_id != old.incarnation_id
    with runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_authentication_required"):
        guard.assert_current(tx, assignments=bound[0])
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await guard.verify_delivery()
    assert not fixture[-1]


async def test_cookie_selection_cannot_adopt_replacement_during_jwt_wait(bound, fixture, runtime, monkeypatch):
    original = auth.verify_production_token
    async def verify(token):
        replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        return await original(token)
    monkeypatch.setattr(auth, "verify_production_token", verify)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await selected(bound, fixture, bearer=False)
    assert not fixture[-1]


async def test_bearer_cookie_first_selection_follows_iam_then_never_adopts_again(
    bound, fixture, runtime, monkeypatch,
):
    """Mixed transport authenticates the Bearer before first persisted B capture.

    This preserves admission's established policy: the signed SID is frozen, but
    no owner/session lookup occurs before verified IAM. Subsequent waits cannot
    replace this first captured issuance, even for an identical SID/row body.
    """
    original = auth.verify_production_token
    selected_row = None

    async def verify(token):
        nonlocal selected_row
        selected_row = replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        return await original(token)

    monkeypatch.setattr(auth, "verify_production_token", verify)
    guard = await selected(bound, fixture)
    assert guard.caller.credential.incarnation_id == selected_row.incarnation_id
    replacement = replace_session_record(runtime, selected_row)
    assert replacement.incarnation_id != guard.caller.credential.incarnation_id
    with runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_authentication_required"):
        guard.assert_current(tx, assignments=bound[0])
    assert not fixture[-1]


async def test_scope_change_during_iam_cannot_replace_headers_method_or_private_state(bound, fixture, monkeypatch):
    req = incoming(bound, fixture)
    original = auth.verify_production_token
    async def verify(token):
        req.scope.update(method="OPTIONS", query_string=b"token=forged", state={"private": "forged"})
        req.scope["headers"].clear()
        return await original(token)
    monkeypatch.setattr(auth, "verify_production_token", verify)
    guard = await module.authenticate_work_control_request(req, assignments=bound[0], sessions=fixture[0])
    assert guard.context.session_id == fixture[2]
    assert guard.context.claims.get("private") is None


@pytest.mark.parametrize("change", ["assignments", "store", "adapter", "sessions", "session_adapter",
                                   "runtime", "catalog", "audit", "app"])
async def test_replaced_composition_refuses_before_new_transaction(bound, fixture, monkeypatch, change):
    guard = await selected(bound, fixture)
    assignments = bound[0]
    orch = assignments.orch
    target, key = {
        "assignments": (orch, "persistent_assignments"), "store": (assignments, "store"),
        "adapter": (assignments.store, "async_runtime"), "sessions": (orch, "web_sessions"),
        "session_adapter": (fixture[0], "_sessions"), "runtime": (orch.runtime_composition.plane, "runtime"),
        "catalog": (orch.runtime_composition.plane, "repositories"), "audit": (orch, "audit_repo"),
        "app": (bound[1].state, "orchestrator"),
    }[change]
    with monkeypatch.context() as patch:
        patch.setattr(target, key, object())
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            guard.assert_current(None, assignments=assignments)
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            await guard.verify_delivery()


async def test_final_guard_rechecks_composition_after_waiting_session_assertion(bound, fixture, runtime, monkeypatch):
    guard = await selected(bound, fixture)
    repository = runtime.repositories.history.sessions
    original = repository.assert_current_consent
    async_adapter = bound[0].store.async_runtime
    def assertion(*args, **kwargs):
        result = original(*args, **kwargs)
        bound[0].store.async_runtime = object()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(repository, "assert_current_consent", assertion)
        try:
            with runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_control_unavailable"):
                guard.assert_current(tx, assignments=bound[0])
        finally:
            bound[0].store.async_runtime = async_adapter


@pytest.mark.parametrize("cookie", [True, False])
async def test_expiry_after_real_audit_table_wait_rolls_back_prior_write(bound, fixture, runtime, cookie):
    guard = await selected(bound, fixture, cookie=cookie)
    # Narrow the private attempt lifetime; production's fixed 15 seconds is unchanged.
    guard = replace(guard, _until=datetime.now(timezone.utc) + timedelta(seconds=.08))
    entered = threading.Event()
    def mutation():
        with runtime.transaction() as tx:
            runtime.repositories.history.sessions.bound_request_execution_waits(tx)
            guard.assert_current(tx, assignments=bound[0])
            tx.execute("UPDATE web_session SET resumed=true WHERE sid=%s", (fixture[2],))
            entered.set()
            tx.fetch_one("SELECT count(*) AS count FROM audit_events")
            guard.assert_current(tx, assignments=bound[0])
    with runtime.transaction() as blocker:
        blocker.execute("LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE")
        pending = asyncio.create_task(asyncio.to_thread(mutation))
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(max(0, guard._until.timestamp() - time.time()) + .02)
        assert not pending.done()
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await pending
    assert get_session_record(runtime, fixture[2]).resumed is False


async def test_session_retirement_serializes_after_the_guarded_transaction(bound, fixture, runtime):
    guard = await selected(bound, fixture)
    held, release, deletion_entered = threading.Event(), threading.Event(), threading.Event()
    def transaction():
        with runtime.transaction() as tx:
            guard.assert_current(tx, assignments=bound[0])
            held.set()
            assert release.wait(3)
            guard.assert_current(tx, assignments=bound[0])
    def delete():
        deletion_entered.set()
        fixture[0].delete(fixture[2])
    task = asyncio.create_task(asyncio.to_thread(transaction))
    assert await asyncio.to_thread(held.wait, 2)
    retirement = asyncio.create_task(asyncio.to_thread(delete))
    assert await asyncio.to_thread(deletion_entered.wait, 2)
    await asyncio.sleep(.03)
    assert not retirement.done()
    release.set()
    await task
    await retirement
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await guard.verify_delivery()


async def test_delivery_rechecks_actual_jwt_policy_and_never_refreshes(bound, fixture, monkeypatch):
    guard = await selected(bound, fixture)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "different-client")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "")
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await guard.verify_delivery()
    assert not fixture[-1]


async def test_missing_session_and_cancelled_capture_never_return_authority(bound, fixture, monkeypatch):
    fixture[0].delete(fixture[2])
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await selected(bound, fixture)
    original = module._authenticate_work_request
    async def cancelled(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError
    monkeypatch.setattr(module, "_authenticate_work_request", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await selected(bound, fixture, cookie=False)
