"""Checks native ordinary settings results against the admitted request and surface owner.
Private owner surfaces retain mandatory correlation while legacy ordinary requests remain compatible.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid1, uuid4

import pytest

from orchestrator import chrome_events, projection_surfaces
from rote.capabilities import DeviceProfile
from shared.protocol import ChromeRender, ChromeSurface, ProtocolValidationError


@pytest.fixture
def ordinary(monkeypatch):
    socket = object()
    state = SimpleNamespace(socket=socket, sent=[], calls=[])
    profile = DeviceProfile.from_dict({"device_type": "ios"})
    state.orch = SimpleNamespace(ui_sessions={socket: {"sub": "owner", "realm_access": {"roles": ["user"]}}},
                                 rote=SimpleNamespace(get_profile=lambda _: profile))

    async def configured(owner):
        return True

    async def send(target, wire):
        state.sent.append((target, json.loads(wire)))
        return True

    async def build(host, owner, roles, params):
        return [{"type": "text", "content": "Current settings"}]

    async def save(host, target, owner, roles, payload):
        state.calls.append((owner, dict(payload)))
        return ("theme", {}, "")

    state.module = SimpleNamespace(TITLE="Theme", components=build, ADMIN_ONLY=False)
    state.orch.llm_configured_for = configured
    state.orch._safe_send = send
    monkeypatch.setattr(projection_surfaces, "get_surface", lambda key: state.module
                        if key in {"theme", "audit", "llm"} else None)
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        "chrome_theme_apply": ("theme", save), "chrome_llm_save": ("llm", save),
    })
    return state


async def open_theme(state, generation=None):
    await chrome_events.handle_chrome_event(state.orch, state.socket, "chrome_open",
        {"surface": "theme"}, "owner", request_generation=generation)


def wire(state):
    return state.sent[-1][1]


def test_native_ordinary_replace_frame_preserves_a_canonical_generation():
    generation = str(uuid4())
    frame = ChromeSurface(surface_key="theme", request_generation=generation)
    assert json.loads(frame.to_json())["request_generation"] == generation


@pytest.mark.parametrize("surface,region,mode", [
    ("theme", "topbar", "replace"),
    ("theme", "modal", "mandatory"), ("a" * 129, "modal", "replace"),
])
def test_ordinary_correlation_requires_a_bounded_named_replace_modal(surface, region, mode):
    with pytest.raises(ProtocolValidationError):
        ChromeSurface(surface_key=surface, region=region, mode=mode,
                      request_generation=str(uuid4())).to_json()


def test_correlated_empty_modal_close_retains_its_request_generation():
    generation = str(uuid4())
    frame = ChromeSurface(request_generation=generation)
    assert json.loads(frame.to_json())["request_generation"] == generation


@pytest.mark.parametrize("changes", [{"title": "Unowned title"}, {"admin_only": True},
    {"components": [{"type": "text", "content": "Unowned content"}]},
    {"region": "topbar"}, {"mode": "mandatory"}, {"mode": "append"},
    {"selection": {"version": 1, "agent": None, "skills": [], "notes": []}}])
def test_correlated_empty_close_rejects_content_or_other_presentation_modes(changes):
    with pytest.raises(ProtocolValidationError):
        ChromeSurface(request_generation=str(uuid4()), **changes).to_json()


@pytest.mark.parametrize("generation", ["", "bad", 1, True, str(uuid1()), str(uuid4()).upper()])
def test_ordinary_wire_generation_must_be_canonical_uuid4(generation):
    with pytest.raises(ProtocolValidationError):
        ChromeSurface(surface_key="theme", request_generation=generation).to_json()


def test_uncorrelated_ordinary_and_mandatory_frames_remain_compatible():
    assert "request_generation" not in json.loads(ChromeSurface(surface_key="theme").to_json())
    assert "request_generation" not in json.loads(ChromeSurface(surface_key="llm", mode="mandatory").to_json())
    assert "request_generation" not in json.loads(ChromeRender(html="settings").to_json())


@pytest.mark.parametrize("surface", ["work", "guidance"])
def test_private_surfaces_still_require_their_generation(surface):
    with pytest.raises(ProtocolValidationError):
        ChromeSurface(surface_key=surface).to_json()


@pytest.mark.parametrize("device", ["ios", "macos", "android", "windows"])
async def test_native_open_echoes_its_surface_request_generation(ordinary, device):
    ordinary.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device})
    generation = str(uuid4())
    await open_theme(ordinary, generation)
    assert wire(ordinary)["surface_key"] == "theme"
    assert wire(ordinary)["request_generation"] == generation


async def test_action_result_echoes_only_the_handlers_owner_generation(ordinary):
    await open_theme(ordinary)
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme", "fields": {"theme": "light"}}, "owner", request_generation=generation)
    assert ordinary.calls == [("owner", {"surface": "theme", "fields": {"theme": "light"}})]
    assert wire(ordinary)["surface_key"] == "theme"
    assert wire(ordinary)["request_generation"] == generation


async def test_client_surface_cannot_change_the_handler_owner_or_run_the_action(ordinary):
    await open_theme(ordinary)
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "audit"}, "owner", request_generation=generation)
    assert ordinary.calls == []
    assert wire(ordinary)["surface_key"] == "theme"
    assert wire(ordinary)["request_generation"] == generation
    assert wire(ordinary)["components"][0]["variant"] == "error"


@pytest.mark.parametrize("generation", ["", "bad", 1, True, str(uuid1()), str(uuid4()).upper()])
async def test_bad_envelope_generation_cannot_dispatch_an_ordinary_action(ordinary, generation):
    with pytest.raises(ProtocolValidationError):
        await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
            {"surface": "theme"}, "owner", request_generation=generation)
    assert ordinary.calls == [] and ordinary.sent == []


async def test_unsolicited_push_does_not_reuse_a_previous_generation(ordinary):
    await open_theme(ordinary, str(uuid4()))
    await chrome_events._push_surface(ordinary.orch, ordinary.socket, "theme", "Theme", False, [])
    assert "request_generation" not in wire(ordinary)


async def test_deferred_child_task_cannot_reuse_a_finished_handlers_response_scope(ordinary, monkeypatch):
    await open_theme(ordinary)
    release = asyncio.Event()
    pending = []

    async def deferred(host, target):
        await release.wait()
        await chrome_events._push_surface(host, target, "theme", "Late result", False, [])

    async def save(host, target, owner, roles, payload):
        pending.append(asyncio.create_task(deferred(host, target)))
        return ("theme", {}, "")

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", save)})
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=str(uuid4()))
    release.set()
    await asyncio.gather(*pending)
    assert len(ordinary.sent) == 2


async def test_cancelled_handler_does_not_leave_an_active_response_scope(ordinary, monkeypatch):
    await open_theme(ordinary)
    entered = asyncio.Event()

    async def held(host, target, owner, roles, payload):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", held)})
    pending = asyncio.create_task(chrome_events.handle_chrome_event(
        ordinary.orch, ordinary.socket, "chrome_theme_apply", {"surface": "theme"}, "owner",
        request_generation=str(uuid4())))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await chrome_events._push_surface(ordinary.orch, ordinary.socket, "theme", "Theme", False, [])
    assert "request_generation" not in wire(ordinary)


async def test_handler_push_to_another_socket_or_surface_cannot_borrow_correlation(ordinary, monkeypatch):
    await open_theme(ordinary)
    other = object()

    async def save(host, target, owner, roles, payload):
        await chrome_events._push_surface(host, other, "theme", "Other socket", False, [])
        await chrome_events._push_surface(host, target, "audit", "Other surface", False, [])
        equivalent_host = SimpleNamespace(**vars(host))
        await chrome_events._push_surface(equivalent_host, target, "theme", "Other host", False, [])

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", save)})
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=str(uuid4()))
    assert all("request_generation" not in frame for _, frame in ordinary.sent[-3:])


async def test_credential_save_alias_keeps_the_llm_handler_owner(ordinary, monkeypatch):
    async def save(host, target, owner, roles, payload):
        ordinary.calls.append((owner, payload))
        return ("llm", {}, "")

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_llm_save": ("llm", save)})
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_open",
        {"surface": "llm"}, "owner")
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_llm_save",
        {"surface": "llm_settings", "fields": {}}, "owner", request_generation=generation)
    assert len(ordinary.calls) == 1
    assert wire(ordinary)["surface_key"] == "llm" and wire(ordinary)["request_generation"] == generation


async def test_navigation_while_a_handler_is_pending_prevents_replacing_the_new_modal(ordinary, monkeypatch):
    await open_theme(ordinary)

    async def save(host, target, owner, roles, payload):
        chrome_events._note_open_surface(host, target, "audit")
        return ("theme", {}, "")

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", save)})
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=str(uuid4()))
    assert len(ordinary.sent) == 1
    assert chrome_events.open_surface_for(ordinary.orch, ordinary.socket) == "audit"


async def test_navigation_while_components_load_prevents_delivering_the_old_modal(ordinary):
    entered, release = asyncio.Event(), asyncio.Event()

    async def build(host, owner, roles, params):
        entered.set()
        await release.wait()
        return [{"type": "text", "content": "Old modal"}]

    ordinary.module.components = build
    task = asyncio.create_task(open_theme(ordinary, str(uuid4())))
    await entered.wait()
    chrome_events._note_open_surface(ordinary.orch, ordinary.socket, "audit")
    release.set()
    await task
    assert ordinary.sent == []


async def test_load_failure_reports_the_same_surface_generation(ordinary):
    async def broken(host, owner, roles, params):
        raise RuntimeError("private diagnostic")

    ordinary.module.components = broken
    generation = str(uuid4())
    await open_theme(ordinary, generation)
    assert wire(ordinary)["request_generation"] == generation
    assert wire(ordinary)["components"][0]["variant"] == "error"
    assert "private diagnostic" not in json.dumps([frame for _, frame in ordinary.sent])


@pytest.mark.parametrize("failure", [PermissionError("denied action"),
                                   ValueError("read-only component"), RuntimeError("budget exceeded")])
async def test_device_adaptation_failure_delivers_a_correlated_error_without_unadapted_controls(
        ordinary, monkeypatch, failure):
    from rote.adapter import ComponentAdapter

    async def unsafe(host, owner, roles, params):
        return [{"type": "button", "label": "Forbidden raw action", "action": "chrome_raw_action"}]

    def denied(components, profile):
        raise failure

    ordinary.module.components = unsafe
    monkeypatch.setattr(ComponentAdapter, "adapt", denied)
    generation = str(uuid4())
    await open_theme(ordinary, generation)
    assert wire(ordinary)["request_generation"] == generation
    assert wire(ordinary)["components"][0]["variant"] == "error"
    assert "Forbidden raw action" not in json.dumps(wire(ordinary))
    assert "chrome_raw_action" not in json.dumps(wire(ordinary))


async def test_admin_denial_is_correlated_without_calling_the_handler(ordinary):
    ordinary.module.ADMIN_ONLY = True
    generation = str(uuid4())
    await open_theme(ordinary, generation)
    assert wire(ordinary)["request_generation"] == generation
    assert "admin role" in wire(ordinary)["components"][0]["message"]
    ordinary.sent.clear()
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=generation)
    assert ordinary.calls == []
    assert wire(ordinary)["request_generation"] == generation


async def test_read_only_refusal_is_an_owned_correlated_result(ordinary, monkeypatch):
    await open_theme(ordinary)

    async def refuse(host, target, owner, roles, payload):
        return ("theme", {}, '<div class="text-red-400">This setting is read only.</div>')

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", refuse)})
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=generation)
    assert wire(ordinary)["request_generation"] == generation
    assert wire(ordinary)["components"][0]["variant"] == "error"


async def test_handler_failure_resets_response_context_and_reports_safe_error(ordinary, monkeypatch):
    await open_theme(ordinary)

    async def broken(host, target, owner, roles, payload):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", broken)})
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=generation)
    assert wire(ordinary)["request_generation"] == generation
    assert "private diagnostic" not in json.dumps(wire(ordinary))
    await chrome_events._push_surface(ordinary.orch, ordinary.socket, "theme", "Theme", False, [])
    assert "request_generation" not in wire(ordinary)


async def test_legacy_action_without_generation_still_renders(ordinary):
    await open_theme(ordinary)
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {}, "owner")
    assert len(ordinary.calls) == 1
    assert "request_generation" not in wire(ordinary)


async def test_close_cannot_borrow_the_last_surface_generation(ordinary):
    previous, closing = str(uuid4()), str(uuid4())
    await open_theme(ordinary, previous)
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_close",
        {}, "owner", request_generation=closing)
    assert wire(ordinary)["surface_key"] == ""
    assert wire(ordinary)["request_generation"] == closing
    assert wire(ordinary)["request_generation"] != previous


@pytest.mark.parametrize("surface", ["theme", "audit"])
async def test_same_or_different_surface_navigation_retires_a_pending_handler_close(ordinary, monkeypatch, surface):
    await open_theme(ordinary)
    entered, release = asyncio.Event(), asyncio.Event()

    async def closing(host, target, owner, roles, payload):
        entered.set()
        await release.wait()
        await chrome_events.push_close(host, target)

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"chrome_theme_apply": ("theme", closing)})
    old = asyncio.create_task(chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket,
        "chrome_theme_apply", {"surface": "theme"}, "owner", request_generation=str(uuid4())))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        generation = str(uuid4())
        await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_open",
            {"surface": surface}, "owner", request_generation=generation)
        release.set()
        await old
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
    assert len(ordinary.sent) == 2
    assert wire(ordinary)["surface_key"] == surface
    assert wire(ordinary)["request_generation"] == generation


async def test_close_rechecks_navigation_after_delivery_authority_await(ordinary, monkeypatch):
    generation = str(uuid4())
    await open_theme(ordinary, generation)
    newer = str(uuid4())

    async def verify(host, target):
        chrome_events.capture_surface_request(host, target, "chrome_open", {"surface": "theme"}, newer)

    monkeypatch.setattr(chrome_events, "_verify_human_delivery", verify)
    assert await chrome_events.push_close(ordinary.orch, ordinary.socket,
        surface_key="theme", request_generation=generation) is False
    assert len(ordinary.sent) == 1
    assert chrome_events.surface_request_current(ordinary.orch, ordinary.socket, "theme", newer)


async def test_surface_rechecks_navigation_after_delivery_authority_await(ordinary, monkeypatch):
    await open_theme(ordinary)
    newer = str(uuid4())

    async def verify(host, target):
        chrome_events.capture_surface_request(host, target, "chrome_open", {"surface": "theme"}, newer)

    monkeypatch.setattr(chrome_events, "_verify_human_delivery", verify)
    await chrome_events.handle_chrome_event(ordinary.orch, ordinary.socket, "chrome_theme_apply",
        {"surface": "theme"}, "owner", request_generation=str(uuid4()))
    assert len(ordinary.sent) == 1


@pytest.mark.parametrize("action,payload", [("load_chat", {}), ("new_chat", {}),
    ("chrome_open", {"surface": "work"}), ("chrome_open", {"surface": "guidance"}),
    ("chrome_open", {"surface": "agent_intro"})])
async def test_navigation_capture_retires_the_ordinary_request(ordinary, action, payload):
    previous = str(uuid4())
    await open_theme(ordinary, previous)
    chrome_events.capture_surface_request(ordinary.orch, ordinary.socket, action, payload, str(uuid4()))
    assert not chrome_events.surface_request_current(ordinary.orch, ordinary.socket, "theme", previous)


@pytest.mark.parametrize("payload", [{"surface": {}}, {"surface": []}, {"surface": None}, {}, None])
async def test_malformed_open_cannot_replace_the_current_ordinary_ticket(ordinary, payload):
    previous = str(uuid4())
    await open_theme(ordinary, previous)
    chrome_events.capture_surface_request(ordinary.orch, ordinary.socket, "chrome_open", payload, str(uuid4()))
    assert chrome_events.surface_request_current(ordinary.orch, ordinary.socket, "theme", previous)


async def test_request_tracking_is_bounded_per_socket_and_cannot_borrow_a_socket_owner(ordinary):
    for _ in range(8):
        chrome_events.capture_surface_request(ordinary.orch, ordinary.socket, "chrome_open",
            {"surface": "theme"}, str(uuid4()))
    current = str(uuid4())
    chrome_events.capture_surface_request(ordinary.orch, ordinary.socket, "chrome_open", {"surface": "theme"}, current)
    assert len(ordinary.orch._ordinary_chrome_requests) == 1
    assert not chrome_events.surface_request_current(ordinary.orch, object(), "theme", current)
    chrome_events._note_open_surface(ordinary.orch, ordinary.socket, "")
    assert ordinary.orch._ordinary_chrome_requests == {}
