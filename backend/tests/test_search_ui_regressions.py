"""Reproduces search UI omissions and the obsolete scanner's dishonest completion flag
with synthetic state, before integration with the qualified shared search service.
"""

import json
from types import SimpleNamespace

import pytest

from orchestrator.projection_surfaces import search
from orchestrator import chrome_events
from personalization import federated_search


def test_zero_hit_incomplete_search_never_claims_no_matches():
    outcome = federated_search.SearchOutcome("needle", ("conversations",), (), None, (), (
        federated_search.SourceReport("conversations", True, False, 0, 0),), 0, None)
    notices = str(search._honest_notices(outcome))
    assert "incomplete" in notices
    assert "No matches found" not in notices


def test_failed_message_read_never_claims_a_complete_scan(monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError("synthetic storage failure")

    context = SimpleNamespace(repository=SimpleNamespace(
        conversations=SimpleNamespace(list_recent_nonempty=lambda *args, **kwargs: [
            SimpleNamespace(conversation_id="synthetic", title="unmatched", updated_at=1)]),
        messages=SimpleNamespace(list_visible=broken)), call=lambda operation, **kwargs: operation(None, **kwargs))
    service = object.__new__(federated_search.FederatedSearchService)
    monkeypatch.setattr(service, "_history_context", lambda: context)
    hits, report = service._scan_conversations("owner", "needle", ())
    assert hits == []
    assert not report.complete


@pytest.mark.asyncio
async def test_conversation_source_refusal_never_resolves_a_corrupt_message_reference(monkeypatch):
    chat = SimpleNamespace(title="t")
    context = SimpleNamespace(repository=SimpleNamespace(
        conversations=SimpleNamespace(get=lambda *args, **kwargs: chat),
        messages=SimpleNamespace(get=lambda *args, **kwargs: None)),
        call=lambda operation, **kwargs: operation(None, **kwargs))
    service = object.__new__(federated_search.FederatedSearchService)
    monkeypatch.setattr(service, "_history_context", lambda: context)
    opened, message = service._current_conversation(federated_search.SearchHit(
        "conversations", "conversation", {"conversation_id": "c"}, "", "", 0, 0, ()), "owner")
    assert opened is chat and message is None
    corrupt = federated_search.SearchHit("conversations", "conversation",
        {"conversation_id": "c", "message_id": "corrupt"}, "", "", 0, 0, ())
    assert service._current_conversation(corrupt, "owner") is None
    assert not service._recheck_conversation(corrupt, "owner")


@pytest.mark.asyncio
async def test_search_components_offer_all_four_sources_and_type_project_controls():
    components = await search.components(None, "owner", [], {})
    payload = json.dumps(components)
    assert "atlas" in payload
    fields = components[0]["fields"]
    assert {field["name"] for field in fields} >= {"q", "source", "type", "project_id"}


def test_web_search_requests_keep_the_existing_surface_correlation_guard():
    from uuid import uuid4

    socket = object()
    orch = SimpleNamespace(rote=SimpleNamespace(get_profile=lambda _: SimpleNamespace(device_type="desktop")))
    generation = str(uuid4())
    chrome_events.capture_surface_request(orch, socket, "chrome_open", {"surface": "search"}, generation)
    assert chrome_events.surface_request_current(orch, socket, "search", generation)


@pytest.mark.asyncio
async def test_late_search_response_is_not_delivered_after_another_search():
    from uuid import uuid4

    socket, sent = object(), []

    async def send(*args):
        sent.append(args)
        return True

    orch = SimpleNamespace(_safe_send=send)
    previous, current = str(uuid4()), str(uuid4())
    chrome_events._note_open_surface(orch, socket, "search")
    orch._ordinary_chrome_requests = {id(socket): (socket, "search", current)}
    response = chrome_events._OrdinarySurfaceResponse(orch, socket, "search", previous)
    token = chrome_events._ordinary_surface_response.set(response)
    try:
        assert not await chrome_events._push_modal(orch, socket, "stale source")
    finally:
        chrome_events._ordinary_surface_response.reset(token)
    assert sent == []


def test_partial_source_and_stale_notices_survive_without_hits():
    outcome = federated_search.SearchOutcome("needle", ("memory", "notes"), (), None, (), (
        federated_search.SourceReport("memory", True, True, 1, 0, stale=1),
        federated_search.SourceReport("notes", False, False, 0, 0)), 0, None)
    messages = str(search._honest_notices(outcome))
    assert "could not be searched" in messages and "no longer available" in messages
    assert "No matches found" not in messages


@pytest.mark.asyncio
async def test_source_preview_bound_is_explicit_to_the_user():
    service = object.__new__(federated_search.FederatedSearchService)
    service.repo = SimpleNamespace(get_memory=lambda *args: {
        "id": "m", "value": "x" * (federated_search.MAX_SOURCE_CHARS + 1)})
    view = await service.open_source(owner_id="owner", ref={"kind": "memory", "memory_id": "m"})
    assert len(view.text) == federated_search.MAX_SOURCE_CHARS and not view.complete
    assert "shortened" in str(search._source_components(view, {}))


@pytest.mark.asyncio
async def test_search_uses_existing_chrome_form_dispatch_instead_of_a_chat_template():
    html = await search.render(None, "owner", [], {})
    assert 'data-ui-form="true"' in html
    assert 'data-ui-action="chrome_open"' in html
    assert 'data-ui-collect="true"' in html
    assert "astral-pp-submit" not in html
    for field in ("q", "source", "type", "project_id"):
        assert f'name="{field}"' in html


@pytest.mark.parametrize("chat_id", [
    "conversation-1", 'quote"slash\\emoji😀', "x" * 512,
    "C1137F19-AE88-4A35-82F3-253F77BDEEA0",
    "c1137f19-ae88-1a35-82f3-253f77bdeea0", "", None, 42,
])
def test_archived_conversation_preview_does_not_offer_unsupported_navigation(chat_id):
    view = SimpleNamespace(source="conversations", title="Archived title", text="Authorized preview",
                           complete=True, ref={"conversation_id": chat_id})
    definitions = search._source_components(view, {"q": "needle"})
    assert not any(node.get("action") == "load_chat" for node in definitions)
    assert "Authorized preview" in str(definitions)
    assert "This archived conversation is available in this preview." in str(definitions)
    html = search.render_html(search.ChromeViewModel("search", "Search", tuple(
        search.ComponentView(node["type"], {key: value for key, value in node.items() if key != "type"})
        for node in definitions)))
    assert "Authorized preview" in html
    assert "This archived conversation is available in this preview." in html
    assert 'data-ui-action="load_chat"' not in html


def test_canonical_conversation_preview_offers_exact_normal_navigation():
    chat_id = "c1137f19-ae88-4a35-82f3-253f77bdeea0"
    view = SimpleNamespace(source="conversations", title="Current title", text="Authorized preview",
                           complete=True, ref={"conversation_id": chat_id})
    definitions = search._source_components(view, {})
    button = next(node for node in definitions if node.get("action") == "load_chat")
    assert button["payload"] == {"chat_id": chat_id}
    assert "This archived conversation is available in this preview." not in str(definitions)
