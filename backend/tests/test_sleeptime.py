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


# --- sleeptime_enabled flag --------------------------------------------------

def test_flag_default_off(monkeypatch):
    """Absent env var -> disabled (fail-closed)."""
    monkeypatch.delenv("FF_SLEEPTIME_COMPUTE", raising=False)
    assert sleeptime.sleeptime_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", "  on  ", "On"])
def test_flag_on_for_truthy_values(monkeypatch, val):
    """Recognized truthy values (case/whitespace-insensitive) -> enabled."""
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", val)
    assert sleeptime.sleeptime_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "maybe", "2"])
def test_flag_off_for_falsey_values(monkeypatch, val):
    """Anything not in the truthy set -> disabled."""
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", val)
    assert sleeptime.sleeptime_enabled() is False


# --- anticipate_questions: messages ------------------------------------------

def test_anticipate_from_message_topics():
    """Capitalized topic tokens become 'know more about' questions."""
    out = sleeptime.anticipate_questions(["Tell me about Kubernetes scaling"], [])
    assert out, "expected at least one anticipated question"
    assert all(isinstance(a, sleeptime.Anticipated) for a in out)
    questions = [a.question for a in out]
    assert any("Kubernetes" in q for q in questions)


def test_anticipate_skips_stopwords():
    """Leading capitalized stopwords (The, This, ...) are not treated as topics."""
    out = sleeptime.anticipate_questions(["The thing is broken"], [])
    # "The" is a stopword; lowercase "thing"/"broken" are not capitalized tokens.
    assert all("The" not in a.question.replace("them", "") for a in out)
    # Specifically: no topic should equal the stopword "The".
    assert not any(a.question == "Do you want to know more about The?" for a in out)


def test_anticipate_quoted_phrase_outranks_bare_token():
    """A quoted phrase scores above a bare capitalized token in the same message."""
    out = sleeptime.anticipate_questions(['Compare "service mesh" and Istio'], [])
    questions = [a.question for a in out]
    assert any("service mesh" in q for q in questions)
    quoted = next(a for a in out if "service mesh" in a.question)
    bare = next(a for a in out if "Istio" in a.question)
    assert quoted.priority > bare.priority


def test_anticipate_recency_weighting():
    """A topic in the most-recent message outranks one in an older message."""
    out = sleeptime.anticipate_questions(["Discuss Postgres", "Discuss Redis"], [])
    by_topic = {}
    for a in out:
        if "Postgres" in a.question:
            by_topic["Postgres"] = a.priority
        if "Redis" in a.question:
            by_topic["Redis"] = a.priority
    assert "Postgres" in by_topic and "Redis" in by_topic
    # "Redis" is in the later (more recent) message -> higher priority.
    assert by_topic["Redis"] > by_topic["Postgres"]


# --- anticipate_questions: memories ------------------------------------------

def test_anticipate_from_goal_memory():
    """A 'goal' memory yields a next-step question containing the goal value."""
    mems = [{"category": "goal", "value": "ship the v2 release"}]
    out = sleeptime.anticipate_questions([], mems)
    assert any(
        a.question == "What's the next step toward ship the v2 release?" for a in out
    )


def test_anticipate_from_workflow_tag_memory():
    """A 'workflow_tag' memory yields a re-run question containing the tag."""
    mems = [{"category": "workflow_tag", "value": "weekly-report"}]
    out = sleeptime.anticipate_questions([], mems)
    assert any(
        a.question == "Want me to run the weekly-report workflow again?" for a in out
    )


def test_anticipate_memory_salience_weighting():
    """Higher-salience memories produce higher-priority questions."""
    mems = [
        {"category": "goal", "value": "low goal", "salience": 1.0},
        {"category": "goal", "value": "high goal", "salience": 4.0},
    ]
    out = sleeptime.anticipate_questions([], mems)
    low = next(a for a in out if "low goal" in a.question)
    high = next(a for a in out if "high goal" in a.question)
    assert high.priority > low.priority


def test_anticipate_ignores_unknown_memory_categories():
    """Categories other than goal/workflow are ignored (no question emitted)."""
    mems = [{"category": "preference", "value": "dark mode"}]
    out = sleeptime.anticipate_questions([], mems)
    assert out == []


def test_anticipate_combines_messages_and_memories():
    """Both sources contribute candidates in a single call."""
    msgs = ["Looking into Terraform modules"]
    mems = [{"category": "goal", "value": "automate infra"}]
    out = sleeptime.anticipate_questions(msgs, mems, k=10)
    questions = [a.question for a in out]
    assert any("Terraform" in q for q in questions)
    assert any("automate infra" in q for q in questions)


# --- anticipate_questions: ordering, dedup, cap, empties ---------------------

def test_anticipate_sorted_by_priority_desc():
    """Output is sorted by priority descending."""
    msgs = ["Discuss Alpha", "Discuss Beta", "Discuss Gamma"]
    out = sleeptime.anticipate_questions(msgs, [], k=10)
    priorities = [a.priority for a in out]
    assert priorities == sorted(priorities, reverse=True)


def test_anticipate_dedups_normalized_questions():
    """The same topic across messages yields exactly one question."""
    msgs = ["Tell me about Vault", "More on Vault please"]
    out = sleeptime.anticipate_questions(msgs, [], k=10)
    vault = [a for a in out if "Vault" in a.question]
    assert len(vault) == 1


def test_anticipate_respects_k_cap():
    """No more than k results are returned even when many topics are available."""
    # Comma-separated so each name is its own capitalized token (7 distinct topics).
    msgs = ["Topics include Alpha, Beta, Gamma, Delta, Epsilon, Zeta, Eta"]
    out = sleeptime.anticipate_questions(msgs, [], k=3)
    assert len(out) == 3


def test_anticipate_k_zero_returns_empty():
    """k <= 0 short-circuits to an empty list."""
    msgs = ["Discuss Something"]
    assert sleeptime.anticipate_questions(msgs, [], k=0) == []


def test_anticipate_empty_inputs_return_empty():
    """No messages and no memories -> empty list."""
    assert sleeptime.anticipate_questions([], []) == []


def test_anticipate_blank_and_malformed_inputs():
    """Blank messages and non-dict / empty memories are skipped gracefully."""
    msgs = ["", "   "]
    mems = ["not a dict", {}, {"category": "goal", "value": ""}]
    assert sleeptime.anticipate_questions(msgs, mems) == []


# --- determinism -------------------------------------------------------------

def test_anticipate_is_deterministic():
    """Identical inputs produce identical output across repeated calls."""
    msgs = ["Compare Spark and Flink", 'Look at "stream processing"']
    mems = [
        {"category": "goal", "value": "cut latency", "salience": 2.0},
        {"category": "workflow_tag", "value": "nightly-etl", "salience": 1.0},
    ]
    first = sleeptime.anticipate_questions(msgs, mems, k=8)
    second = sleeptime.anticipate_questions(msgs, mems, k=8)
    assert first == second
    # frozen dataclass equality means field-by-field equality holds.
    assert [(a.question, a.priority) for a in first] == [
        (a.question, a.priority) for a in second
    ]


# --- precompute_plan ---------------------------------------------------------

def _anti(q, p):
    return sleeptime.Anticipated(question=q, rationale="r", priority=p)


def test_precompute_plan_takes_top_budget():
    """Only the highest-priority `budget` items survive."""
    items = [_anti("a", 0.2), _anti("b", 0.9), _anti("c", 0.5), _anti("d", 0.7)]
    plan = sleeptime.precompute_plan(items, budget=2)
    assert [a.question for a in plan] == ["b", "d"]


def test_precompute_plan_sorts_unordered_input():
    """precompute_plan re-sorts defensively even if input is unordered."""
    items = [_anti("low", 0.1), _anti("high", 0.99)]
    plan = sleeptime.precompute_plan(items, budget=1)
    assert plan[0].question == "high"


def test_precompute_plan_budget_zero_or_empty():
    """budget <= 0 or empty input -> empty plan."""
    items = [_anti("a", 0.5)]
    assert sleeptime.precompute_plan(items, budget=0) == []
    assert sleeptime.precompute_plan([], budget=3) == []


def test_precompute_plan_budget_exceeds_available():
    """A budget larger than the candidate set returns everything."""
    items = [_anti("a", 0.5), _anti("b", 0.6)]
    plan = sleeptime.precompute_plan(items, budget=10)
    assert len(plan) == 2


def test_precompute_plan_default_budget_is_three():
    """The default budget caps precompute at 3 items."""
    items = [_anti(str(i), float(i)) for i in range(6)]
    plan = sleeptime.precompute_plan(items)
    assert len(plan) == 3


# --- is_idle -----------------------------------------------------------------

def test_is_idle_just_under_threshold_false():
    """One ms short of the threshold is not yet idle."""
    assert sleeptime.is_idle(0, 299_999, idle_after_ms=300_000) is False


def test_is_idle_at_threshold_true():
    """Exactly the threshold counts as idle (inclusive boundary)."""
    assert sleeptime.is_idle(0, 300_000, idle_after_ms=300_000) is True


def test_is_idle_over_threshold_true():
    """Well past the threshold is idle."""
    assert sleeptime.is_idle(1_000, 1_000_000, idle_after_ms=300_000) is True


def test_is_idle_default_threshold_five_minutes():
    """Default idle_after_ms is 5 minutes (300_000 ms)."""
    assert sleeptime.is_idle(0, 299_999) is False
    assert sleeptime.is_idle(0, 300_000) is True


def test_is_idle_active_user_false():
    """Recent activity (now == last_activity) is not idle."""
    assert sleeptime.is_idle(500_000, 500_000) is False


# --- end-to-end pipeline -----------------------------------------------------

def test_pipeline_anticipate_then_plan():
    """anticipate_questions feeds precompute_plan end-to-end."""
    msgs = ["Discuss Kafka", "Discuss Airflow", "Discuss Spark"]
    mems = [{"category": "goal", "value": "build a data platform", "salience": 3.0}]
    anticipated = sleeptime.anticipate_questions(msgs, mems, k=5)
    plan = sleeptime.precompute_plan(anticipated, budget=2)
    assert 0 < len(plan) <= 2
    # The plan is a subset of the anticipated set, top-priority first.
    assert all(p in anticipated for p in plan)
    assert plan == sleeptime.precompute_plan(anticipated, budget=2)  # deterministic


# --- REAL integration: run_sweep wires sleeptime through the live repo -------
#
# These drive dreaming.consolidation.run_sweep against the REAL
# PersonalizationRepository + Postgres (the same path scheduler/runner.py uses
# per-user), proving the precompute is produced and PERSISTED into the existing
# user_personalization.personality jsonb (no new table) — and that the flag OFF
# leaves it untouched.

class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def _clean_gate():
    from personalization.phi_gate import PHIGate
    return PHIGate(analyzer=_CleanAnalyzer())


@pytest.fixture
def repo_user(plane_runtime):
    """Real Plane-backed repository plus a UUID-unique fixture owner."""
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
    """Flag ON + idle: run_sweep over the real repo anticipates next questions
    and stores the plan in user_personalization.personality (read back from DB)."""
    from dreaming.consolidation import run_sweep
    monkeypatch.setenv("FF_SLEEPTIME_COMPUTE", "on")
    repo, user, _db = repo_user

    # Seed: a durable goal memory (survives) + a one-off signal carrying a topic.
    repo.upsert_profile(user, profession="researcher", personality={"tone": "warm"})
    repo.create_memory(user, "goal", "ship the v2 release", source="explicit", salience=3.0)
    repo.add_signal(user, "context", "Looking into Kubernetes scaling")

    sweep = run_sweep(repo, _clean_gate(), user, now_ms=NOW,
                      last_activity_ms=NOW - 10 * 60_000)

    # A real plan came back...
    assert sweep["precompute"], "expected anticipated questions persisted"
    qs = [q["question"] for q in sweep["precompute"]]
    assert any("v2 release" in q for q in qs) or any("Kubernetes" in q for q in qs)

    # ...and it is readable from the DB-backed profile, alongside the preserved
    # user-facing trait (NO new table was used).
    profile = repo.get_profile(user)
    personality = profile["personality"]
    assert personality.get("tone") == "warm"
    plan = personality.get("_sleeptime_precompute")
    assert plan is not None and plan["trigger"] == "idle"
    assert [dict(question) for question in plan["questions"]] == sweep["precompute"]


def test_run_sweep_no_precompute_when_disabled(monkeypatch, repo_user):
    """Flag OFF (default): the same inputs leave the personality jsonb unchanged."""
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
