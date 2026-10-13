"""Verify Safety HTML delivery through chrome dispatch and current signed socket authority.
Correlated browser results retain owner/request custody across navigation and delivery waits.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.emergency_stop import EmergencyStopCoordinator
from orchestrator.human_request_authority import capture_human_socket_request
from orchestrator.projection_surfaces import safety
from persistent_agents.models import AssignmentError
from rote.capabilities import DeviceProfile
from shared.protocol import ChromeRender, ProtocolValidationError
from tests.test_emergency_stop_postgres import (
    stop as stop, human as human, bound as bound, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key, socket_request as socket_request,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def browser():
    socket = object()
    state = SimpleNamespace(socket=socket, sent=[])
    state.orch = SimpleNamespace(
        emergency_stop=EmergencyStopCoordinator(), ui_sessions={socket: {"sub": "owner"}},
        rote=SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": "browser"})),
    )
    async def send(target, wire):
        state.sent.append((target, json.loads(wire)))
        return True
    state.orch._safe_send = send
    return state


async def open_safety(state, generation):
    return await chrome_events._handle_chrome_event(state.orch, state.socket, "chrome_open",
        {"surface": "safety", "params": {}}, "owner", request_generation=generation)


async def test_browser_open_emits_the_exact_correlated_safety_modal(browser):
    generation = str(uuid4())
    await open_safety(browser, generation)
    target, frame = browser.sent[-1]
    assert target is browser.socket
    assert set(frame) == {"type", "region", "mode", "html", "surface_key", "request_generation"}
    assert frame["surface_key"] == "safety" and frame["request_generation"] == generation
    assert frame["type"] == "chrome_render" and frame["mode"] == "replace" and frame["region"] == "modal"
    assert "Stop everything now" in frame["html"] and "Recheck responders" in frame["html"]


@pytest.mark.parametrize("changes", [
    {"request_generation": None}, {"request_generation": "stale"}, {"request_generation": True},
    {"region": "topbar"},
])
async def test_safety_html_rejects_missing_generation_and_foreign_regions(changes):
    values = {"surface_key": "safety", "request_generation": str(uuid4()), **changes}
    with pytest.raises(ProtocolValidationError):
        ChromeRender(**values).to_json()


@pytest.mark.parametrize("action,payload", [
    ("chrome_open", {"surface": "safety"}), ("chrome_open", {"surface": "theme"}),
    ("chrome_close", {}), ("new_chat", {}),
])
async def test_browser_navigation_retires_a_pending_safety_render(browser, monkeypatch, action, payload):
    entered, release = asyncio.Event(), asyncio.Event()
    original = safety.render
    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(safety, "render", held)
    pending = asyncio.create_task(open_safety(browser, str(uuid4())))
    await entered.wait()
    chrome_events.capture_surface_request(browser.orch, browser.socket, action, payload, str(uuid4()))
    release.set()
    await pending
    assert browser.sent == []


async def test_safety_delivery_rechecks_request_after_authority_await(browser, monkeypatch):
    async def verify(host, target):
        chrome_events.capture_surface_request(host, target, "chrome_open", {"surface": "safety"}, str(uuid4()))
    monkeypatch.setattr(chrome_events, "_verify_human_delivery", verify)
    await open_safety(browser, str(uuid4()))
    assert browser.sent == []


async def test_finished_response_scope_cannot_publish_a_deferred_safety_modal(browser, monkeypatch):
    release = asyncio.Event()
    tasks = []
    original = safety.render
    async def later():
        await release.wait()
        return await chrome_events._push_modal(browser.orch, browser.socket, "Retired private result")
    async def render(*args):
        tasks.append(asyncio.create_task(later()))
        return await original(*args)
    monkeypatch.setattr(safety, "render", render)
    await open_safety(browser, str(uuid4()))
    release.set()
    assert await asyncio.gather(*tasks) == [False]
    assert len(browser.sent) == 1


async def test_failed_safety_render_keeps_the_requested_generation_without_private_details(browser, monkeypatch):
    async def broken(*args):
        raise RuntimeError("private failure detail")
    monkeypatch.setattr(safety, "render", broken)
    generation = str(uuid4())
    await open_safety(browser, generation)
    frame = browser.sent[-1][1]
    assert frame["request_generation"] == generation and frame["surface_key"] == "safety"
    assert "private failure detail" not in frame["html"] and "failed to load" in frame["html"]


async def test_unsolicited_and_other_browser_modals_do_not_borrow_safety_correlation(browser):
    await open_safety(browser, str(uuid4()))
    await chrome_events._push_modal(browser.orch, browser.socket, "Ordinary settings")
    assert "request_generation" not in browser.sent[-1][1]
    chrome_events.capture_surface_request(browser.orch, browser.socket, "chrome_open", {"surface": "theme"}, str(uuid4()))
    assert chrome_events.open_surface_for(browser.orch, browser.socket) == ""
    chrome_events.capture_surface_request(browser.orch, browser.socket, "chrome_open", {"surface": "theme"}, str(uuid4()))
    assert not browser.orch._ordinary_chrome_requests


async def dispatch(human, socket_request, action, payload, owner):
    socket, context, message = socket_request
    generation = str(uuid4())
    message.update(action=action, payload=payload, request_generation=generation, submission_id=str(uuid4()))
    chrome_events.capture_surface_request(human[2], socket, action, payload, generation)
    pending = capture_human_socket_request(human[1], websocket=socket, context=context, message=message)
    token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        await chrome_events.handle_chrome_event(human[2], socket, action, payload, owner, request_generation=generation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
        pending.close()
    return generation


async def test_real_browser_owner_lifecycle_delivers_correlated_controls(stop, human, socket_request, fixture):
    frames = []
    async def send(target, wire):
        frames.append(json.loads(wire))
        return True
    human[2]._safe_send = send
    human[2].rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": "browser"}))
    for action, changes, engaged in [
        ("chrome_open", {"params": {}}, False), ("chrome_safety_stop", {}, True),
        ("chrome_safety_verify", {}, True), ("chrome_safety_resume", {"expected_revision": 1}, False),
    ]:
        generation = await dispatch(human, socket_request, action, {"surface": "safety", **changes}, fixture[1])
        assert frames[-1]["surface_key"] == "safety" and frames[-1]["request_generation"] == generation
        assert frames[-1]["type"] == "chrome_render" and frames[-1]["mode"] == "replace"
        assert ("Resume explicitly" in frames[-1]["html"]) is engaged
        assert stop[0].status(fixture[1])["engaged"] is engaged
    socket_request[1].closing = True
    with pytest.raises(AssignmentError):
        await dispatch(human, socket_request, "chrome_safety_stop", {"surface": "safety"}, fixture[1])
    assert len(frames) == 4 and not stop[0].status(fixture[1])["engaged"]


@pytest.mark.parametrize("loss", ["owner", "connection"])
async def test_real_browser_owner_loss_during_render_cannot_deliver(stop, human, socket_request, fixture, monkeypatch, loss):
    socket, context, _ = socket_request
    sent = []
    async def send(target, wire):
        sent.append(wire)
        return True
    human[2]._safe_send = send
    human[2].rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": "browser"}))
    original = safety.render
    async def replaced(*args):
        result = await original(*args)
        if loss == "owner":
            human[2].ui_sessions[socket]["sub"] = "different-owner"
        else:
            context.connection_generation = uuid4()
        return result
    monkeypatch.setattr(safety, "render", replaced)
    with pytest.raises(AssignmentError):
        await dispatch(human, socket_request, "chrome_open", {"surface": "safety"}, fixture[1])
    assert sent == []
