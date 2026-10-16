"""The reproducibility unit for an answer-behavior run: the run key ties every verdict
to (actor model, corpus version, suite version, seed), and the record holds
per-actor-per-case rubric results as a written artifact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from answer_behavior import SUITE_VERSION
from answer_behavior.rubric import RubricResult

ARTIFACTS_DIRNAME = "_artifacts"
DEFAULT_ARTIFACTS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ARTIFACTS_DIRNAME
)


@dataclass(frozen=True)
class RunKey:
    model: str
    corpus_version: str
    suite_version: str = SUITE_VERSION
    seed: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "model": self.model,
            "corpus_version": self.corpus_version,
            "suite_version": self.suite_version,
            "seed": self.seed,
        }


@dataclass
class CaseScore:
    case_id: str
    dimension: str
    category: str
    identity: str
    identity_scope: str
    actor: str
    result: RubricResult
    expected: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension,
            "category": self.category,
            "identity": self.identity,
            "identity_scope": self.identity_scope,
            "actor": self.actor,
            "result": self.result.to_dict(),
            "expected": self.expected,
        }


@dataclass
class RunRecord:
    key: RunKey
    run_id: str
    mode: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    scores: List[CaseScore] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.key.to_dict(),
            "run_id": self.run_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "scores": [score.to_dict() for score in self.scores],
        }

    def write_json(self, artifacts_root: str) -> str:
        run_dir = os.path.join(artifacts_root, self.run_id)
        os.makedirs(run_dir, exist_ok=True)
        slug = f"{self.key.model}_seed{self.key.seed}".replace("/", "-")
        path = os.path.join(run_dir, f"answer_behavior_{slug}.record.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path
