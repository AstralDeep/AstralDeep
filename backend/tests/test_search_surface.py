"""Search chrome surface tests over real IAM, encryption, SQL, and audit: federated
queries through the authenticated chrome_open dispatch, source-linked opening, honest
availability states, deleted-source handling, sanitization, and denials.
"""

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.recorder import Recorder, set_recorder
from audit.repository import AuditRepository
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
    recorder = Recorder(AuditRepository(plane_runtime=runtime))
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


async def test_search_open_is_an_authenticated_socket_read():
    assert human_module._socket_method(
        {"type": "ui_event", "action": "chrome_open", "payload": {"surface": "search"}}
    ) == "WS_READ"


async def test_intro_renders_without_query_and_dispatch_uses_caller_identity(surface):
    state = surface
    delivered, _, caller = await dispatch(state, {})
    assert delivered
    frame = last_frame(state)
    assert frame["type"] == "chrome_render" and "Search your conversations" in frame["html"]
    assert caller.owner_id == fixture_owners(state)[0]


def fixture_owners(state):
    return [state.orch.ui_sessions[state.socket]["sub"]]


async def test_query_federates_all_three_sources_with_audit(surface, runtime):
    state = surface
    orch, owner = state.orch, fixture_owners(state)[0]
    caller = await bind_caller(state, write=True)
    await save_note(state, caller, "Retain the quartz protocol draft.")
    orch.personalization_service.repo.create_memory(
        owner, "preference", "wants quartz summaries", keywords="quartz")
    create_chat(runtime, owner, str(uuid4()), "Quartz planning",
                "we agreed the quartz protocol ships first")
    delivered, _, _ = await dispatch(state, {"q": "quartz"})
    assert delivered
    frame = last_frame(state)
    html = frame["html"]
    assert "Retain the quartz protocol draft." in html
    assert "wants quartz summaries" in html
    assert "we agreed the quartz protocol ships first" in html
    assert html.count("data-search-hit") == 3
    assert 'data-action="load_chat"' in html and "Open note" in html and "Open memory" in html
    with runtime.transaction() as tx:
        events = tx.fetch_all(
            "SELECT * FROM audit_events WHERE actor_user_id=%s AND action_type=%s",
            (owner, "personalization.source_search"))
    assert len(events) == 1
    assert "quartz" not in str(events[0])


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


async def test_note_beyond_first_storage_page_is_found(surface, runtime):
    state = surface
    caller = await bind_caller(state, write=True)
    for index in range(55):
        await save_note(state, caller, f"filler note number {index} about ordinary topics")
    kept = await save_note(state, caller, "the zenith keyword lives here")
    delivered, _, _ = await dispatch(state, {"q": "zenith"})
    html = last_frame(state)["html"]
    assert "the zenith keyword lives here" in html
    assert kept.note_id in html
    with runtime.transaction() as tx:
        stored = tx.fetch_one("SELECT revision FROM explicit_note_current WHERE note_id=%s",
                              (kept.note_id,))
    assert stored["revision"] == 1


async def test_source_filter_and_cursor_pagination(surface):
    state = surface
    owner = fixture_owners(state)[0]
    for index in range(21):
        state.orch.personalization_service.repo.create_memory(
            owner, "preference", f"paged memory item {index} needle", keywords="")
    delivered, _, _ = await dispatch(state, {"q": "needle", "source": "memory"})
    html = last_frame(state)["html"]
    assert html.count("data-search-hit") == 20
    assert "More results" in html
    next_payload = json.loads(html.split("data-ui-payload='")[-1].split("'")[0].replace("&quot;", '"'))
    assert next_payload["params"]["source"] == "memory"
    delivered, _, _ = await dispatch(state, next_payload["params"])
    html = last_frame(state)["html"]
    assert html.count("data-search-hit") == 1
    assert "More results" not in html
    delivered, _, _ = await dispatch(state, {"q": "needle", "source": "conversations"})
    html = last_frame(state)["html"]
    assert "No matches found" in html


async def test_unavailable_and_partial_states_are_honest(surface, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "partial visibility needle", keywords="")
    async def broken_notes(*args, **kwargs):
        raise RuntimeError("note service down")
    state.orch.explicit_notes.list = broken_notes
    import personalization.source_search as source_search

    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    delivered, _, _ = await dispatch(state, {"q": "needle"})
    html = last_frame(state)["html"]
    assert "could not be searched right now" in html
    assert "partial visibility needle" in html
    delivered, _, _ = await dispatch(state, {"q": "needle", "source": "conversations"})
    html = last_frame(state)["html"]
    assert "Search is unavailable right now" in html
    assert "partial visibility needle" not in html
    delivered, _, _ = await dispatch(state, {"q": "absent-term"})
    html = last_frame(state)["html"]
    assert "could not be searched right now" in html
    assert "Nothing else matched" in html
    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    state.orch.personalization_service = None
    delivered, _, _ = await dispatch(state, {"q": "needle"})
    html = last_frame(state)["html"]
    assert "Search is unavailable right now" in html


async def test_deleted_conversation_drops_out_and_mid_render_staleness_is_honest(
        surface, runtime, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    chat_id = str(uuid4())
    create_chat(runtime, owner, chat_id, "Doomed conversation", "ephemeral needle inside")
    delivered, _, _ = await dispatch(state, {"q": "ephemeral"})
    html = last_frame(state)["html"]
    assert 'data-action="load_chat"' in html
    with runtime.transaction() as tx:
        assert runtime.repositories.history.conversations.delete(
            tx, owner_id=owner, conversation_id=chat_id)
    delivered, _, _ = await dispatch(state, {"q": "ephemeral"})
    html = last_frame(state)["html"]
    assert "No matches found" in html

    import personalization.source_search as source_search

    create_chat(runtime, owner, chat_id, "Recreated conversation", "ephemeral needle inside")
    real_resolve = source_search.resolve_source

    async def vanishing(orch, *, user_id, ref):
        if ref.get("chat_id") == chat_id:
            return "That conversation is no longer available."
        return await real_resolve(orch, user_id=user_id, ref=ref)

    monkeypatch.setattr(search, "resolve_source", vanishing)
    delivered, _, _ = await dispatch(state, {"q": "ephemeral"})
    html = last_frame(state)["html"]
    assert "That conversation is no longer available." in html
    assert 'data-action="load_chat"' not in html


async def test_forgotten_note_leaves_results_and_stale_memory_is_honest(surface, runtime):
    state = surface
    owner = fixture_owners(state)[0]
    caller = await bind_caller(state, write=True)
    kept = await save_note(state, caller, "vanishing needle note")
    memory = state.orch.personalization_service.repo.create_memory(
        owner, "preference", "vanishing needle memory", keywords="")
    delivered, _, _ = await dispatch(state, {"q": "vanishing"})
    html = last_frame(state)["html"]
    assert html.count("data-search-hit") == 2
    await state.orch.explicit_notes.command(caller=caller, body=ExplicitNoteCommand(
        command="forget", note_id=kept.note_id,
        expected_revision=kept.revision))
    state.orch.personalization_service.repo.delete_memory(owner, memory["id"])
    delivered, _, _ = await dispatch(state, {"q": "vanishing"})
    html = last_frame(state)["html"]
    assert "No matches found" in html


async def test_hostile_query_is_escaped_and_long_queries_are_refused(surface, runtime):
    state = surface
    caller = await bind_caller(state, write=True)
    await save_note(state, caller, 'keep <script>alert("x")</script> safe & tidy')
    create_chat(runtime, fixture_owners(state)[0], str(uuid4()),
                'evil <img src=x onerror=alert(1)> title', 'harmless body needle')
    delivered, _, _ = await dispatch(state, {"q": "needle"})
    html = last_frame(state)["html"]
    assert "<img src=x onerror" not in html
    assert "&lt;img src=x onerror=alert(1)&gt; title" in html
    delivered, _, _ = await dispatch(state, {"q": "<script>"})
    html = last_frame(state)["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "safe &amp; tidy" in html
    delivered, _, _ = await dispatch(state, {"q": "x" * 500})
    html = last_frame(state)["html"]
    assert "Search is limited to 200 characters" in html
    delivered, _, _ = await dispatch(state, {"q": "safe"})
    html = last_frame(state)["html"]
    assert 'role="list" aria-label="Search results"' in html
    assert 'aria-label="Open private note"' in html


async def test_form_fields_merge_through_the_dispatch_hook(surface):
    state = surface
    owner = fixture_owners(state)[0]
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "field merge needle", keywords="")
    delivered, _, _ = await dispatch(state, {}, fields={"q": "field merge needle"})
    html = last_frame(state)["html"]
    assert "field merge needle" in html
    delivered, _, _ = await dispatch(state, {}, fields={"q": "   "})
    html = last_frame(state)["html"]
    assert "Enter a search term." in html


async def test_native_components_render_allowed_primitives_with_source_actions(surface, runtime):
    state = surface
    owner = fixture_owners(state)[0]
    caller = await bind_caller(state, write=True)
    await save_note(state, caller, "the native needle appears in this note")
    create_chat(runtime, owner, str(uuid4()), "Native conversation", "native needle message")
    from webrender.renderer import allowed_primitive_types

    allowed = set(allowed_primitive_types()) | {"color_picker", "theme_apply"}
    message = {"type": "ui_event", "action": "chrome_open",
               "payload": {"surface": "search", "params": {"q": "native needle"}},
               "submission_id": str(uuid4()), "request_generation": str(uuid4()),
               "connection_generation": str(state.context.connection_generation)}
    pending = human_module.capture_human_socket_request(
        state.boundary, websocket=state.socket, context=state.context, message=message)
    state.pending.append(pending)
    caller = await pending.authenticate()
    with human_module.bind_human_caller(caller):
        components = await search.components(state.orch, caller.owner_id, ["user"],
                                             {"q": "native needle"})
    types = [node.get("type") for node in flat(components) if "type" in node]
    assert set(types) <= allowed
    actions = []
    for node in flat(components):
        if node.get("action"):
            actions.append(node["action"])
        for entry in (node.get("actions") or (node.get("attributes") or {}).get("actions") or []):
            if isinstance(entry, dict) and entry.get("action"):
                actions.append(entry["action"])
    assert "load_chat" in actions and actions.count("chrome_open") >= 2
    rendered = json.dumps(components)
    assert "the native needle appears in this note" in rendered
    assert "native needle message" in rendered


async def _components_for(state, params, *, caller=None):
    message = {"type": "ui_event", "action": "chrome_open",
               "payload": {"surface": "search", "params": params},
               "submission_id": str(uuid4()), "request_generation": str(uuid4()),
               "connection_generation": str(state.context.connection_generation)}
    pending = human_module.capture_human_socket_request(
        state.boundary, websocket=state.socket, context=state.context, message=message)
    state.pending.append(pending)
    caller = caller or await pending.authenticate()
    with human_module.bind_human_caller(caller):
        return await search.components(state.orch, caller.owner_id, ["user"], params)


def _alerts(components):
    return [node.get("message") for node in flat(components)
            if node.get("type") == "alert" and node.get("message")]


async def test_native_components_cover_every_honest_state(surface, runtime, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    intro = await _components_for(state, {})
    assert any("Search your conversations" in str(alert) for alert in _alerts(intro))
    refused = await _components_for(state, {"q": "x" * 500})
    assert any("Search is limited to 200 characters" in str(alert) for alert in _alerts(refused))
    import personalization.source_search as source_search

    saved_repo = state.orch.personalization_service
    saved_notes = state.orch.explicit_notes
    state.orch.personalization_service = None
    state.orch.explicit_notes = None
    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    broken = await _components_for(state, {"q": "needle"})
    assert any("Search is unavailable right now" in str(alert) for alert in _alerts(broken))
    state.orch.personalization_service = saved_repo
    state.orch.explicit_notes = saved_notes
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "native states needle", keywords="")
    partial_hits = await _components_for(state, {"q": "needle"})
    messages = _alerts(partial_hits)
    assert any("could not be searched right now" in str(m) for m in messages)
    assert any("The sources above were searched." in str(m) for m in messages)
    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    empty = await _components_for(state, {"q": "nothing-matches-this"})
    assert any("Nothing else matched this search." in str(m) for m in _alerts(empty))
    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: FakeHealthyHistory())
    plain = await _components_for(state, {"q": "nothing-matches-this"})
    assert any("No matches found" in str(m) for m in _alerts(plain)), _alerts(plain)


class FakeHealthyHistory:
    def __init__(self):
        empty = SimpleNamespace(
            list_recent_nonempty=lambda tx, *, owner_id, limit: (),
            get=lambda tx, *, owner_id, conversation_id: None)
        self.repository = SimpleNamespace(conversations=empty, messages=SimpleNamespace(
            list_visible=lambda tx, *, owner_id, conversation_id, limit=None,
            through_render_revision=None: ()))

    def call(self, operation, **kwargs):
        return operation(None, **kwargs)


async def test_native_components_mark_stale_hits_and_offer_pager(surface, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    for index in range(21):
        state.orch.personalization_service.repo.create_memory(
            owner, "preference", f"native pager item {index} marker", keywords="")
    outcome = await _components_for(state, {"q": "marker", "source": "memory"})
    joined = json.dumps(outcome)
    assert "More results" in joined

    async def vanishing(orch, *, user_id, ref):
        return "That memory is no longer available."

    monkeypatch.setattr(search, "resolve_source", vanishing)
    stale = await _components_for(state, {"q": "native pager item 0 marker", "source": "memory"})
    assert any("That memory is no longer available." in str(alert)
               for alert in _alerts(stale))


async def test_web_render_marks_stale_hits_and_shows_partial_notices(surface, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "web staleness needle", keywords="")
    import personalization.source_search as source_search

    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    real_federated = search.federated_search

    async def partial_search(*args, **kwargs):
        outcome = await real_federated(*args, **kwargs)
        return SearchOutcome(query=outcome.query, source=outcome.source, hits=outcome.hits,
                             reports=tuple(SourceReport(
                                 report.source, report.available, False,
                                 report.matched, report.scanned)
                                 for report in outcome.reports),
                             next_offset=outcome.next_offset)

    monkeypatch.setattr(search, "federated_search", partial_search)
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await search.render(state.orch, owner, ["user"], {"q": "web staleness needle"})
    assert "scan limit was reached" in html
    assert "could not be searched right now" in html


async def test_audit_skips_when_no_recorder_and_notes_fail_open_without_caller(
        surface, monkeypatch):
    from audit.recorder import set_recorder

    state = surface
    set_recorder(None)
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await search.render(state.orch, fixture_owners(state)[0], ["user"],
                                   {"q": "audit skip needle"})
    assert "No matches found" in html
    callerless = SimpleNamespace(personalization_service=None,
                                 explicit_notes=None, runtime_composition=None)
    outcome = await source_search_module.federated_search(
        callerless, user_id="u1", query="anything", notes_caller=None)
    assert not outcome.any_available


from personalization.source_search import SearchOutcome, SourceReport  # noqa: E402
import personalization.source_search as source_search_module  # noqa: E402


async def test_stale_reasons_carry_source_unavailable_and_audit_failures(surface, runtime, monkeypatch):
    from audit.recorder import Recorder, set_recorder
    from audit.repository import AuditRepository

    state = surface
    owner = fixture_owners(state)[0]
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "defensive path needle", keywords="")

    recorder = Recorder(AuditRepository(plane_runtime=runtime))

    async def exploding(event):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(recorder, "record", exploding)
    set_recorder(recorder)
    import personalization.source_search as source_search

    async def unavailable(orch, *, user_id, ref):
        raise source_search.SourceUnavailable("That memory is no longer available.")

    monkeypatch.setattr(search, "resolve_source", unavailable)
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await search.render(state.orch, owner, ["user"], {"q": "defensive path needle"})
    assert "That memory is no longer available." in html

    async def broken(orch, *, user_id, ref):
        raise RuntimeError("resolve exploded")

    monkeypatch.setattr(search, "resolve_source", broken)
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await search.render(state.orch, owner, ["user"], {"q": "defensive path needle"})
    assert "defensive path needle" in html
    set_recorder(None)


async def test_render_tolerates_non_string_query_and_missing_caller(surface, monkeypatch):
    import personalization.source_search as source_search

    state = surface
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await search.render(state.orch, fixture_owners(state)[0], ["user"],
                                   {"q": 12345})
    assert "Search your conversations" in html
    monkeypatch.setattr("orchestrator.human_request_authority.current_human_caller",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError()))
    assert search._current_caller(state.orch) is None
    outcome = await source_search.federated_search(
        state.orch, user_id="u1", query="anything", notes_caller=None)
    reports = {report.source: report for report in outcome.reports}
    assert not reports["notes"].available


async def test_native_components_show_partial_notice(surface, monkeypatch):
    state = surface
    owner = fixture_owners(state)[0]
    state.orch.personalization_service.repo.create_memory(
        owner, "preference", "native partial needle", keywords="")
    import personalization.source_search as source_search

    monkeypatch.setattr(source_search, "_history_context",
                        lambda _orch: (_ for _ in ()).throw(RuntimeError("history down")))
    real_federated = search.federated_search

    async def partial_search(*args, **kwargs):
        outcome = await real_federated(*args, **kwargs)
        return SearchOutcome(query=outcome.query, source=outcome.source, hits=outcome.hits,
                             reports=tuple(SourceReport(
                                 report.source, report.available, False,
                                 report.matched, report.scanned)
                                 for report in outcome.reports),
                             next_offset=outcome.next_offset)

    monkeypatch.setattr(search, "federated_search", partial_search)
    components = await _components_for(state, {"q": "native partial needle"})
    assert any("scan limit was reached" in str(message) for message in _alerts(components))


async def test_personalization_surfaces_link_to_search(surface):
    from orchestrator.projection_surfaces import personalization

    state = surface
    with human_module.bind_human_caller(await bind_caller(state)):
        html = await personalization.render(state.orch, fixture_owners(state)[0],
                                            ["user"], {"tab": "memory"})
    assert "Search everything" in html
    assert "&quot;surface&quot;: &quot;search&quot;" in html
    components = await personalization.components(state.orch, fixture_owners(state)[0],
                                                  ["user"], {"tab": "memory"})
    actions = []
    for node in flat(components):
        if node.get("action"):
            actions.append(node["action"])
        for entry in (node.get("actions") or []):
            if isinstance(entry, dict) and entry.get("action"):
                actions.append(entry["action"])
    assert "chrome_open" in actions
    assert any(isinstance(node, dict) and (node.get("payload") or {}).get("surface") == "search"
               for node in flat(components))
