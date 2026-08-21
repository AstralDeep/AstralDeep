"""Feature 033 (capability C-M2) — A-MEM linked memory notes.

Each new memory gets deterministic keywords and is linked to its
keyword-overlapping neighbours; recall pulls in a hit's linked neighbours
(single-step multi-hop). Covers the pure keyword derivation, the write-time
linking, the link-aware retrieval, and a real-DB round-trip.
"""
from __future__ import annotations

import uuid

import pytest

from personalization import project_scope as ps
from personalization.memory_tools import (
    MemoryTools,
    derive_keywords,
    linking_enabled,
)
from personalization.repository import PersonalizationRepository
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


class _FakeGate:
    def contains_phi(self, value):
        return False


class _LinkRepo:
    """In-memory repo with the C-M2 link surface."""
    def __init__(self):
        self.rows = []
        self.links = set()  # directed (memory_id, linked_id) pairs

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

    def add_link(self, user_id, a_id, b_id):
        if not a_id or not b_id or a_id == b_id:
            return False
        self.links.add((a_id, b_id))
        self.links.add((b_id, a_id))
        return True

    def linked_ids(self, user_id, mem_id):
        live = {r["id"] for r in self.rows if r["superseded_at"] is None}
        return [b for (a, b) in self.links if a == mem_id and b in live]

    def supersede_memory(self, user_id, old_id, new_id=None):
        for r in self.rows:
            if r["id"] == old_id and r["superseded_at"] is None:
                r["superseded_at"] = 1
                return True
        return False


def _mt(repo=None):
    return MemoryTools(repo or _LinkRepo(), phi_gate=_FakeGate())


# ───────────────────────── flag ──────────────────────────────────────────────

def test_linking_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_MEMORY_LINKING", raising=False)
    assert linking_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_linking_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_MEMORY_LINKING", value)
    assert linking_enabled() is False


# ───────────────────────── derive_keywords ───────────────────────────────────

def test_keywords_extract_content_skip_stopwords():
    kw = derive_keywords("I prefer concise answers in markdown")
    toks = kw.split()
    assert "concise" in toks and "answers" in toks and "markdown" in toks
    assert "prefer" not in toks and "in" not in toks and "i" not in toks


def test_keywords_dedup_and_limit():
    kw = derive_keywords("grant grant grant " + " ".join(f"word{i}" for i in range(20)), limit=5)
    parts = kw.split()
    assert len(parts) == 5
    assert parts.count("grant") == 1


def test_keywords_empty_and_short():
    assert derive_keywords("") == ""
    assert derive_keywords("a an to") == ""  # all stopwords / <3 chars


# ───────────────────────── write-time linking ────────────────────────────────

def test_overlapping_memories_get_linked():
    repo = _LinkRepo()
    mt = _mt(repo)
    a = mt._do_add("u", "goal", "track NSF grant deadlines")
    b = mt._do_add("u", "workflow_tag", "grant submission portal")  # shares "grant"
    assert b["id"] in repo.linked_ids("u", a["id"])
    assert a["id"] in repo.linked_ids("u", b["id"])  # undirected


def test_unrelated_memories_are_not_linked():
    repo = _LinkRepo()
    mt = _mt(repo)
    a = mt._do_add("u", "preference", "enjoys hiking on weekends")
    mt._do_add("u", "goal", "learn the cello")
    assert repo.linked_ids("u", a["id"]) == []


def test_keywords_are_stored_on_write():
    repo = _LinkRepo()
    mt = _mt(repo)
    mt._do_add("u", "preference", "prefers dark mode themes")
    assert "dark" in repo.rows[0]["keywords"]


def test_linking_off_creates_no_links(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_LINKING", "false")
    repo = _LinkRepo()
    mt = _mt(repo)
    mt._do_add("u", "goal", "track NSF grant deadlines")
    mt._do_add("u", "workflow_tag", "grant submission portal")
    assert repo.links == set()


# ───────────────────────── link-aware retrieval ──────────────────────────────

def test_search_pulls_in_linked_neighbour():
    """A neighbour sharing NO query token surfaces via its link (multi-hop)."""
    repo = _LinkRepo()
    mt = _mt(repo)
    a = repo.create_memory("u", "goal", "track grant deadlines", keywords="track grant deadlines")
    b = repo.create_memory("u", "workflow_tag", "submission portal login",
                           keywords="submission portal login")
    repo.add_link("u", a["id"], b["id"])
    hits = mt.memory_search("u", "grant")  # only A matches the query directly
    ids = [h["id"] for h in hits]
    assert a["id"] in ids and b["id"] in ids  # B arrives via the link


def test_search_without_links_is_direct_only():
    repo = _LinkRepo()
    mt = _mt(repo)
    repo.create_memory("u", "goal", "track grant deadlines", keywords="track grant deadlines")
    repo.create_memory("u", "preference", "enjoys hiking", keywords="enjoys hiking")
    hits = mt.memory_search("u", "grant")
    assert [h["value"] for h in hits] == ["track grant deadlines"]


# ───────────────────────── real-DB round-trip ────────────────────────────────

def test_repo_links_round_trip_and_exclude_superseded():
    with isolated_plane_runtime("personalization_links") as runtime:
        repo = PersonalizationRepository(
            None,
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        user = f"pytest-link-{uuid.uuid4().hex[:8]}"
        first = repo.create_memory(
            user,
            "goal",
            "track grant deadlines",
            keywords="track grant deadlines",
        )
        second = repo.create_memory(
            user,
            "workflow_tag",
            "grant portal",
            keywords="grant portal",
        )
        assert repo.create_memory(user, "context", "x")["keywords"] is None

        assert repo.add_link(user, first["id"], second["id"]) is True
        assert second["id"] in repo.linked_ids(user, first["id"])
        assert first["id"] in repo.linked_ids(user, second["id"])  # undirected
        assert repo.add_link(user, first["id"], first["id"]) is False

        # Superseding a linked memory drops it from the neighbour's link list.
        repo.supersede_memory(user, second["id"], None)
        assert repo.linked_ids(user, first["id"]) == []
