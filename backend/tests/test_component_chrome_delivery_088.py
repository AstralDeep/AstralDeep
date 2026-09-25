"""Tests for outbound component-action delivery (backend/orchestrator/orchestrator.py,
AstralProjection's rote.py): host flags and pre-adaptation facts carried through
canvas, snapshot, readaptation, and per-device upsert.
"""

from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.history import _canonical_component, augment_conversation_snapshot_for_target
from orchestrator.orchestrator import Orchestrator
from rote.capabilities import DeviceProfile
from rote.rote import ROTE
from shared.feature_flags import flags


def table(**changes):
    return {"type": "table", "component_id": "wc_table", "headers": ["a"],
            "rows": [["1"]], "versions": [{"version_no": 1, "body": "private prior body"}],
            "component_chrome": {"version": 9, "actions": [{"kind": "delete"}]}, **changes}


def actions(component):
    return [entry["kind"] for entry in component["component_chrome"]["actions"]]


def snapshot(components):
    return {"transcript": [], "canvas": {"components": components}}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    for feature, variable in (("component_refine", "FF_COMPONENT_REFINE"),
                              ("artifact_export", "FF_ARTIFACT_EXPORT"),
                              ("artifact_sharing", "FF_ARTIFACT_SHARING")):
        monkeypatch.setitem(flags._flags, feature, True)
        monkeypatch.setenv(variable, "true")


def harness(device="browser"):
    socket = object()
    host = object.__new__(Orchestrator)
    host.rote = ROTE()
    host.rote.register_device(socket, {"device_type": device, "supported_types": ["text"]})
    host._safe_send = AsyncMock()
    host.ui_clients = [socket]
    host._get_user_id = lambda ws: "owner"
    host._ws_active_chat = {id(socket): "chat"}
    return host, socket


@pytest.mark.parametrize("device", ["browser", "android", "ios", "macos", "windows"])
async def test_full_canvas_and_snapshot_keep_original_table_actions_after_real_rote(device):
    host, socket = harness(device)
    original = [table()]
    untouched = copy.deepcopy(original)
    await host.send_ui_render(socket, original, speak=False)
    render = json.loads(host._safe_send.await_args.args[1])
    received = render["components"][0]
    assert actions(received) == ["refine", "history", "csv", "share"]
    assert received["versions"] == [{"version_no": 1, "reason": "", "created_at": "", "title": ""}]
    assert "astral-export-csv" in render["html"]
    if device != "browser":
        assert received["type"] == "text"
    hydrated = host._adapt_conversation_snapshot(socket, snapshot(original))
    canvas = hydrated["canvas"]["components"][0]
    assert canvas["component_chrome"] == received["component_chrome"]
    if device == "browser":
        assert "astral-export-csv" in canvas["_presentation"]["html"]
    else:
        assert "_presentation" not in canvas
    assert original == untouched
    assert host.rote._last_components[socket] == original


@pytest.mark.parametrize("device", ["watch", "voice"])
async def test_restricted_profiles_omit_component_actions_and_version_metadata(device):
    host, socket = harness(device)
    await host.send_ui_render(socket, [table()], speak=False)
    received = json.loads(host._safe_send.await_args.args[1])["components"][0]
    assert "component_chrome" not in received and "versions" not in received
    result = host._adapt_conversation_snapshot(socket, snapshot([table()]))
    received = result["canvas"]["components"][0]
    assert "component_chrome" not in received and "versions" not in received


async def test_upsert_uses_each_receivers_profile_and_preserves_remove_fanout():
    host, browser = harness()
    android, watch, other_owner, other_chat = object(), object(), object(), object()
    for socket, device in ((android, "android"), (watch, "watch"), (other_owner, "ios"), (other_chat, "ios")):
        host.rote.register_device(socket, {"device_type": device, "supported_types": ["text"]})
    host.ui_clients = [browser, android, watch, other_owner, other_chat]
    host._ws_active_chat = {id(ws): "chat" for ws in host.ui_clients}
    host._ws_active_chat[id(other_chat)] = "elsewhere"
    host._get_user_id = lambda ws: "other" if ws is other_owner else "owner"
    comp = table()
    before = copy.deepcopy(comp)
    await host.send_ui_upsert(browser, "chat", "owner", [
        {"op": "upsert", "component_id": "wc_table", "component": comp},
        {"op": "remove", "component_id": "removed"},
    ])
    assert host._safe_send.await_count == 3
    for call in host._safe_send.await_args_list:
        socket, encoded = call.args
        payload = json.loads(encoded)
        assert payload["ops"][1] == {"op": "remove", "component_id": "removed"}
        received = payload["ops"][0]["component"]
        if socket is watch:
            assert "component_chrome" not in received and "versions" not in received
        else:
            assert actions(received) == ["refine", "history", "csv", "share"]
            assert "astral-export-csv" in payload["ops"][0]["html"]
    assert comp == before


async def test_targeted_readaptation_carries_actions_after_original_type_fallback():
    host, socket = harness("android")
    original = table()
    before = copy.deepcopy(original)
    assert await host._readapt_targeted(socket, "chat", DeviceProfile.default(),
                                       host.rote.get_profile(socket), [original])
    payload = json.loads(host._safe_send.await_args.args[1])
    op = payload["ops"][0]
    assert op["component"]["type"] == "text"
    assert "csv" in actions(op["component"])
    assert "astral-export-csv" in op["html"]
    assert original == before


async def test_unknown_identity_and_host_flag_off_cannot_be_overridden(monkeypatch):
    host, socket = harness("android")
    monkeypatch.setitem(flags._flags, "component_refine", False)
    monkeypatch.setitem(flags._flags, "artifact_export", False)
    monkeypatch.setitem(flags._flags, "artifact_sharing", False)
    await host.send_ui_render(socket, [table()], speak=False)
    payload = json.loads(host._safe_send.await_args.args[1])
    assert actions(payload["components"][0]) == []
    assert "astral-component-chrome" not in payload["html"]
    for feature in ("component_refine", "artifact_export", "artifact_sharing"):
        monkeypatch.setitem(flags._flags, feature, True)
    originals = [table(), table()]
    out = host._adapt_conversation_snapshot(socket, snapshot(originals))
    assert all(actions(c) == [] for c in out["canvas"]["components"])


@pytest.mark.parametrize("device", ["browser", "android", "windows"])
@pytest.mark.parametrize("distinct_rows", [False, True])
def test_snapshot_consolidation_retains_duplicate_identity_denial(device, distinct_rows):
    host, socket = harness(device)
    originals = [table(), table(rows=[["different"]] if distinct_rows else [["1"]])]
    before = copy.deepcopy(originals)
    result = host._adapt_conversation_snapshot(socket, snapshot(originals))
    received = result["canvas"]["components"]
    assert len(received) == (2 if distinct_rows else 1)
    assert all(component["component_id"] == "wc_table" for component in received)
    assert all(actions(component) == [] for component in received)
    assert all("versions" not in component for component in received)
    assert "private prior body" not in json.dumps(result)
    assert originals == before
    assert host.rote.get_cached_components(socket) == before


@pytest.mark.parametrize("device", ["browser", "android", "windows"])
def test_snapshot_consolidation_keeps_unambiguous_survivor_actions(device):
    host, socket = harness(device)
    originals = [table(), table(component_id="wc_second")]
    before = copy.deepcopy(originals)
    value = snapshot(originals)
    value["transcript"] = [{"parts": [{"type": "text", "text": "Retained transcript"},
                                     {"type": "components", "components": [{"type": "text", "content": "Earlier result"}]}]}]
    result = host._adapt_conversation_snapshot(socket, value)
    received, = result["canvas"]["components"]
    assert received["component_id"] == "wc_second"
    assert actions(received) == ["refine", "history", "csv", "share"]
    assert received["versions"] == [{"version_no": 1, "reason": "", "created_at": "", "title": ""}]
    assert "private prior body" not in json.dumps(result)
    assert result["transcript"][0]["parts"][0]["text"] == "Retained transcript"
    assert originals == before
    assert host.rote.get_cached_components(socket) == before


@pytest.mark.parametrize("device", ["browser", "android", "windows"])
@pytest.mark.parametrize("workspace_state", ["absent", "empty", "unavailable"])
@pytest.mark.parametrize("distinct_rows", [False, True])
async def test_snapshot_duplicate_identity_stays_denied_after_resize_fallback(
    device, workspace_state, distinct_rows, monkeypatch,
):
    import audit.hooks
    host, socket = harness(device)
    host.ui_sessions = {socket: {"sub": "owner"}}
    host.speech_server_available = lambda: False
    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())

    def workspace(*_args):
        if workspace_state == "unavailable":
            raise RuntimeError("synthetic storage failure")
        return []

    host._canvas_components = workspace
    if workspace_state == "absent":
        host._ws_active_chat.clear()
    original = [table(), table(rows=[["different"]] if distinct_rows else [["1"]])]
    before = copy.deepcopy(original)
    result = host._adapt_conversation_snapshot(socket, snapshot(original))
    assert all(actions(component) == [] for component in result["canvas"]["components"])
    await host.handle_ui_message(socket, json.dumps({
        "type": "ui_event", "action": "update_device", "payload": {"device": {
            "device_type": device, "viewport_width": 320, "supported_types": ["text"],
        }},
    }))
    await asyncio.sleep(0)
    frames = [json.loads(call.args[1]) for call in host._safe_send.await_args_list]
    assert [frame["type"] for frame in frames] == ["rote_config", "ui_update"]
    received = frames[-1]["components"]
    assert len(received) == 2
    assert all(component["component_id"] == "wc_table" for component in received)
    assert all(actions(component) == [] for component in received)
    assert all("versions" not in component for component in received)
    assert "private prior body" not in json.dumps(frames)
    assert original == before and host.rote.get_cached_components(socket) == before


async def test_html_failure_keeps_the_same_safe_native_inventory(monkeypatch):
    import webrender
    host, socket = harness("android")

    def broken(*_args, **_kwargs):
        raise RuntimeError("renderer unavailable")
    monkeypatch.setattr(webrender, "render_workspace", broken)
    await host.send_ui_render(socket, [table()], speak=False)
    payload = json.loads(host._safe_send.await_args.args[1])
    assert payload.get("html") is None
    assert actions(payload["components"][0]) == ["refine", "history", "csv", "share"]
    monkeypatch.setattr(webrender, "render_component_fragment", broken)
    await host.send_ui_upsert(socket, "chat", "owner", [{"op": "upsert", "component_id": "wc_table", "component": table()}])
    op = json.loads(host._safe_send.await_args.args[1])["ops"][0]
    assert op["html"] is None and "csv" in actions(op["component"])


def test_semantic_component_discards_only_top_level_receiver_metadata():
    proposed = table(data={"component_chrome": "ordinary user data"})
    clean = _canonical_component(proposed, 0)
    assert "component_chrome" not in clean
    assert clean["data"] == {"component_chrome": "ordinary user data"}
    assert "component_chrome" in proposed


def test_snapshot_html_uses_original_table_identity_without_mutating_inputs():
    source = table()
    adapted = {"type": "text", "component_id": "wc_table", "content": "Reduced table"}
    original_snapshot = snapshot([adapted])
    before = copy.deepcopy(original_snapshot)
    result = augment_conversation_snapshot_for_target(
        original_snapshot, DeviceProfile.default(), target="web", canonical_canvas=[source],
    )
    received = result["canvas"]["components"][0]
    assert "csv" in actions(received)
    assert "astral-export-csv" in received["_presentation"]["html"]
    assert original_snapshot == before
    assert "component_chrome" not in _canonical_component(received, 0)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("workspace_state", ["absent", "empty", "unavailable"])
async def test_real_update_device_legacy_fallback_rebuilds_metadata_from_raw_cache(
    enabled, workspace_state, monkeypatch,
):
    import audit.hooks
    host, socket = harness("android")
    host.ui_sessions = {socket: {"sub": "owner"}}
    host.speech_server_available = lambda: False
    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    for feature in ("component_refine", "artifact_export", "artifact_sharing"):
        monkeypatch.setitem(flags._flags, feature, enabled)

    def workspace(*_args):
        if workspace_state == "unavailable":
            raise RuntimeError("synthetic storage failure")
        return []
    host._canvas_components = workspace
    if workspace_state == "absent":
        host._ws_active_chat.clear()
    original = [table()]
    before = copy.deepcopy(original)
    host.rote.adapt(socket, original)
    await host.handle_ui_message(socket, json.dumps({
        "type": "ui_event", "action": "update_device", "payload": {"device": {
            "device_type": "android", "viewport_width": 320, "supported_types": ["text"],
        }},
    }))
    await asyncio.sleep(0)
    frames = [json.loads(call.args[1]) for call in host._safe_send.await_args_list]
    assert [frame["type"] for frame in frames] == ["rote_config", "ui_update"]
    received = frames[-1]["components"][0]
    assert received["type"] == "text"
    assert actions(received) == (["refine", "history", "csv", "share"] if enabled else [])
    assert received["versions"] == [{"version_no": 1, "reason": "", "created_at": "", "title": ""}]
    assert original == before and host.rote.get_cached_components(socket) == before


@pytest.mark.parametrize("change", ["new_owner", "same_owner_registration", "mutated_owner", "retired"])
async def test_viewport_send_held_across_registration_change_drops_captured_content(change, monkeypatch):
    import audit.hooks
    host, socket = harness("android")
    host.ui_sessions = {socket: {"sub": "owner"}}
    host._get_user_id = lambda ws: (host.ui_sessions.get(ws) or {}).get("sub")
    host.speech_server_available = lambda: False
    host._canvas_components = lambda *_args: pytest.fail("old workspace must not be read after registration changes")
    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    host.rote.adapt(socket, [table()])
    entered, release = asyncio.Event(), asyncio.Event()
    frames = []

    async def held_send(_socket, data):
        frames.append(json.loads(data))
        if frames[-1]["type"] == "rote_config":
            entered.set()
            await release.wait()
    host._safe_send = held_send
    pending = asyncio.create_task(host.handle_ui_message(socket, json.dumps({
        "type": "ui_event", "action": "update_device", "payload": {"device": {
            "device_type": "android", "viewport_width": 320, "supported_types": ["text"],
        }},
    })))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "retired":
            host.ui_sessions.pop(socket)
        elif change == "mutated_owner":
            host.ui_sessions[socket]["sub"] = "replacement"
        else:
            host.ui_sessions[socket] = {"sub": "owner" if change == "same_owner_registration" else "replacement"}
    finally:
        release.set()
        await asyncio.wait_for(pending, 2)
    assert [frame["type"] for frame in frames] == ["rote_config"]
