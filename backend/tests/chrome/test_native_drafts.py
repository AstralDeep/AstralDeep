"""Exercises native draft list, detail and mutation refresh through Deep's chrome dispatcher.
Owner-scoped persistence doubles preserve authorization boundaries without generating agents.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events
from orchestrator.projection_surfaces import drafts
from rote.capabilities import DeviceProfile
from tests.chrome.test_surface_drafts import _draft, orch_with


def flatten(components):
    for component in components:
        yield component
        children = component.get("children", component.get("content", []))
        if isinstance(children, list):
            yield from flatten(children)


async def build(orch, params=None):
    builder = getattr(drafts, "components", None)
    assert callable(builder), "Drafts must supply native structured components"
    return await builder(orch, "u1", ["user"], params or {})


async def test_empty_native_drafts_offers_the_existing_generation_form():
    components = await build(orch_with([]))
    assert "No drafts yet" in json.dumps(components)
    form, = [component for component in components if component["type"] == "param_picker"]
    assert form["submit_action"] == "chrome_draft_create"
    assert form["submit_label"] == "Generate & self-test"
    assert [(field["name"], field["kind"]) for field in form["fields"]] == [
        ("agent_name", "text"), ("description", "textarea"), ("tools", "textarea"),
    ]


async def test_native_list_contains_only_current_owner_drafts_and_real_open_actions():
    components = await build(orch_with([
        _draft(agent_name="Owner tracker"),
        _draft(id="foreign", user_id="u2", agent_name="Foreign private agent"),
        _draft(id="live", agent_name="Already approved", status="live"),
    ]))
    wire = json.dumps(components)
    assert "Owner tracker" in wire
    assert "Foreign private agent" not in wire and "Already approved" not in wire
    opened, = [component for component in flatten(components)
               if component.get("action") == "chrome_open"]
    assert opened["payload"] == {"surface": "drafts", "params": {"draft_id": "d1"}}
    assert "self-test passed" in wire


@pytest.mark.parametrize("revision,approve,discard", [
    (None, "draft_approve", "draft_discard"),
    ("live-agent", "revision_apply", "revision_discard"),
])
async def test_native_detail_retains_normal_and_revision_authorized_action_payloads(revision, approve, discard):
    components = await build(orch_with([_draft(revises_agent_id=revision)]), {"draft_id": "d1"})
    actions = {component["action"]: component for component in flatten(components)
               if component.get("action") and component["action"] != "chrome_open"}
    assert set(actions) == {approve, discard}
    assert all(component["payload"] == {"draft_id": "d1"} for component in actions.values())
    navigation = [component for component in flatten(components) if component.get("action") == "chrome_open"]
    assert any(component["payload"] == {
        "surface": "drafts", "params": {"draft_id": "d1", "refine": True},
    } for component in navigation)


async def test_native_refine_collects_guidance_without_submitting_a_chat_query():
    components = await build(orch_with([_draft()]), {"draft_id": "d1", "refine": True})
    form, = [component for component in components if component["type"] == "param_picker"]
    assert form["submit_action"] == "draft_refine"
    assert form["submit_payload"] == {"draft_id": "d1"}
    assert form["fields"][0]["name"] == "message" and form["fields"][0]["kind"] == "textarea"
    assert "chat_message" not in json.dumps(components)


async def test_native_live_detail_has_no_draft_decision_controls():
    components = await build(orch_with([_draft(status="live")]), {"draft_id": "d1"})
    assert not [component for component in flatten(components)
                if component.get("action") in {
                    "draft_approve", "draft_discard", "revision_apply", "revision_discard",
                }]
    assert "approved and is live" in json.dumps(components)


async def test_native_detail_retains_failure_and_revision_context():
    components = await build(orch_with([_draft(status="rejected", error_message="Security gate declined",
                                              revises_agent_id="owner-live-agent")]), {"draft_id": "d1"})
    wire = json.dumps(components)
    assert "Security gate declined" in wire and "revises: owner-live-agent" in wire
    assert "rejected" in wire


@pytest.mark.parametrize("self_test", [None, "bad json", "[]", '"unexpected"', "{}"])
async def test_native_missing_or_malformed_self_test_never_breaks_the_draft_list(self_test):
    components = await build(orch_with([_draft(self_test=self_test)]))
    assert "not self-tested yet" in json.dumps(components)
    assert "Tracker" in json.dumps(components)


@pytest.mark.parametrize("params", [{"draft_id": "missing"}, {"draft_id": "d1", "refine": True}])
async def test_native_missing_or_foreign_detail_discloses_no_draft_data(params):
    components = await build(orch_with([_draft(user_id="u2", agent_name="Private foreign draft")]), params)
    assert len(components) == 1 and components[0]["type"] == "alert"
    assert components[0]["variant"] == "error" and "Draft not found" in components[0]["message"]
    assert "Private foreign draft" not in json.dumps(components)


async def test_native_missing_persistence_fails_instead_of_claiming_no_drafts():
    builder = getattr(drafts, "components", None)
    assert callable(builder), "Drafts must supply native structured components"
    with pytest.raises(RuntimeError, match="Plane draft persistence is unavailable"):
        await builder(SimpleNamespace(), "u1", ["user"], {})
    with pytest.raises(RuntimeError, match="Plane draft persistence is unavailable"):
        await builder(SimpleNamespace(), "u1", ["user"], {"draft_id": "d1"})


def connected(orch, device="ios"):
    socket = object()
    profile = DeviceProfile.from_dict({
        "device_type": device, "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "button", "param_picker", "container"],
    })
    orch.rote = SimpleNamespace(get_profile=lambda _: profile)
    orch.ui_sessions = {socket: {"sub": "u1", "realm_access": {"roles": ["user"]}}}
    orch.sent = []

    async def configured(owner):
        return True

    async def send(target, frame):
        assert target is socket
        orch.sent.append(json.loads(frame))
        return True

    orch._safe_send = send
    orch.llm_configured_for = configured
    return socket


@pytest.mark.parametrize("device", ["ios", "macos", "android", "windows"])
async def test_native_dispatch_delivers_drafts_instead_of_an_unavailable_placeholder(device):
    orch = orch_with([_draft()])
    socket = connected(orch, device)
    assert await chrome_events.handle_chrome_event(
        orch, socket, "chrome_open", {"surface": "drafts"}, "u1")
    frame, = orch.sent
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "drafts"
    assert "Tracker" in json.dumps(frame)
    assert "isn't available" not in json.dumps(frame)


@pytest.mark.parametrize("action", ["draft_approve", "draft_refine", "draft_discard", "revision_apply", "revision_discard"])
async def test_draft_decision_refreshes_current_native_surface_without_guessing_success(monkeypatch, action):
    orch = orch_with([_draft()])
    socket = connected(orch)
    await chrome_events.handle_chrome_event(orch, socket, "chrome_open", {"surface": "drafts"}, "u1")

    async def mutation(host, target, owner, roles, payload):
        assert host is orch and target is socket and owner == "u1"
        if action in {"draft_discard", "revision_discard"}:
            host.lifecycle_manager.draft_store.rows.clear()
        else:
            host.lifecycle_manager.draft_store.rows[0]["agent_name"] = "Updated owner draft"
        return None

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {action: ("drafts", mutation)})
    await chrome_events.handle_chrome_event(orch, socket, action, {"draft_id": "d1"}, "u1")
    assert len(orch.sent) == 2
    assert orch.sent[-1]["surface_key"] == "drafts"
    wire = json.dumps(orch.sent[-1])
    assert ("No drafts yet" if action in {"draft_discard", "revision_discard"}
            else "Updated owner draft") in wire
    assert "success" not in wire


async def test_draft_result_cannot_replace_a_surface_opened_while_the_action_was_pending(monkeypatch):
    orch = orch_with([_draft()])
    socket = connected(orch)
    await chrome_events.handle_chrome_event(orch, socket, "chrome_open", {"surface": "drafts"}, "u1")

    async def mutation(host, target, owner, roles, payload):
        chrome_events._note_open_surface(host, target, "agents")
        return None

    monkeypatch.setattr(chrome_events, "_handlers", lambda: {"draft_approve": ("drafts", mutation)})
    await chrome_events.handle_chrome_event(orch, socket, "draft_approve", {"draft_id": "d1"}, "u1")
    assert len(orch.sent) == 1
    assert chrome_events.open_surface_for(orch, socket) == "agents"


@pytest.mark.parametrize("action", ["draft_approve", "draft_refine", "draft_discard", "revision_apply", "revision_discard"])
async def test_actual_native_draft_decision_handlers_deny_foreign_owner_without_data_or_mutations(monkeypatch, action):
    from orchestrator import agentic_creation

    foreign = _draft(user_id="u2", agent_name="Foreign private draft", revises_agent_id="foreign-live-agent")
    orch = orch_with([foreign])
    socket = connected(orch)
    orch.chat_cards = []

    async def send_card(websocket, components, **kwargs):
        assert websocket is socket
        orch.chat_cards.extend(components)

    async def forbidden(*args, **kwargs):
        pytest.fail("Foreign-owner action must not call a lifecycle mutation")

    orch.send_ui_render = send_card
    for name in ("approve_agent", "refine_agent", "delete_draft"):
        setattr(orch.lifecycle_manager, name, forbidden)
    await chrome_events.handle_chrome_event(orch, socket, "chrome_open", {"surface": "drafts"}, "u1")
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        action: ("drafts", agentic_creation.HANDLERS[action]),
    })
    generation = str(uuid4())
    await chrome_events.handle_chrome_event(orch, socket, action,
        {"surface": "drafts", "draft_id": "d1", "fields": {"message": "Refine private draft"}}, "u1",
        request_generation=generation)
    assert orch.lifecycle_manager.draft_store.rows == [foreign]
    assert orch.chat_cards and orch.chat_cards[0]["type"] == "alert"
    assert "Foreign private draft" not in json.dumps(orch.chat_cards + orch.sent)
    assert orch.sent[-1]["request_generation"] == generation
    assert "No drafts yet" in json.dumps(orch.sent[-1])


async def test_native_draft_store_failure_produces_a_visible_error(monkeypatch):
    orch = orch_with([])
    socket = connected(orch)

    def failed_read(owner):
        raise RuntimeError("persistence unavailable")

    monkeypatch.setattr(orch.lifecycle_manager.draft_store, "get_decidable_drafts", failed_read)
    await chrome_events.handle_chrome_event(orch, socket, "chrome_open", {"surface": "drafts"}, "u1")
    frame, = orch.sent
    assert frame["components"][0]["type"] == "alert"
    assert frame["components"][0]["variant"] == "error"
    assert "No drafts yet" not in json.dumps(frame)
