"""Tests for orchestrator/memory_chat.py: memory meta-tool definitions, injection
gating, remember/search/get dispatch, and refusing to persist PHI.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

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
        self.superseded = []

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

    def delete_memory(self, user_id, mem_id):
        before = len(self.memory)
        self.memory = [item for item in self.memory
                       if not (item["id"] == mem_id and item["user_id"] == user_id)]
        return len(self.memory) < before

    def supersede_memory(self, user_id, old_id, new_id=None):
        self.superseded.append(old_id)
        return True

    def add_signal(self, user_id, category, value):
        self.signals.append((category, value))

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
    orch._memory_tools = MemoryTools(repo, phi_gate=gate)
    return orch


def test_definitions_expose_four_tools():
    names = {d["function"]["name"] for d in memory_chat.meta_tool_definitions()}
    assert names == {"remember", "memory_search", "memory_get", "forget"}


_FACT_ID = "7f1c2a7e-3b1d-4c55-9a0e-2d5b8f4e6a11"


def _repo_with_fact(owner="u1"):
    repo = _FakeRepo()
    repo.memory.append({"id": _FACT_ID, "user_id": owner, "category": "preference",
                        "value": "favorite tea is oolong"})
    return repo


def test_forget_erases_the_stored_fact():
    repo = _repo_with_fact()
    res = asyncio.run(memory_chat.handle_meta_tool(
        _fake_orch(repo, _Gate()), "forget", {"id": _FACT_ID},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.result["status"] == "deleted"
    assert repo.memory == []
    assert repo.superseded == []


def test_forget_cannot_reach_another_owners_fact():
    repo = _repo_with_fact(owner="someone-else")
    res = asyncio.run(memory_chat.handle_meta_tool(
        _fake_orch(repo, _Gate()), "forget", {"id": _FACT_ID},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.result["status"] == "not_found"
    assert len(repo.memory) == 1


def test_forget_reports_an_id_that_is_not_a_memory_id():
    repo = _repo_with_fact()
    res = asyncio.run(memory_chat.handle_meta_tool(
        _fake_orch(repo, _Gate()), "forget", {"id": "favorite tea"},
        user_id="u1", chat_id="c1", websocket=None))
    assert res.result["status"] == "not_found"
    assert len(repo.memory) == 1


@pytest.fixture
def audits(monkeypatch):
    events = []

    class _Recorder:
        async def record(self, event):
            events.append(event)

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Recorder())
    return events


def _forget(repo, memory_id):
    return asyncio.run(memory_chat.handle_meta_tool(
        _fake_orch(repo, _Gate()), "forget", {"id": memory_id},
        user_id="u1", chat_id="c1", websocket=None))


def _atlas_envelope():
    return json.dumps({"body": "ciphertext", "format": "astral.atlas-page/v1", "kind": "page",
                       "owner_id": "u1", "page_id": "p1", "revision": 1,
                       "slug": "tea-notes", "title": "Tea notes"}, sort_keys=True)


def test_forget_normalizes_the_id_before_it_reaches_the_repository(audits):
    repo = _repo_with_fact()
    res = _forget(repo, f"urn:uuid:{_FACT_ID.upper()}")
    assert res.result == {"status": "deleted", "id": _FACT_ID}
    assert repo.memory == []
    assert [(e.action_type, e.outcome, e.inputs_meta) for e in audits] == [
        ("memory.delete", "success", {"memory_id": _FACT_ID})]


def test_forget_cannot_erase_an_atlas_page_row(audits):
    repo = _FakeRepo()
    repo.memory.append({"id": _FACT_ID, "user_id": "u1", "category": "context",
                        "value": _atlas_envelope(), "source": "explicit"})
    assert _forget(repo, _FACT_ID).result["status"] == "not_found"
    assert len(repo.memory) == 1
    assert [(e.action_type, e.outcome) for e in audits] == [("memory.delete", "failure")]


def test_forget_cannot_erase_a_tampered_row(monkeypatch, audits):
    monkeypatch.setenv("MEMORY_HMAC_KEY", "test-only-key")
    repo = _repo_with_fact()
    repo.memory[0]["signature"] = "not-the-real-signature"
    assert _forget(repo, _FACT_ID).result["status"] == "not_found"
    assert len(repo.memory) == 1


@pytest.mark.parametrize("memory_id,audited", [
    ("0b8f0a52-6f1e-4d8e-9a43-1c2d3e4f5a6b", "0b8f0a52-6f1e-4d8e-9a43-1c2d3e4f5a6b"),
    ("favorite tea", None),
    (None, None),
])
def test_a_forget_that_finds_nothing_is_audited_as_a_failure(audits, memory_id, audited):
    repo = _repo_with_fact()
    assert _forget(repo, memory_id).result["status"] == "not_found"
    assert len(repo.memory) == 1
    assert [(e.action_type, e.outcome, e.inputs_meta) for e in audits] == [
        ("memory.delete", "failure", {"memory_id": audited})]


def test_forget_reports_a_row_that_vanished_before_the_delete(audits):
    repo = _repo_with_fact()
    repo.delete_memory = lambda user_id, mem_id: False
    assert _forget(repo, _FACT_ID).result["status"] == "not_found"
    assert [(e.action_type, e.outcome) for e in audits] == [("memory.delete", "failure")]


def test_a_refused_remember_is_audited_as_a_failure(audits):
    repo = _FakeRepo()
    asyncio.run(memory_chat.handle_meta_tool(
        _fake_orch(repo, _Gate(phi=True)), "remember", {"value": "MRN 4471"},
        user_id="u1", chat_id="c1", websocket=None))
    assert [(e.action_type, e.outcome) for e in audits] == [("memory.remember_refused", "failure")]


def test_a_reconcile_delete_supersedes_instead_of_erasing():
    repo = _repo_with_fact()

    async def decide(messages):
        return '{"action": "DELETE", "target": 1}'

    res = asyncio.run(MemoryTools(repo, phi_gate=_Gate()).remember_reconciled(
        "u1", "preference", "no longer likes oolong tea", llm_call=decide))
    assert res["action"] == "delete"
    assert repo.superseded == [_FACT_ID]
    assert [item["id"] for item in repo.memory] == [_FACT_ID]


def test_should_inject_respects_draft_and_flag():
    assert memory_chat.should_inject(None) is True
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
