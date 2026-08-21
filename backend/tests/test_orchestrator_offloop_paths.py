"""Feature 052 — the orchestrator's off-loop inner seams execute for real.

Drives the ``get_history``/``load_chat`` WS actions, legacy canvas identity
canonicalization, and the delegation scope-read through a live
Orchestrator so the ``asyncio.to_thread`` inner functions introduced by the
perf pass (``_hydrate_loaded_chat``, ``_stamp_and_snapshot``,
``_scope_reads``) run end-to-end instead of being replicated in test code.
Requires the docker-compose Postgres; skipped where unreachable.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://fake.api")
os.environ.setdefault("LLM_MODEL", "test-model")

pytestmark = pytest.mark.asyncio

USER_ID = "test_user"


def _fresh_socket():
    """A VirtualWebSocket capturing every frame the handlers send."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    return VirtualWebSocket(task)


@pytest.fixture()
async def orch():
    """One real Orchestrator (mock auth) shared by the module's tests.

    Mock auth must be forced under BOTH env names AFTER imports: the
    ``shared`` package normalizes ``USE_MOCK_AUTH``/``USE_MOCK_AUTH``
    at import time and a container-exported ``USE_MOCK_AUTH=false`` would
    otherwise win over a module-level assignment.
    """
    saved = {name: os.environ.get(name)
             for name in ("USE_MOCK_AUTH", "USE_MOCK_AUTH")}
    os.environ["USE_MOCK_AUTH"] = "true"
    os.environ["USE_MOCK_AUTH"] = "true"
    from orchestrator.orchestrator import Orchestrator
    instance = None
    try:
        try:
            instance = await asyncio.to_thread(Orchestrator)
        except Exception as exc:
            pytest.skip(f"orchestrator/database unavailable: {exc}")
        yield instance
    finally:
        try:
            if instance is not None:
                await instance._close_started_services()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@pytest.fixture()
async def registered_ws(orch):
    """A VirtualWebSocket that completed the register_ui handshake."""
    ws = _fresh_socket()
    orch._registered_events[id(ws)] = asyncio.Event()
    await orch.handle_ui_message(ws, json.dumps(
        {"type": "register_ui", "token": "dev-token", "device": {}}))
    assert ws in orch.ui_sessions
    return ws


@pytest.fixture()
async def chat_env(orch):
    """A real chat owned by the mock-auth user; deleted on teardown."""
    chat_id = await asyncio.to_thread(
        orch.history.create_chat,
        user_id=USER_ID,
    )
    try:
        yield chat_id
    finally:
        await asyncio.to_thread(
            orch.history.delete_chat,
            chat_id,
            user_id=USER_ID,
        )


def _frames(ws, frame_type):
    return [f for f in ws.task.outputs if f.get("type") == frame_type]


async def test_get_history_pushes_skeleton_then_list(orch, registered_ws):
    ws = registered_ws
    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "get_history", "payload": {}}))
    listings = _frames(ws, "history_list")
    assert listings, "get_history must answer with a history_list frame"
    assert isinstance(listings[-1].get("chats"), list)


async def test_ui_event_before_auth_is_dropped_silently(orch):
    """A ui_event on an unauthenticated socket (register_ui not yet succeeded)
    must NOT paint a dead-end 'Unauthorized' alert. On a cold boot register_ui
    can transiently fail and send the recoverable auth_required frame; a
    concurrently-gated get_history reaching this branch would otherwise render
    a stale error the instant re-auth succeeds. It is dropped silently.
    """
    ws = _fresh_socket()
    assert ws not in orch.ui_sessions
    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "get_history", "payload": {}}))
    assert "Unauthorized" not in json.dumps(ws.task.outputs)
    assert _frames(ws, "history_list") == []  # dropped, not processed


async def test_load_chat_hydrates_transcript_html_off_loop(
        orch, registered_ws, chat_env):
    ws, chat_id = registered_ws, chat_env
    await asyncio.to_thread(
        orch.history.add_message, chat_id, "user", "show me my labs",
        user_id=USER_ID)
    await asyncio.to_thread(
        orch.history.add_message, chat_id, "assistant",
        [{"type": "alert", "message": "Lab results ready", "variant": "info"},
         {"type": "table", "title": "Labs", "headers": ["Test"], "rows": [["A1C"]]}],
        user_id=USER_ID)

    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "load_chat",
         "payload": {"chat_id": chat_id}}))

    loaded = _frames(ws, "chat_loaded")
    assert loaded, "load_chat must answer with a chat_loaded frame"
    messages = loaded[-1]["chat"]["messages"]
    comp_msg = next(m for m in messages if isinstance(m["content"], list))
    assert "Lab results ready" in comp_msg.get("html", "")
    assert "<table" not in comp_msg.get("html", "")
    text_msg = next(m for m in messages if isinstance(m["content"], str))
    assert "html" not in text_msg


async def test_load_chat_rehydrates_attachment_chips(
        orch, registered_ws, chat_env, monkeypatch):
    ws, chat_id = registered_ws, chat_env
    await asyncio.to_thread(
        orch.history.add_message, chat_id, "user", "read this file",
        user_id=USER_ID)

    await asyncio.to_thread(
        orch.history.add_message, chat_id, "assistant", "no chips for me",
        user_id=USER_ID)

    from orchestrator.attachments.message_attachment_repo import (
        MessageAttachmentRepository,
    )
    from orchestrator.attachments.repository import AttachmentRepository
    att = SimpleNamespace(attachment_id="att-052", filename="notes.md",
                          category="text")
    monkeypatch.setattr(
        MessageAttachmentRepository, "list_for_message",
        lambda self, message_id, user_id: [{"attachment_id": "att-052"}])
    monkeypatch.setattr(
        AttachmentRepository, "get_by_id",
        lambda self, attachment_id, user_id: att)

    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "load_chat",
         "payload": {"chat_id": chat_id}}))

    loaded = _frames(ws, "chat_loaded")
    assert loaded
    user_msg = next(m for m in loaded[-1]["chat"]["messages"]
                    if m["role"] == "user")
    assert user_msg.get("attachments") == [
        {"attachment_id": "att-052", "filename": "notes.md", "category": "text"}]
    assistant_msg = next(m for m in loaded[-1]["chat"]["messages"]
                         if m["role"] == "assistant")
    assert "attachments" not in assistant_msg


async def test_load_chat_rehydrates_attachment_chips_real_row(
        orch, registered_ws, chat_env):
    """Regression for the int-vs-text message_id bug: a real
    message_attachment row (written through the repo, keyed on the integer
    messages.id) must re-hydrate on load_chat. A monkeypatched repo hides
    this because the actual WHERE message_id = <int> never runs; here it does,
    against real Postgres, so a `text = integer` mismatch would fail the test.
    """
    from orchestrator.attachments.message_attachment_repo import (
        MessageAttachmentRepository,
    )
    from orchestrator.attachments.repository import AttachmentRepository
    from orchestrator.plane_repository_context import plane_source_from_orchestrator
    from tests.helpers.attachment_materialization import publish_attachment_for_test

    ws, chat_id = registered_ws, chat_env
    att_id = str(uuid.uuid4())
    plane = orch.runtime_composition.plane
    source = plane_source_from_orchestrator(orch)
    await asyncio.to_thread(
        publish_attachment_for_test,
        plane.runtime,
        plane.repositories,
        plane.blobs,
        owner_id=USER_ID,
        attachment_id=att_id,
        filename="real-notes.md",
        content_type="text/markdown",
        category="text",
        extension="md",
        chunks=(b"hello world!",),
        max_bytes=12,
    )
    att_repo = AttachmentRepository.from_plane_source(source)
    link_repo = MessageAttachmentRepository.from_plane_source(source)
    try:
        await asyncio.to_thread(
            orch.history.add_message, chat_id, "user", "read this real file",
            user_id=USER_ID)
        msg_id = await asyncio.to_thread(
            orch.history.get_latest_message_id, chat_id, USER_ID)
        assert isinstance(msg_id, int), "messages.id is the integer PK"
        assert await asyncio.to_thread(
            att_repo.get_by_id,
            att_id,
            USER_ID,
        ) is not None
        await asyncio.to_thread(
            link_repo.insert, chat_id=chat_id, attachment_id=att_id,
            user_id=USER_ID, message_id=msg_id)

        await orch.handle_ui_message(ws, json.dumps(
            {"type": "ui_event", "action": "load_chat",
             "payload": {"chat_id": chat_id}}))

        loaded = _frames(ws, "chat_loaded")
        assert loaded
        user_msg = next(m for m in loaded[-1]["chat"]["messages"]
                        if m["role"] == "user")
        assert user_msg.get("attachments") == [
            {"attachment_id": att_id, "filename": "real-notes.md",
             "category": "text"}]
    finally:
        await asyncio.to_thread(
            orch.attachment_purge_coordinator.schedule_attachment,
            owner_id=USER_ID,
            attachment_id=att_id,
        )


async def test_load_chat_survives_transcript_render_failure(
        orch, registered_ws, chat_env, monkeypatch):
    ws, chat_id = registered_ws, chat_env
    await asyncio.to_thread(
        orch.history.add_message, chat_id, "assistant",
        [{"type": "alert", "message": "boom bait", "variant": "info"}],
        user_id=USER_ID)

    from orchestrator.orchestrator import Orchestrator

    def _boom(content):
        raise RuntimeError("renderer down")

    monkeypatch.setattr(Orchestrator, "_transcript_html", staticmethod(_boom))

    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "load_chat",
         "payload": {"chat_id": chat_id}}))

    loaded = _frames(ws, "chat_loaded")
    assert loaded, "transcript render failure must not break load_chat"
    assert all("html" not in m for m in loaded[-1]["chat"]["messages"])


async def test_load_chat_survives_attachment_rehydration_failure(
        orch, registered_ws, chat_env, monkeypatch):
    ws, chat_id = registered_ws, chat_env
    await asyncio.to_thread(
        orch.history.add_message, chat_id, "user", "hello", user_id=USER_ID)

    from orchestrator.attachments.message_attachment_repo import (
        MessageAttachmentRepository,
    )

    def _boom(self, message_id, user_id):
        raise RuntimeError("link repo down")

    monkeypatch.setattr(MessageAttachmentRepository, "list_for_message", _boom)

    await orch.handle_ui_message(ws, json.dumps(
        {"type": "ui_event", "action": "load_chat",
         "payload": {"chat_id": chat_id}}))

    loaded = _frames(ws, "chat_loaded")
    assert loaded, "chip re-hydration failure must not break load_chat"
    assert loaded[-1]["chat"]["id"] == chat_id


async def test_legacy_replacement_resolves_identities_in_publication(
        orch, chat_env):
    chat_id = chat_env
    now_ms = int(time.time() * 1000)

    def _seed_rows():
        fresh_id = str(uuid.uuid4())
        kept_id = str(uuid.uuid4())
        runtime = orch.runtime_composition.plane.runtime
        with runtime.transaction() as transaction:
            transaction.execute(
                "INSERT INTO saved_components "
                "(id, chat_id, user_id, component_data, component_type, title, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (fresh_id, chat_id, USER_ID,
                 json.dumps({"type": "metric", "title": "Fresh", "value": "1",
                             "_source_agent": "agent-a", "_source_tool": "tool-a",
                             "_source_params": {}}),
                 "metric", "Fresh", now_ms),
            )
            transaction.execute(
                "INSERT INTO saved_components "
                "(id, chat_id, user_id, component_data, component_type, title, "
                "created_at, component_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (kept_id, chat_id, USER_ID,
                 json.dumps({"type": "text", "content": "kept",
                             "component_id": "wc_prestamped"}),
                 "text", "Kept", now_ms + 1, "wc_prestamped"),
            )
        return fresh_id, kept_id

    fresh_id, kept_id = await asyncio.to_thread(_seed_rows)
    fresh = await asyncio.to_thread(
        orch.history.get_component_by_id, fresh_id, user_id=USER_ID
    )
    kept = await asyncio.to_thread(
        orch.history.get_component_by_id, kept_id, user_id=USER_ID
    )
    assert fresh is not None and kept is not None

    identities = {}

    async def _mutation():
        identities["Fresh"] = await orch._workspace_identity_for_saved_row(
            chat_id=chat_id, user_id=USER_ID, row=fresh
        )
        identities["Kept"] = await orch._workspace_identity_for_saved_row(
            chat_id=chat_id, user_id=USER_ID, row=kept
        )
        await orch.workspace.aupsert(
            chat_id,
            USER_ID,
            [{"type": "text", "content": "publication marker"}],
        )
        await orch.workspace.asnapshot(
            chat_id, USER_ID, cause="combine_components"
        )

    await orch.run_detached_conversation_mutation(
        chat_id=chat_id, user_id=USER_ID, mutation=_mutation
    )

    rows = await asyncio.to_thread(orch.workspace.live_rows, chat_id, USER_ID)
    by_title = {r["title"]: r for r in rows}
    by_identity = {r["component_id"]: r for r in rows}
    assert identities["Fresh"].startswith("cc_")
    assert by_title["Fresh"]["component_id"] == identities["Fresh"]
    assert identities["Kept"] == "wc_prestamped"
    assert by_identity["wc_prestamped"]["component_id"] == "wc_prestamped"
    count = await asyncio.to_thread(
        orch.workspace.count_snapshots, chat_id, USER_ID)
    assert count >= 1


async def test_legacy_publication_rejects_malformed_component_json(
        orch, chat_env):
    from astralplane.repositories import RepositoryDataError

    chat_id = chat_env
    runtime = orch.runtime_composition.plane.runtime

    def _seed_malformed():
        with runtime.transaction() as transaction:
            transaction.execute(
                "INSERT INTO saved_components "
                "(id, chat_id, user_id, component_data, component_type, title, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), chat_id, USER_ID, "not-json{", "text",
                 "Broken", int(time.time() * 1000)),
            )

    await asyncio.to_thread(_seed_malformed)
    mutation_called = False

    async def _mutation():
        nonlocal mutation_called
        mutation_called = True

    with pytest.raises(RepositoryDataError, match="not valid JSON"):
        await orch.run_detached_conversation_mutation(
            chat_id=chat_id, user_id=USER_ID, mutation=_mutation
        )
    assert mutation_called is False
    record = await asyncio.to_thread(
        orch.history.get_conversation_record,
        chat_id,
        user_id=USER_ID,
    )
    assert record is not None and record.render_revision == 0


async def test_get_delegation_token_scopes_off_loop(orch, monkeypatch):
    from shared.protocol import AgentCard, AgentSkill
    agent_id = f"deleg-test-{uuid.uuid4().hex[:8]}"
    card = AgentCard(
        name="Delegation Test", description="d", agent_id=agent_id,
        skills=[
            AgentSkill(name="a", description="", id="tool_a", scope="tools:read"),
            AgentSkill(name="b", description="", id="tool_b", scope="tools:read"),
            AgentSkill(name="c", description="", id="tool_c", scope="tools:read"),
        ])
    orch.agent_cards[agent_id] = card
    orch.security_flags[agent_id] = {"tool_b": {"blocked": True}}
    monkeypatch.setattr(
        orch.tool_permissions, "is_tool_allowed",
        lambda user_id, aid, tool: tool != "tool_c")
    monkeypatch.setattr(
        orch.tool_permissions, "get_enabled_scope_names",
        lambda user_id, aid: ["tools:read"])

    exchanged = {}

    class _StubDelegation:
        async def exchange_token_for_agent(self, raw_token, aid, allowed_tools,
                                           user_id, enabled_scopes):
            exchanged.update(raw_token=raw_token, allowed_tools=allowed_tools,
                             enabled_scopes=enabled_scopes)
            return {"access_token": "delegated-token"}

    monkeypatch.setattr(orch, "delegation", _StubDelegation())

    ws = _fresh_socket()
    orch.ui_sessions[ws] = {"sub": USER_ID, "_raw_token": "raw-user-token"}
    try:
        token = await orch._get_delegation_token(ws, agent_id, USER_ID)
        assert token == "delegated-token"
        assert exchanged["raw_token"] == "raw-user-token"
        assert exchanged["allowed_tools"] == ["tool_a"]
        assert exchanged["enabled_scopes"] == ["tools:read"]

        assert await orch._get_delegation_token(ws, "missing-agent", USER_ID) is None
        orch.ui_sessions[ws] = {"sub": USER_ID}
        assert await orch._get_delegation_token(ws, agent_id, USER_ID) is None
    finally:
        orch.ui_sessions.pop(ws, None)
        orch.agent_cards.pop(agent_id, None)
        orch.security_flags.pop(agent_id, None)
