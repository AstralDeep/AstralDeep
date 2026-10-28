"""Exercises retained-result publication through real socket metadata admission and original caller transaction fences."""

import asyncio
import json
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rote.capabilities import DeviceProfile
from orchestrator.human_request_authority import HumanRequestBoundary
from tests.test_work_surface_postgres_088 import (
    save_surface as save_surface, saved as saved, completed as completed, fixture as fixture,
    plane as plane, signing_key as signing_key, gate_orchestrator as gate_orchestrator,
    operation as operation, research as research, _propose_payload, _save_payload,
)
from tests.test_work_surface_ingress_088 import (
    ingress as ingress, registered, register_chrome_branch as register_chrome_branch,
)
from tests.test_work_save_postgres_088 import proposal_body, post, path, save_path, approval_for

runtime = plane
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def surface(save_surface):
    value, orch, socket = save_surface
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.default())
    orch.history = SimpleNamespace(create_chat=None)
    return orch, socket, value.op.completed.assignment_id


@pytest.fixture
async def host(ingress, save_surface):
    state = ingress
    value = save_surface[0]
    value.op.executor.orch = state.orch
    value.app.state.orchestrator = state.orch
    boundary = HumanRequestBoundary(state.orch)
    state.orch.human_request_boundary = boundary
    await registered(state)
    state.orch._ws_active_chat[id(state.socket)] = value.chat_id
    try:
        yield state, value
    finally:
        boundary.close()


def request(state, payload):
    generation = payload.get("request_generation") or str(uuid4())
    frame = {"type": "ui_event", "action": "chrome_work_result_save", "session_id": None,
        "submission_id": payload["submission_id"], "request_generation": generation,
        "connection_generation": state.connection_generation, "payload": payload}
    state.socket.feed(json.dumps(frame))
    return generation


def head(value):
    with value.op.runtime.transaction() as tx:
        row = value.op.runtime.repositories.history.conversations.get(tx,
            owner_id=value.op.owner, conversation_id=value.chat_id)
    return row.render_revision, row.publication_id


@pytest.fixture
async def browser_action(request):
    factory = getattr(request, "param", None)
    if factory is not None:
        async with factory() as action:
            yield action
        return
    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.commands = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "button" and (values.get("data-ui-action") or values.get("data-action")) == "chrome_work_result_save":
                self.commands.append(json.loads(values.get("data-ui-payload") or values["data-payload"]))

    async def action(state, frame, *, final=False):
        if final:
            assert ("refused" if final == "refused" else "Result saved") in frame["html"]
            return None
        controls = Controls()
        controls.feed(frame["html"])
        assert len(controls.commands) == 1
        payload = controls.commands[0]
        return {"type": "ui_event", "action": "chrome_work_result_save", "session_id": None,
                "payload": payload, "submission_id": payload["submission_id"],
                "request_generation": str(uuid4()), "connection_generation": state.connection_generation}

    yield action


async def test_actual_socket_proposal_review_and_save_publish_once(host, browser_action):
    state, value = host
    state.request(mode="result")
    result_frame = await state.arrived(lambda frame: frame.get("surface_key") == "work")
    proposed = await browser_action(state, result_frame)
    state.socket.feed(json.dumps(proposed))
    review_frame = await state.arrived(lambda frame: frame.get("surface_key") == "work")
    assert review_frame["request_generation"] == proposed["request_generation"]
    assert "Review the exact content" in review_frame["html"]
    assert head(value) == (0, None)
    approval = await browser_action(state, review_frame)
    state.socket.feed(json.dumps(approval))
    saved_frame = await state.arrived(lambda frame: frame.get("surface_key") == "work")
    assert saved_frame["request_generation"] == approval["request_generation"]
    assert "Result saved" in saved_frame["html"]
    assert await browser_action(state, saved_frame, final=True) is None
    assert head(value) == (1, proposed["payload"]["publication_id"])
    rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
    assert sum(row.action_type == "work.result.propose" for row in rows) == 1
    assert sum(row.action_type == "work.result.save" for row in rows) == 1
    assert state.orch.audit_repo.verify_chain(value.op.owner) is None


@pytest.mark.parametrize("change", ["registration", "session", "read_retirement", "read_replacement"])
async def test_original_save_caller_retired_before_transaction_cannot_publish(host, monkeypatch, change):
    state, value = host
    command = proposal_body(value)
    response = await post(value, path(value), command)
    assert response.status_code == 200
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch.persistent_assignments.store.transaction

    async def held(callback, **kwargs):
        if callback.__name__ == "current":
            entered.set()
            await release.wait()
        return await original(callback, **kwargs)

    monkeypatch.setattr(state.orch.persistent_assignments.store, "transaction", held)
    generation = request(state, _save_payload(value, command["submission_id"], response.json()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "registration":
            state.orch.ui_sessions[state.socket] = dict(state.orch.ui_sessions[state.socket])
        elif change == "session":
            await asyncio.to_thread(value.op.sessions.delete, value.op.sid)
        elif change == "read_retirement":
            from orchestrator.work_surface_authority import invalidate
            invalidate(state.orch, state.socket)
        else:
            from orchestrator.work_surface_authority import WorkSurfaceRead
            WorkSurfaceRead(state.orch, state.socket, value.op.owner, request_generation=str(uuid4()),
                context=state.orch._connection_contexts[id(state.socket)])
        release.set()
        terminal = await state.arrived(lambda frame: frame.get("request_generation") == generation and frame.get("terminal") is True)
        assert terminal["state"] == "failed"
        assert head(value) == (0, None)
        rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
        assert not any(row.action_type == "work.result.save" for row in rows)
    finally:
        release.set()


async def test_native_without_save_capability_retains_complete_read_only_result(host):
    state, _ = host
    state.orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": "android"}))
    generation = state.request(mode="result")
    frame = await state.arrived(lambda value: value.get("surface_key") == "work")
    assert frame["type"] == "chrome_surface" and frame["request_generation"] == generation
    assert "Exact retained" in json.dumps(frame) or "Public release" in json.dumps(frame)
    assert "chrome_work_result_save" not in json.dumps(frame)


@pytest.mark.parametrize("denial", ["stale", "unknown", "cross_owner", "origin"])
async def test_real_metadata_denials_do_not_publish_or_disclose(host, denial):
    state, value = host
    payload = _propose_payload(value, proposal_body(value))
    if denial == "stale":
        payload["expected_revision"] += 1
    elif denial == "unknown":
        payload["request_generation"] = str(uuid4())
    elif denial == "cross_owner":
        state.token = value.fixture[3](sub="other-owner")
        state.connection_generation = str(uuid4())
        await registered(state)
    else:
        state.socket.scope["headers"] = [row for row in state.socket.scope["headers"] if row[0] != b"origin"] + [(b"origin", b"https://evil.invalid")]
    generation = request(state, payload)
    response = await state.arrived(lambda frame: frame.get("surface_key") == "work"
        or frame.get("request_generation") == generation and frame.get("terminal") is True
        or frame.get("type") == "error" and frame.get("accepted") is False)
    assert "Result saved" not in json.dumps(response)
    if denial == "stale":
        assert "refused" in response["html"]
    assert head(value) == (0, None)
    rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
    assert not any(row.action_type in {"work.result.save", "work.result.propose"} for row in rows)


async def test_saved_result_stays_committed_if_the_original_read_retires_after_commit(host, monkeypatch):
    from orchestrator.work_surface_authority import invalidate

    state, value = host
    command = proposal_body(value)
    proposed = await post(value, path(value), command)
    assert proposed.status_code == 200
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch.persistent_assignments.store.transaction

    async def held(callback, **kwargs):
        result = await original(callback, **kwargs)
        if isinstance(result, dict) and result.get("status") == "saved":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(state.orch.persistent_assignments.store, "transaction", held)
    generation = request(state, _save_payload(value, command["submission_id"], proposed.json()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        invalidate(state.orch, state.socket)
        release.set()
        terminal = await state.arrived(lambda frame: frame.get("request_generation") == generation and frame.get("terminal") is True)
        assert terminal["state"] == "failed"
        assert head(value) == (1, command["publication_id"])
        rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
        assert sum(row.action_type == "work.result.save" for row in rows) == 1
        assert not any(frame.get("surface_key") == "work" for frame in state.socket.payloads())
    finally:
        release.set()


async def test_native_without_declared_save_capability_cannot_invoke_the_command(host):
    state, value = host
    state.orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": "android"}))
    generation = request(state, _propose_payload(value, proposal_body(value)))
    terminal = await state.arrived(lambda frame: frame.get("request_generation") == generation and frame.get("terminal") is True)
    assert terminal["state"] == "failed"
    assert head(value) == (0, None)
    rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
    assert not any(row.action_type == "work.result.propose" for row in rows)


@pytest.mark.parametrize("change", ["stale", "cross_owner", "connection"])
async def test_actual_browser_command_cannot_reuse_retired_or_stale_bindings(host, browser_action, change):
    state, value = host
    state.request(mode="result")
    view = await state.arrived(lambda frame: frame.get("surface_key") == "work")
    frame = await browser_action(state, view)
    expected = (0, None)
    if change == "stale":
        concurrent = proposal_body(value)
        proposed = await post(value, path(value), concurrent)
        assert proposed.status_code == 200
        saved_result = await post(value, save_path(value, concurrent), approval_for(proposed.json()))
        assert saved_result.status_code == 200
        expected = (1, concurrent["publication_id"])
    elif change == "cross_owner":
        state.token = value.fixture[3](sub="other-owner")
        await registered(state)
    else:
        state.socket.closed = True
    state.socket.feed(json.dumps(frame))
    response = await state.arrived(lambda value: value.get("surface_key") == "work"
        or value.get("type") == "error" and value.get("accepted") is False)
    if change == "stale":
        assert "refused" in response["html"]
        assert await browser_action(state, response, final="refused") is None
    else:
        assert response["type"] == "error" and response["accepted"] is False
    assert head(value) == expected
    rows, _ = state.orch.audit_repo.list_for_user(value.op.owner)
    expected_count = int(change == "stale")
    assert sum(row.action_type == "work.result.propose" for row in rows) == expected_count
    assert sum(row.action_type == "work.result.save" for row in rows) == expected_count


async def test_save_admission_digest_binds_the_entire_closed_command(host):
    state, value = host
    payload = _propose_payload(value, proposal_body(value))
    frame = {"type": "ui_event", "action": "chrome_work_result_save", "session_id": None,
        "submission_id": payload["submission_id"], "request_generation": str(uuid4()),
        "connection_generation": state.connection_generation, "payload": payload}
    context = state.orch._connection_contexts[id(state.socket)]
    first = state.orch._connection_frame(context, json.dumps(frame), frame)
    payload["publication_id"] = str(uuid4())
    second = state.orch._connection_frame(context, json.dumps(frame), frame)
    assert first.normalized_digest != second.normalized_digest
    payload["unexpected"] = "forbidden"
    assert state.orch._connection_frame(context, json.dumps(frame), frame) is None
