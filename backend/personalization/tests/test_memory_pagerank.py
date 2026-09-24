"""Tests for the Personalized-PageRank recall in memory_tools.py: pure PPR properties
(mass conservation, determinism, decay by hop distance), PPR-backed memory_search
ranking and fallback, and a real-DB multi-hop round-trip.
"""

from __future__ import annotations

import uuid

import pytest

from personalization import project_scope as ps
from personalization.memory_tools import (
    MemoryTools,
    pagerank_enabled,
    personalized_pagerank,
)
from personalization.repository import PersonalizationRepository
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


def test_ppr_empty_graph_is_empty():
    assert personalized_pagerank({}, {}) == {}


def test_ppr_seed_outranks_its_neighbour():
    adj = {"A": ["B"], "B": ["A"]}
    r = personalized_pagerank(adj, {"A": 1.0})
    assert r["A"] > r["B"] > 0.0


def test_ppr_decays_along_a_chain():
    adj = {"A": ["B"], "B": ["A", "C"], "C": ["B"]}
    r = personalized_pagerank(adj, {"A": 1.0})
    assert r["C"] == min(r.values()) and r["C"] > 0.0
    assert r["A"] > r["C"]


def test_ppr_is_deterministic():
    adj = {"A": ["B"], "B": ["A"]}
    assert personalized_pagerank(adj, {"A": 1.0}) == personalized_pagerank(adj, {"A": 1.0})


def test_ppr_conserves_mass():
    adj = {"A": ["B"], "B": ["A", "C"], "C": ["B"], "D": []}
    r = personalized_pagerank(adj, {"A": 1.0})
    assert abs(sum(r.values()) - 1.0) < 1e-6


def test_ppr_unseeded_is_uniform_restart():
    adj = {"A": ["B"], "B": ["A"]}
    r = personalized_pagerank(adj, {})
    assert abs(r["A"] - r["B"]) < 1e-9


def test_ppr_unreachable_node_gets_no_personalized_mass():
    adj = {"A": ["B"], "B": ["A"], "X": ["Y"], "Y": ["X"]}
    r = personalized_pagerank(adj, {"A": 1.0})
    assert r["A"] > 0 and r["X"] < 1e-6


def test_pagerank_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_MEMORY_PAGERANK", raising=False)
    assert pagerank_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_pagerank_flag_off(monkeypatch, value):
    monkeypatch.setenv("FF_MEMORY_PAGERANK", value)
    assert pagerank_enabled() is False


class _Gate:
    def contains_phi(self, value):
        return False


class _GraphRepo:
    def __init__(self):
        self.rows = []
        self.edges = set()

    def create_memory(self, user_id, category, value, *, source="explicit",
                       salience=0.0, keywords=None, project_id=None):
        item = {"id": f"m{len(self.rows)}", "user_id": user_id, "category": category,
                "value": value, "source": source, "keywords": keywords,
                "superseded_at": None, "project_id": project_id}
        self.rows.append(item)
        return dict(item)

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        rows = [dict(r) for r in self.rows
                if r["user_id"] == user_id and r["superseded_at"] is None]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    def add_link(self, user_id, a, b):
        if not a or not b or a == b:
            return False
        self.edges.add((a, b))
        self.edges.add((b, a))
        return True

    def linked_ids(self, user_id, mid):
        live = {r["id"] for r in self.rows if r["superseded_at"] is None}
        return [b for (a, b) in self.edges if a == mid and b in live]

    def list_links(self, user_id):
        live = {r["id"] for r in self.rows if r["superseded_at"] is None}
        return [{"memory_id": a, "linked_id": b} for (a, b) in self.edges
                if a in live and b in live]


def _seed_chain(repo, user="u"):
    a = repo.create_memory(user, "goal", "track grant deadlines", keywords="track grant deadlines")
    b = repo.create_memory(user, "workflow_tag", "submission portal", keywords="submission portal")
    c = repo.create_memory(user, "context", "uses two factor auth", keywords="uses factor auth")
    repo.add_link(user, a["id"], b["id"])
    repo.add_link(user, b["id"], c["id"])
    return a, b, c


def test_search_ranks_seed_then_multi_hop_neighbours():
    repo = _GraphRepo()
    a, b, c = _seed_chain(repo)
    mt = MemoryTools(repo, phi_gate=_Gate())
    hits = mt.memory_search("u", "grant")
    ids = [h["id"] for h in hits]
    assert ids[0] == a["id"]
    assert b["id"] in ids and c["id"] in ids
    assert ids.index(b["id"]) < ids.index(c["id"])


def test_search_excludes_unconnected_nonmatch():
    repo = _GraphRepo()
    a, b, c = _seed_chain(repo)
    repo.create_memory("u", "preference", "enjoys hiking", keywords="enjoys hiking")
    mt = MemoryTools(repo, phi_gate=_Gate())
    vals = [h["value"] for h in mt.memory_search("u", "grant")]
    assert "enjoys hiking" not in vals


def test_search_no_graph_falls_back_to_direct():
    repo = _GraphRepo()
    repo.create_memory("u", "goal", "track grant deadlines", keywords="track grant deadlines")
    repo.create_memory("u", "preference", "enjoys hiking", keywords="enjoys hiking")
    mt = MemoryTools(repo, phi_gate=_Gate())
    assert [h["value"] for h in mt.memory_search("u", "grant")] == ["track grant deadlines"]


def test_search_respects_limit():
    repo = _GraphRepo()
    _seed_chain(repo)
    mt = MemoryTools(repo, phi_gate=_Gate())
    assert len(mt.memory_search("u", "grant", limit=2)) == 2


def test_pagerank_search_over_real_db():
    with isolated_plane_runtime("personalization_pagerank") as runtime:
        repo = PersonalizationRepository(
            None,
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        user = f"pytest-ppr-{uuid.uuid4().hex[:8]}"
        first = repo.create_memory(
            user,
            "goal",
            "track grant deadlines",
            keywords="track grant deadlines",
        )
        second = repo.create_memory(
            user,
            "workflow_tag",
            "submission portal",
            keywords="submission portal",
        )
        repo.add_link(user, first["id"], second["id"])
        edges = repo.list_links(user)
        assert {(edge["memory_id"], edge["linked_id"]) for edge in edges} >= {
            (first["id"], second["id"]),
            (second["id"], first["id"]),
        }

        tools = MemoryTools(repo, phi_gate=_Gate())
        values = [hit["value"] for hit in tools.memory_search(user, "grant")]
        assert "track grant deadlines" in values
        assert "submission portal" in values
