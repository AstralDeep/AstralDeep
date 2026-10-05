"""Tests for orchestrator/work_admission.py's durable credential-save completion: a
native provider Save closes the surface via _complete_connection_operation rather
than the chrome handler, so the dialog is never left stranded.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from orchestrator.work_admission import OperationState


def _completed():
    return SimpleNamespace(state=OperationState.COMPLETED)


def _work(action="chrome_llm_save", generation=None):
    return SimpleNamespace(
        frame=SimpleNamespace(
            operation_kind="llm_credential_save",
            action=action,
            deadline_at_monotonic=float("inf"),
            request_generation=generation or str(uuid4()),
        ),
        owner=SimpleNamespace(owner_user_id="owner-1"),
        operation_id="op-1",
        committed_operation=_completed(),
        auth_principal="owner-1@example",
    )


@pytest.fixture
def orch(monkeypatch):
    from orchestrator import chrome_events, projection_surfaces
    from orchestrator.orchestrator import Orchestrator
    from rote.rote import ROTE

    o = Orchestrator.__new__(Orchestrator)
    o.rote = ROTE()
    o.work_admission = MagicMock()
    o._send_operation_terminal = AsyncMock()
    o._send_operation_projection = AsyncMock()
    o._notify_interactive_capacity = AsyncMock()
    o._call_work_admission = AsyncMock(return_value=_completed())
    o.ui_sessions = {}
    o.llm_configured_for = AsyncMock(return_value=True)
    sent = []

    async def _safe_send(ws, data):
        sent.append((ws, data))
        return True

    o._safe_send = _safe_send
    o.sent = sent
    async def components(host, owner, roles, params):
        return [{"type": "text", "content": "Current settings"}]

    async def save(host, target, owner, roles, payload):
        return None

    module = SimpleNamespace(TITLE="Settings", components=components, ADMIN_ONLY=False)
    monkeypatch.setattr(projection_surfaces, "get_surface", lambda key: module)
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_llm_save": ("llm", save)})
    return o


def _ctx(orch, device):
    from orchestrator.chrome_events import _note_open_surface

    ws = MagicMock()
    orch.rote.register_device(ws, {"device_type": device})
    orch.ui_sessions[ws] = {"sub": "owner-1", "realm_access": {"roles": ["user"]}}
    _note_open_surface(orch, ws, "llm")
    return SimpleNamespace(websocket=ws), ws


async def _pending_save(orch, websocket, work):
    from orchestrator.chrome_events import handle_chrome_event

    await handle_chrome_event(orch, websocket, "chrome_llm_save", {"surface": "llm"},
        "owner-1", request_generation=work.frame.request_generation)


def _close_frames(orch):
    import json

    out = []
    for _ws, data in orch.sent:
        try:
            f = json.loads(data)
        except (TypeError, ValueError):
            continue
        if (f.get("type") == "chrome_surface"
                and f.get("surface_key") == ""
                and not (f.get("components") or [])):
            out.append(f)
    return out


@pytest.mark.parametrize("device", ["macos", "ios", "windows", "android"])
async def test_completed_save_closes_the_surface_when_no_gate_unlocked(
    orch, monkeypatch, device
):
    from orchestrator import llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=False))
    context, ws = _ctx(orch, device)
    work = _work()
    await _pending_save(orch, ws, work)

    await orch._complete_connection_operation(context, work)

    assert len(_close_frames(orch)) == 1
    assert _close_frames(orch)[0]["request_generation"] == work.frame.request_generation


@pytest.mark.parametrize("surface", ["theme", "llm"])
async def test_completed_save_cannot_close_a_newer_surface_generation(orch, monkeypatch, surface):
    from orchestrator import chrome_events, llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=False))
    context, ws = _ctx(orch, "android")
    work = _work()
    await _pending_save(orch, ws, work)
    newer = str(uuid4())
    await chrome_events.handle_chrome_event(orch, ws, "chrome_open", {"surface": surface},
        "owner-1", request_generation=newer)

    await orch._complete_connection_operation(context, work)

    assert _close_frames(orch) == []
    assert chrome_events.open_surface_for(orch, ws) == surface
    assert any('"type": "llm_config_ack"' in data for _, data in orch.sent)


async def test_completed_save_cannot_close_after_native_navigation_retirement(orch, monkeypatch):
    from orchestrator import chrome_events, llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=False))
    context, ws = _ctx(orch, "windows")
    work = _work()
    await _pending_save(orch, ws, work)
    await chrome_events.handle_chrome_event(orch, ws, "chrome_close", {}, "owner-1")
    before = len(_close_frames(orch))

    await orch._complete_connection_operation(context, work)

    assert len(_close_frames(orch)) == before


async def test_completed_save_does_not_double_close_when_the_gate_unlocked(
    orch, monkeypatch
):
    from orchestrator import llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=True))
    context, _ws = _ctx(orch, "macos")

    await orch._complete_connection_operation(context, _work())

    assert _close_frames(orch) == []


async def test_completed_save_leaves_the_web_modal_alone(orch, monkeypatch):
    from orchestrator import llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=False))
    context, _ws = _ctx(orch, "browser")

    await orch._complete_connection_operation(context, _work())

    assert _close_frames(orch) == []


async def test_legacy_llm_config_set_does_not_close_a_surface(orch, monkeypatch):
    from orchestrator import llm_gate

    monkeypatch.setattr(llm_gate, "unlock_after_save", AsyncMock(return_value=False))
    context, _ws = _ctx(orch, "macos")

    await orch._complete_connection_operation(context, _work(action="llm_config_set"))

    assert _close_frames(orch) == []
