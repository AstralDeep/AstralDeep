"""030 — memory meta-tool wiring (US2 / T014)."""
import asyncio
import sys
import types
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import memory_chat  # noqa: E402
from personalization import project_scope as ps  # noqa: E402
from personalization.memory_tools import MemoryTools  # noqa: E402


class _FakeRepo:
    def __init__(self):
        self.memory = []
        self.signals = []

    def create_memory(self, user_id, category, value, *, source="explicit",
                       salience=0.0, keywords=None, project_id=None):
        item = {"id": f"m{len(self.memory)}", "user_id": user_id,
                "category": category, "value": value, "source": source,
                "keywords": keywords, "project_id": project_id}
        self.memory.append(item)
        return item

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        rows = [dict(item) for item in self.memory if item["user_id"] == user_id]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    def add_signal(self, user_id, category, value):
        self.signals.append((category, value))

    # C-M2 link surface (links not asserted by these dispatch tests).
    def add_link(self, user_id, a_id, b_id):
        return True

    def linked_ids(self, user_id, mem_id):
        return []


class _Gate:
    def __init__(self, phi=False):
        self._phi = phi

    def contains_phi(self, value):
        return self._phi


def _fake_orch(repo, gate):
    orch = types.SimpleNamespace(
        personalization_service=types.SimpleNamespace(repo=repo),
    )
    # Pre-seed the cached MemoryTools so handle_meta_tool uses our injected gate
    # (avoids constructing the real Presidio gate in tests).
    orch._memory_tools = MemoryTools(repo, phi_gate=gate)
    return orch


def test_definitions_expose_three_tools():
    names = {d["function"]["name"] for d in memory_chat.meta_tool_definitions()}
    assert names == {"remember", "memory_search", "memory_get"}


def test_should_inject_respects_draft_and_flag():
    assert memory_chat.should_inject(None) is True  # flag default ON
    assert memory_chat.should_inject("draft-123") is False


def test_remember_stores_clean_value():
    repo = _FakeRepo()
    orch = _fake_orch(repo, _Gate(phi=False))
    resp = asyncio.run(memory_chat.handle_meta_tool(
        orch, "remember", {"value": "Prefers concise answers", "category": "preference"},
        user_id="u1", chat_id="c1", websocket=object()))
    assert resp.error is None
    assert resp.result["status"] == "stored"
    assert repo.memory[0]["value"] == "Prefers concise answers"


def test_remember_refuses_phi_and_persists_nothing():
    repo = _FakeRepo()
    orch = _fake_orch(repo, _Gate(phi=True))
    resp = asyncio.run(memory_chat.handle_meta_tool(
        orch, "remember", {"value": "patient SSN 123-45-6789"},
        user_id="u1", chat_id="c1", websocket=object()))
    assert resp.result["status"] == "refused"
    assert repo.memory == []


def test_memory_search_and_get():
    repo = _FakeRepo()
    orch = _fake_orch(repo, _Gate(phi=False))
    asyncio.run(memory_chat.handle_meta_tool(
        orch, "remember", {"value": "Works on NSF grants", "category": "context"},
        user_id="u1", chat_id="c1", websocket=object()))
    got = asyncio.run(memory_chat.handle_meta_tool(
        orch, "memory_get", {}, user_id="u1", chat_id="c1", websocket=object()))
    assert got.result["count"] == 1
    found = asyncio.run(memory_chat.handle_meta_tool(
        orch, "memory_search", {"query": "NSF"}, user_id="u1", chat_id="c1", websocket=object()))
    assert found.result["count"] == 1


def test_unknown_tool_errors():
    repo = _FakeRepo()
    orch = _fake_orch(repo, _Gate(phi=False))
    resp = asyncio.run(memory_chat.handle_meta_tool(
        orch, "nope", {}, user_id="u1", chat_id="c1", websocket=object()))
    assert resp.error is not None
