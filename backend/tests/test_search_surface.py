"""Search chrome surface tests over real IAM, encryption, SQL, and audit: federated
queries through the authenticated chrome_open dispatch, source-linked opening, honest
availability states, deleted-source handling, sanitization, and denials.
"""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.recorder import Recorder, set_recorder
from orchestrator import chrome_events, human_request_authority as human_module
from orchestrator.credential_manager import CredentialManager
from orchestrator.projection_surfaces import search
from personalization import phi_gate
from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from personalization.repository import PersonalizationRepository
from rote.capabilities import DeviceProfile
from tests.test_guidance_surface_088 import save
from tests.test_guidance_surface_postgres_088 import notes as notes
from tests.test_human_socket_authority_088 import (
    bound as bound, fixture as fixture, human as human, runtime as runtime,
    service as service, signing_key as signing_key, socket_request as socket_request,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def surface(notes, runtime, monkeypatch, tmp_path):
    state = notes
    orch = state.orch
    orch.credential_manager = CredentialManager(plane_runtime=runtime, data_dir=str(tmp_path))
    orch.explicit_notes = ExplicitNoteService(orch)
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.default())
    orch.personalization_service = SimpleNamespace(repo=PersonalizationRepository(
        None, plane_runtime=runtime, plane_repositories=runtime.repositories))
    monkeypatch.setattr(phi_gate, "_GATE", phi_gate.PHIGate(
        analyzer=SimpleNamespace(analyze=lambda **_: [])))
    recorder = Recorder(orch.audit_repo)
    set_recorder(recorder)
    yield state
    set_recorder(None)


def create_chat(runtime, owner, chat_id, title, message):
    stamp = int(time.time() * 1000)
    with runtime.transaction() as tx:
        repos = runtime.repositories
        repos.history.conversations.create(tx, conversation_id=chat_id, owner_id=owner,
                                           title=title, agent_id=None, created_at=stamp)
        repos.history.messages.append(tx, owner_id=owner, conversation_id=chat_id,
                                      role="user", content=message, timestamp=stamp)


async def save_note(state, caller, value, *, category="context"):
    return await state.orch.explicit_notes.command(caller=caller, body=ExplicitNoteCommand(
        command="save", note_id=str(uuid4()), expected_revision=0, category=category,
        value=value, enabled=True))


async def dispatch(state, params, *, fields=None):
    payload = {"surface": "search", "params": params}
    if fields is not None:
        payload["fields"] = fields
    message = {"type": "ui_event", "action": "chrome_open", "payload": payload,
               "submission_id": str(uuid4()), "request_generation": str(uuid4()),
               "connection_generation": str(state.context.connection_generation)}
    pending = human_module.capture_human_socket_request(
        state.boundary, websocket=state.socket, context=state.context, message=message)
    state.pending.append(pending)
    caller = await pending.authenticate()
    chrome_events.capture_surface_request(state.orch, state.socket, "chrome_open", payload,
                                         pending.request_generation)
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT
    token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        with human_module.bind_human_caller(caller):
            delivered = await chrome_events.handle_chrome_event(
                state.orch, state.socket, "chrome_open", payload, "untrusted-handler-owner",
                request_generation=pending.request_generation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    return delivered, state.sent, caller


def last_frame(state):
    assert state.sent, "no chrome frame was delivered"
    return state.sent[-1]


def flat(components):
    out = []

    def walk(node):
        if not isinstance(node, dict):
            return
        out.append(node)
        for key in ("children", "content"):
            for child in (node.get(key) or []):
                walk(child)
    for component in components:
        walk(component)
    return out


def fixture_owners(state):
    return [state.orch.ui_sessions[state.socket]["sub"]]


async def bind_caller(state, *, write=False):
    payload = ({"surface": "search", "params": {}} if not write
               else save())
    action = "chrome_open" if not write else "chrome_note_save"
    message = {"type": "ui_event", "action": action,
               "payload": payload,
               "submission_id": str(uuid4()), "request_generation": str(uuid4()),
               "connection_generation": str(state.context.connection_generation)}
    pending = human_module.capture_human_socket_request(
        state.boundary, websocket=state.socket, context=state.context, message=message)
    state.pending.append(pending)
    return await pending.authenticate()


async def component_state(state, params):
    caller = await bind_caller(state)
    with human_module.bind_human_caller(caller):
        return await search.components(state.orch, caller.owner_id, ["user"], params)


def buttons(components, label=None):
    return [node for node in flat(components) if node.get("type") == "button"
            and (label is None or node.get("label") == label)]


def alerts(components):
    return [node["message"] for node in flat(components) if node.get("type") == "alert"]


async def test_search_open_remains_an_authenticated_socket_read():
    assert human_module._socket_method({"type": "ui_event", "action": "chrome_open",
        "payload": {"surface": "search", "params": {"open": {"kind": "memory"}}}}) == "WS_READ"


async def test_intro_and_authenticated_dispatch_use_projection_renderer(surface):
    delivered, _, caller = await dispatch(surface, {})
    assert delivered and caller.owner_id == fixture_owners(surface)[0]
    assert "Search your conversations" in last_frame(surface)["html"]
    assert "astral-param-picker" in last_frame(surface)["html"]


async def test_all_four_sources_share_controls_and_audited_current_source_opening(surface, runtime):
    from personalization.atlas_service import AtlasPageService, PageInput

    state = surface
    owner = fixture_owners(state)[0]
    caller = await bind_caller(state, write=True)
    note = await save_note(state, caller, "quartz private note")
    memory = state.orch.personalization_service.repo.create_memory(owner, "preference", "quartz memory", keywords="")
    conversation = str(uuid4())
    create_chat(runtime, owner, conversation, "Quartz planning", "quartz conversation")
    state.orch.atlas_pages = AtlasPageService(plane_runtime=runtime, audit_repo=state.orch.audit_repo,
        fernet=state.orch.credential_manager._fernet, phi_gate=SimpleNamespace(contains_phi=lambda _: False))
    page = state.orch.atlas_pages.create_page(owner_id=owner,
        page=PageInput(slug="quartz", title="Quartz Atlas", body="quartz atlas body", project_id="project"))
    result = await component_state(state, {"q": "quartz"})
    for label in ("Open note", "Open memory", "View conversation result", "Open Atlas page"):
        target = buttons(result, label)[0]["payload"]["params"]
        opened = await component_state(state, target)
        assert not alerts(opened)
        assert buttons(opened, "Back to search")
    assert "quartz atlas body" in str(opened)
    selected = await component_state(state, {"q": "quartz", "source": "atlas", "type": "atlas_page", "project_id": "project"})
    assert len(buttons(selected, "Open Atlas page")) == 1
    assert not buttons(selected, "Open note")
    with runtime.transaction() as tx:
        events = tx.fetch_all("SELECT * FROM audit_events WHERE actor_user_id=%s AND action_type=%s",
                             (owner, "personalization.search"))
    assert events and "quartz" not in str(events)
    state.orch.personalization_service.repo.delete_memory(owner, memory["id"])
    stale = await component_state(state, {"open": {"kind": "memory", "memory_id": memory["id"]}})
    assert "no longer available" in str(alerts(stale))
    changed = await component_state(state, {"open": {"kind": "note", "note_id": note.note_id,
                                                  "expected_revision": note.revision + 1}})
    assert "no longer available" in str(alerts(changed))
    changed = await component_state(state, {"open": {"kind": "atlas_page", "page_id": page.page_id,
                                                   "project_id": "project", "revision": page.revision + 1}})
    assert "no longer available" in str(alerts(changed))


async def test_private_note_after_first_storage_page_is_found(surface):
    caller = await bind_caller(surface, write=True)
    for index in range(55):
        await save_note(surface, caller, f"ordinary filler {index}")
    note = await save_note(surface, caller, "zenith private note")
    result = await component_state(surface, {"q": "zenith", "source": "notes"})
    assert buttons(result, "Open note")[0]["payload"]["params"]["open"]["note_id"] == note.note_id


async def test_pagination_and_changed_filter_cursor_binding(surface):
    owner = fixture_owners(surface)[0]
    for index in range(21):
        surface.orch.personalization_service.repo.create_memory(owner, "preference", f"needle {index}", keywords="")
    first = await component_state(surface, {"q": "needle", "source": "memory"})
    assert len(buttons(first, "Open memory")) == 20
    following = buttons(first, "More results")[0]["payload"]["params"]
    second = await component_state(surface, following)
    assert len(buttons(second, "Open memory")) == 1 and not buttons(second, "More results")
    changed = await component_state(surface, dict(following, q="different"))
    assert alerts(changed) and not buttons(changed, "Open memory")
    assert "cursor" not in search.form_params(following, {"q": "needle"})


@pytest.mark.parametrize("source,types,project", [("foreign", "", ""), ("memory", "foreign", ""),
                                                ("memory", "", 3)])
async def test_invalid_source_type_project_filters_are_visible(surface, source, types, project):
    result = await component_state(surface, {"q": "needle", "source": source, "type": types, "project_id": project})
    assert alerts(result) and not buttons(result)


@pytest.mark.parametrize("state_name", ["unavailable", "incomplete", "empty"])
async def test_zero_hit_and_partial_failure_states_remain_honest_on_both_channels(surface, monkeypatch, state_name):
    from personalization.federated_search import SearchOutcome, SourceReport
    available, complete = state_name != "unavailable", state_name == "empty"
    outcome = SearchOutcome("needle", ("memory",), (), None, (),
        (SourceReport("memory", available, complete, 0, 0),), 0, None)
    service = SimpleNamespace(search=lambda **_: None)
    async def query(**kwargs):
        return outcome
    service.search = query
    monkeypatch.setattr(search, "get_search_service", lambda _: service)
    result = await component_state(surface, {"q": "needle"})
    messages = str(alerts(result))
    assert ("No matches found" in messages) is (state_name == "empty")
    if state_name == "incomplete":
        assert "incomplete" in messages
    delivered, _, _ = await dispatch(surface, {"q": "needle"})
    assert delivered
    assert ("No matches found" in last_frame(surface)["html"]) is (state_name == "empty")


async def test_source_failure_keeps_other_sources_and_never_marks_message_failure_complete(surface, runtime, monkeypatch):
    from personalization.federated_search import FederatedSearchService
    owner = fixture_owners(surface)[0]
    surface.orch.personalization_service.repo.create_memory(owner, "preference", "needle memory", keywords="")
    create_chat(runtime, owner, str(uuid4()), "Ordinary", "needle message")
    original = FederatedSearchService._history_context
    def context(service):
        current = original(service)
        call = current.call
        def failure(operation, **kwargs):
            if operation == current.repository.messages.list_visible:
                raise ConnectionError("synthetic failed message read")
            return call(operation, **kwargs)
        return SimpleNamespace(repository=current.repository, call=failure)
    monkeypatch.setattr(FederatedSearchService, "_history_context", context)
    result = await component_state(surface, {"q": "needle"})
    assert buttons(result, "Open memory")
    assert "incomplete" in str(alerts(result))
    assert not buttons(result, "View conversation result")


async def test_owner_denial_and_malformed_source_opening_never_leak(surface, runtime):
    foreign_id = str(uuid4())
    create_chat(runtime, "foreign-owner", foreign_id, "Foreign secret", "Never leak")
    result = await component_state(surface, {"open": {"kind": "conversation", "conversation_id": foreign_id}})
    assert "Never leak" not in str(result) and "no longer available" in str(alerts(result))
    result = await component_state(surface, {"open": {"kind": "memory", "memory_id": "m", "owner_id": "foreign"}})
    assert "reference" in str(alerts(result))
    callerless = await search.components(surface.orch, fixture_owners(surface)[0], [], {"q": "secret"})
    assert "authenticated session" in str(alerts(callerless))


async def test_sanitization_allowed_primitives_and_form_fields_through_dispatch(surface, runtime):
    from webrender import allowed_primitive_types
    owner = fixture_owners(surface)[0]
    create_chat(runtime, owner, str(uuid4()), "<img src=x onerror=alert(1)> needle", "needle & ordinary")
    result = await component_state(surface, {"q": "needle", "source": "conversations"})
    assert {node.get("type") for node in flat(result)} <= set(allowed_primitive_types())
    delivered, _, _ = await dispatch(surface, {}, fields={"q": "needle", "source": "conversations",
                                                       "type": "conversation", "project_id": ""})
    html = last_frame(surface)["html"]
    assert delivered and "needle &amp; ordinary" in html and "<img src=x" not in html
    assert 'role="list"' in html and 'role="listitem"' in html
    delivered, _, _ = await dispatch(surface, {}, fields={"q": "x" * 201})
    assert "200 characters" in last_frame(surface)["html"]


async def test_service_failure_and_opening_failure_are_visible(surface, monkeypatch):
    def unavailable(_):
        raise ConnectionError("synthetic service failure")
    monkeypatch.setattr(search, "get_search_service", unavailable)
    assert "unavailable right now" in str(alerts(await component_state(surface, {"q": "needle"})))


async def test_personalization_still_exposes_shared_search_entry(surface):
    from orchestrator.projection_surfaces import personalization
    caller = await bind_caller(surface)
    with human_module.bind_human_caller(caller):
        html = await personalization.render(surface.orch, caller.owner_id, ["user"], {"tab": "memory"})
    assert "Search everything" in html and "search" in html
