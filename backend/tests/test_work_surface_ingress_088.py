"""Tests for Work reads through socket ingress and admission
(backend/orchestrator/work_admission.py, work_surface_authority.py,
projection_surfaces/work.py): bounded enqueue, navigation invalidation, and
cookie/lifetime guards.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from types import SimpleNamespace

import pytest

from orchestrator import chrome_availability
from orchestrator.work_surface_authority import _transport_headers
from persistent_agents.models import AssignmentError
from tests.chrome.test_watch_workspace_disposition_088 import register_chrome_branch as register_chrome_branch

pytestmark = pytest.mark.asyncio


@dataclass
class Ingress:
    orch: object
    socket: object
    token: str
    identity: str
    connection_generation: str
    registrations: asyncio.Queue
    frames: list
    arrivals: asyncio.Queue
    finished: asyncio.Queue

    def register(self):
        self.socket.feed(json.dumps({"type": "register_ui", "token": self.token,
            "connection_generation": self.connection_generation}))


    async def arrived(self, predicate):
        async with asyncio.timeout(5):
            while True:
                value = await self.arrivals.get()
                if predicate(value):
                    return value

    async def barrier(self):
        self.socket.feed(json.dumps({"type": "ping"}))
        await self.arrived(lambda value: value.get("type") == "pong")


async def forbidden_audit(**_kwargs):
    return None


async def registered(state):
    state.register()
    await asyncio.wait_for(state.registrations.get(), 5)
    await state.barrier()


def work_frames(state):
    return [value for value in state.socket.payloads() if value.get("type") == "chrome_render"
            and value.get("surface_key") == "work"]


@pytest.mark.parametrize("headers", [None, [(1, b"x")], [(b"x", object())], [(b"x", "\ud800")],
    [(b"x", b"x" * 65537)], [(b"x", b"y")] * 257, [(b"x", b"y" * 1000)] * 66])
async def test_malformed_transport_headers_are_bounded_closed_refusals(headers):
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        _transport_headers(SimpleNamespace(scope={"headers": headers}))


@pytest.mark.parametrize("device", ["watch", "ios", "android", "macos", "windows"])
@pytest.mark.parametrize("capable,enabled", [(False, True), (True, False), (True, True)])
async def test_actual_registration_menu_negotiates_only_shared_work(
    monkeypatch, register_chrome_branch, device, capable, enabled,
):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(chrome_availability, "projection_chrome_availability", lambda _orch=None: {
        "work_enabled": enabled, "export_enabled": True, "share_enabled": True,
    })
    host, ws = SimpleNamespace(_safe_send=AsyncMock()), object()
    await register_chrome_branch(device, host, ws, {"realm_access": {"roles": ["user"]},
        "_client_capabilities": ["work_read_v1"] if capable else []})
    if device == "watch" and not capable:
        host._safe_send.assert_not_awaited()
        return
    host._safe_send.assert_awaited_once()
    model = json.loads(host._safe_send.await_args.args[1])["model"]
    keys = [control["key"] for control in model["topbar"]]
    assert ("work" in keys) is (capable and enabled)
    if device == "watch":
        assert keys == (["work"] if capable and enabled else [])
        assert model["menu"] == [] and model["signout"] == {}
    else:
        assert "brand" in keys and "settings" in keys


