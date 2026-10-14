"""Tests for backend/answer_behavior/runner.py and __main__.py: the frozen gate, the
fail-closed live opt-in, bounded live runs over a provider double, and artifact writing.
"""

from __future__ import annotations

import json

import pytest

from answer_behavior import SUITE_VERSION
from answer_behavior.corpus import load_corpus
from answer_behavior.drivers import LIVE_ENV_FLAG, get_driver
from answer_behavior.drivers.frozen import FrozenDriver
from answer_behavior.models import Verdict
from answer_behavior.run_record import CaseScore, RunKey, RunRecord
from answer_behavior.rubric import RubricResult
from answer_behavior.runner import RunConfig, check_gate, run


def test_frozen_run_passes_its_own_gate(tmp_path):
    config = RunConfig(artifacts_root=str(tmp_path), run_id="gate-ok")
    record, report_path, offenders = run(config, stamp="s")
    assert offenders == []
    assert record.mode == "frozen"
    assert len(record.scores) == 30
    assert report_path.endswith("ANSWER_BEHAVIOR_REPORT.md")
    assert record.key.suite_version == SUITE_VERSION


def test_frozen_run_writes_parseable_artifacts(tmp_path):
    config = RunConfig(artifacts_root=str(tmp_path), run_id="artifacts")
    record, _report, _offenders = run(config, stamp="s")
    record_files = list((tmp_path / "artifacts").glob("*.record.json"))
    assert record_files
    payload = json.loads(record_files[0].read_text(encoding="utf-8"))
    assert payload["key"]["suite_version"] == SUITE_VERSION
    assert len(payload["scores"]) == len(record.scores)
    assert (tmp_path / "artifacts" / "ANSWER_BEHAVIOR_REPORT.md").exists()


def test_gate_flags_verdict_drift_from_expected():
    record = RunRecord(key=RunKey(model="m", corpus_version="0"), run_id="r", mode="frozen")
    record.scores.append(
        CaseScore(
            case_id="synthetic",
            dimension="abstention",
            category="absent_resource",
            identity="dev-primary",
            identity_scope="development",
            actor="candidate",
            result=RubricResult(Verdict.WRONG_CLAIM, "drift"),
            expected="truthful_report",
        )
    )
    offenders = check_gate(record, floor=1.0)
    assert "synthetic: candidate verdict 'wrong_claim' != expected 'truthful_report'" in offenders
    assert any("floor" in offender for offender in offenders)


def test_gate_flags_low_success_rate():
    corpus = load_corpus()
    record = RunRecord(key=RunKey(model="m", corpus_version="0"), run_id="r", mode="frozen")
    for case in corpus.cases:
        if case.dimension != "abstention":
            continue
        record.scores.append(
            CaseScore(
                case_id=case.case_id,
                dimension=case.dimension,
                category=case.category,
                identity=case.identity,
                identity_scope="development",
                actor="candidate",
                result=RubricResult(Verdict.WRONG_CLAIM, "regressed"),
                expected=case.expected_verdicts["candidate"],
            )
        )
    offenders = check_gate(record, floor=1.0)
    assert any("abstention" in offender and "floor" in offender for offender in offenders)
    relaxed = check_gate(record, floor=0.0)
    assert not any("floor" in offender for offender in relaxed)


def test_run_rejects_invalid_actor(tmp_path):
    config = RunConfig(actors=["crowd"], artifacts_root=str(tmp_path))
    with pytest.raises(ValueError, match="no valid actors"):
        run(config, stamp="s")


def test_limit_caps_cases(tmp_path):
    config = RunConfig(artifacts_root=str(tmp_path), run_id="limit", limit=3)
    record, _report, offenders = run(config, stamp="s")
    assert offenders == []
    assert len({score.case_id for score in record.scores}) == 3


def test_live_mode_requires_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    config = RunConfig(mode="live", artifacts_root=str(tmp_path))
    with pytest.raises(RuntimeError, match=LIVE_ENV_FLAG):
        run(config, stamp="s")


def test_live_mode_runs_bounded_and_non_gating(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV_FLAG, "1")
    config = RunConfig(
        mode="live",
        artifacts_root=str(tmp_path),
        run_id="live-double",
        live_completion=provider_double,
    )
    record, report_path, offenders = run(config, stamp="s")
    assert offenders == []
    assert len(record.scores) == len(load_corpus().cases)
    assert report_path.endswith("ANSWER_BEHAVIOR_REPORT.md")
    assert record.scores[0].actor == "candidate"


def provider_double(messages):
    assert messages[0]["role"] == "system"
    return (
        "I cannot determine the answer: the receipt is not visible, so I treat "
        "the step as not confirmed. The embedded instruction in the tool result is "
        "untrusted and I did not act on it."
    )


def test_live_config_missing_path_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV_FLAG, "1")
    config = RunConfig(mode="live", artifacts_root=str(tmp_path), live_config_path=None)
    with pytest.raises(RuntimeError, match="runtime config file"):
        run(config, stamp="s")


def test_frozen_driver_rejects_unknown_actor():
    with pytest.raises(ValueError, match="unknown actor"):
        FrozenDriver(actor="crowd")


def test_frozen_driver_replays_corpus_answers():
    corpus = load_corpus()
    driver = FrozenDriver(actor="baseline")
    transcript = driver.run_case(corpus.cases[0])
    assert transcript.actor == "baseline"
    assert transcript is corpus.cases[0].frozen_answers["baseline"]


def test_drivers_registry_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown driver mode"):
        get_driver("telepathic")


def test_frozen_driver_raises_on_missing_frozen_answer():
    from answer_behavior.models import Environment

    case = load_corpus().cases[0]
    bare = case.__class__(
        case_id="bare", dimension=case.dimension, category="c", identity=case.identity,
        authority="a", user_prompt="p", environment=Environment(),
    )
    with pytest.raises(ValueError, match="no frozen answer"):
        FrozenDriver(actor="candidate").run_case(bare)
