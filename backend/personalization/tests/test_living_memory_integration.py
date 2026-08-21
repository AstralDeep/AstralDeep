"""Feature 033 — real integration of living memory and project scope.

Unlike ``test_living_memory.py`` (pure functions) and the fake-repo reconcile
suite, these drive a Plane-backed ``PersonalizationRepository`` + ``MemoryTools``
and assert the wired behavior end-to-end with the feature flags on and off.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from personalization import living_memory as lm  # noqa: E402
from personalization.memory_tools import MemoryTools  # noqa: E402
from personalization.repository import PersonalizationRepository  # noqa: E402
from tests.helpers.voice_plane_runtime import (  # noqa: E402
    PlaneTestRuntime,
    isolated_plane_runtime,
)


# ───────────────────────── harness ───────────────────────────────────────────

@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("personalization_living") as runtime:
        yield runtime

DAY = 24 * 3600 * 1000


class _CleanGate:
    """A PHI gate that never flags — keeps these tests Presidio-free."""
    def contains_phi(self, value):
        return False


def _tools(runtime: PlaneTestRuntime):
    repo = PersonalizationRepository(
        None,
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    return MemoryTools(repo, phi_gate=_CleanGate()), repo


def _user(tag: str) -> str:
    return f"pytest-living-{tag}-{uuid.uuid4().hex[:8]}"


def _on(monkeypatch, **flags):
    for k, v in flags.items():
        monkeypatch.setenv(k, v)


# ───────────────────────── C-M6 temporal validity (READ) ─────────────────────

def test_expired_valid_to_excluded_from_search(monkeypatch, plane_runtime):
    """A memory whose validity window has closed (valid_to in the past) is hidden
    from memory_search / memory_get when FF_MEMORY_TEMPORAL is on."""
    _on(monkeypatch, FF_MEMORY_TEMPORAL="true")
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("temporal")

    # Two distinct preferences; one will be force-expired.
    live = mt.remember(user, "preference", "prefers espresso coffee")
    stale = mt.remember(user, "preference", "prefers decaf tea")
    assert live["stored"] and stale["stored"]

    # Close the stale one's window in the past → must drop out of recall.
    past = int(time.time() * 1000) - DAY
    assert repo.set_validity(user, stale["id"], valid_from=past - DAY,
                             valid_to=past, ingested_at=past - DAY) is True

    got_get = {m["value"] for m in mt.memory_get(user)}
    assert "prefers espresso coffee" in got_get
    assert "prefers decaf tea" not in got_get  # expired hidden

    hits = {m["value"] for m in mt.memory_search(user, "coffee tea preference")}
    assert "prefers espresso coffee" in hits
    assert "prefers decaf tea" not in hits


def test_temporal_off_keeps_expired_visible(monkeypatch, plane_runtime):
    """Flag OFF: an expired valid_to is ignored — recall is unchanged (today)."""
    monkeypatch.delenv("FF_MEMORY_TEMPORAL", raising=False)
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("temporal-off")

    m = mt.remember(user, "preference", "prefers oat milk lattes")
    past = int(time.time() * 1000) - DAY
    repo.set_validity(user, m["id"], valid_from=past - DAY, valid_to=past)

    # With temporal off, the closed window is invisible to recall logic.
    assert any(x["value"] == "prefers oat milk lattes" for x in mt.memory_get(user))
    hits = mt.memory_search(user, "oat milk lattes")
    assert any(x["value"] == "prefers oat milk lattes" for x in hits)


def test_singular_category_contradiction_closes_prior_window(
    monkeypatch,
    plane_runtime,
):
    """C-M6 WRITE: in a SINGULAR category (profession) a new value temporally
    supersedes the prior one — the older fact's window is closed so an as-of
    recall surfaces only the latest. Provenance (ingested_at) is stamped."""
    _on(monkeypatch, FF_MEMORY_TEMPORAL="true")
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("contradict")

    first = mt.remember(user, "profession", "Software engineer at Acme")
    time.sleep(0.01)
    second = mt.remember(user, "profession", "Engineering manager at Acme")

    # The earlier profession's window was closed on the contradicting write.
    row = repo.get_memory(user, first["id"])
    assert row is not None and row["valid_to"] is not None
    # Recall (as-of now) shows the latest value, not the superseded one.
    vals = {m["value"] for m in mt.memory_get(user)}
    assert "Engineering manager at Acme" in vals
    assert "Software engineer at Acme" not in vals
    # The newest fact carries an open window + ingested_at provenance.
    newrow = repo.get_memory(user, second["id"])
    assert newrow["valid_to"] is None and newrow["ingested_at"] is not None


def test_multivalued_category_keeps_all_live(monkeypatch, plane_runtime):
    """C-M6 WRITE: a multi-valued category (preference) is NOT auto-closed — two
    distinct preferences both stay live (a user holds many at once)."""
    _on(monkeypatch, FF_MEMORY_TEMPORAL="true")
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("multi")

    p1 = mt.remember(user, "preference", "prefers concise summaries")
    p2 = mt.remember(user, "preference", "prefers metric units")
    # Both windows stay open — neither closed the other.
    assert repo.get_memory(user, p1["id"])["valid_to"] is None
    assert repo.get_memory(user, p2["id"])["valid_to"] is None
    vals = {m["value"] for m in mt.memory_get(user)}
    assert {"prefers concise summaries", "prefers metric units"} <= vals


# ───────────────────────── C-M7 reinforcement on recall ──────────────────────

def test_recall_bumps_recall_count(monkeypatch, plane_runtime):
    """C-M7: a recall (memory_search / memory_get) reinforces the surfaced rows —
    recall_count increments and last_recalled_at is stamped."""
    _on(monkeypatch, FF_MEMORY_FORGETTING="true")
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    monkeypatch.delenv("FF_MEMORY_TEMPORAL", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("reinforce")

    m = mt.remember(user, "goal", "ship the quarterly roadmap")
    assert int(repo.get_memory(user, m["id"]).get("recall_count") or 0) == 0

    hits = mt.memory_search(user, "quarterly roadmap")
    assert any(h["id"] == m["id"] for h in hits)
    after_search = int(repo.get_memory(user, m["id"])["recall_count"])
    assert after_search == 1

    mt.memory_get(user)  # full recall reinforces again
    row = repo.get_memory(user, m["id"])
    assert int(row["recall_count"]) == 2
    assert row["last_recalled_at"] is not None


def test_forgetting_off_does_not_reinforce(monkeypatch, plane_runtime):
    """Flag OFF: recall does NOT touch recall_count (byte-identical to today)."""
    monkeypatch.delenv("FF_MEMORY_FORGETTING", raising=False)
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    monkeypatch.delenv("FF_MEMORY_TEMPORAL", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("reinforce-off")

    m = mt.remember(user, "goal", "ship the annual report")
    mt.memory_search(user, "annual report")
    mt.memory_get(user)
    assert int(repo.get_memory(user, m["id"]).get("recall_count") or 0) == 0


# ───────────────────────── C-M8 evolving persona ─────────────────────────────

def test_persona_evolves_after_repeated_signals(monkeypatch, plane_runtime):
    """C-M8: repeated preference signals fold into the persona via keep-best —
    new uncovered signals grow it, already-covered ones don't regress it."""
    _on(monkeypatch, FF_MEMORY_PERSONA="true")
    mt, repo = _tools(plane_runtime)
    user = _user("persona")

    assert mt.get_persona(user) == ""  # none yet

    p1 = mt.evolve_persona(user, ["dark mode"])
    assert "dark mode" in p1.lower()
    row1 = repo.get_persona(user)
    assert row1 is not None and "dark mode" in row1["persona"].lower()

    # A new uncovered signal folds into and grows the persona (keep-best persists).
    p2 = mt.evolve_persona(user, ["dark mode", "terse replies"])
    assert "terse replies" in p2.lower() and "dark mode" in p2.lower()
    row2 = repo.get_persona(user)
    assert len(row2["persona"]) > len(row1["persona"])  # grew to cover the new signal
    assert row2["score"] == pytest.approx(
        lm.persona_score(row2["persona"], ["dark mode", "terse replies"]))

    # Re-feeding only already-covered signals must NOT regress the stored persona.
    before = repo.get_persona(user)["persona"]
    mt.evolve_persona(user, ["dark mode"])
    assert repo.get_persona(user)["persona"] == before


def test_persona_off_is_noop(monkeypatch, plane_runtime):
    """Flag OFF: persona seams are inert — nothing is read or written."""
    monkeypatch.delenv("FF_MEMORY_PERSONA", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("persona-off")

    assert mt.get_persona(user) == ""
    assert mt.evolve_persona(user, ["dark mode", "terse"]) == ""
    assert repo.get_persona(user) is None  # nothing persisted


# ───────────────────────── C-U9 project scoping ──────────────────────────────

def test_project_scope_filters_search(monkeypatch, plane_runtime):
    """C-U9: a project-tagged memory is private to that project; an untagged
    memory is global (visible from every project); the global view excludes
    project-private rows."""
    _on(monkeypatch, FF_PROJECT_MEMORY="true")
    monkeypatch.delenv("FF_MEMORY_TEMPORAL", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("project")

    a = mt.remember(user, "context", "uses alpha staging cluster", project_id="alpha")
    b = mt.remember(user, "context", "uses beta staging cluster", project_id="beta")
    g = mt.remember(user, "context", "uses shared logging stack")  # global
    assert a.get("project_id") == "alpha" and b.get("project_id") == "beta"
    assert "project_id" not in g  # untagged write stays global

    # From inside alpha: alpha's own row + global, never beta's.
    alpha_vals = {m["value"] for m in mt.memory_search(user, "staging cluster logging",
                                                       project_id="alpha")}
    assert "uses alpha staging cluster" in alpha_vals
    assert "uses shared logging stack" in alpha_vals
    assert "uses beta staging cluster" not in alpha_vals

    # The global view (no project) sees only the global row.
    global_vals = {m["value"] for m in mt.memory_get(user)}
    assert "uses shared logging stack" in global_vals
    assert "uses alpha staging cluster" not in global_vals
    assert "uses beta staging cluster" not in global_vals

    # memory_get scoped to beta: beta's own + global, not alpha's.
    beta_vals = {m["value"] for m in mt.memory_get(user, project_id="beta")}
    assert beta_vals == {"uses beta staging cluster", "uses shared logging stack"}


def test_project_scope_off_ignores_project_id(monkeypatch, plane_runtime):
    """Flag OFF: project_id is ignored on both write and read — every memory is
    the single global slice (today's behavior), so a 'project' write is visible
    everywhere and the column stays NULL."""
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    monkeypatch.delenv("FF_MEMORY_TEMPORAL", raising=False)
    mt, repo = _tools(plane_runtime)
    user = _user("project-off")

    m = mt.remember(user, "context", "uses gamma staging cluster", project_id="gamma")
    # The scope collapsed to global → stored project_id is NULL, no echo key.
    assert "project_id" not in m
    assert repo.get_memory(user, m["id"]).get("project_id") is None

    # A scoped read still returns it because scoping is inert when the flag is off.
    vals = {x["value"] for x in mt.memory_search(user, "gamma staging cluster",
                                                 project_id="anything")}
    assert "uses gamma staging cluster" in vals
