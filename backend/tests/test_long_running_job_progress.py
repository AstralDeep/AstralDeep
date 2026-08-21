"""Long-running job progress auto-posts to the chat and survives refresh /
cross-device, and a completed job is narrated (model-written comparison) in the
chat rail.

Regression for the bug where a training job said "progress will post here
automatically" but nothing posted until the user manually asked for status: the
orchestrator's ToolProgress handler was gated behind the off-by-default
``progress_streaming`` flag (so progress + the cap release were dropped), and
delivery keyed on an ephemeral per-request socket (so a refreshed / other-device
client never saw it). The fix routes progress to the job's CHAT, persists the
terminal result into the per-chat workspace (028) so returning clients re-hydrate
the completed UI, AND narrates the comparison via the model into the chat rail.

Uses the established "real unbound Orchestrator methods bound onto a fake self
over a real Postgres-backed WorkspaceManager" pattern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import types
import uuid

import pytest

from orchestrator.history import ConversationCommitRepository
from orchestrator.orchestrator import Orchestrator
from orchestrator.workspace import WorkspaceManager
from shared.protocol import ToolProgress
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime

NARRATION = (
    "Random Forest performed best at 71.8% accuracy (AUC 0.739), "
    "beating Gradient Boosting's 61.5% (AUC 0.655)."
)


def _json_mapping_default(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported detached value: {type(value).__name__}")


@pytest.fixture(scope="module")
def plane_runtime():
    """Create one isolated current Plane runtime for job progress tests."""

    with isolated_plane_runtime("long_job_progress") as runtime:
        yield runtime


class _FakeWS:
    def __init__(self, label: str = ""):
        self.label = label


class _FakeCap:
    def __init__(self):
        self.released = []

    async def release(self, user_id, agent_id, job_id):
        self.released.append((user_id, agent_id, job_id))


@pytest.fixture
def chat_env(tmp_path, plane_runtime: PlaneTestRuntime):
    from orchestrator.history import HistoryManager

    history = HistoryManager(
        data_dir=str(tmp_path),
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    user_id = f"pytest-job-{uuid.uuid4().hex[:10]}"
    chat_id = history.create_chat(user_id=user_id)
    history.add_message(chat_id, "user", "train a model", user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


def _run(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    return asyncio.run(_wrapper())


def _make_fake(history, user_id, llm_content=NARRATION):
    """Fake orchestrator self with the real job-progress methods bound on. The
    LLM and chat-narrative seams are stubbed so the test is deterministic; pass
    ``llm_content=None`` to simulate no LLM available (fallback path)."""
    from rote.rote import ROTE

    sent = []

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload) if isinstance(payload, str) else payload))
        return True

    async def _call_llm(
        websocket, messages, tools_desc=None, temperature=None, feature="tool_dispatch"
    ):
        if llm_content is None:
            return None, None
        return types.SimpleNamespace(content=llm_content), {}

    ui_sessions = {}
    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        conversation_commits=ConversationCommitRepository(
            plane_runtime=history.plane_runtime,
            plane_repositories=history.plane_repositories,
        ),
        rote=ROTE(),
        ui_clients=[],
        ui_sessions=ui_sessions,
        _ws_active_chat={},
        _conversation_scopes={},
        _workspace_locks={},
        pending_ui_sockets={},
        _job_context={},
        _pending_cap_entries={},
        _hop_cap_entries={},
        concurrency_cap=_FakeCap(),
        _safe_send=_safe_send,
        _call_llm=_call_llm,
        _chat_narrative=lambda content: [{"type": "text", "content": content}],
        _get_user_id=lambda ws: (ui_sessions.get(ws) or {}).get("sub"),
    )
    for name in (
        "_handle_tool_progress",
        "_finalize_long_running_job",
        "_build_job_result_component",
        "_narrate_job_result",
        "_sockets_on_chat",
        "send_ui_upsert",
        "send_ui_render",
        "_release_hop_cap_slot",
        "_begin_detached_conversation_publication",
        "_append_conversation_message",
        "_publish_conversation_snapshot",
        "_deliver_committed_conversation_snapshot",
        "_conversation_snapshot_candidate",
        "_adapt_conversation_snapshot",
        "_bind_conversation_scope",
        "_broadcast_user_history",
        "_refresh_history_after_commit",
        "_push_history_surface",
    ):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._sent = sent
    return fake


def _seed_job(fake, user_id, chat_id, cap):
    fake._job_context[cap] = {
        "user_id": user_id,
        "agent_id": "ml-services-1",
        "chat_id": chat_id,
        "tool_name": "classify_start_training_job",
        "publication_request_generation": str(uuid.uuid4()),
    }
    fake._pending_cap_entries[cap] = (user_id, "ml-services-1")


def _register_socket(fake, user_id, chat_id):
    ws = _FakeWS()
    fake.ui_clients.append(ws)
    fake.ui_sessions[ws] = {"sub": user_id}
    fake._ws_active_chat[id(ws)] = chat_id
    fake._conversation_scopes[id(ws)] = {
        "chat_id": chat_id,
        "connection_generation": str(uuid.uuid4()),
        "request_generation": str(uuid.uuid4()),
        "purpose": "hydration",
        "base_render_revision": 0,
        "frame_sequence": 0,
    }
    return ws


def _terminal(cap, result=None, phase="completed", message="Training complete."):
    md = {"request_id": "req1", "phase": phase, "terminal": True, "cap_job_id": cap}
    if result is not None:
        md["result"] = result
    return ToolProgress(
        tool_name="classify_start_training_job",
        agent_id="ml-services-1",
        message=message,
        percentage=100,
        metadata=md,
    )


def test_terminal_result_persists_fans_out_and_narrates(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cap = "cap_classify_x"
    _seed_job(fake, user_id, chat_id, cap)
    _register_socket(fake, user_id, chat_id)

    result = {
        "random_forest": {"accuracy": 0.718, "auc": 0.739},
        "gradient_boosting": {"accuracy": 0.615, "auc": 0.655},
    }
    _run(fake._handle_tool_progress(_terminal(cap, result)))

    # Result Table persisted into the workspace (what load_chat re-hydrates).
    comps = fake.workspace.live_components(chat_id, user_id)
    blob = json.dumps(comps)
    assert "0.718" in blob and "random_forest.accuracy" in blob
    # Table delivered live as a ui_upsert.
    assert any(m.get("type") == "ui_upsert" for _, m in fake._sent)
    assert [m.get("type") for _, m in fake._sent].count(
        "conversation_commit_ready"
    ) == 1
    snapshots = [m for _, m in fake._sent if m.get("type") == "conversation_snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_purpose"] == "commit"
    assert snapshots[0]["render_revision"] == 1
    # Model-written comparison narrated into the chat rail (live)...
    assert any(
        m.get("type") == "ui_render"
        and m.get("target") == "chat"
        and NARRATION in json.dumps(m)
        for _, m in fake._sent
    )
    # ...and persisted in the transcript (so reload shows it).
    chat = history.get_chat(chat_id, user_id)
    assert NARRATION in json.dumps(
        chat.get("messages", []), default=_json_mapping_default
    )
    # Cap released + job context cleaned up.
    assert fake.concurrency_cap.released == [(user_id, "ml-services-1", cap)]
    assert cap not in fake._job_context and cap not in fake._pending_cap_entries


def test_completed_result_and_narration_available_after_refresh(chat_env):
    """No socket connected when the job finishes (user navigated away / switched
    device). Both the result component AND the narration must still be persisted
    so a returning client re-hydrates the completed UI + comparison."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cap = "cap_classify_y"
    _seed_job(fake, user_id, chat_id, cap)
    # No socket registered.

    _run(fake._handle_tool_progress(_terminal(cap, {"accuracy": 0.9})))

    comps = fake.workspace.live_components(chat_id, user_id)
    assert any("0.9" in json.dumps(c) for c in comps), (
        "result must persist even when nobody is connected"
    )
    chat = history.get_chat(chat_id, user_id)
    assert NARRATION in json.dumps(
        chat.get("messages", []), default=_json_mapping_default
    ), "narration must persist for a returning client"
    assert fake.concurrency_cap.released == [(user_id, "ml-services-1", cap)]


def test_narration_falls_back_to_note_when_no_llm(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_content=None)  # LLM unavailable
    cap = "cap_fallback"
    _seed_job(fake, user_id, chat_id, cap)
    _register_socket(fake, user_id, chat_id)

    _run(fake._handle_tool_progress(_terminal(cap, {"accuracy": 0.7})))

    chat = history.get_chat(chat_id, user_id)
    blob = json.dumps(chat.get("messages", []), default=_json_mapping_default)
    assert "Training complete" in blob, (
        "a deterministic completion note must still post"
    )
    # A chat-rail render still went out live.
    assert any(
        m.get("type") == "ui_render" and m.get("target") == "chat"
        for _, m in fake._sent
    )


def test_works_with_progress_streaming_flag_off(chat_env, monkeypatch):
    monkeypatch.setenv("FF_PROGRESS_STREAMING", "false")
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cap = "cap_classify_z"
    _seed_job(fake, user_id, chat_id, cap)
    _register_socket(fake, user_id, chat_id)

    _run(fake._handle_tool_progress(_terminal(cap, {"accuracy": 0.5})))

    comps = fake.workspace.live_components(chat_id, user_id)
    assert any("0.5" in json.dumps(c) for c in comps), (
        "auto-post must not depend on the progress_streaming flag"
    )


def test_live_progress_fans_out_without_persisting(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cap = "cap_classify_live"
    _seed_job(fake, user_id, chat_id, cap)
    _register_socket(fake, user_id, chat_id)

    msg = ToolProgress(
        tool_name="classify_start_training_job",
        agent_id="ml-services-1",
        message="Training... 50%",
        percentage=50,
        metadata={"request_id": "r", "phase": "training", "cap_job_id": cap},
    )
    _run(fake._handle_tool_progress(msg))

    # A live tool_progress was delivered to the connected socket...
    assert any(
        m.get("type") == "tool_progress" and m.get("percentage") == 50
        for _, m in fake._sent
    )
    # ...nothing persisted yet (not terminal); the cap is still held.
    assert fake.workspace.live_components(chat_id, user_id) == []
    assert cap in fake._pending_cap_entries
