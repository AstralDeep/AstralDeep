"""Exercises explicit continuity through real socket ingress, owner custody and durable wake fences."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import pytest

from orchestrator.human_request_authority import HumanRequestBoundary
from tests.test_work_surface_ingress_088 import (
    ingress as ingress, surface as surface, runtime as runtime, service as service,
    fixture as fixture, signing_key as signing_key, registered,
    register_chrome_branch as register_chrome_branch,
)
from tests.test_work_surface_postgres_088 import plane as plane, context, command
from tests.test_work_continuity_postgres import complete_without_wake
from tests.test_work_completion_wake_postgres_088 import subscribe
from tests.test_work_continuation_authority_088 import current

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def lost_wake(ingress, service, fixture, runtime):
    state = ingress
    state.socket.scope["headers"].append((b"origin", b"https://app.invalid"))
    await registered(state)
    state.orch.ui_sessions[state.socket]["_client_capabilities"].append("work_continuity_v1")
    state.orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    state.orch.audit_repo = service.audit
    boundary = HumanRequestBoundary(state.orch)
    state.orch.human_request_boundary = boundary
    app = FastAPI()
    app.state.orchestrator = state.orch
    api = SimpleNamespace(service=service, fixture=fixture, runtime=runtime, app=app, orch=state.orch)
    source = (await service.submit(await context(fixture, runtime), command(name="Source before disconnect"))).record
    waiter = await asyncio.to_thread(current, runtime, SimpleNamespace(owner_id=fixture[1], assignment_id=state.identity))
    await subscribe(api, waiter, source)
    await complete_without_wake(api, source)
    waiter = await asyncio.to_thread(current, runtime, waiter)
    try:
        yield state, api, waiter
    finally:
        boundary.close()


def request(state, waiter):
    generation, submission = str(uuid4()), str(uuid4())
    payload = {"version": 1, "operation_id": waiter.assignment_id,
               "expected_revision": waiter.state_version, "submission_id": submission}
    state.socket.feed(json.dumps({"type": "ui_event", "action": "chrome_work_continuity", "session_id": None,
        "submission_id": submission, "request_generation": generation,
        "connection_generation": state.connection_generation, "payload": payload}))
    return generation


async def test_current_socket_recovers_the_durable_lost_wake_once(lost_wake):
    state, api, waiter = lost_wake
    generation = request(state, waiter)
    response = await state.arrived(lambda value: value.get("surface_key") == "work")
    assert response["request_generation"] == generation
    assert "Source before disconnect" not in response["html"]
    awake = await asyncio.to_thread(current, api.runtime, waiter)
    assert awake.phase == "waiting" and awake.state_version == waiter.state_version + 1
    rows, _ = await asyncio.to_thread(api.service.audit.list_for_user, waiter.owner_id)
    assert sum(row.action_type == "assignment_continuity" for row in rows) == 1
    assert api.service.audit.verify_chain(waiter.owner_id) is None


@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("change", ["session", "registration", "composition"])
async def test_recovery_keeps_original_caller_across_awaits(lost_wake, monkeypatch, stage, change):
    from orchestrator.projection_surfaces import work
    from tests.helpers.session_plane_runtime import get_session_record

    state, api, waiter = lost_wake
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "before":
        original = work._ws_publication_caller

        async def held(*args):
            caller = await original(*args)
            entered.set()
            await release.wait()
            return caller

        monkeypatch.setattr(work, "_ws_publication_caller", held)
    else:
        original = api.service.store.transaction

        async def held(callback, **kwargs):
            result = await original(callback, **kwargs)
            if isinstance(result, dict) and result.get("operation", {}).get("id") == waiter.assignment_id and "applied" in result:
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(api.service.store, "transaction", held)
    generation = request(state, waiter)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "session":
            record = await asyncio.to_thread(get_session_record, api.runtime, api.fixture[2])

            def retire():
                with api.runtime.transaction() as tx:
                    api.runtime.repositories.history.sessions.delete(tx, owner_id=waiter.owner_id,
                        session_id=record.session_id, expected_incarnation_id=record.incarnation_id)

            await asyncio.to_thread(retire)
        elif change == "registration":
            state.orch.ui_sessions[state.socket] = dict(state.orch.ui_sessions[state.socket])
        else:
            state.orch.runtime_composition.plane = SimpleNamespace(runtime=object(), repositories=object())
        release.set()
        terminal = await state.arrived(lambda value: value.get("request_generation") == generation and value.get("terminal") is True)
        assert terminal["state"] == "failed"
        assert not [value for value in state.socket.payloads() if value.get("surface_key") == "work"]
        durable = await asyncio.to_thread(current, api.runtime, waiter)
        assert durable.state_version == waiter.state_version + (stage == "after")
        assert durable.phase == ("waiting" if stage == "after" else "awaiting_event")
        rows, _ = await asyncio.to_thread(api.service.audit.list_for_user, waiter.owner_id)
        assert sum(row.action_type == "assignment_continuity" for row in rows) == (stage == "after")
    finally:
        release.set()


@pytest.mark.parametrize("change", ["extra", "missing_connection", "chat", "replaced_submission"])
async def test_continuity_admission_requires_closed_current_command(lost_wake, change):
    state, _, waiter = lost_wake
    submission = str(uuid4())
    value = {"type": "ui_event", "action": "chrome_work_continuity", "session_id": None,
        "submission_id": submission, "request_generation": str(uuid4()), "connection_generation": state.connection_generation,
        "payload": {"version": 1, "operation_id": waiter.assignment_id, "expected_revision": waiter.state_version, "submission_id": submission}}
    if change == "extra":
        value["authority"] = "forbidden"
    elif change == "missing_connection":
        del value["connection_generation"]
    elif change == "chat":
        value["session_id"] = str(uuid4())
    else:
        value["submission_id"] = str(uuid4())
    context = state.orch._connection_contexts[id(state.socket)]
    assert state.orch._connection_frame(context, json.dumps(value), value) is None


async def test_continuity_idempotency_digest_includes_exact_command(lost_wake):
    state, _, waiter = lost_wake
    submission = str(uuid4())
    value = {"type": "ui_event", "action": "chrome_work_continuity", "session_id": None,
        "submission_id": submission, "request_generation": str(uuid4()), "connection_generation": state.connection_generation,
        "payload": {"version": 1, "operation_id": waiter.assignment_id, "expected_revision": waiter.state_version, "submission_id": submission}}
    context = state.orch._connection_contexts[id(state.socket)]
    first = state.orch._connection_frame(context, json.dumps(value), value)
    value["payload"]["expected_revision"] += 1
    second = state.orch._connection_frame(context, json.dumps(value), value)
    assert first.normalized_digest != second.normalized_digest
    assert first.read_only is False and first.surface == "work" and first.chat_id is None


@pytest.mark.parametrize("change", ["capability", "stale_revision", "missing_operation"])
async def test_recovery_denials_do_not_wake_or_disclose(lost_wake, change):
    state, api, waiter = lost_wake
    if change == "capability":
        state.orch.ui_sessions[state.socket]["_client_capabilities"].remove("work_continuity_v1")
    selected = SimpleNamespace(assignment_id=waiter.assignment_id, state_version=waiter.state_version)
    if change == "stale_revision":
        selected.state_version += 1
    elif change == "missing_operation":
        selected.assignment_id = str(uuid4())
    generation = request(state, selected)
    terminal = await state.arrived(lambda value: value.get("request_generation") == generation and value.get("terminal") is True)
    assert terminal["state"] == "failed"
    assert not [value for value in state.socket.payloads() if value.get("surface_key") == "work"]
    assert (await asyncio.to_thread(current, api.runtime, waiter)).state_version == waiter.state_version
    rows, _ = await asyncio.to_thread(api.service.audit.list_for_user, waiter.owner_id)
    assert not any(row.action_type == "assignment_continuity" for row in rows)
