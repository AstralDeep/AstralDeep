"""Tests for the read-only memory seams: memory_search_ranked reproduces the exact
memory_search ordering with scores and without reinforcing recall, live_memory_snapshot
applies owner/project, temporal, and tamper filters, and memory_search itself still
reinforces.
"""

from __future__ import annotations


from personalization import project_scope as ps
from personalization.memory_tools import MemoryTools
from personalization.phi_gate import PHIGate


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


class _CountingRepo:
    def __init__(self):
        self.memory = []
        self.links = []
        self.recall_calls = []

    def create_memory(self, user_id, category, value, *, source="explicit",
                      salience=0.0, keywords=None, project_id=None):
        item = {
            "id": f"m{len(self.memory)}", "user_id": user_id, "category": category,
            "value": value, "source": source, "salience": salience,
            "keywords": keywords, "project_id": project_id,
            "created_at": len(self.memory), "updated_at": len(self.memory),
        }
        self.memory.append(item)
        return dict(item)

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        rows = [dict(item) for item in self.memory if item["user_id"] == user_id]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    def add_link(self, user_id, a_id, b_id):
        self.links.append((a_id, b_id))
        return True

    def linked_ids(self, user_id, mem_id):
        return [b for a, b in self.links if a == mem_id]

    def list_links(self, user_id):
        return [{"memory_id": a, "linked_id": b} for a, b in self.links]

    def record_recall(self, user_id, mem_id, now=None):
        self.recall_calls.append((user_id, mem_id))
        return True

    def get_memory(self, user_id, mem_id):
        for item in self.memory:
            if item["id"] == mem_id and item["user_id"] == user_id:
                return dict(item)
        return None


def _tools(repo=None):
    repo = repo or _CountingRepo()
    return MemoryTools(repo, phi_gate=PHIGate(analyzer=_CleanAnalyzer())), repo


def _seeded_tools():
    tools, repo = _tools()
    tools._do_add("u1", "preference", "prefers bullet points in summaries")
    tools._do_add("u1", "goal", "track grant deadlines each quarter")
    tools._do_add("u1", "context", "keeps receipts in a shoebox")
    return tools, repo


def test_ranked_seam_matches_memory_search_order():
    tools, _ = _seeded_tools()
    hits = tools.memory_search("u1", "grant deadlines")
    ranked = tools.memory_search_ranked("u1", "grant deadlines")
    assert [row["id"] for row, _ in ranked] == [row["id"] for row in hits]
    assert all(isinstance(score, float) for _, score in ranked)
    assert all(score > 0 for _, score in ranked)


def test_ranked_seam_does_not_reinforce():
    tools, repo = _seeded_tools()
    tools.memory_search_ranked("u1", "grant deadlines")
    assert repo.recall_calls == []


def test_memory_search_reinforces_when_forgetting_enabled(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_FORGETTING", "1")
    tools, repo = _seeded_tools()
    hits = tools.memory_search("u1", "grant deadlines")
    assert sorted(repo.recall_calls) == sorted(("u1", row["id"]) for row in hits)


def test_ranked_seam_empty_seed_returns_empty():
    tools, _ = _seeded_tools()
    ranked = tools.memory_search_ranked("u1", "zzzqqq")
    assert ranked == []


def test_memory_search_empty_seed_also_empty():
    tools, _ = _seeded_tools()
    assert tools.memory_search("u1", "zzzqqq") == []


def test_ranked_seam_empty_query_scores_zero():
    tools, _ = _seeded_tools()
    ranked = tools.memory_search_ranked("u1", "   ")
    assert len(ranked) == 3
    assert all(score == 0.0 for _, score in ranked)


def test_ranked_seam_pagerank_orders_seeds_first(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_PAGERANK", "1")
    tools, repo = _seeded_tools()
    repo.add_link("u1", "m1", "m2")
    repo.add_link("u1", "m2", "m1")
    ranked = tools.memory_search_ranked("u1", "grant deadlines")
    assert ranked[0][0]["id"] == "m1"
    assert any(row["id"] == "m2" for row, _ in ranked)


def test_live_memory_snapshot_scopes_and_filters(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "snapshot-test-key")
    tools, repo = _tools()
    tools._do_add("u1", "preference", "expired global row")
    tools._do_add("u1", "preference", "aurora only row", project_id="proj-aurora")
    tools._do_add("u1", "preference", "borealis only row", project_id="proj-borealis")
    tools._do_add("u1", "preference", "tampered aurora row", project_id="proj-aurora")
    tools._do_add("u1", "preference", "live global row")
    repo.memory[0]["valid_to"] = 1
    repo.memory[3]["signature"] = "f" * 64

    snapshot = tools.live_memory_snapshot("u1", project_id="proj-aurora")
    values = [row["value"] for row in snapshot]
    assert "expired global row" not in values
    assert "aurora only row" in values
    assert "borealis only row" not in values
    assert "tampered aurora row" not in values
    assert "live global row" in values
    assert repo.recall_calls == []


def test_live_memory_snapshot_without_scope(monkeypatch):
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    tools, _ = _tools()
    tools._do_add("u1", "preference", "aurora only row", project_id="proj-aurora")
    snapshot = tools.live_memory_snapshot("u1", project_id="proj-borealis")
    assert len(snapshot) == 1
