"""Tests for memory tools (feature 025, T033/T035)."""
from __future__ import annotations

from personalization import project_scope as ps
from personalization.memory_tools import MemoryTools
from personalization.phi_gate import PHIGate


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def _gate() -> PHIGate:
    return PHIGate(analyzer=_CleanAnalyzer())


class _FakeRepo:
    def __init__(self):
        self.memory = []
        self.signals = []

    def create_memory(self, user_id, category, value, *, source="explicit",
                       salience=0.0, keywords=None, project_id=None):
        item = {"id": f"m{len(self.memory)}", "user_id": user_id, "category": category,
                "value": value, "source": source, "keywords": keywords,
                "project_id": project_id}
        self.memory.append(item)
        return item

    def add_signal(self, user_id, category, value):
        self.signals.append({"category": category, "value": value})
        return {"id": f"s{len(self.signals)}"}

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        rows = [dict(item) for item in self.memory if item["user_id"] == user_id]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    # C-M2 link surface (links not asserted by these legacy tests).
    def add_link(self, user_id, a_id, b_id):
        return True

    def linked_ids(self, user_id, mem_id):
        return []


def test_remember_stores_clean_value():
    repo = _FakeRepo()
    mt = MemoryTools(repo, phi_gate=_gate())
    res = mt.remember("u1", "preference", "Prefers concise answers")
    assert res["stored"] is True
    assert repo.memory[0]["value"] == "Prefers concise answers"


def test_remember_refuses_phi():
    repo = _FakeRepo()
    mt = MemoryTools(repo, phi_gate=_gate())
    res = mt.remember("u1", "context", "patient SSN 123-45-6789")
    assert res["stored"] is False
    assert "protected health information" in res["reason"]
    assert repo.memory == []  # nothing persisted (SC-005)


def test_capture_signal_drops_phi():
    repo = _FakeRepo()
    mt = MemoryTools(repo, phi_gate=_gate())
    assert mt.capture_signal("u1", "preference", "likes dark mode") is True
    assert mt.capture_signal("u1", "context", "DOB 1980-04-12") is False
    assert len(repo.signals) == 1


def test_memory_search_ranks_by_overlap():
    repo = _FakeRepo()
    mt = MemoryTools(repo, phi_gate=_gate())
    mt.remember("u1", "preference", "prefers bullet points in summaries")
    mt.remember("u1", "goal", "track grant deadlines")
    hits = mt.memory_search("u1", "grant deadline")
    assert hits and hits[0]["value"] == "track grant deadlines"


def test_unknown_category_defaults_to_context():
    repo = _FakeRepo()
    mt = MemoryTools(repo, phi_gate=_gate())
    res = mt.remember("u1", "bogus", "some note")
    assert res["category"] == "context"
