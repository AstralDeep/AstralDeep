"""Feature 033 (capability C-M1) — reconcile-don't-append memory writes.

Covers the pure decision helpers, the fail-open reconcile orchestration over a
fake repo (ADD / UPDATE / DELETE / NOOP + every fallback), and the real-DB
supersession + retrieval-exclusion.
"""
from __future__ import annotations

import uuid

import pytest

from personalization import project_scope as ps
from personalization.memory_tools import (
    MemoryTools,
    build_reconcile_messages,
    parse_reconcile_decision,
    reconcile_enabled,
)


# ───────────────────────── fakes ─────────────────────────────────────────────

class _FakeGate:
    def __init__(self, phi: bool = False):
        self._phi = phi

    def contains_phi(self, value):
        return self._phi


class _FakeRepo:
    """In-memory repo honoring the supersession contract (list excludes
    superseded; supersede soft-deletes)."""
    def __init__(self):
        self.rows = []

    def create_memory(self, user_id, category, value, *, source="explicit",
                       salience=0.0, keywords=None, project_id=None):
        item = {"id": f"m{len(self.rows)}", "user_id": user_id, "category": category,
                "value": value, "source": source, "keywords": keywords,
                "superseded_at": None, "superseded_by": None,
                "project_id": project_id}
        self.rows.append(item)
        return dict(item)

    def list_memory(self, user_id, *, project_id=None, include_global=True):
        rows = [dict(r) for r in self.rows
                if r["user_id"] == user_id and r["superseded_at"] is None]
        if project_id is None:
            return rows
        return ps.filter_to_project(rows, project_id, include_global=include_global)

    # C-M2 linked-note surface (links not asserted by the reconcile tests).
    def add_link(self, user_id, a_id, b_id):
        return True

    def linked_ids(self, user_id, mem_id):
        return []

    def supersede_memory(self, user_id, old_id, new_id=None):
        for r in self.rows:
            if r["id"] == old_id and r["user_id"] == user_id and r["superseded_at"] is None:
                r["superseded_at"] = 1
                r["superseded_by"] = new_id
                return True
        return False


def _stub_llm(reply, calls=None):
    async def _call(messages):
        if calls is not None:
            calls.append(messages)
        return reply
    return _call


def _mt(repo=None, phi=False):
    return MemoryTools(repo or _FakeRepo(), phi_gate=_FakeGate(phi))


# ───────────────────────── flag ──────────────────────────────────────────────

def test_reconcile_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_MEMORY_RECONCILE", raising=False)
    assert reconcile_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_reconcile_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_MEMORY_RECONCILE", value)
    assert reconcile_enabled() is False


# ───────────────────────── parse_reconcile_decision ──────────────────────────

@pytest.mark.parametrize("action", ["ADD", "UPDATE", "DELETE", "NOOP"])
def test_parse_each_action(action):
    d = parse_reconcile_decision(f'{{"action":"{action}","target":1,"value":"x"}}')
    assert d["action"] == action and d["target"] == 1 and d["value"] == "x"


def test_parse_tolerates_fence_and_prose():
    d = parse_reconcile_decision('Here:\n```json\n{"action":"update","target":"2"}\n```')
    assert d["action"] == "UPDATE" and d["target"] == 2 and d["value"] is None


def test_parse_null_target_and_value():
    d = parse_reconcile_decision('{"action":"ADD","target":null,"value":null}')
    assert d["action"] == "ADD" and d["target"] is None and d["value"] is None


@pytest.mark.parametrize("bad", ["", "not json", "{}", '{"action":"FROB"}', None, '{"target":1}'])
def test_parse_rejects_malformed(bad):
    assert parse_reconcile_decision(bad) is None


def test_build_messages_lists_new_and_existing():
    msgs = build_reconcile_messages("Lives in Seattle", "context",
                                    [{"category": "context", "value": "Lives in Portland"}])
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "Lives in Seattle" in blob and "Lives in Portland" in blob
    assert "ADD" in blob and "UPDATE" in blob and "DELETE" in blob and "NOOP" in blob


# ───────────────────────── reconcile orchestration ───────────────────────────

async def test_add_keeps_both():
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers dark mode")
    mt = _mt(repo)
    res = await mt.remember_reconciled("u", "preference", "Prefers concise answers",
                                       llm_call=_stub_llm('{"action":"ADD","target":null}'))
    assert res["stored"] is True and res["action"] == "add"
    assert {r["value"] for r in repo.list_memory("u")} == {
        "Prefers dark mode", "Prefers concise answers"}


async def test_update_supersedes_old():
    repo = _FakeRepo()
    old = repo.create_memory("u", "context", "Lives in Portland")
    mt = _mt(repo)
    res = await mt.remember_reconciled(
        "u", "context", "Lives in Seattle",
        llm_call=_stub_llm('{"action":"UPDATE","target":1,"value":"Lives in Seattle"}'))
    assert res["stored"] is True and res["action"] == "update"
    assert res["superseded"] == old["id"]
    live = repo.list_memory("u")
    assert [r["value"] for r in live] == ["Lives in Seattle"]  # Portland is gone


async def test_delete_supersedes_without_adding():
    repo = _FakeRepo()
    old = repo.create_memory("u", "workflow_tag", "Uses Vim")
    mt = _mt(repo)
    res = await mt.remember_reconciled("u", "workflow_tag", "No longer uses Vim",
                                       llm_call=_stub_llm('{"action":"DELETE","target":1}'))
    assert res["stored"] is False and res["action"] == "delete"
    assert res["superseded"] == old["id"]
    assert repo.list_memory("u") == []


async def test_noop_changes_nothing():
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers concise answers")
    mt = _mt(repo)
    res = await mt.remember_reconciled("u", "preference", "Likes concise responses",
                                       llm_call=_stub_llm('{"action":"NOOP","target":1}'))
    assert res["stored"] is False and res["action"] == "noop"
    assert len(repo.list_memory("u")) == 1


async def test_no_candidates_appends_without_calling_llm():
    repo = _FakeRepo()
    calls = []
    mt = _mt(repo)
    res = await mt.remember_reconciled("u", "goal", "Ship the memory feature",
                                       llm_call=_stub_llm('{"action":"NOOP"}', calls))
    assert res["stored"] is True
    assert calls == []  # empty repo → no candidates → LLM never consulted


async def test_llm_none_appends():
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers dark mode")
    res = await _mt(repo).remember_reconciled("u", "preference", "Prefers light mode",
                                              llm_call=None)
    assert res["stored"] is True
    assert len(repo.list_memory("u")) == 2  # plain append


async def test_llm_error_fails_open_to_append():
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers dark mode")

    async def _boom(_messages):
        raise RuntimeError("llm down")

    res = await _mt(repo).remember_reconciled("u", "preference", "x", llm_call=_boom)
    assert res["stored"] is True and len(repo.list_memory("u")) == 2


async def test_flag_off_appends(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_RECONCILE", "false")
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers dark mode")
    calls = []
    res = await _mt(repo).remember_reconciled("u", "preference", "Prefers light mode",
                                              llm_call=_stub_llm('{"action":"NOOP"}', calls))
    assert res["stored"] is True and calls == []  # reconcile skipped entirely


async def test_unresolvable_target_falls_back_to_add():
    repo = _FakeRepo()
    repo.create_memory("u", "preference", "Prefers dark mode")
    mt = _mt(repo)
    # UPDATE pointing at a non-existent candidate number → safe append
    res = await mt.remember_reconciled("u", "preference", "Prefers light mode",
                                       llm_call=_stub_llm('{"action":"UPDATE","target":9}'))
    assert res["stored"] is True and res["action"] == "add"
    assert len(repo.list_memory("u")) == 2


async def test_phi_is_refused_before_any_llm():
    repo = _FakeRepo()
    calls = []
    mt = _mt(repo, phi=True)
    res = await mt.remember_reconciled("u", "context", "patient SSN 123-45-6789",
                                       llm_call=_stub_llm('{"action":"ADD"}', calls))
    assert res["stored"] is False
    assert "protected health information" in res["reason"]
    assert repo.rows == [] and calls == []  # nothing persisted, LLM never saw it


# ───────────────────────── real-DB supersession ──────────────────────────────

def test_repo_supersede_excludes_from_recall():
    """The schema migration + supersede_memory round-trip over a real DB:
    a superseded row drops out of list_memory and carries superseded_by."""
    from shared.database import Database
    from personalization.repository import PersonalizationRepository
    repo = PersonalizationRepository(Database())
    user = f"pytest-recon-{uuid.uuid4().hex[:8]}"
    a = repo.create_memory(user, "context", "Lives in Portland")
    b = repo.create_memory(user, "context", "Lives in Seattle")
    assert {m["value"] for m in repo.list_memory(user)} == {"Lives in Portland", "Lives in Seattle"}

    assert repo.supersede_memory(user, a["id"], b["id"]) is True
    live = repo.list_memory(user)
    assert [m["value"] for m in live] == ["Lives in Seattle"]  # Portland excluded
    # second supersede of the same row is a no-op (already superseded)
    assert repo.supersede_memory(user, a["id"], b["id"]) is False
    # the soft-deleted row still exists with its pointer
    row = repo.get_memory(user, a["id"])
    assert row is not None and str(row["superseded_by"]) == b["id"]
