"""Holds the reproducibility identity and per-case results of one clinical diagnostic run,
binding every score to the corpus, reviewed-asset, producer, grader, configuration, and
suite identities; the record is written as a machine-readable artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Mapping, Tuple

from clinical_diagnostic import SUITE_VERSION
from clinical_diagnostic.healthbench import PROFESSIONAL_SCHEMA
from clinical_diagnostic.models import CaseResult

ARTIFACTS_DIRNAME = "_artifacts"
DEFAULT_ARTIFACTS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ARTIFACTS_DIRNAME
)


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class RunIdentity:
    mode: str
    corpus: Mapping[str, object]
    producers: Tuple[str, ...]
    grader: str
    config: Mapping[str, object]
    models: Mapping[str, str] = field(default_factory=dict)
    suite_version: str = SUITE_VERSION
    schema: str = PROFESSIONAL_SCHEMA

    def __post_init__(self) -> None:
        if self.mode not in ("frozen", "public"):
            raise ValueError("run mode must be frozen or public")
        if not self.producers:
            raise ValueError("run identity must name at least one producer")
        if not self.grader:
            raise ValueError("run identity must name a grader")

    @property
    def config_digest(self) -> str:
        return hashlib.sha256(canonical_json({
            "mode": self.mode,
            "corpus": self.corpus,
            "producers": self.producers,
            "grader": self.grader,
            "models": self.models,
            "config": self.config,
            "suite_version": self.suite_version,
            "schema": self.schema,
        })).hexdigest()

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "corpus": self.corpus,
            "producers": list(self.producers),
            "grader": self.grader,
            "models": dict(self.models),
            "config": self.config,
            "config_digest": self.config_digest,
            "suite_version": self.suite_version,
            "schema": self.schema,
        }


@dataclass
class RunRecord:
    run_id: str
    identity: RunIdentity
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    results: List[CaseResult] = field(default_factory=list)
    review_counts: Mapping[str, int] = field(default_factory=dict)
    summary: Mapping[str, object] = field(default_factory=dict)
    gate_offenders: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "identity": self.identity.to_dict(),
            "review_counts": dict(self.review_counts),
            "summary": dict(self.summary),
            "gate_offenders": list(self.gate_offenders),
            "results": [result.to_dict() for result in self.results],
        }

    def write_json(self, artifacts_root: str) -> str:
        if not self.run_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for character in self.run_id
        ):
            raise ValueError("run_id must use only identifier characters")
        run_dir = os.path.join(artifacts_root, self.run_id)
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "clinical_diagnostic.record.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path


