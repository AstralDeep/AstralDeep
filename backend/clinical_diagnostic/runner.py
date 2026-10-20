"""Executes the bounded clinical diagnostic: selects stratified cases, runs each producer
and rubric grader under call/retry/elapsed/cost/concurrency limits, records infrastructure
failures separately, enforces the frozen fixture regression gate, and writes the record.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from clinical_diagnostic import SUITE_VERSION
from clinical_diagnostic.budget import (
    BudgetExhausted,
    BudgetTracker,
    ExecutionLimits,
    RunCancelled,
    use_control,
)
from clinical_diagnostic.corpus import (
    canonical_cases,
    load_review_overlay,
    load_synthetic_corpus,
)
from clinical_diagnostic.grading import (
    CallableGrader,
    Grader,
    GraderError,
    MarkerGrader,
)
from clinical_diagnostic.healthbench import (
    REVIEWED_ASSET_SHA256,
    AssetIdentity,
    load_reviewed_asset_bundle,
)
from clinical_diagnostic.models import (
    AnswerSource,
    CaseAnswer,
    CaseResult,
    ClinicalCase,
    FailureCode,
    InfrastructureFailure,
    Partition,
    ReviewStatus,
)
from clinical_diagnostic.provider import (
    BareModelProducer,
    CompletionProvider,
    FrozenAnswerProducer,
    ProducerError,
)
from clinical_diagnostic.report import render_markdown, summarize
from clinical_diagnostic.run_record import (
    DEFAULT_ARTIFACTS_ROOT,
    RunIdentity,
    RunRecord,
)
from clinical_diagnostic.scoring import agreement_for, is_complete, score_case

PUBLIC_RUN_ENV = "ASTRAL_CLINICAL_PUBLIC_RUN"
EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2
EXIT_INFRASTRUCTURE = 3


@dataclass
class DiagnosticConfig:
    mode: str = "frozen"
    partitions: Tuple[Partition, ...] = (Partition.DEVELOPMENT, Partition.HELD_OUT)
    corpus_path: Optional[str] = None
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT
    run_id: str = "local"
    producers: Tuple[object, ...] = ()
    grader: Optional[Grader] = None
    asset_path: Optional[str] = None
    asset_member: str = ""
    asset_expected_sha256: str = REVIEWED_ASSET_SHA256
    review_file: Optional[str] = None
    acknowledge_unreviewed_public: bool = False
    enforce_frozen_gate: bool = True
    public_opt_in: bool = False
    cancellation: Optional[threading.Event] = None
    clock: Optional[Callable[[], float]] = None
    sleep: Optional[Callable[[float], None]] = None

    def __post_init__(self) -> None:
        if self.mode not in ("frozen", "public"):
            raise ValueError("mode must be frozen or public")
        if not self.partitions or any(
            not isinstance(partition, Partition) for partition in self.partitions
        ):
            raise ValueError("partitions must name development and/or held_out")
        if len(set(self.partitions)) != len(self.partitions):
            raise ValueError("partitions must not repeat")


@dataclass
class DiagnosticRun:
    record: RunRecord
    record_path: str
    report_path: str
    offenders: Tuple[str, ...]
    exit_code: int


def run(config: DiagnosticConfig) -> DiagnosticRun:
    if config.mode == "public" and not config.public_opt_in:
        raise ValueError(
            f"public runs are opt-in: set {PUBLIC_RUN_ENV}=1 and pass an explicit provider "
            "and grader configuration"
        )
    budget = BudgetTracker(
        config.limits,
        clock=config.clock,
        sleep=config.sleep,
        cancel_event=config.cancellation,
    )
    if config.mode == "frozen":
        corpus = load_synthetic_corpus(config.corpus_path)
        available = list(corpus.cases)
        review_digest = ""
        if config.review_file:
            available, review_digest = load_review_overlay(config.review_file, available)
        cases = _select_cases(tuple(available), config.partitions, config.limits.max_cases)
        producers = (FrozenAnswerProducer("baseline"), FrozenAnswerProducer("candidate"))
        grader = MarkerGrader()
        review_counts = _review_counts(cases)
        asset: Mapping[str, object] = dict(corpus.provenance["asset"])  # type: ignore[index]
        corpus_identity = {
            "file_digest": corpus.file_digest,
            "content_digest": corpus.content_digest,
            "license_name": corpus.license_name,
            "asset": asset,
            "provenance": corpus.provenance,
            "review_file_digest": review_digest,
            "case_ids": [case.case_id for case in cases],
        }
        enforce_gate = config.enforce_frozen_gate
    else:
        cases, asset_identity, review_digest = _public_cases(config)
        review_counts = _review_counts(cases)
        _require_review(cases, config.acknowledge_unreviewed_public)
        producers = tuple(config.producers)
        if not producers:
            raise ValueError("public mode requires explicit bare-model and dispatcher producers")
        if config.grader is None:
            raise ValueError("public mode requires an explicit rubric grader")
        grader = config.grader
        corpus_identity = {
            "file_digest": "",
            "content_digest": hashlib.sha256(canonical_cases(cases)).hexdigest(),
            "license_name": asset_identity.license_name,
            "asset": asset_identity.to_dict(),
            "review_file_digest": review_digest,
            "case_ids": [case.case_id for case in cases],
            "review_acknowledged": config.acknowledge_unreviewed_public,
        }
        enforce_gate = False
    if not cases:
        raise ValueError("no cases selected for this run")
    config_payload: Dict[str, object] = {
        "limits": config.limits.to_dict(),
        "partitions": [partition.value for partition in config.partitions],
        "enforce_frozen_gate": enforce_gate,
        "suite_version": SUITE_VERSION,
    }
    if config.mode == "public":
        config_payload["producer_configs"] = {
            _source_value(producer): _provider_config(producer) for producer in producers
        }
        config_payload["grader_config"] = _provider_config(grader)
    identity = RunIdentity(
        mode=config.mode,
        corpus=corpus_identity,
        producers=tuple(_source_value(producer) for producer in producers),
        grader=getattr(grader, "identity", type(grader).__name__),
        config=config_payload,
        models={_source_value(producer): getattr(producer, "identity", "") for producer in producers},
    )
    jobs = [(producer, case) for producer in producers for case in cases]
    results = _execute_jobs(jobs, grader, budget, config.limits.max_retries)
    record = RunRecord(
        run_id=config.run_id,
        identity=identity,
        results=results,
        review_counts=review_counts,
    )
    offenders = tuple(_frozen_gate(record, producers)) if enforce_gate else ()
    record.gate_offenders = offenders
    record.summary = summarize(record.results, review_counts)
    record.summary["accounting"] = {"admitted_calls": budget.calls, "known_cost": budget.cost,
                                     "unknown_failed_request_cost": "unknown"}
    record_path = record.write_json(config.artifacts_root)
    report_path = _write_report(record, config.artifacts_root)
    exit_code = EXIT_OK
    if offenders:
        exit_code = EXIT_GATE
    elif record.summary.get("infrastructure_failures"):
        exit_code = EXIT_INFRASTRUCTURE
    return DiagnosticRun(record, record_path, report_path, offenders, exit_code)


def _public_cases(
    config: DiagnosticConfig,
) -> Tuple[List[ClinicalCase], AssetIdentity, str]:
    if not config.asset_path or not config.asset_member:
        raise ValueError("public mode requires --asset and --asset-member")
    cases, identity = load_reviewed_asset_bundle(
        config.asset_path,
        member=config.asset_member,
        expected_sha256=config.asset_expected_sha256,
    )
    review_digest = ""
    if config.review_file:
        cases, review_digest = load_review_overlay(config.review_file, cases)
    selected = _select_cases(tuple(cases), config.partitions, config.limits.max_cases)
    return selected, identity, review_digest


def _require_review(cases: Sequence[ClinicalCase], acknowledged: bool) -> None:
    unreviewed = [
        case.case_id for case in cases
        if case.review.status is not ReviewStatus.CLINICIAN_REVIEWED
    ]
    if unreviewed and not acknowledged:
        raise ValueError(
            f"{len(unreviewed)} selected case(s) are not clinician-reviewed; supply "
            "review records or pass --acknowledge-unreviewed to run a clearly labeled "
            "diagnostic without a clinical-validity claim"
        )


def _select_cases(
    cases: Sequence[ClinicalCase],
    partitions: Sequence[Partition],
    limit: int,
) -> List[ClinicalCase]:
    if limit < 1:
        raise ValueError("max_cases must be positive")
    grouped = [
        [case for case in cases if case.partition is partition]
        for partition in partitions
    ]
    selected: List[ClinicalCase] = []
    for index in itertools.count():
        added = False
        for group in grouped:
            if index < len(group):
                selected.append(group[index])
                added = True
                if len(selected) >= limit:
                    return selected
        if not added:
            return selected


def _provider_config(producer: object) -> Optional[dict]:
    provider = getattr(producer, "provider", None)
    provider = provider or producer
    config = getattr(provider, "config", None)
    to_dict = getattr(config, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def _source_value(producer: object) -> str:
    source = getattr(producer, "source", None)
    if isinstance(source, AnswerSource):
        return source.value
    if isinstance(source, str):
        return source
    raise ValueError("producer must declare an AnswerSource")


def _review_counts(cases: Sequence[ClinicalCase]) -> Dict[str, int]:
    counts = {status.value: 0 for status in ReviewStatus}
    for case in cases:
        counts[case.review.status.value] += 1
    return counts


def _execute_jobs(
    jobs: Sequence[Tuple[object, ClinicalCase]],
    grader: Grader,
    budget: BudgetTracker,
    max_retries: int,
) -> List[CaseResult]:
    results: List[Optional[CaseResult]] = [None] * len(jobs)
    lock = threading.Lock()
    cursor = itertools.count()

    def worker() -> None:
        while True:
            with lock:
                index = next(cursor)
            if index >= len(jobs):
                return
            producer, case = jobs[index]
            results[index] = _execute_one(producer, case, grader, budget, max_retries)

    workers = min(budget.limits.concurrency, len(jobs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clinical-diag") as pool:
        futures = [pool.submit(worker) for _ in range(workers)]
        for future in futures:
            future.result()
    return [result for result in results if result is not None]


def _execute_one(
    producer: object,
    case: ClinicalCase,
    grader: Grader,
    budget: BudgetTracker,
    max_retries: int,
) -> CaseResult:
    attempt = 0
    while True:
        try:
            with use_control(budget):
                return _attempt(producer, case, grader, budget)
        except ProducerError as exc:
            if exc.completed_cost is not None:
                budget.charge_known_cost(exc.completed_cost)
            if exc.retryable and attempt < max_retries:
                attempt += 1
                try:
                    budget.retry_delay(attempt)
                except (BudgetExhausted, RunCancelled) as delay_exc:
                    return _failure(
                        case, producer, delay_exc.code, delay_exc.detail, False
                    )
                continue
            return _failure(case, producer, exc.code, exc.detail, exc.retryable)
        except GraderError as exc:
            if exc.completed_cost is not None:
                budget.charge_known_cost(exc.completed_cost)
            if exc.retryable and attempt < max_retries:
                attempt += 1
                try:
                    budget.retry_delay(attempt)
                except (BudgetExhausted, RunCancelled) as delay_exc:
                    return _failure(
                        case, producer, delay_exc.code, delay_exc.detail, False
                    )
                continue
            return _failure(case, producer, exc.code, exc.detail, exc.retryable)
        except BudgetExhausted as exc:
            return _failure(
                case, producer, FailureCode.BUDGET_EXHAUSTED, exc.detail, False
            )
        except RunCancelled as exc:
            return _failure(case, producer, FailureCode.CANCELLED, exc.detail, False)
        except Exception as exc:
            budget.request_cancel()
            return _failure(case, producer, FailureCode.INTERNAL_ERROR, type(exc).__name__, False)


def _attempt(
    producer: object,
    case: ClinicalCase,
    grader: Grader,
    budget: BudgetTracker,
) -> CaseResult:
    budget.begin_call()
    answer: CaseAnswer = producer.produce(case)  # type: ignore[attr-defined]
    budget.record_cost(answer.cost)
    budget.begin_call()
    reply = grader.assess(case, answer.text)
    budget.record_cost(reply.cost)
    score = score_case(case, reply.grade)
    key = getattr(producer, "agreement_key", None)
    agreement = agreement_for(case, key, reply.grade) if key else None
    return CaseResult(
        case_id=case.case_id,
        partition=case.partition,
        setting=case.setting,
        specialty=case.specialty,
        review_status=case.review.status,
        labels_source=case.review.labels_source,
        answer_source=AnswerSource(_source_value(producer)),
        answer_digest=hashlib.sha256(answer.text.encode("utf-8")).hexdigest(),
        status="scored",
        score=score,
        agreement=agreement,
        model_identity=answer.model_identity or "unknown",
        grader_identity=reply.model_identity or "unknown",
        official_use_case=case.official_use_case,
    )


def _failure(
    case: ClinicalCase,
    producer: object,
    code: FailureCode,
    detail: str,
    retryable: bool,
) -> CaseResult:
    return CaseResult(
        case_id=case.case_id,
        partition=case.partition,
        setting=case.setting,
        specialty=case.specialty,
        review_status=case.review.status,
        labels_source=case.review.labels_source,
        answer_source=AnswerSource(_source_value(producer)),
        answer_digest="",
        status="infrastructure_failure",
        official_use_case=case.official_use_case,
        failure=InfrastructureFailure(code=code, detail=detail, retryable=retryable),
    )


def _frozen_gate(record: RunRecord, producers: Sequence[object]) -> List[str]:
    offenders: List[str] = []
    candidate_source = AnswerSource.FROZEN_CANDIDATE.value
    baseline_source = AnswerSource.FROZEN_BASELINE.value
    for result in record.results:
        if result.answer_source.value != candidate_source or result.status != "scored":
            continue
        if result.score is None or not is_complete(result.score):
            offenders.append(f"{result.case_id}: candidate fixture is not a complete answer")
    candidate = [
        result.score.normalized for result in record.results
        if result.answer_source.value == candidate_source
        and result.score is not None and result.score.normalized is not None
    ]
    baseline = [
        result.score.normalized for result in record.results
        if result.answer_source.value == baseline_source
        and result.score is not None and result.score.normalized is not None
    ]
    if candidate and baseline:
        candidate_mean = sum(candidate) / len(candidate)
        baseline_mean = sum(baseline) / len(baseline)
        if candidate_mean <= baseline_mean:
            offenders.append(
                f"candidate fixture mean {candidate_mean:.3f} does not exceed "
                f"baseline {baseline_mean:.3f}"
            )
    return offenders


def _write_report(record: RunRecord, artifacts_root: str) -> str:
    run_dir = os.path.join(artifacts_root, record.run_id)
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "CLINICAL_DIAGNOSTIC_REPORT.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(record))
    return path


def build_public_producers(
    bare_provider: CompletionProvider,
    dispatcher_adapter: object,
) -> Tuple[object, object]:
    from clinical_diagnostic.dispatcher import DispatcherProducer

    return (BareModelProducer(bare_provider), DispatcherProducer(dispatcher_adapter))


def build_public_grader(provider: CompletionProvider) -> Grader:
    return CallableGrader(provider)


__all__ = [
    "DiagnosticConfig",
    "DiagnosticRun",
    "EXIT_CONFIG",
    "EXIT_GATE",
    "EXIT_INFRASTRUCTURE",
    "EXIT_OK",
    "PUBLIC_RUN_ENV",
    "build_public_grader",
    "build_public_producers",
    "run",
]
