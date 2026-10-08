"""Exercises custom palette persistence and accepted results through authenticated native ingress.
Real Plane theme preferences retain owner isolation, while invalid saves produce correlated failures.
"""

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from astralplane.repositories.preferences import ThemePreferenceRepository
from orchestrator import chrome_events
from orchestrator.plane_repository_context import PlaneRepositoryContext
from orchestrator.projection_surfaces import theme
from rote.capabilities import DeviceProfile
from tests.test_native_selection_ingress import (
    native_picker as native_picker, notes as notes, metadata as metadata, runtime as runtime,
    fixture as fixture, service as service, signing_key as signing_key, ingress as ingress,
    surface as surface, command as command, context as context, send, terminal,
)


@pytest.fixture
async def native_theme(native_picker, runtime, monkeypatch):
    state = native_picker
    state.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({
        "device_type": "ios", "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "button", "param_picker",
                            "container", "theme_apply", "color_picker"],
    })
    state.orch.theme_preference_context = PlaneRepositoryContext(
        plane_runtime=runtime, repository=runtime.repositories.preferences.theme)
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        "save_theme": ("theme", theme.HANDLERS["save_theme"]),
        "chrome_theme_preset": ("theme", theme.HANDLERS["chrome_theme_preset"]),
    })
    generation = send(state, payload={"surface": "theme"})
    assert (await terminal(state, generation))["state"] == "completed"
    return state


def responses(state, generation):
    return [frame for frame in state.socket.payloads()
            if frame.get("type") == "chrome_surface" and frame.get("surface_key") == "theme"
            and frame.get("request_generation") == generation]


async def stored(state, owner):
    context = state.orch.theme_preference_context
    record = await asyncio.to_thread(context.call, context.repository.get, owner_id=owner)
    return None if record is None else dict(record.theme)


async def test_actual_native_custom_color_returns_accepted_full_palette(native_theme, fixture):
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "abcdef"},
    })
    assert (await terminal(native_theme, generation))["state"] == "completed"
    frame, = responses(native_theme, generation)
    applied = frame["components"][0]
    assert applied["type"] == "theme_apply" and applied["colors"]["accent"] == "#ABCDEF"
    assert set(applied["colors"]) == {key for key, _ in theme._COLOR_KEYS}
    assert await stored(native_theme, fixture[1]) == {"colors": applied["colors"]}


async def test_actual_retained_theme_form_reclaims_its_owned_result_after_registration(native_theme, fixture):
    from tests.test_work_surface_ingress_088 import registered

    await registered(native_theme)
    assert chrome_events.open_surface_for(native_theme.orch, native_theme.socket) == ""
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#ABCDEF"},
    })
    assert (await terminal(native_theme, generation))["state"] == "completed"
    frame, = responses(native_theme, generation)
    assert frame["components"][0]["type"] == "theme_apply"
    assert frame["components"][0]["colors"]["accent"] == "#ABCDEF"
    assert chrome_events.open_surface_for(native_theme.orch, native_theme.socket) == "theme"
    assert await stored(native_theme, fixture[1]) == {"colors": frame["components"][0]["colors"]}


@pytest.mark.parametrize("change", [
    "register", "owner", "reused_generation", "context", "registration", "roles", "connection_generation",
])
@pytest.mark.parametrize("phase", ["handler", "render", "delivery"])
async def test_actual_held_theme_result_stays_with_original_socket_registration(
    native_theme, fixture, monkeypatch, change, phase,
):
    from tests.test_work_surface_ingress_088 import registered

    entered, release = asyncio.Event(), asyncio.Event()
    handler = theme.HANDLERS["save_theme"]
    calls = 0
    deliveries = 0

    async def held(host, socket, owner, roles, payload):
        nonlocal calls
        calls += 1
        result = await handler(host, socket, owner, roles, payload)
        if phase == "handler":
            entered.set()
            await release.wait()
        return result

    builder = theme.components
    delivery = chrome_events._verify_human_delivery

    async def held_builder(*args, **kwargs):
        result = await builder(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    async def held_delivery(*args, **kwargs):
        nonlocal deliveries
        deliveries += 1
        if deliveries == 2:
            entered.set()
            await release.wait()
        await delivery(*args, **kwargs)

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"save_theme": ("theme", held)})
    if phase == "render":
        monkeypatch.setattr(theme, "components", held_builder)
    if phase == "delivery":
        monkeypatch.setattr(chrome_events, "_verify_human_delivery", held_delivery)
    context = native_theme.orch._connection_contexts[id(native_theme.socket)]
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#123456"},
    })
    newer = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await stored(native_theme, fixture[1]))["colors"]["accent"] == "#123456"
        if change in {"register", "owner", "reused_generation"}:
            if change != "register":
                native_theme.token = fixture[3](sub=str(uuid4()))
            await registered(native_theme)
            assert chrome_events.open_surface_for(native_theme.orch, native_theme.socket) == ""
            if change == "reused_generation":
                newer = send(native_theme, payload={"surface": "theme"}, generation=generation)
                await native_theme.barrier()
                assert chrome_events.surface_request_current(native_theme.orch, native_theme.socket, "theme", newer)
        elif change == "context":
            native_theme.orch._connection_contexts[id(native_theme.socket)] = object()
        elif change == "registration":
            native_theme.orch.ui_sessions[native_theme.socket] = deepcopy(native_theme.orch.ui_sessions[native_theme.socket])
        elif change == "roles":
            native_theme.orch.ui_sessions[native_theme.socket]["realm_access"]["roles"].append("admin")
        else:
            context.connection_generation = uuid4()
        release.set()
        assert (await terminal(native_theme, generation))["state"] == "completed"
        if newer is not None:
            assert (await terminal(native_theme, newer))["state"] == "completed"
            assert chrome_events.open_surface_for(native_theme.orch, native_theme.socket) == "theme"
        assert not any("#123456" in json.dumps(frame) for frame in responses(native_theme, generation))
        assert calls == 1 and (await stored(native_theme, fixture[1]))["colors"]["accent"] == "#123456"
    finally:
        native_theme.orch._connection_contexts[id(native_theme.socket)] = context
        release.set()


@pytest.mark.parametrize("change", ["register", "owner", "context", "connection_generation"])
async def test_actual_theme_claim_retired_during_authorization_never_invokes_handler(
    native_theme, fixture, monkeypatch, change,
):
    from tests.test_work_surface_ingress_088 import registered

    entered, release = asyncio.Event(), asyncio.Event()
    original = chrome_events._verify_human_delivery

    async def held(host, socket):
        entered.set()
        await release.wait()
        await original(host, socket)

    handler = AsyncMock(wraps=theme.HANDLERS["save_theme"])
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"save_theme": ("theme", handler)})
    monkeypatch.setattr(chrome_events, "_verify_human_delivery", held)
    context = native_theme.orch._connection_contexts[id(native_theme.socket)]
    previous = await stored(native_theme, fixture[1])
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#123456"},
    })
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change in {"register", "owner"}:
            if change == "owner":
                native_theme.token = fixture[3](sub=str(uuid4()))
            await registered(native_theme)
        elif change == "context":
            native_theme.orch._connection_contexts[id(native_theme.socket)] = object()
        else:
            context.connection_generation = uuid4()
        release.set()
        assert (await terminal(native_theme, generation))["state"] == "failed"
        handler.assert_not_awaited()
        assert responses(native_theme, generation) == []
        assert await stored(native_theme, fixture[1]) == previous
    finally:
        native_theme.orch._connection_contexts[id(native_theme.socket)] = context
        release.set()


async def test_actual_native_successive_custom_colors_preserve_other_saved_roles(native_theme, fixture):
    for role, value in (("primary", "#123456"), ("accent", "#ABCDEF")):
        generation = send(native_theme, action="save_theme", payload={
            "surface": "theme", "theme": {"color_key": role, "color_value": value},
        })
        assert (await terminal(native_theme, generation))["state"] == "completed"
    saved = await stored(native_theme, fixture[1])
    assert saved["colors"]["primary"] == "#123456" and saved["colors"]["accent"] == "#ABCDEF"
    assert responses(native_theme, generation)[0]["components"][0]["colors"] == saved["colors"]


@pytest.mark.parametrize("submitted", [None, [], {}, {"color_key": "unknown", "color_value": "#123456"},
    {"color_key": "accent", "color_value": "<script>"}, {"color_key": "accent", "color_value": 123456},
    {"colors": {"extra-role": "#123456"}}, {"colors": {}}, {"preset": "unavailable"}])
async def test_actual_native_invalid_palette_fails_without_persisting_or_painting(native_theme, fixture, submitted):
    previous = await stored(native_theme, fixture[1])
    generation = send(native_theme, action="save_theme", payload={"surface": "theme", "theme": submitted})
    assert (await terminal(native_theme, generation))["state"] == "failed"
    frame, = responses(native_theme, generation)
    assert frame["components"][0]["type"] == "alert" and frame["components"][0]["variant"] == "error"
    assert "theme_apply" not in json.dumps(frame)
    assert await stored(native_theme, fixture[1]) == previous


async def test_actual_native_theme_write_failure_is_correlated_and_never_completed(native_theme, monkeypatch, fixture):
    previous = await stored(native_theme, fixture[1])

    def fail(self, transaction, **kwargs):
        raise RuntimeError("private persistence failure")

    monkeypatch.setattr(ThemePreferenceRepository, "put", fail)
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#123456"},
    })
    assert (await terminal(native_theme, generation))["state"] == "failed"
    frame, = responses(native_theme, generation)
    assert frame["components"][0]["variant"] == "error"
    assert "theme_apply" not in json.dumps(frame) and "private persistence" not in json.dumps(frame)
    assert await stored(native_theme, fixture[1]) == previous


async def test_actual_native_theme_read_failure_cannot_turn_into_an_accepted_default(native_theme, monkeypatch):
    def fail(self, query, **kwargs):
        raise RuntimeError("private preference read failure")

    monkeypatch.setattr(ThemePreferenceRepository, "get", fail)
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#123456"},
    })
    assert (await terminal(native_theme, generation))["state"] == "failed"
    frame, = responses(native_theme, generation)
    assert frame["components"][0]["variant"] == "error"
    assert "theme_apply" not in json.dumps(frame)


async def test_actual_web_custom_color_returns_the_accepted_html_palette_side_effect(native_theme, fixture):
    native_theme.orch.rote.get_profile = lambda _: DeviceProfile.default()
    generation = send(native_theme, action="save_theme", payload={
        "surface": "theme", "theme": {"color_key": "accent", "color_value": "#123456"},
    })
    assert (await terminal(native_theme, generation))["state"] == "completed"
    rendered, = [frame for frame in native_theme.socket.payloads() if frame.get("type") == "chrome_render"]
    assert "astral-theme-apply" in rendered["html"] and "#123456" in rendered["html"]
    assert (await stored(native_theme, fixture[1]))["colors"]["accent"] == "#123456"


async def test_actual_native_custom_theme_cannot_select_another_owner(native_theme, fixture, runtime):
    foreign = str(uuid4())

    def seed():
        with runtime.transaction() as transaction:
            runtime.repositories.identity.upsert_identity(transaction, owner_id=foreign, observed_at=1)
            runtime.repositories.preferences.theme.put(transaction, owner_id=foreign, theme={"preset": "sunset"})

    await asyncio.to_thread(seed)
    generation = send(native_theme, action="save_theme", payload={"surface": "theme", "user_id": foreign,
        "theme": {"color_key": "accent", "color_value": "#123456"}})
    assert (await terminal(native_theme, generation))["state"] == "completed"
    assert await stored(native_theme, foreign) == {"preset": "sunset"}
    assert (await stored(native_theme, fixture[1]))["colors"]["accent"] == "#123456"
    assert foreign not in json.dumps(responses(native_theme, generation))
