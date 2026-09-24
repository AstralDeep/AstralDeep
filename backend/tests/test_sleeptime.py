"""Tests for the sleeptime idle-anticipation pipeline (dreaming/sleeptime.py): the
enable flag, question anticipation from messages and memories, precompute budgeting,
idle detection, and persistence via the personalization repository.
"""

from __future__ import annotations
import sys
import uuid
from pathlib import Path
import pytest
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from dreaming import sleeptime  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("FF_SLEEPTIME_COMPUTE", raising=False)
    assert sleeptime.sleeptime_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", "  on  ", "On"])
def test_flag_on_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", val)
    assert sleeptime.sleeptime_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "maybe", "2"])
def test_flag_off_for_falsey_values(monkeypatch, val):
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", val)
    assert sleeptime.sleeptime_enabled() is False


def test_anticipate_from_message_topics():
    out = sleeptime.anticipate_questions(["Tell me about Kubernetes scaling"], [])
    assert out, "expected at least one anticipated question"
    assert all(isinstance(a, sleeptime.Anticipated) for a in out)
    questions = [a.question for a in out]
    assert any("Kubernetes" in q for q in questions)


def test_anticipate_skips_stopwords():
    out = sleeptime.anticipate_questions(["The thing is broken"], [])
    assert all("The" not in a.question.replace("them", "") for a in out)
    assert not any(a.question == "Do you want to know more about The?" for a in out)


def test_anticipate_quoted_phrase_outranks_bare_token():
    out = sleeptime.anticipate_questions(['Compare "service mesh" and Istio'], [])
    questions = [a.question for a in out]
    assert any("service mesh" in q for q in questions)
    quoted = next(a for a in out if "service mesh" in a.question)
    bare = next(a for a in out if "Istio" in a.question)
    assert quoted.priority > bare.priority


def test_anticipate_recency_weighting():
    out = sleeptime.anticipate_questions(["Discuss Postgres", "Discuss Redis"], [])
    by_topic = {}
    for a in out:
        if "Postgres" in a.question:
            by_topic["Postgres"] = a.priority
        if "Redis" in a.question:
            by_topic["Redis"] = a.priority
    assert "Postgres" in by_topic and "Redis" in by_topic
    assert by_topic["Redis"] > by_topic["Postgres"]


def test_anticipate_from_goal_memory():
    mems = [{"category": "goal", "value": "ship the v2 release"}]
    out = sleeptime.anticipate_questions([], mems)
    assert any(
        a.question == "What's the next step toward ship the v2 release?" for a in out
    )


def test_anticipate_from_workflow_tag_memory():
    mems = [{"category": "workflow_tag", "value": "weekly-report"}]
    out = sleeptime.anticipate_questions([], mems)
    assert any(
        a.question == "Want me to run the weekly-report workflow again?" for a in out
    )


def test_anticipate_memory_salience_weighting():
    mems = [
        {"category": "goal", "value": "low goal", "salience": 1.0},
        {"category": "goal", "value": "high goal", "salience": 4.0},
    ]
    out = sleeptime.anticipate_questions([], mems)
    low = next(a for a in out if "low goal" in a.question)
    high = next(a for a in out if "high goal" in a.question)
    assert high.priority > low.priority


def test_anticipate_ignores_unknown_memory_categories():
    mems = [{"category": "preference", "value": "dark mode"}]
    out = sleeptime.anticipate_questions([], mems)
    assert out == []


def test_anticipate_combines_messages_and_memories():
    msgs = ["Looking into Terraform modules"]
    mems = [{"category": "goal", "value": "automate infra"}]
    out = sleeptime.anticipate_questions(msgs, mems, k=10)
    questions = [a.question for a in out]
    assert any("Terraform" in q for q in questions)
    assert any("automate infra" in q for q in questions)


def test_anticipate_sorted_by_priority_desc():
    msgs = ["Discuss Alpha", "Discuss Beta", "Discuss Gamma"]
    out = sleeptime.anticipate_questions(msgs, [], k=10)
    priorities = [a.priority for a in out]
    assert priorities == sorted(priorities, reverse=True)


def test_anticipate_dedups_normalized_questions():
    msgs = ["Tell me about Vault", "More on Vault please"]
    out = sleeptime.anticipate_questions(msgs, [], k=10)
    vault = [a for a in out if "Vault" in a.question]
    assert len(vault) == 1


def test_anticipate_respects_k_cap():
    msgs = ["Topics include Alpha, Beta, Gamma, Delta, Epsilon, Zeta, Eta"]
    out = sleeptime.anticipate_questions(msgs, [], k=3)
    assert len(out) == 3


def test_anticipate_k_zero_returns_empty():
    msgs = ["Discuss Something"]
    assert sleeptime.anticipate_questions(msgs, [], k=0) == []


def test_anticipate_empty_inputs_return_empty():
    assert sleeptime.anticipate_questions([], []) == []


def test_anticipate_blank_and_malformed_inputs():
    msgs = ["", "   "]
    mems = ["not a dict", {}, {"category": "goal", "value": ""}]
    assert sleeptime.anticipate_questions(msgs, mems) == []


def test_anticipate_is_deterministic():
    msgs = ["Compare Spark and Flink", 'Look at "stream processing"']
    mems = [
        {"category": "goal", "value": "cut latency", "salience": 2.0},
        {"category": "workflow_tag", "value": "nightly-etl", "salience": 1.0},
    ]
    first = sleeptime.anticipate_questions(msgs, mems, k=8)
    second = sleeptime.anticipate_questions(msgs, mems, k=8)
    assert first == second
    assert [(a.question, a.priority) for a in first] == [
        (a.question, a.priority) for a in second
    ]


def _anti(q, p):
    return sleeptime.Anticipated(question=q, rationale="r", priority=p)


def test_precompute_plan_takes_top_budget():
    items = [_anti("a", 0.2), _anti("b", 0.9), _anti("c", 0.5), _anti("d", 0.7)]
    plan = sleeptime.precompute_plan(items, budget=2)
    assert [a.question for a in plan] == ["b", "d"]


def test_precompute_plan_sorts_unordered_input():
    items = [_anti("low", 0.1), _anti("high", 0.99)]
    plan = sleeptime.precompute_plan(items, budget=1)
    assert plan[0].question == "high"


def test_precompute_plan_budget_zero_or_empty():
    items = [_anti("a", 0.5)]
    assert sleeptime.precompute_plan(items, budget=0) == []
    assert sleeptime.precompute_plan([], budget=3) == []


def test_precompute_plan_budget_exceeds_available():
    items = [_anti("a", 0.5), _anti("b", 0.6)]
    plan = sleeptime.precompute_plan(items, budget=10)
    assert len(plan) == 2


def test_precompute_plan_default_budget_is_three():
    items = [_anti(str(i), float(i)) for i in range(6)]
    plan = sleeptime.precompute_plan(items)
    assert len(plan) == 3


def test_is_idle_just_under_threshold_false():
    assert sleeptime.is_idle(0, 299_999, idle_after_ms=300_000) is False


def test_is_idle_at_threshold_true():
    assert sleeptime.is_idle(0, 300_000, idle_after_ms=300_000) is True


def test_is_idle_over_threshold_true():
    assert sleeptime.is_idle(1_000, 1_000_000, idle_after_ms=300_000) is True


def test_is_idle_default_threshold_five_minutes():
    assert sleeptime.is_idle(0, 299_999) is False
    assert sleeptime.is_idle(0, 300_000) is True


def test_is_idle_active_user_false():
    assert sleeptime.is_idle(500_000, 500_000) is False


def test_pipeline_anticipate_then_plan():
    msgs = ["Discuss Kafka", "Discuss Airflow", "Discuss Spark"]
    mems = [{"category": "goal", "value": "build a data platform", "salience": 3.0}]
    anticipated = sleeptime.anticipate_questions(msgs, mems, k=5)
    plan = sleeptime.precompute_plan(anticipated, budget=2)
    assert 0 < len(plan) <= 2
    assert all(p in anticipated for p in plan)
    assert plan == sleeptime.precompute_plan(anticipated, budget=2)


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def _clean_gate():
    from personalization.phi_gate import PHIGate
    return PHIGate(analyzer=_CleanAnalyzer())


@pytest.fixture
def repo_user(plane_runtime):
    from personalization.repository import PersonalizationRepository

    repo = PersonalizationRepository(
        None,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    user = f"pytest-sleeptime-{uuid.uuid4().hex[:8]}"
    return repo, user, plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("sleeptime") as runtime:
        yield runtime


NOW = 1_748_300_000_000


def test_run_sweep_persists_precompute_when_enabled(monkeypatch, repo_user):
    from dreaming.consolidation import run_sweep
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo, user, _db = repo_user

    repo.upsert_profile(user, profession="researcher", personality={"tone": "warm"})
    repo.create_memory(user, "goal", "ship the v2 release", source="explicit", salience=3.0)
    repo.add_signal(user, "context", "Looking into Kubernetes scaling")

    sweep = run_sweep(repo, _clean_gate(), user, now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000)

    assert sweep["precompute"], "expected anticipated questions persisted"
    qs = [q["question"] for q in sweep["precompute"]]
    assert any("v2 release" in q for q in qs) or any("Kubernetes" in q for q in qs)

    profile = repo.get_profile(user)
    personality = profile["personality"]
    assert personality.get("tone") == "warm"
    plan = personality.get("_sleeptime_precompute")
    assert plan is not None and plan["trigger"] == "idle"
    assert [dict(question) for question in plan["questions"]] == sweep["precompute"]


def test_run_sweep_no_precompute_when_disabled(monkeypatch, repo_user):
    from dreaming.consolidation import run_sweep
    monkeypatch.delenv("FF_SLEEPTIME_COMPUTE", raising=False)
    repo, user, _db = repo_user

    repo.upsert_profile(user, personality={"tone": "warm"})
    repo.create_memory(user, "goal", "ship the v2 release", source="explicit", salience=3.0)
    repo.add_signal(user, "context", "Looking into Kubernetes scaling")

    sweep = run_sweep(repo, _clean_gate(), user, now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000)

    assert sweep["precompute"] == []
    profile = repo.get_profile(user)
    assert "_sleeptime_precompute" not in (profile["personality"] or {})
    assert profile["personality"].get("tone") == "warm"
