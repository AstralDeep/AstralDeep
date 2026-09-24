"""Tests for personalization/living_memory.py: temporal validity,
contradiction/abstention, Ebbinghaus retention and reinforcement, keep-best persona
evolution, feedback, and provenance/unlearn classification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from personalization import living_memory as lm  # noqa: E402

DAY = 24 * 3600 * 1000


@pytest.mark.parametrize("fn,env", [
    (lm.temporal_enabled, "FF_MEMORY_TEMPORAL"),
    (lm.forgetting_enabled, "FF_MEMORY_FORGETTING"),
    (lm.persona_enabled, "FF_MEMORY_PERSONA"),
])
def test_flags_default_off(monkeypatch, fn, env):
    monkeypatch.delenv(env, raising=False)
    assert fn() is False
    monkeypatch.setenv(env, "true")
    assert fn() is True


def test_is_valid_at_bounds():
    m = {"valid_from": 100, "valid_to": 200}
    assert lm.is_valid_at(m, 150) is True
    assert lm.is_valid_at(m, 200) is False
    assert lm.is_valid_at(m, 50) is False


def test_open_bounds_and_no_columns_always_valid():
    assert lm.is_valid_at({"valid_from": None, "valid_to": None}, 999) is True
    assert lm.is_valid_at({}, 12345) is True


def test_as_of_filters():
    mems = [{"id": "a", "valid_from": 0, "valid_to": 100},
            {"id": "b", "valid_from": 100, "valid_to": None}]
    assert [m["id"] for m in lm.as_of(mems, 50)] == ["a"]
    assert [m["id"] for m in lm.as_of(mems, 150)] == ["b"]


def test_detect_contradiction():
    mems = [{"category": "city", "value": "Paris"},
            {"category": "city", "value": "Lyon"},
            {"category": "diet", "value": "vegan"}]
    cats = dict(lm.detect_contradiction(mems))
    assert "city" in cats and "diet" not in cats


def test_should_abstain_on_conflict_or_low_salience():
    conflict = [{"category": "city", "value": "Paris", "salience": 0.9},
                {"category": "city", "value": "Lyon", "salience": 0.9}]
    assert lm.should_abstain(conflict) is True
    weak = [{"category": "city", "value": "Paris", "salience": 0.1}]
    assert lm.should_abstain(weak, min_salience=0.5) is True
    assert lm.should_abstain(weak, min_salience=0.0) is False
    assert lm.should_abstain([]) is False


def test_retention_decays_with_age():
    fresh = {"created_at": 0, "recall_count": 0}
    assert lm.retention_strength(fresh, 0) == pytest.approx(1.0)
    older = lm.retention_strength(fresh, 30 * DAY)
    assert 0.0 < older < lm.retention_strength(fresh, 3 * DAY)


def test_reinforcement_slows_decay():
    base = {"created_at": 0, "recall_count": 0}
    reinforced = {"created_at": 0, "recall_count": 10, "last_recalled_at": 0}
    t = 14 * DAY
    assert lm.retention_strength(reinforced, t) > lm.retention_strength(base, t)


def test_reinforce_deltas():
    d = lm.reinforce({"recall_count": 3}, now=999)
    assert d == {"recall_count": 4, "last_recalled_at": 999}


def test_should_forget_floor_and_exemptions():
    decayed = {"created_at": 0, "recall_count": 0, "source": "promoted"}
    assert lm.should_forget(decayed, 365 * DAY) is True
    assert lm.should_forget({**decayed, "source": "explicit"}, 365 * DAY) is False
    assert lm.should_forget({**decayed, "pinned": True}, 365 * DAY) is False


def test_safety_forget_fails_closed():
    assert lm.safety_forget({"value": "SSN 1"}, phi_check=lambda t: True) is True
    assert lm.safety_forget({"value": "ok"}, phi_check=lambda t: False) is False
    assert lm.safety_forget({"value": "x"}, phi_check=None) is False

    def boom(_):
        raise RuntimeError()
    assert lm.safety_forget({"value": "x"}, phi_check=boom) is True


def test_persona_score_rewards_coverage_penalizes_length():
    assert lm.persona_score("likes dark mode and short replies",
                            ["dark mode", "short replies"]) == pytest.approx(1.0, abs=0.05)
    assert lm.persona_score("", ["x"]) == 0.0


def test_evolve_persona_never_regresses():
    cur = "likes dark mode"
    out = lm.evolve_persona(cur, ["dark mode", "terse"])
    assert "terse" in out.text.lower()
    assert out.score >= lm.persona_score(cur, ["dark mode", "terse"])
    keep = lm.evolve_persona("likes dark mode and terse", ["dark mode", "terse"],
                             proposal="")
    assert keep.text == "likes dark mode and terse"


def test_apply_feedback():
    assert "Likes: charts." in lm.apply_feedback("", "charts", "up")
    assert "Avoid: charts." in lm.apply_feedback("", "charts", "down")


def test_provenance_of():
    p = lm.provenance_of({"source": "promoted", "category": "goal",
                          "created_at": 5, "signature": "abc"})
    assert p["source"] == "promoted" and p["signed"] is True
    assert p["ingested_at"] == 5


@pytest.mark.parametrize("req,kind", [
    ("forget my address", "hard"),
    ("delete everything about my job", "hard"),
    ("right to be forgotten", "hard"),
    ("actually my city is Lyon now", "supersede"),
    ("no, it should be vegan", "supersede"),
    ("change it to blue", "supersede"),
])
def test_unlearn_kind(req, kind):
    assert lm.unlearn_kind(req) == kind
