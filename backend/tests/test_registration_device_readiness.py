"""Verifies device updates preserve the latest profile without publishing registration readiness early.
Exercises orchestrator registration and device handling across delayed and failed authentication.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.orchestrator import ConnectionContext, Orchestrator
from tests.test_register_ui_auth_required import (
    _FakeWS, _make_fake, _register_msg, auth_audit as auth_audit,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def ws_audit(monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())


def device(width):
    return {"device_type": "windows", "viewport_width": width, "viewport_height": 720}


def setup_host(validate, *, registered):
    host = _make_fake(validate=validate)
    socket = _FakeWS()
    context = ConnectionContext(socket, uuid4(), time.monotonic() + 30,
        registered=registered, work_registrations_pending=1,
        connection_generation=uuid4())
    host._connection_contexts = {id(socket): context}
    host._registered_events[id(socket)] = asyncio.Event()
    host._get_user_id = lambda selected: host.ui_sessions[selected]["sub"]
    if registered:
        host.ui_sessions[socket] = {"sub": "previous"}
        host.rote.register_device(socket, device(640))
    return host, socket, context


async def update(host, socket, width):
    await host.handle_ui_message(socket, json.dumps({
        "type": "ui_event", "action": "update_device", "payload": {"device": device(width)}}))
    assert host.rote.get_profile(socket).capabilities.viewport_width == width


def configurations(host):
    return [frame for _, frame in host._sent if frame["type"] == "rote_config"]


@pytest.mark.parametrize("registered", [False, True])
async def test_device_updates_wait_for_registration_and_publish_latest_profile(registered, auth_audit):
    async def validate(_token):
        return {"sub": "owner"}

    host, socket, context = setup_host(validate, registered=registered)
    release = asyncio.Event()

    async def dashboard(_socket):
        await release.wait()

    host.send_dashboard = dashboard
    task = asyncio.create_task(Orchestrator._run_ui_registration(
        host, context, _register_msg(token="synthetic", device=device(800))))
    try:
        await asyncio.wait_for(host._registered_events[id(socket)].wait(), 5)
        context.registered = True
        assert context.work_registrations_pending == 1 and not task.done()
        for width in (960, 1280):
            await update(host, socket, width)
            assert configurations(host) == []
        release.set()
        await asyncio.wait_for(task, 5)
        frames = configurations(host)
        assert context.work_registrations_pending == 0 and len(frames) == 1
        assert frames[0]["viewport_snapshot_supported"] is True
        assert frames[0]["device_profile"] == host.rote.get_profile(socket).to_dict()
        assert frames[0]["device_profile"]["capabilities"]["viewport_width"] == 1280
        await update(host, socket, 1440)
        assert len(configurations(host)) == 2
        assert configurations(host)[-1]["device_profile"] == host.rote.get_profile(socket).to_dict()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_rejected_registration_never_publishes_device_readiness(auth_audit):
    entered, release = asyncio.Event(), asyncio.Event()

    async def reject(_token):
        entered.set()
        await release.wait()
        return None

    host, socket, context = setup_host(reject, registered=True)
    task = asyncio.create_task(Orchestrator._run_ui_registration(
        host, context, _register_msg(token="synthetic-rejected", device=device(800))))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await update(host, socket, 1280)
        assert configurations(host) == []
        release.set()
        await asyncio.wait_for(task, 5)
        assert context.work_registrations_pending == 0
        assert configurations(host) == []
        assert any(frame["type"] == "auth_required" for _, frame in host._sent)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_pending_scoped_device_update_is_refused_without_readiness():
    from tests.test_viewport_hydration import frames, harness, message

    host, socket, _binding, _authority = harness()
    host._connection_contexts[id(socket)].work_registrations_pending = 1
    event = message()
    await host.handle_ui_message(socket, event.to_json())
    assert host.rote.get_profile(socket).capabilities.viewport_width == 320
    delivered = frames(host)
    assert len(delivered) == 1 and delivered[0]["type"] == "error"
    assert delivered[0]["code"] == "viewport_snapshot_rejected"
    assert delivered[0]["request_generation"] == event.request_generation
    host.conversation_commits.build_snapshot.assert_not_called()
