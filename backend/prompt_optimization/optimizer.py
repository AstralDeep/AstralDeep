"""The bounded offline optimization run: evaluates the baseline and validated candidate
instructions on disjoint partitions, selects deterministically on the selection-validation
partition only, then evaluates once on the untouched final hold-out, enforcing the call
budget, cancellation, a hold-out reuse seal, and honest qualified/unqualified outcomes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from prompt_optimization import budget as budget_module
from prompt_optimization import metrics as metrics_module
from prompt_optimization import model_io as model_io_module
from prompt_optimization import proposals as proposals_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import Fixture
from prompt_optimization.holdout import HoldoutReuseError as HoldoutReuseError, PlaneHoldoutSeals

STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_BUDGET_EXHAUSTED = "budget_exhausted"
STATUS_FAILED = "failed"

RECOMMEND_ADOPT = "adopt"
RECOMMEND_REJECT = "reject"
RECOMMEND_INCONCLUSIVE = "inconclusive"

FEEDBACK_MAX_CHARS = 4000
OUTPUT_PREVIEW_CHARS = 400

class RunStateError(RuntimeError):
    pass


@dataclass
class CaseResult:
    case_id: str
    outcome: str
    metrics: Optional[dict]
    components: Optional[dict]
    output_preview: str
    error_class: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "outcome": self.outcome,
            "metrics": self.metrics,
            "components": self.components,
            "output_preview": self.output_preview,
            "error_class": self.error_class,
        }


@dataclass
class PartitionResult:
    partition: str
    case_results: List[CaseResult] = field(default_factory=list)

    def mean(self) -> Optional[Dict[str, float]]:
        scored = [
            result.metrics for result in self.case_results if result.metrics is not None
        ]
        if not scored:
            return None
        keys = ("schema", "answer", "evidence", "combined")
        return {
            key: round(sum(metrics[key] for metrics in scored) / len(scored), 4)
            for key in keys
        }

    def error_count(self) -> int:
        return sum(1 for result in self.case_results if result.outcome != "succeeded")

    def to_dict(self) -> dict:
        return {
            "partition": self.partition,
            "mean": self.mean(),
            "case_count": len(self.case_results),
            "error_count": self.error_count(),
            "cases": [result.to_dict() for result in self.case_results],
        }


@dataclass
class Candidate:
    instruction: str
    digest: str
    train: Optional[PartitionResult] = None
    validation: Optional[PartitionResult] = None

    def train_score(self) -> float:
        mean = self.train.mean() if self.train else None
        return mean["combined"] if mean else -1.0

    def to_summary(self) -> dict:
        return {
            "instruction": self.instruction,
            "instruction_digest": self.digest,
            "instruction_chars": len(self.instruction),
            "train_mean": self.train.mean() if self.train else None,
            "validation_mean": self.validation.mean() if self.validation else None,
        }


def _feedback_text(partition: Optional[PartitionResult]) -> str:
    if partition is None:
        return "no train evaluation completed yet"
    lines: List[str] = []
    for result in partition.case_results:
        if result.metrics is not None:
            lines.append(
                f"{result.case_id}: metrics={result.metrics} "
                f"components={result.components} "
                f"output={result.output_preview[:200]!r}"
            )
        else:
            lines.append(f"{result.case_id}: {result.outcome} ({result.error_class})")
    return "\n".join(lines)[:FEEDBACK_MAX_CHARS]


@dataclass
class OptimizationResult:
    run_id: str
    created_at: float
    suite_version: str
    status: str
    qualified: bool
    recommendation: str
    recommendation_basis: str
    task: Dict[str, Any]
    fixture: Dict[str, Any]
    optimizer: Dict[str, Any]
    model: Dict[str, Any]
    budget: Dict[str, Any]
    baseline_train: Optional[PartitionResult]
    baseline_validation: Optional[PartitionResult]
    baseline_holdout: Optional[PartitionResult]
    selected_holdout: Optional[PartitionResult]
    candidates: List[Dict[str, Any]]
    selected_digest: Optional[str]
    selection_pool: List[str]
    proposal_errors: List[Dict[str, Any]]
    next_holdout_required: bool

    def to_dict(self) -> dict:
        def partition_dict(result: Optional[PartitionResult]) -> Optional[dict]:
            return result.to_dict() if result else None

        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "suite_version": self.suite_version,
            "status": self.status,
            "qualified": self.qualified,
            "recommendation": self.recommendation,
            "recommendation_basis": self.recommendation_basis,
            "task": self.task,
            "fixture": self.fixture,
            "optimizer": self.optimizer,
            "model": self.model,
            "budget": self.budget,
            "baseline_train": partition_dict(self.baseline_train),
            "baseline_validation": partition_dict(self.baseline_validation),
            "baseline_holdout": partition_dict(self.baseline_holdout),
            "selected_holdout": partition_dict(self.selected_holdout),
            "candidates": self.candidates,
            "selected_digest": self.selected_digest,
            "selection_pool": self.selection_pool,
            "proposal_errors": self.proposal_errors,
            "next_holdout_required": self.next_holdout_required,
        }


class OptimizationRun:
    def __init__(
        self,
        *,
        fixture: Fixture,
        task_prompt: task_module.TaskPrompt,
        adapter: Any,
        holdout_seals: PlaneHoldoutSeals,
        proposal_source: Any = None,
        limits: budget_module.BudgetLimits = budget_module.BudgetLimits(
            max_reflection_calls=1,
            max_evaluation_calls=1,
            max_total_tokens=1,
        ),
        controls: model_io_module.Controls,
        model_identity: str,
        run_id: Optional[str] = None,
    ) -> None:
        self.holdout_seals = holdout_seals
        self._holdout_sealed = False
        self.fixture = fixture
        self.task_prompt = task_prompt
        self.adapter = adapter
        self.proposal_source = proposal_source
        self.limits = limits
        self.controls = controls
        self.model_identity = model_identity
        self.run_id = run_id or str(uuid.uuid4())
        self.budget = budget_module.Budget(limits)
        self._executed = False
        self._baseline: Optional[Candidate] = None
        self._pool: List[Candidate] = []
        self._selected: Optional[Candidate] = None
        self._baseline_holdout: Optional[PartitionResult] = None
        self._selected_holdout: Optional[PartitionResult] = None
        self._proposal_errors: List[Dict[str, Any]] = []

    def execute(self) -> OptimizationResult:
        if self._executed:
            raise RunStateError("run already executed")
        self._executed = True
        self.holdout_seals.assert_available(self.fixture)
        try:
            result = self._run()
        except model_io_module.RunCancelled:
            result = self._result(STATUS_CANCELLED)
        except budget_module.BudgetExhausted:
            result = self._result(STATUS_BUDGET_EXHAUSTED)
        except BaseException:
            if self._holdout_sealed:
                self.holdout_seals.finish(self.fixture, succeeded=False)
            raise
        if self._holdout_sealed:
            self.holdout_seals.finish(self.fixture, succeeded=result.status == STATUS_COMPLETED)
        return result

    def _run(self) -> OptimizationResult:
        baseline = Candidate(
            instruction=self.task_prompt.rules,
            digest=task_module.digest_text(self.task_prompt.rules),
        )
        self._baseline = baseline
        self._pool = [baseline]
        baseline.train = self._evaluate(baseline.instruction, "train", "search")
        proposed: set = {baseline.digest}

        for round_index in range(self.limits.max_rounds):
            if self.controls.cancelled():
                raise model_io_module.RunCancelled("cancelled before proposal round")
            feedback = _feedback_text(baseline.train)
            try:
                instructions = self.proposal_source.propose(
                    round_index, feedback, baseline.instruction
                )
            except (
                model_io_module.ProviderDenied,
                model_io_module.ProviderFailed,
                proposals_module.ProposalError,
            ) as error:
                self._proposal_errors.append(
                    {
                        "round_index": round_index,
                        "error_class": type(error).__name__,
                        "detail": str(error)[:200],
                    }
                )
                break
            fresh = 0
            for instruction in instructions:
                digest = task_module.digest_text(instruction)
                if digest in proposed:
                    continue
                proposed.add(digest)
                candidate = Candidate(instruction=instruction, digest=digest)
                candidate.train = self._evaluate(instruction, "train", "search")
                self._pool.append(candidate)
                fresh += 1
            if not fresh:
                break

        self._selected = self._select()
        self.holdout_seals.seal(self.fixture, self.run_id, context_digest=task_module.digest_text(
            self.task_prompt.prompt_digest + self.model_identity
            + (self._selected.digest if self._selected is not None else "")
        ))
        self._holdout_sealed = True
        self._baseline_holdout = self._evaluate(
            baseline.instruction, "holdout", "holdout"
        )
        if self._selected is not None and self._selected.digest != baseline.digest:
            self._selected_holdout = self._evaluate(
                self._selected.instruction, "holdout", "holdout"
            )
        result = self._result(STATUS_COMPLETED)
        self._decide(result)
        return result

    def _select(self) -> Optional[Candidate]:
        assert self._baseline is not None
        ranked = sorted(
            (c for c in self._pool if c is not self._baseline),
            key=lambda candidate: (-candidate.train_score(), candidate.digest),
        )
        shortlist = [self._baseline] + ranked[
            : max(1, self.limits.max_candidates_per_round)
        ]
        for candidate in shortlist:
            candidate.validation = self._evaluate(
                candidate.instruction, "validation", "selection"
            )
        scored = [
            candidate
            for candidate in shortlist
            if candidate.validation is not None
            and candidate.validation.mean() is not None
        ]
        if not scored:
            return None
        scored.sort(
            key=lambda candidate: (
                -candidate.validation.mean()["combined"],
                candidate.digest,
            )
        )
        return scored[0]

    def _evaluate(
        self, instruction: str, partition: str, phase: str
    ) -> PartitionResult:
        prompt_text = self.task_prompt.render(instruction)
        results: List[CaseResult] = []
        for case in self.fixture.cases(partition):
            if self.controls.cancelled():
                raise model_io_module.RunCancelled("cancelled during evaluation")
            messages = model_io_module.evaluation_messages(prompt_text, case)
            try:
                text, _record = model_io_module.bounded_complete(
                    self.budget,
                    self.adapter,
                    kind="evaluation",
                    phase=phase,
                    subject=case.case_id,
                    messages=messages,
                    controls=self.controls,
                    max_output_chars=self.limits.max_output_chars,
                    max_attempts=self.limits.max_retries_per_call + 1,
                )
            except model_io_module.RunCancelled:
                raise
            except model_io_module.ProviderDenied as error:
                results.append(
                    CaseResult(
                        case_id=case.case_id,
                        outcome="denied",
                        metrics=None,
                        components=None,
                        output_preview="",
                        error_class=_error_class(error),
                    )
                )
                continue
            except model_io_module.ProviderFailed as error:
                results.append(
                    CaseResult(
                        case_id=case.case_id,
                        outcome="failed",
                        metrics=None,
                        components=None,
                        output_preview="",
                        error_class=_error_class(error),
                    )
                )
                continue
            case_metrics, components = metrics_module.score_output(case, text)
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    outcome="succeeded",
                    metrics=case_metrics.to_dict(),
                    components=components.to_dict(),
                    output_preview=text[:OUTPUT_PREVIEW_CHARS],
                )
            )
        return PartitionResult(partition=partition, case_results=results)

    def _decide(self, result: OptimizationResult) -> None:
        clean = (
            self.budget.usage_complete
            and not self.budget.failures()
            and not self._proposal_errors
        )
        baseline = self._baseline
        selected = self._selected
        if not clean:
            result.qualified = False
            result.recommendation = RECOMMEND_INCONCLUSIVE
            result.recommendation_basis = (
                "unresolved provider, accounting, or infrastructure failures; "
                "no adoption claim is possible"
            )
            return
        if (
            selected is None
            or baseline is None
            or selected.digest == baseline.digest
        ):
            result.recommendation = RECOMMEND_REJECT
            result.recommendation_basis = (
                "no candidate beat the baseline on the selection-validation "
                "partition; retain the baseline instruction"
            )
            return
        selected_mean = (
            self._selected_holdout.mean() if self._selected_holdout else None
        )
        baseline_mean = (
            self._baseline_holdout.mean() if self._baseline_holdout else None
        )
        validation_mean = selected.validation.mean() if selected.validation else None
        baseline_validation_mean = (
            baseline.validation.mean() if baseline.validation else None
        )
        if (
            selected_mean is None
            or baseline_mean is None
            or validation_mean is None
            or baseline_validation_mean is None
        ):
            result.qualified = False
            result.recommendation = RECOMMEND_INCONCLUSIVE
            result.recommendation_basis = (
                "hold-out or selection evaluation incomplete; no adoption claim"
            )
            return
        if (
            selected_mean["combined"] > baseline_mean["combined"]
            and validation_mean["combined"] >= baseline_validation_mean["combined"]
        ):
            result.recommendation = RECOMMEND_ADOPT
            result.recommendation_basis = (
                "scripted double improved on the final hold-out and selection-validation; "
                "this verifies plumbing only, not actual model benefit or production adoption"
            )
        else:
            result.recommendation = RECOMMEND_REJECT
            result.recommendation_basis = (
                "candidate did not improve on the untouched final hold-out; "
                "retain the baseline instruction"
            )

    def _result(self, status: str) -> OptimizationResult:
        completed_clean = (
            status == STATUS_COMPLETED
            and self.budget.usage_complete
            and not self.budget.failures()
            and not self._proposal_errors
        )
        return OptimizationResult(
            run_id=self.run_id,
            created_at=time.time(),
            suite_version=_suite_version(),
            status=status,
            qualified=completed_clean,
            recommendation=RECOMMEND_INCONCLUSIVE,
            recommendation_basis=(
                f"run ended with status {status!r}; no adoption claim is possible"
            ),
            task={
                "task_id": self.task_prompt.task_id,
                "source_path": self.task_prompt.source_path,
                "source_digest": self.task_prompt.source_digest,
                "prompt_digest": self.task_prompt.prompt_digest,
                "allowlisted_fields": list(task_module.ALLOWED_FIELDS),
                "baseline_instruction": self.task_prompt.rules,
                "baseline_instruction_digest": self.task_prompt.rules_digest,
                "frozen_head_digest": self.task_prompt.head_digest,
            },
            fixture=self.fixture.to_dict(),
            optimizer={
                "name": "bounded-reflection-v1",
                "holdout_exposure_run_ids": (
                    list(self.holdout_seals.receipt_ids(self.fixture)) if self._holdout_sealed else None
                ),
                "limits": self.limits.to_dict(),
            },
            model={
                "mode": self.model_identity,
                "data_scope": "synthetic-fixture-only",
                "egress": "none",
                "qualification_scope": "scripted-control-plumbing-only",
                "actual_model_qualified": False,
            },
            budget=self.budget.to_dict(),
            baseline_train=self._baseline.train if self._baseline else None,
            baseline_validation=(
                self._baseline.validation if self._baseline else None
            ),
            baseline_holdout=self._baseline_holdout,
            selected_holdout=self._selected_holdout,
            candidates=[
                candidate.to_summary()
                for candidate in self._pool
                if candidate.instruction != (self.task_prompt.rules)
            ],
            selected_digest=(
                self._selected.digest if self._selected is not None else None
            ),
            selection_pool=[
                candidate.digest
                for candidate in self._pool
                if candidate.validation is not None
            ],
            proposal_errors=list(self._proposal_errors),
            next_holdout_required=True,
        )


def _error_class(error: Exception) -> str:
    record = getattr(error, "record", None)
    value = getattr(record, "error_class", None)
    if isinstance(value, str) and value:
        return value
    return type(error).__name__


def _suite_version() -> str:
    from prompt_optimization import SUITE_VERSION

    return SUITE_VERSION
