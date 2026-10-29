"""Connection watch refresh and close traverse real socket capture and admission.
The existing PostgreSQL-backed owner/session harness supplies authenticated registration.
"""

import asyncio
import json
from uuid import uuid4

from orchestrator.projection_surfaces import connection
from rote.capabilities import DeviceProfile
from tests import test_work_surface_ingress_088 as flow

ingress = flow.ingress
surface = flow.surface
runtime = flow.runtime
service = flow.service
fixture = flow.fixture
signing_key = flow.signing_key


def send_request(state, action):
    generation = str(uuid4())
    state.socket.feed(json.dumps({"type": "ui_event", "action": action, "submission_id": str(uuid4()),
        "request_generation": generation, "connection_generation": state.connection_generation,
        "payload": {"surface": "connection"} if action == "chrome_open" else {}}))
    return generation


async def prepare(state, monkeypatch):
    state.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": "watch"})
    async def configured(_):
        return True
    async def status(*args):
        return {"overall": "unavailable", "endpoint": {}, "checks": []}
    state.orch.llm_configured_for = configured
    monkeypatch.setattr(connection, "guide_payload", status)
    await flow.registered(state)


async def test_real_watch_ingress_preserves_native_open_and_close_generations(ingress, monkeypatch):
    state = ingress
    await prepare(state, monkeypatch)
    opening = send_request(state, "chrome_open")
    opened = await state.arrived(lambda value: value.get("type") == "chrome_surface"
        and value.get("request_generation") == opening)
    assert opened["surface_key"] == "connection"
    assert any(str(frame.request_generation) == opening for frame in state.frames)
    closing = send_request(state, "chrome_close")
    closed = await state.arrived(lambda value: value.get("type") == "chrome_surface"
        and value.get("request_generation") == closing)
    assert closed["surface_key"] == "" and not closed["components"]
    assert not any(value.get("type") == "chrome_render" and value.get("surface_key") == "connection"
                   for value in state.socket.payloads())


async def test_real_reregistration_retires_pending_watch_refresh(ingress, monkeypatch):
    state = ingress
    await prepare(state, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def status(*args):
        entered.set()
        await release.wait()
        return {"overall": "unavailable", "endpoint": {}, "checks": []}
    monkeypatch.setattr(connection, "guide_payload", status)
    generation = send_request(state, "chrome_open")
    await asyncio.wait_for(entered.wait(), 5)
    state.register()
    await asyncio.wait_for(state.registrations.get(), 5)
    release.set()
    await state.barrier()
    assert not any(value.get("type") == "chrome_surface" and value.get("request_generation") == generation
                   for value in state.socket.payloads())
