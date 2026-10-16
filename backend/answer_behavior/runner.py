"""Assembles the suite pipeline: loads the frozen corpus, drives each case through the
selected driver, applies the rubric, writes the record and report, and — in frozen mode
only — enforces the candidate success floor against both the floor and the corpus's
declared expected verdicts; live mode is diagnostic and never gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from answer_behavior.corpus import Corpus, load_corpus
from answer_behavior.drivers import LIVE_ENV_FLAG, get_driver
from answer_behavior.models import ACTIONS
from answer_behavior.report import dimension_stats, render_markdown
from answer_behavior.run_record import (
    DEFAULT_ARTIFACTS_ROOT,
    CaseScore,
    RunKey,
    RunRecord,
)
from answer_behavior.rubric import adjudicate

LIVE_DEFAULT_FLOOR_NOTE = "live mode is diagnostic and non-gating"


@dataclass
class RunConfig:
    mode: str = "frozen"
    actors: List[str] = field(default_factory=lambda: ["baseline", "candidate"])
    model: str = "frozen-corpus"
    corpus_path: Optional[str] = None
    seed: int = 0
    limit: Optional[int] = None
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT
    run_id: Optional[str] = None
    success_floor: float = 1.0
    live_config_path: Optional[str] = None
    live_max_cases: int = 8
    live_completion: Optional[Callable[[List[dict]], str]] = None

    def normalized_run_id(self, stamp: str = "local") -> str:
        return self.run_id or stamp or "local"


def run(config: RunConfig, stamp: str = "local") -> Tuple[RunRecord, str, List[str]]:
    if config.mode not in ("frozen", "live"):
        raise ValueError("mode must be frozen or live")
    if config.mode == "live" and os.environ.get(LIVE_ENV_FLAG) != "1":
        raise RuntimeError(
            f"live mode is opt-in: set {LIVE_ENV_FLAG}=1 to run the bounded, non-gating "
            "live driver"
        )
    corpus: Corpus = load_corpus(config.corpus_path)
    live_driver = None
    if config.mode == "frozen":
        actors = [actor for actor in config.actors if actor in ACTIONS]
        if not actors:
            raise ValueError(f"no valid actors selected: {config.actors!r}")
    else:
        live_driver = get_driver(
            "live",
            config_path=config.live_config_path,
            max_cases=config.live_max_cases,
            completion=config.live_completion,
        )
        actors = ["candidate"]
    cases = corpus.cases if config.limit is None else corpus.cases[: config.limit]
    if live_driver is not None:
        cases = cases[:config.live_max_cases]

    run_id = config.normalized_run_id(stamp)
    record = RunRecord(
        key=RunKey(model=config.model, corpus_version=corpus.suite_version, seed=config.seed),
        run_id=run_id,
        mode=config.mode,
    )
    scope_by_identity = {
        name: scope
        for scope, names in corpus.identities.items()
        for name in names
    }
    for case in cases:
        for actor in actors:
            driver = live_driver or get_driver("frozen", actor=actor)
            transcript = driver.run_case(case)
            result = adjudicate(case, transcript)
            record.scores.append(
                CaseScore(
                    case_id=case.case_id,
                    dimension=case.dimension,
                    category=case.category,
                    identity=case.identity,
                    identity_scope=scope_by_identity.get(case.identity, "undeclared"),
                    actor=actor,
                    result=result,
                    expected=case.expected_verdicts.get(actor, ""),
                )
            )

    record_path = record.write_json(config.artifacts_root)
    report_path = _write_report(record, corpus, config.artifacts_root, run_id)
    offenders = [] if config.mode == "live" else check_gate(record, config.success_floor)
    return record, report_path or record_path, offenders


def check_gate(record: RunRecord, floor: float) -> List[str]:
    offenders: List[str] = []
    for score in record.scores:
        if score.actor != "candidate" or not score.expected:
            continue
        if score.result.verdict.value != score.expected:
            offenders.append(
                f"{score.case_id}: candidate verdict {score.result.verdict.value!r} "
                f"!= expected {score.expected!r}"
            )
    for dimension in ("instruction_precedence", "abstention", "truthful_completion"):
        stats = dimension_stats(record.scores, "candidate", dimension)
        rate = stats.success_rate
        if rate is not None and rate < floor:
            offenders.append(
                f"{dimension}: candidate success rate {rate:.3f} < floor {floor:.3f}"
            )
    return offenders


def _write_report(
    record: RunRecord, corpus: Corpus, artifacts_root: str, run_id: str
) -> Optional[str]:
    import os as _os

    run_dir = _os.path.join(artifacts_root, run_id)
    _os.makedirs(run_dir, exist_ok=True)
    path = _os.path.join(run_dir, "ANSWER_BEHAVIOR_REPORT.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(record, len(corpus.cases)))
        handle.write("\n")
    return path
