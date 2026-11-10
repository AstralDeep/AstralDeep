"""Tests for orchestrator/projection_surfaces/audit.py's chat-scoped audit view: owner-scoped
conversation filtering through audit/repository.py, scope display and preservation on web and
native, validation, and real Plane storage isolation when PostgreSQL is configured.
"""

from datetime import datetime, timedelta, timezone
import html as html_module
import json
import re
from types import SimpleNamespace
import uuid

import pytest

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate
from orchestrator import chrome_events
from orchestrator.projection_surfaces import audit as audit_surface
from tests.chrome.test_chrome_surface import FakeOrch
from tests.chrome.test_surface_audit import FakeRepo, make_dto, make_orch, recorder as recorder

CHAT = "6f1c1d1e-2b9a-4f0e-9a51-0c1d2e3f4a5b"
OTHER_CHAT = "7a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"


class FakeHistory:
    def __init__(self, titles=None, error=None):
        self.titles = titles or {}
        self.error = error
        self.calls = []

    def get_conversation_record(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error is not None:
            raise self.error
        title = self.titles.get((user_id, chat_id))
        return None if title is None else SimpleNamespace(conversation_id=chat_id, title=title)


def scoped_orch(repo, history=None):
    orch = make_orch(repo)
    orch.history = history or FakeHistory({("owner", CHAT): "Sourdough <research>"})
    return orch


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)


def _button(components, label):
    return next(node for node in _nodes(components)
                if node.get("type") == "button" and node.get("label") == label)


def _html_payload(html, label):
    match = re.search(
        r"data-ui-payload='([^']*)'[^>]*>" + re.escape(label) + "</button>", html)
    assert match, label
    return json.loads(html_module.unescape(match.group(1)))


async def test_web_view_filters_one_owned_chat_and_names_it():
    repo = FakeRepo(items=[make_dto(conversation_id=CHAT)], next_cursor="page-two")
    history = FakeHistory({("owner", CHAT): "Sourdough <research>"})
    html = await audit_surface.render(scoped_orch(repo, history), "owner", [], {
        "conversation_id": f"  {CHAT}  ", "outcome": "failure"})

    assert repo.list_calls[0][0] == "owner"
    assert repo.list_calls[0][1]["conversation_id"] == CHAT
    assert history.calls == [(CHAT, "owner")]
    assert "Chat: Sourdough &lt;research&gt;" in html
    assert "<research>" not in html
    assert '<option value="">All chats</option>' in html
    assert f'<option value="{CHAT}" selected>Sourdough &lt;research&gt;</option>' in html
    assert _html_payload(html, "Reset") == {"fields": {"conversation_id": CHAT}}
    assert _html_payload(html, "Failures") == {"fields": {"conversation_id": CHAT,
                                                          "outcome": "failure"}}
    assert _html_payload(html, "Tool calls")["fields"]["conversation_id"] == CHAT
    assert _html_payload(html, "Sign ins")["fields"]["conversation_id"] == CHAT
    assert _html_payload(html, "All activity") == {"fields": {}}
    assert _html_payload(html, "Next")["fields"]["conversation_id"] == CHAT
    assert f"&quot;conversation_id&quot;: &quot;{CHAT}&quot;" in html


async def test_unscoped_web_view_has_no_chat_controls_and_no_lookup():
    repo = FakeRepo(items=[make_dto()])
    history = FakeHistory()
    html = await audit_surface.render(scoped_orch(repo, history), "owner", [], {})
    assert repo.list_calls[0][1]["conversation_id"] is None
    assert history.calls == []
    assert "All chats" not in html and "Chat:" not in html
    assert _html_payload(html, "Reset") == {"fields": {}}


@pytest.mark.parametrize("history", [
    FakeHistory(),
    FakeHistory(error=ValueError("conversation_id must be a bounded identifier")),
])
async def test_unknown_or_unreadable_chat_is_named_by_its_id_and_still_owner_scoped(history):
    repo = FakeRepo()
    html = await audit_surface.render(scoped_orch(repo, history), "owner", [], {
        "conversation_id": OTHER_CHAT})
    assert repo.list_calls[0][0] == "owner"
    assert repo.list_calls[0][1]["conversation_id"] == OTHER_CHAT
    assert f"Chat: {OTHER_CHAT}" in html
    assert "No audit entries match the current filters." in html


@pytest.mark.parametrize("value", ["c" * 257, "chat\ninjected", "chat\x00id", "chat\x7f"])
async def test_invalid_chat_ids_are_refused_before_any_query(value, recorder):
    repo = FakeRepo()
    html = await audit_surface.render(scoped_orch(repo), "owner", [], {"conversation_id": value})
    components = await audit_surface.components(scoped_orch(repo), "owner", [], {
        "conversation_id": value})
    assert repo.list_calls == []
    assert recorder.events == []
    assert ("Choose a valid chat for the audit log." in html
            or "Audit filters cannot contain null characters." in html)
    assert components[0]["type"] == "alert" and components[0]["variant"] == "error"


async def test_nonscalar_chat_scope_is_ignored_and_never_retargets_owner():
    repo = FakeRepo()
    await audit_surface.components(scoped_orch(repo), "owner", [], {
        "conversation_id": {"owner_id": "other-owner"}, "user_id": "other-owner"})
    assert repo.list_calls[0][0] == "owner"
    assert repo.list_calls[0][1]["conversation_id"] is None


async def test_native_view_names_the_chat_and_keeps_scope_on_every_navigation(recorder):
    repo = FakeRepo(items=[make_dto(conversation_id=CHAT)], next_cursor="page-two")
    components = await audit_surface.components(scoped_orch(repo), "owner", [], {
        "conversation_id": CHAT, "q": "login"})
    assert repo.list_calls[0][1]["conversation_id"] == CHAT
    assert {"type": "text", "content": "Chat: Sourdough <research>", "variant": "h3"} in components
    form = next(node for node in _nodes(components) if node.get("type") == "param_picker")
    chat_field = next(field for field in form["fields"] if field["name"] == "conversation_id")
    assert chat_field["default"] == CHAT
    assert chat_field["options"][0] == {"value": "", "label": "All chats"}
    assert _button(components, "Next")["payload"]["fields"]["conversation_id"] == CHAT
    assert _button(components, "Failures")["payload"]["fields"] == {
        "conversation_id": CHAT, "outcome": "failure"}
    details = _button(components, "View details")["payload"]["params"]
    assert details["return_to"]["conversation_id"] == CHAT
    assert recorder.events[0].inputs_meta["filters"]["conversation_id"] == CHAT


async def test_page_handler_accepts_the_chat_field_and_all_chats_clears_it():
    surface, params, notice = await audit_surface._handle_audit_page(
        None, None, "owner", [], {"fields": {"conversation_id": CHAT, "outcome": "failure"}})
    assert (surface, params, notice) == ("audit", {"conversation_id": CHAT, "outcome": "failure"}, "")
    _, cleared, _ = await audit_surface._handle_audit_page(
        None, None, "owner", [], {"fields": {"conversation_id": "", "outcome": "failure"}})
    assert cleared == {"outcome": "failure"}
    _, refused, notice = await audit_surface._handle_audit_page(
        None, None, "owner", [], {"fields": {"conversation_id": "x" * 257}})
    assert refused == {} and "Choose a valid chat" in notice


async def test_actual_macos_dispatch_keeps_the_chat_across_apply_and_paging(recorder, monkeypatch):
    orch = FakeOrch(device="macos")
    repo = FakeRepo(items=[make_dto(conversation_id=CHAT)], next_cursor="page-two")
    orch.audit_repo = repo
    orch.attachment_repository = make_orch(repo).attachment_repository
    orch.history = FakeHistory({("verified-owner", CHAT): "Plan"})
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)

    assert await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {
        "surface": "audit", "params": {"conversation_id": CHAT, "user_id": "forged"},
    }, "verified-owner") is True
    frame = orch.sent[-1]
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "audit"
    assert {"type": "text", "content": "Chat: Plan", "variant": "h3"} in frame["components"]
    assert repo.list_calls[-1][0] == "verified-owner"
    assert repo.list_calls[-1][1]["conversation_id"] == CHAT

    await chrome_events.handle_chrome_event(orch, orch.ws, "chrome_audit_page", {
        "fields": {"conversation_id": CHAT, "outcome": "failure", "q": ""}}, "verified-owner")
    assert repo.list_calls[-1][1]["conversation_id"] == CHAT
    assert repo.list_calls[-1][1]["outcomes"] == ["failure"]

    next_button = _button(orch.sent[-1]["components"], "Next")
    await chrome_events.handle_chrome_event(
        orch, orch.ws, next_button["action"], next_button["payload"], "verified-owner")
    assert repo.list_calls[-1][1]["conversation_id"] == CHAT
    assert repo.list_calls[-1][1]["cursor"] == "page-two"

    all_chats = _button(orch.sent[-1]["components"], "All activity")
    await chrome_events.handle_chrome_event(
        orch, orch.ws, all_chats["action"], all_chats["payload"], "verified-owner")
    assert repo.list_calls[-1][1]["conversation_id"] is None


def _event(owner, conversation, second):
    at = datetime(2026, 11, 9, 12, tzinfo=timezone.utc) + timedelta(seconds=second)
    return AuditEventCreate(
        actor_user_id=owner, auth_principal=owner, event_class="conversation",
        action_type="conversation.message", description="Chat scope test",
        conversation_id=conversation, correlation_id=str(uuid.uuid4()), outcome="success",
        started_at=at, completed_at=at,
    )


def test_real_plane_storage_filters_own_chat_hides_foreign_and_keeps_filter_when_paging(
        monkeypatch):
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    monkeypatch.setenv("AUDIT_HMAC_SECRET", "audit-chat-scope-regression-32-characters")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "audit_chat_scope")
    with isolated_plane_runtime("ad_audit_chat_scope") as plane:
        repository = AuditRepository(plane_runtime=plane, plane_repositories=plane.repositories)
        seeded = [
            repository.insert(_event(owner, conversation, second))
            for second, (owner, conversation) in enumerate((
                ("owner-a", CHAT), ("owner-a", OTHER_CHAT), ("owner-a", CHAT),
                ("owner-a", None), ("owner-b", CHAT), ("owner-a", CHAT),
            ))
        ]
        expected = [seeded[index].event_id for index in (5, 2, 0)]

        first, cursor = repository.list_for_user("owner-a", conversation_id=CHAT, limit=2)
        second, end = repository.list_for_user(
            "owner-a", conversation_id=CHAT, cursor=cursor, limit=2)
        foreign, _ = repository.list_for_user("owner-c", conversation_id=CHAT)
        unknown, _ = repository.list_for_user("owner-a", conversation_id=str(uuid.uuid4()))
        everything, _ = repository.list_for_user("owner-a")

    assert cursor is not None and end is None
    assert [dto.event_id for dto in (*first, *second)] == expected
    assert {dto.conversation_id for dto in (*first, *second)} == {CHAT}
    assert foreign == [] and unknown == []
    assert len(everything) == 5


async def test_untitled_chat_uses_the_default_chat_name():
    html = await audit_surface.render(scoped_orch(FakeRepo(), FakeHistory({("owner", CHAT): ""})),
                                      "owner", [], {"conversation_id": CHAT})
    assert "Chat: New Chat" in html


async def test_refused_dates_keep_the_named_chat_scope_for_correction(recorder):
    repo = FakeRepo()
    params = {"conversation_id": CHAT, "from": "2026-02-30"}
    html = await audit_surface.render(scoped_orch(repo), "owner", [], params)
    components = await audit_surface.components(scoped_orch(repo), "owner", [], params)
    assert repo.list_calls == [] and recorder.events == []
    assert "Choose valid dates" in html and "Chat: Sourdough &lt;research&gt;" in html
    assert components[0]["type"] == "alert"
    assert {"type": "text", "content": "Chat: Sourdough <research>", "variant": "h3"} in components
    assert _button(components, "Failures")["payload"]["fields"]["conversation_id"] == CHAT
