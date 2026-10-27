"""Tests for the owner-surface confirmation gate in orchestrator/hitl_confirmation.py: a
single-use, expiring confirmation bound to the signed-in human socket, owner, action and exact
target, with bounded issuance and audited proposal and consumption.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as hc

TARGET = {"machine_id": "m1"}


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return object()


def _orch(*sockets, owner="u1", claims=None):
    orch = SimpleNamespace(ui_sessions={}, audit_recorder=_Recorder(), _ws_active_chat={})
    for ws in sockets:
        orch.ui_sessions[ws] = dict(claims or {"sub": owner})
    return orch


async def test_a_plain_request_issues_one_confirmation_that_runs_once():
    ws = object()
    orch = _orch(ws)
    issued = await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET)
    assert isinstance(issued, str) and len(issued) == 32
    assert await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET) == issued
    assert await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET, issued)
    assert not await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET, issued)
    transitions = [event.action_type for event in orch.audit_recorder.events]
    assert transitions == ["hitl.proposed", "hitl.consumed"]
    assert orch.audit_recorder.events[0].inputs_meta["tool"] == "chrome_machine_delete"


@pytest.mark.parametrize("claims", [{"sub": "u2"}, {"sub": "u1", "machine_class": "agent"},
                                    {"sub": "u1", "_invocation_channel": "mcp"}])
async def test_only_the_signed_in_owner_socket_can_request(claims):
    ws = object()
    orch = _orch(ws, claims=claims)
    assert await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET) is None
    assert await hc.request_surface_confirmation(orch, None, "u1", "chrome_machine_delete", TARGET) is None


@pytest.mark.parametrize("change", ["socket", "owner", "action", "target", "unknown", "type"])
async def test_a_confirmation_is_bound_to_socket_owner_action_and_target(change):
    ws, other = object(), object()
    orch = _orch(ws, other)
    issued = await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET)
    args = {"socket": (other, "u1", "chrome_machine_delete", TARGET, issued),
            "owner": (ws, "u2", "chrome_machine_delete", TARGET, issued),
            "action": (ws, "u1", "chrome_machine_credential_delete", TARGET, issued),
            "target": (ws, "u1", "chrome_machine_delete", {"machine_id": "m2"}, issued),
            "unknown": (ws, "u1", "chrome_machine_delete", TARGET, "0" * 32),
            "type": (ws, "u1", "chrome_machine_delete", TARGET, ["x"])}[change]
    assert not await hc.consume_surface_confirmation(orch, *args)
    assert await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET, issued)


async def test_a_confirmation_expires(monkeypatch):
    ws = object()
    orch = _orch(ws)
    now = [1000.0]
    monkeypatch.setattr(hc.time, "monotonic", lambda: now[0])
    issued = await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET)
    now[0] += hc.SURFACE_CONFIRMATION_TTL_S + 1
    assert not await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET, issued)


async def test_a_signed_out_socket_cannot_consume():
    ws = object()
    orch = _orch(ws)
    issued = await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET)
    orch.ui_sessions.pop(ws)
    assert not await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", TARGET, issued)


async def test_issuance_is_bounded_per_owner():
    ws = object()
    orch = _orch(ws)
    for index in range(hc.SURFACE_CONFIRMATIONS_PER_OWNER):
        assert await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete",
                                                     {"machine_id": f"m{index}"})
    assert await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete",
                                                 {"machine_id": "overflow"}) is None


async def test_an_unserializable_target_is_refused():
    ws = object()
    orch = _orch(ws)
    assert await hc.request_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", {"x": object()}) is None
    assert not await hc.consume_surface_confirmation(orch, ws, "u1", "chrome_machine_delete", {"x": object()}, "a")
