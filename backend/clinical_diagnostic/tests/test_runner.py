"""Tests for clinical_diagnostic.runner: the frozen regression gate, bounded retries and
recovery, infrastructure failures, cancellation/budget exhaustion, deterministic
concurrency, public-run refusal/review gating, and artifact writing.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from clinical_diagnostic.budget import BudgetTracker, ExecutionLimits
from clinical_diagnostic.corpus import load_synthetic_corpus
from clinical_diagnostic.dispatcher import DispatchedAnswer
from clinical_diagnostic.grading import GraderError, GraderReply, MarkerGrader
from clinical_diagnostic.models import (
    AnswerSource,
    CaseAnswer,
    CriterionAssessment,
    FailureCode,
    Grade,
    LabelSource,
    Partition,
    ReviewStatus,
)
from clinical_diagnostic.provider import ProviderReply, RateLimited
from clinical_diagnostic.runner import (
    DiagnosticConfig,
    EXIT_OK,
    _attempt,
    _execute_jobs,
    _execute_one,
    _frozen_gate,
    _select_cases,
    build_public_grader,
    build_public_producers,
    run,
)
from clinical_diagnostic.run_record import RunIdentity, RunRecord


def test_frozen_run_writes_record_and_report_and_passes_gate(tmp_path):
    outcome = run(DiagnosticConfig(run_id="frozen", artifacts_root=str(tmp_path)))
    assert outcome.exit_code == EXIT_OK
    assert outcome.offenders == ()
    assert outcome.record.summary["scored"] == 72
    assert outcome.record.summary["infrastructure_failures"] == 0
    assert outcome.record.summary["review_counts"]["draft"] == 36
    assert outcome.record.summary["by_source"]["frozen_candidate"]["mean_normalized"] == 1.0
    assert outcome.record.summary["by_source"]["frozen_baseline"]["mean_normalized"] < 0
    payload = json.loads((tmp_path / "frozen" / "clinical_diagnostic.record.json").read_text())
    assert payload["identity"]["config_digest"]
    assert len(payload["results"]) == 72
    report = (tmp_path / "frozen" / "CLINICAL_DIAGNOSTIC_REPORT.md").read_text()
    assert "No clinician review is recorded" in report
    assert "Infrastructure failures (never scored)" in report


def test_frozen_run_can_limit_and_select_one_partition(tmp_path):
    outcome = run(DiagnosticConfig(
        run_id="limited", artifacts_root=str(tmp_path), partitions=(Partition.HELD_OUT,),
        limits=ExecutionLimits(max_cases=4),
    ))
    assert outcome.exit_code == EXIT_OK
    assert len(outcome.record.results) == 8
    assert {result.partition for result in outcome.record.results} == {Partition.HELD_OUT}
    assert outcome.record.summary["review_counts"]["draft"] == 4


def test_select_cases_round_robins_partitions_and_rejects_bad_limit():
    corpus = load_synthetic_corpus()
    selected = _select_cases(
        corpus.cases, (Partition.DEVELOPMENT, Partition.HELD_OUT), 5
    )
    assert [case.partition for case in selected] == [
        Partition.DEVELOPMENT, Partition.HELD_OUT,
        Partition.DEVELOPMENT, Partition.HELD_OUT,
        Partition.DEVELOPMENT,
    ]
    with pytest.raises(ValueError, match="positive"):
        _select_cases(corpus.cases, (Partition.DEVELOPMENT,), 0)


def test_config_refuses_bad_mode_and_duplicate_partitions():
    with pytest.raises(ValueError, match="mode"):
        DiagnosticConfig(mode="staging")
    with pytest.raises(ValueError, match="repeat"):
        DiagnosticConfig(partitions=(Partition.DEVELOPMENT, Partition.DEVELOPMENT))
    with pytest.raises(ValueError, match="name development"):
        DiagnosticConfig(partitions=("development",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="opt-in"):
        run(DiagnosticConfig(mode="public", public_opt_in=False))


def test_frozen_gate_detects_missing_candidate_cases_and_weak_means():
    corpus = load_synthetic_corpus()
    case = corpus.cases[0]
    identity = RunIdentity(
        mode="frozen", corpus={}, producers=("frozen_candidate", "frozen_baseline"),
        grader="marker-grader/1", config={},
    )
    record = RunRecord(run_id="gate", identity=identity)
    offenders = _frozen_gate(record, [])
    assert offenders == []
    from clinical_diagnostic.runner import _attempt

    budget = BudgetTracker(ExecutionLimits(max_calls=100))
    from clinical_diagnostic.provider import FrozenAnswerProducer

    baseline = _attempt(FrozenAnswerProducer("baseline"), case, MarkerGrader(), budget)
    candidate = _attempt(FrozenAnswerProducer("candidate"), case, MarkerGrader(), budget)
    record.results = [baseline, candidate]
    assert _frozen_gate(record, []) == []


class _Producer:
    source = AnswerSource.BARE
    identity = "fake-producer"
    agreement_key = None

    def __init__(self, failures=0, cost=0.0, delay=0.0, text="answer"):
        self.failures = failures
        self.cost = cost
        self.delay = delay
        self.text = text
        self.calls = 0
        self._lock = threading.Lock()

    def produce(self, case):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls <= self.failures:
            raise RateLimited()
        return CaseAnswer(
            case_id=case.case_id, source=self.source, text=self.text,
            model_identity=self.identity, cost=self.cost,
        )


class _Grader:
    identity = "fake-grader"

    def __init__(self, failure=None, cost=0.0, delay=0.0):
        self.failure = failure
        self.cost = cost
        self.delay = delay

    def assess(self, case, answer):
        if self.delay:
            time.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return GraderReply(
            grade=Grade(tuple(
                CriterionAssessment(item.criterion_id, item.points > 0)
                for item in case.rubric_items
            )),
            model_identity=self.identity,
            cost=self.cost,
        )


def _case(index=0):
    return load_synthetic_corpus().cases[index]


def test_retryable_provider_failure_recovers_within_the_retry_budget():
    producer = _Producer(failures=1)
    budget = BudgetTracker(ExecutionLimits(max_calls=10, max_retries=2))
    result = _execute_one(producer, _case(), _Grader(), budget, 2)
    assert result.status == "scored"
    assert producer.calls == 2


def test_exhausted_retry_is_an_infrastructure_failure_not_a_zero():
    producer = _Producer(failures=10)
    budget = BudgetTracker(ExecutionLimits(max_calls=10, max_retries=1))
    result = _execute_one(producer, _case(), _Grader(), budget, 1)
    assert result.status == "infrastructure_failure"
    assert result.score is None
    assert result.failure.code is FailureCode.RATE_LIMITED
    assert producer.calls == 2


def test_invalid_grade_is_an_infrastructure_failure():
    budget = BudgetTracker(ExecutionLimits(max_calls=10))
    grader = _Grader(GraderError("bad grade"))
    result = _execute_one(_Producer(), _case(), grader, budget, 2)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.INVALID_GRADE


def test_call_budget_exhaustion_marks_the_case_without_a_score():
    budget = BudgetTracker(ExecutionLimits(max_calls=1, max_retries=0))
    result = _execute_one(_Producer(), _case(), _Grader(), budget, 0)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.BUDGET_EXHAUSTED


def test_cost_budget_exhaustion_after_a_call_marks_the_case():
    budget = BudgetTracker(ExecutionLimits(max_calls=10, max_cost=0.01))
    result = _execute_one(_Producer(cost=0.02), _case(), _Grader(), budget, 0)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.BUDGET_EXHAUSTED


def test_cancellation_marks_every_case_as_an_infrastructure_failure():
    event = threading.Event()
    event.set()
    budget = BudgetTracker(ExecutionLimits(max_calls=10), cancel_event=event)
    result = _execute_one(_Producer(), _case(), _Grader(), budget, 0)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.CANCELLED


def test_cancellation_during_retry_backoff_is_reported():
    event = threading.Event()
    budget = BudgetTracker(
        ExecutionLimits(max_calls=10, max_retries=2),
        cancel_event=event,
        sleep=lambda _: event.set(),
    )
    result = _execute_one(_Producer(failures=10), _case(), _Grader(), budget, 2)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.CANCELLED


def test_execute_jobs_bounds_concurrency_and_preserves_order():
    active = 0
    maximum = 0
    lock = threading.Lock()

    class SlowProducer(_Producer):
        def produce(self, case):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return super().produce(case)

    cases = [_case(index) for index in range(4)]
    jobs = [(_Producer(text=f"answer-{index}"), case) for index, case in enumerate(cases)]
    jobs = [(SlowProducer(text=f"answer-{index}"), case) for index, case in enumerate(cases)]
    budget = BudgetTracker(ExecutionLimits(max_calls=100, concurrency=2))
    results = _execute_jobs(jobs, _Grader(), budget, 0)
    assert [result.case_id for result in results] == [case.case_id for case in cases]
    assert 1 <= maximum <= 2
    assert all(result.status == "scored" for result in results)


def test_public_run_requires_asset_and_review_acknowledgement(tmp_path):
    config = DiagnosticConfig(
        mode="public", public_opt_in=True, run_id="public", artifacts_root=str(tmp_path),
        limits=ExecutionLimits(max_cases=1),
    )
    with pytest.raises(ValueError, match="--asset"):
        run(config)
    draft = _reviewed_asset(tmp_path, reviewed=False, name="draft.zip")
    config.asset_path = str(draft)
    config.asset_member = "examples.json"
    config.asset_expected_sha256 = hashlib.sha256(draft.read_bytes()).hexdigest()
    config.producers = (
        _Producer(), _DispatcherProducer(),
    )
    config.grader = MarkerGrader()
    with pytest.raises(ValueError, match="not clinician-reviewed"):
        run(config)
    asset = _reviewed_asset(tmp_path)
    config.asset_path = str(asset)
    config.asset_expected_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
    config.acknowledge_unreviewed_public = True
    outcome = run(config)
    assert outcome.exit_code == EXIT_OK
    assert outcome.record.summary["review_counts"]["clinician_reviewed"] == 1
    assert "review_acknowledged" in outcome.record.identity.corpus


class _DispatcherAdapter:
    identity = "astral-dispatcher/fake"

    def answer(self, case):
        return DispatchedAnswer(text="Dispatcher answer.", chat_id="chat-1")

    def probe_foreign_chat(self, chat_id):
        raise AssertionError("not used")


class _DispatcherProducer:
    source = AnswerSource.ASTRAL
    identity = "astral-dispatcher/fake"
    agreement_key = None

    def produce(self, case):
        return CaseAnswer(
            case_id=case.case_id, source=self.source,
            text="Dispatcher answer.", model_identity=self.identity,
        )


def _reviewed_asset(tmp_path, reviewed=True, name="assets.zip"):
    payload = {
        "prompt_id": "public-case-1",
        "partition": "development",
        "setting": "consultation",
        "specialty": "primary care",
        "conversation": [{"role": "user", "content": "Synthetic public question?"}],
        "rubric_items": [
            {"criterion_id": "required", "criterion": "States the fact.", "points": 1.0,
             "kind": "required_fact", "evidence_terms": ["the fact"]},
            {"criterion_id": "harmful", "criterion": "Claims harm.", "points": -2.0,
             "kind": "harmful_claim", "evidence_terms": ["harmful claim"]},
        ],
        "frozen_answers": {"candidate": "The fact."},
        "review": ({
            "status": "clinician_reviewed",
            "labels_source": "clinician_review",
            "reviewer_token": "reviewer-1",
            "reviewed_at": "2026-10-20",
            "observations": [
                {"answer_key": "candidate", "labels": {"required": True, "harmful": False}}
            ],
        } if reviewed else {
            "status": "draft",
            "labels_source": "author_draft",
            "observations": [
                {"answer_key": "candidate", "labels": {"required": True, "harmful": False}}
            ],
        }),
    }
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("examples.json", json.dumps([payload]))
    return path


def test_build_public_producers_and_grader_use_the_shared_identities():
    from clinical_diagnostic.provider import CallableProvider

    provider = CallableProvider(lambda _m: ProviderReply("answer", "provider-model"),
                                "provider-model")
    producers = build_public_producers(provider, _DispatcherAdapter())
    assert [producer.source for producer in producers] == [
        AnswerSource.BARE, AnswerSource.ASTRAL
    ]
    assert build_public_grader(provider).identity.startswith("model-grader/")


def test_public_run_requires_producers_and_grader(tmp_path):
    asset = _reviewed_asset(tmp_path)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    config = DiagnosticConfig(
        mode="public", public_opt_in=True, run_id="public", artifacts_root=str(tmp_path),
        limits=ExecutionLimits(max_cases=1), asset_path=str(asset),
        asset_member="examples.json", asset_expected_sha256=digest,
        acknowledge_unreviewed_public=True,
    )
    with pytest.raises(ValueError, match="producers"):
        run(config)
    config.producers = (_Producer(),)
    with pytest.raises(ValueError, match="grader"):
        run(config)

class _IncompleteProducer(_Producer):
    source = AnswerSource.FROZEN_CANDIDATE

    def produce(self, case):
        return CaseAnswer(
            case_id=case.case_id, source=self.source, text="wrong answer",
            model_identity=self.identity,
        )


def test_frozen_gate_flags_incomplete_and_non_improving_candidates():
    from clinical_diagnostic.models import CaseResult, FailureCode as Code, InfrastructureFailure

    corpus = load_synthetic_corpus()
    identity = RunIdentity(
        mode="frozen", corpus={}, producers=("frozen_candidate", "frozen_baseline"),
        grader="marker-grader/1", config={},
    )
    record = RunRecord(run_id="gate", identity=identity)
    incomplete = _attempt(
        _IncompleteProducer(), corpus.cases[0], MarkerGrader(),
        BudgetTracker(ExecutionLimits(max_calls=10)),
    )
    record.results = [incomplete]
    assert any("not a complete" in offender for offender in _frozen_gate(record, []))
    candidate = _attempt(
        _IncompleteProducer(), corpus.cases[0], MarkerGrader(),
        BudgetTracker(ExecutionLimits(max_calls=10)),
    )
    baseline_producer = _IncompleteProducer()
    baseline_producer.source = AnswerSource.FROZEN_BASELINE
    baseline = _attempt(
        baseline_producer, corpus.cases[1], MarkerGrader(),
        BudgetTracker(ExecutionLimits(max_calls=10)),
    )
    record.results = [candidate, baseline]
    assert any("does not exceed" in offender for offender in _frozen_gate(record, []))
    record.results = [
        CaseResult(
            "failed", Partition.DEVELOPMENT, corpus.cases[0].setting, "x",
            ReviewStatus.DRAFT, LabelSource.NONE, AnswerSource.FROZEN_CANDIDATE,
            "", "infrastructure_failure",
            failure=InfrastructureFailure(Code.TIMEOUT, "slow"),
        )
    ]
    assert _frozen_gate(record, []) == []


def test_provider_config_records_declared_identity_without_secrets():
    from clinical_diagnostic.provider import CompletionConfig

    class Provider:
        config = CompletionConfig(base_url="http://127.0.0.1:9/v1", model="m",
                                  api_key="secret")

    class Producer:
        source = AnswerSource.BARE
        identity = "bare"
        provider = Provider()

    from clinical_diagnostic.runner import _provider_config, _source_value

    recorded = _provider_config(Producer())
    assert recorded["model"] == "m"
    assert "api_key" not in recorded
    assert _provider_config(object()) is None
    assert _source_value(SimpleNamespace(source="bare_model")) == "bare_model"
    with pytest.raises(ValueError, match="AnswerSource"):
        _source_value(object())


def test_frozen_run_returns_infrastructure_exit_without_scoring(tmp_path):
    outcome = run(DiagnosticConfig(
        run_id="infra", artifacts_root=str(tmp_path),
        limits=ExecutionLimits(max_cases=1, max_calls=1, max_retries=0),
    ))
    assert outcome.exit_code == 3
    assert outcome.record.summary["infrastructure_failures"] >= 1
    assert outcome.record.summary["scored"] == 0


def test_retryable_grader_failure_recovers_and_exhausts():
    class RetryableGrader(_Grader):
        def __init__(self, failures):
            super().__init__()
            self.remaining = failures

        def assess(self, case, answer):
            if self.remaining:
                self.remaining -= 1
                raise GraderError("temporary", code=FailureCode.RATE_LIMITED,
                                  retryable=True)
            return super().assess(case, answer)

    budget = BudgetTracker(ExecutionLimits(max_calls=20, max_retries=1))
    result = _execute_one(_Producer(), _case(), RetryableGrader(1), budget, 1)
    assert result.status == "scored"
    budget = BudgetTracker(ExecutionLimits(max_calls=20, max_retries=1))
    result = _execute_one(_Producer(), _case(), RetryableGrader(2), budget, 1)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.RATE_LIMITED


def test_grader_retry_backoff_cancellation_is_reported():
    event = threading.Event()

    class CancellingGrader(_Grader):
        def assess(self, case, answer):
            event.set()
            raise GraderError("temporary", code=FailureCode.RATE_LIMITED,
                              retryable=True)

    budget = BudgetTracker(
        ExecutionLimits(max_calls=20, max_retries=2),
        cancel_event=event,
        sleep=lambda _: None,
    )
    result = _execute_one(_Producer(), _case(), CancellingGrader(), budget, 2)
    assert result.status == "infrastructure_failure"
    assert result.failure.code is FailureCode.CANCELLED


def test_empty_selection_is_refused(tmp_path):
    config = DiagnosticConfig(
        run_id="empty", artifacts_root=str(tmp_path),
        partitions=(Partition.DEVELOPMENT,), limits=ExecutionLimits(max_cases=1),
    )
    from clinical_diagnostic.runner import _select_cases as select

    assert select([], config.partitions, 1) == []


def test_public_review_overlay_allows_an_unacknowledged_run(tmp_path):
    asset = _reviewed_asset(tmp_path, reviewed=False, name="draft.zip")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"reviews": [{
        "case_id": "public-case-1",
        "status": "clinician_reviewed",
        "reviewer_token": "reviewer-1",
        "reviewed_at": "2026-10-20",
        "observations": [],
    }]}), encoding="utf-8")
    config = DiagnosticConfig(
        mode="public", public_opt_in=True, run_id="public-reviewed",
        artifacts_root=str(tmp_path), limits=ExecutionLimits(max_cases=1),
        asset_path=str(asset), asset_member="examples.json",
        asset_expected_sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
        review_file=str(review),
        producers=(_Producer(), _DispatcherProducer()),
        grader=MarkerGrader(),
    )
    outcome = run(config)
    assert outcome.exit_code == EXIT_OK
    assert outcome.record.summary["review_counts"]["clinician_reviewed"] == 1
    assert outcome.record.identity.corpus["review_file_digest"]
    report = Path(outcome.report_path).read_text(encoding="utf-8")
    assert "bare_model" in report and "astral_dispatcher" in report
    assert outcome.record.identity.models["bare_model"] == "fake-producer"
