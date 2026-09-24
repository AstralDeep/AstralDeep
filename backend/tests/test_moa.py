"""Tests for orchestrator/moa.py: the mixture-of-agents should_invoke gate
(difficulty/confidence thresholds), proposal aggregation and majority-answer
tie-breaking, the debate/ranking judges, and turn-difficulty scoring that gates
escalation.
"""

from __future__ import annotations
import sys
from pathlib import Path
import pytest
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from orchestrator import moa  # noqa: E402
from orchestrator.moa import Proposal  # noqa: E402


def test_moa_enabled_default_off(monkeypatch):
    monkeypatch.delenv("FF_MOA_DEBATE", raising=False)
    assert moa.moa_enabled() is False


def test_moa_enabled_truthy_values(monkeypatch):
    for value in ("1", "true", "TRUE", "Yes", " on ", "On"):
        monkeypatch.setenv("FF_MOA_DEBATE", value)
        assert moa.moa_enabled() is True, value


def test_moa_enabled_falsy_values(monkeypatch):
    for value in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("FF_MOA_DEBATE", value)
        assert moa.moa_enabled() is False, value


def test_should_invoke_high_difficulty_triggers():
    assert moa.should_invoke(difficulty=0.9, confidence=0.95) is True


def test_should_invoke_low_confidence_triggers():
    assert moa.should_invoke(difficulty=0.1, confidence=0.2) is True


def test_should_invoke_easy_and_confident_does_not():
    assert moa.should_invoke(difficulty=0.1, confidence=0.95) is False


def test_should_invoke_difficulty_boundary():
    assert moa.should_invoke(difficulty=0.6, confidence=0.95) is True
    assert moa.should_invoke(difficulty=0.5999, confidence=0.95) is False


def test_should_invoke_confidence_boundary():
    assert moa.should_invoke(difficulty=0.1, confidence=0.5) is True
    assert moa.should_invoke(difficulty=0.1, confidence=0.5001) is False


def test_should_invoke_custom_thresholds():
    assert (
        moa.should_invoke(
            difficulty=0.7,
            confidence=0.6,
            difficulty_threshold=0.8,
            confidence_threshold=0.4,
        )
        is False
    )
    assert (
        moa.should_invoke(
            difficulty=0.85,
            confidence=0.6,
            difficulty_threshold=0.8,
            confidence_threshold=0.4,
        )
        is True
    )


def test_aggregate_picks_top_score():
    proposals = [
        Proposal(agent="a", text="low", score=0.1),
        Proposal(agent="b", text="high", score=0.9),
        Proposal(agent="c", text="mid", score=0.5),
    ]
    winner = moa.aggregate(proposals)
    assert winner.agent == "b"
    assert winner.text == "high"


def test_aggregate_tie_break_is_earliest():
    proposals = [
        Proposal(agent="first", text="x", score=0.8),
        Proposal(agent="second", text="y", score=0.8),
    ]
    assert moa.aggregate(proposals).agent == "first"


def test_aggregate_empty_raises():
    with pytest.raises(ValueError):
        moa.aggregate([])


def test_majority_answer_counts_normalized_text():
    proposals = [
        Proposal(agent="a", text="Yes"),
        Proposal(agent="b", text="  yes "),
        Proposal(agent="c", text="no"),
    ]
    assert moa.majority_answer(proposals) == "yes"


def test_majority_answer_tie_break_earliest():
    proposals = [
        Proposal(agent="a", text="alpha"),
        Proposal(agent="b", text="beta"),
    ]
    assert moa.majority_answer(proposals) == "alpha"


def test_majority_answer_custom_key():
    proposals = [
        Proposal(agent="a", text="ANSWER-1"),
        Proposal(agent="b", text="answer-2"),
        Proposal(agent="c", text="answer-3"),
    ]
    result = moa.majority_answer(proposals, key=lambda t: t.split("-")[0].lower())
    assert result == "answer"


def test_majority_answer_empty_is_none():
    assert moa.majority_answer([]) is None


def _const_judge(value: int):
    def judge(a: Proposal, b: Proposal) -> int:
        return value

    return judge


def test_debate_judge_a_wins():
    a = Proposal(agent="a", text="a", score=0.0)
    b = Proposal(agent="b", text="b", score=1.0)
    assert moa.debate_judge(a, b, _const_judge(-1)) is a


def test_debate_judge_b_wins():
    a = Proposal(agent="a", text="a", score=1.0)
    b = Proposal(agent="b", text="b", score=0.0)
    assert moa.debate_judge(a, b, _const_judge(1)) is b


def test_debate_judge_tie_picks_a():
    a = Proposal(agent="a", text="a", score=0.0)
    b = Proposal(agent="b", text="b", score=0.0)
    assert moa.debate_judge(a, b, _const_judge(0)) is a


def test_debate_judge_raises_falls_back_to_higher_score():
    a = Proposal(agent="a", text="a", score=0.2)
    b = Proposal(agent="b", text="b", score=0.9)

    def boom(a, b):
        raise RuntimeError("judge exploded")

    assert moa.debate_judge(a, b, boom) is b


def test_debate_judge_raises_fallback_tie_prefers_a():
    a = Proposal(agent="a", text="a", score=0.5)
    b = Proposal(agent="b", text="b", score=0.5)

    def boom(a, b):
        raise ValueError("nope")

    assert moa.debate_judge(a, b, boom) is a


def test_panel_without_judge_uses_aggregate():
    proposals = [
        Proposal(agent="a", text="low", score=0.1),
        Proposal(agent="b", text="high", score=0.9),
    ]
    assert moa.panel(proposals).agent == "b"


def test_panel_with_judge_runs_single_elimination():
    proposals = [
        Proposal(agent="a", text="a", score=0.0),
        Proposal(agent="b", text="b", score=0.0),
        Proposal(agent="c", text="c", score=0.0),
    ]

    assert moa.panel(proposals, judge=_const_judge(1)).agent == "c"

    assert moa.panel(proposals, judge=_const_judge(-1)).agent == "a"


def test_panel_with_judge_single_proposal():
    only = Proposal(agent="solo", text="x", score=0.3)
    assert moa.panel([only], judge=_const_judge(1)) is only


def test_panel_empty_raises():
    with pytest.raises(ValueError):
        moa.panel([])


HARD = ("Compare PostgreSQL and MySQL for a write-heavy analytics workload: "
        "analyze the trade-offs, then explain why you would recommend one "
        "over the other?")


def test_difficulty_threshold_default_and_env(monkeypatch):
    monkeypatch.delenv("MOA_DIFFICULTY_THRESHOLD", raising=False)
    assert moa.difficulty_threshold() == moa.DEFAULT_DIFFICULTY_THRESHOLD
    monkeypatch.setenv("MOA_DIFFICULTY_THRESHOLD", "0.3")
    assert moa.difficulty_threshold() == 0.3
    monkeypatch.setenv("MOA_DIFFICULTY_THRESHOLD", "garbage")
    assert moa.difficulty_threshold() == moa.DEFAULT_DIFFICULTY_THRESHOLD
    monkeypatch.setenv("MOA_DIFFICULTY_THRESHOLD", "7")
    assert moa.difficulty_threshold() == 1.0
    monkeypatch.setenv("MOA_DIFFICULTY_THRESHOLD", "nan")
    assert moa.difficulty_threshold() == moa.DEFAULT_DIFFICULTY_THRESHOLD


def test_request_difficulty_simple_asks_are_easy():
    assert moa.request_difficulty("") == 0.0
    assert moa.request_difficulty("hi") == 0.0
    assert moa.request_difficulty("What is the capital of France?") < 0.3
    assert moa.request_difficulty("thanks!") < 0.3


def test_request_difficulty_reasoning_asks_are_hard():
    assert moa.request_difficulty(HARD) >= moa.DEFAULT_DIFFICULTY_THRESHOLD
    assert moa.request_difficulty("why?") < moa.request_difficulty(
        "Why does this happen, and how should we fix it? Also, what are the "
        "implications?")


def test_request_difficulty_is_deterministic_and_bounded():
    assert moa.request_difficulty(HARD) == moa.request_difficulty(HARD)
    huge = ("compare contrast analyze evaluate why explain trade-off " * 30) + "also ???"
    assert moa.request_difficulty(huge) == pytest.approx(0.9)
    assert moa.turn_difficulty(huge, "I'm not sure. " + "x" * 1600) == 1.0


def test_draft_uncertainty_hedges_and_length():
    assert moa.draft_uncertainty("") == 0.0
    assert moa.draft_uncertainty("Paris.") == 0.0
    assert moa.draft_uncertainty("I'm not sure, but probably Paris.") == 0.25
    assert moa.draft_uncertainty("x" * 1600) == 0.1
    assert moa.draft_uncertainty("It is unclear. " + "x" * 1600) == 0.35


def test_turn_difficulty_hedge_alone_never_opens_gate():
    score = moa.turn_difficulty("What is 2+2?", "I'm not sure.")
    assert score < moa.DEFAULT_DIFFICULTY_THRESHOLD
    assert not moa.should_invoke(difficulty=score, confidence=1.0,
                                 difficulty_threshold=moa.difficulty_threshold())


def test_turn_difficulty_hard_ask_plus_hedge_opens_gate(monkeypatch):
    monkeypatch.delenv("MOA_DIFFICULTY_THRESHOLD", raising=False)
    score = moa.turn_difficulty(HARD, "I'm not sure, it depends.")
    assert score >= moa.difficulty_threshold()
    assert score <= 1.0


def test_ranking_judge_picks_ranked_winner_not_longest():
    draft = Proposal(agent="draft", text="d", score=1.0)
    best = Proposal(agent="cand0", text="short but right", score=0.0)
    longest = Proposal(agent="cand1", text="x" * 5000, score=0.0)
    judge = moa.ranking_judge({"draft": 1, "cand0": 0, "cand1": 1})
    assert moa.panel([draft, best, longest], judge=judge) is best
    assert judge(best, draft) == -1
    assert judge(draft, best) == 1
    assert judge(draft, longest) == 0


def test_ranking_judge_missing_agent_falls_back_to_score():
    draft = Proposal(agent="draft", text="d", score=1.0)
    other = Proposal(agent="cand0", text="o", score=0.0)
    assert moa.panel([draft, other], judge=moa.ranking_judge({"draft": 0})) is draft
    assert moa.panel([other, draft], judge=moa.ranking_judge({})) is draft
