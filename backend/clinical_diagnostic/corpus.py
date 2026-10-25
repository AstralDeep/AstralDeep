"""Loads and stratifies the frozen synthetic Professional-shaped corpus, validates its
development/held-out split, criterion coverage, frozen answers, and review records, and
keeps the reviewed asset, license, and content hashes attached to every run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from clinical_diagnostic import SUITE_VERSION
from clinical_diagnostic.healthbench import (
    PROFESSIONAL_SCHEMA,
    REVIEWED_ASSET_LICENSE,
    REVIEWED_ASSET_RETRIEVED,
    REVIEWED_ASSET_SHA256,
    REVIEWED_ASSET_URL,
    SchemaError,
    parse_document,
)
from clinical_diagnostic.models import (
    MAX_CASES,
    MIN_CASES,
    SETTINGS,
    REVIEW_STATUSES,
    ClinicalCase,
    CriterionKind,
    LabelSource,
    Partition,
    ReviewObservation,
    ReviewRecord,
    ReviewStatus,
    Setting,
)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SYNTHETIC_CORPUS_PATH = os.path.join(FIXTURES_DIR, "professional_synthetic.json")
SYNTHETIC_LICENSE = "Apache-2.0 (Astral-original synthetic content)"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REVIEW_BYTES = 1024 * 1024

PROVENANCE: Dict[str, object] = {
    "schema": PROFESSIONAL_SCHEMA,
    "asset": {
        "url": REVIEWED_ASSET_URL,
        "sha256": REVIEWED_ASSET_SHA256,
        "license": REVIEWED_ASSET_LICENSE,
        "retrieved": REVIEWED_ASSET_RETRIEVED,
    },
    "sources": (
        {
            "name": "HealthBench Professional licensed asset bundle",
            "pin": REVIEWED_ASSET_SHA256,
            "url": REVIEWED_ASSET_URL,
            "license": REVIEWED_ASSET_LICENSE,
            "retrieved": REVIEWED_ASSET_RETRIEVED,
            "used_for": "Professional conversation/rubric_items schema and rubric shape",
        },
        {
            "name": "openai/simple-evals healthbench_eval.py",
            "pin": "652c89d0ca9df547706735883097e9537d40dc47",
            "url": (
                "https://github.com/openai/simple-evals/blob/"
                "652c89d0ca9df547706735883097e9537d40dc47/healthbench_eval.py"
            ),
            "license": "MIT",
            "retrieved": "2026-10-06",
            "used_for": "positive-weight normalization and negative-point penalties only",
        },
        {
            "name": "Hicks et al., HealthBench Professional preprint",
            "pin": "arXiv:2604.27470v1",
            "url": "https://arxiv.org/abs/2604.27470v1",
            "license": "preprint terms",
            "retrieved": "2026-10-06",
            "used_for": "professional consultation/documentation/research rubric concepts",
        },
        {
            "name": "PacificAI/medhelm",
            "pin": "5f4db10513b64d78483b3e29f08f88a7a74d5b2f",
            "url": (
                "https://github.com/PacificAI/medhelm/tree/"
                "5f4db10513b64d78483b3e29f08f88a7a74d5b2f"
            ),
            "license": "Apache-2.0",
            "retrieved": "2026-10-06",
            "used_for": "optional workflow-taxonomy coverage metadata only",
        },
        {
            "name": "Bedi, Cui, Fuentes et al., MedHELM, Nature Medicine 2026",
            "pin": "doi:10.1038/s41591-025-04151-2",
            "url": "https://www.nature.com/articles/s41591-025-04151-2",
            "license": "journal terms",
            "retrieved": "2026-10-06",
            "used_for": "workflow taxonomy motivation; no private dataset permission",
        },
    ),
}


@dataclass(frozen=True)
class SyntheticCorpus:
    cases: Tuple[ClinicalCase, ...]
    file_digest: str
    content_digest: str
    suite_version: str
    license_name: str
    provenance: Mapping[str, object]

    def for_partition(self, partition: Partition, limit: Optional[int] = None) -> List[ClinicalCase]:
        selected = [case for case in self.cases if case.partition is partition]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            selected = selected[:limit]
        return selected

    def review_counts(self) -> Dict[str, int]:
        counts = {status.value: 0 for status in ReviewStatus}
        for case in self.cases:
            counts[case.review.status.value] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "suite_version": self.suite_version,
            "file_digest": self.file_digest,
            "content_digest": self.content_digest,
            "license_name": self.license_name,
            "provenance": self.provenance,
            "case_ids": [case.case_id for case in self.cases],
        }


def load_synthetic_corpus(path: Optional[str] = None) -> SyntheticCorpus:
    source = path or SYNTHETIC_CORPUS_PATH
    with open(source, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SchemaError(f"synthetic corpus exceeds {MAX_FILE_BYTES} bytes")
    cases = tuple(parse_document(data, default_partition=Partition.DEVELOPMENT))
    validate_corpus(cases)
    return SyntheticCorpus(
        cases=cases,
        file_digest=hashlib.sha256(data).hexdigest(),
        content_digest=hashlib.sha256(canonical_cases(cases)).hexdigest(),
        suite_version=SUITE_VERSION,
        license_name=SYNTHETIC_LICENSE,
        provenance=PROVENANCE,
    )


def canonical_cases(cases: Iterable[ClinicalCase]) -> bytes:
    return json.dumps(
        [case.to_dict() for case in cases],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def conversation_digest(case: ClinicalCase) -> str:
    payload = json.dumps(
        [message.to_dict() for message in case.conversation],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_corpus(cases: Tuple[ClinicalCase, ...]) -> None:
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        raise SchemaError(f"corpus must hold {MIN_CASES}..{MAX_CASES} cases, got {len(cases)}")
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise SchemaError("corpus case ids must be unique")
    partitions = {case.partition for case in cases}
    if partitions != {Partition.DEVELOPMENT, Partition.HELD_OUT}:
        raise SchemaError("corpus must retain development and held-out partitions")
    for partition in (Partition.DEVELOPMENT, Partition.HELD_OUT):
        selected = [case for case in cases if case.partition is partition]
        if len(selected) < 15:
            raise SchemaError(f"{partition.value} must hold at least 15 cases")
        settings = {case.setting for case in selected}
        if settings != {Setting(value) for value in SETTINGS}:
            raise SchemaError(f"{partition.value} must cover every workflow setting")
        kinds = {item.kind for case in selected for item in case.rubric_items}
        for required in (
            CriterionKind.REQUIRED_FACT,
            CriterionKind.OMISSION,
            CriterionKind.HARMFUL_CLAIM,
            CriterionKind.APPROPRIATE_ABSTENTION,
        ):
            if required not in kinds:
                raise SchemaError(
                    f"{partition.value} must include {required.value} criteria"
                )
        for case in selected:
            for key in ("baseline", "candidate"):
                if key not in case.frozen_answers:
                    raise SchemaError(f"{case.case_id}: missing frozen {key} answer")
            if not any(
                observation.answer_key == "candidate"
                for observation in case.review.observations
            ):
                raise SchemaError(f"{case.case_id}: missing candidate review observation")
    development = {conversation_digest(case) for case in cases
                   if case.partition is Partition.DEVELOPMENT}
    held_out = {conversation_digest(case) for case in cases
                if case.partition is Partition.HELD_OUT}
    if development & held_out:
        raise SchemaError("development and held-out conversations must be distinct")
    abstention_cases = [
        case for case in cases
        if any(
            item.kind is CriterionKind.APPROPRIATE_ABSTENTION
            for item in case.rubric_items
        )
    ]
    if len(abstention_cases) < 10:
        raise SchemaError("corpus must include at least 10 appropriate-abstention cases")


def load_review_overlay(
    path: str, cases: Sequence[ClinicalCase]
) -> Tuple[List[ClinicalCase], str]:
    with open(path, "rb") as handle:
        data = handle.read(MAX_REVIEW_BYTES + 1)
    if len(data) > MAX_REVIEW_BYTES:
        raise SchemaError(f"review overlay exceeds {MAX_REVIEW_BYTES} bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError("review overlay is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) - {"reviews"} or not isinstance(
        payload.get("reviews"), list
    ):
        raise SchemaError("review overlay must contain a reviews list")
    by_id = {case.case_id: case for case in cases}
    updated: Dict[str, ClinicalCase] = {}
    for entry in payload["reviews"]:
        if not isinstance(entry, Mapping):
            raise SchemaError("review overlay entries must be objects")
        if set(entry) - {"case_id", "status", "reviewer_token", "reviewed_at", "observations"}:
            raise SchemaError("review overlay entry has unknown fields")
        case_id = entry.get("case_id")
        if not isinstance(case_id, str) or case_id not in by_id:
            raise SchemaError("review overlay names an unknown case")
        if case_id in updated:
            raise SchemaError("review overlay repeats a case")
        status = entry.get("status")
        if status not in REVIEW_STATUSES:
            raise SchemaError("review overlay status is unknown")
        reviewer_token = entry.get("reviewer_token", "")
        reviewed_at = entry.get("reviewed_at", "")
        observations = _review_observations(
            entry.get("observations", []), by_id[case_id], case_id
        )
        labels_source = (
            LabelSource.CLINICIAN_REVIEW
            if reviewer_token and reviewed_at else LabelSource.AUTHOR_DRAFT
        )
        try:
            review = ReviewRecord(
                status=ReviewStatus(status),
                labels_source=labels_source,
                reviewer_token=reviewer_token,
                reviewed_at=reviewed_at,
                observations=tuple(observations),
            )
        except ValueError as exc:
            raise SchemaError(f"{case_id}: {exc}") from exc
        updated[case_id] = replace(by_id[case_id], review=review)
    merged = [updated.get(case.case_id, case) for case in cases]
    return merged, hashlib.sha256(data).hexdigest()


def _review_observations(
    value: object, case: ClinicalCase, case_id: str
) -> List[ReviewObservation]:
    if not isinstance(value, list):
        raise SchemaError(f"{case_id}: review observations must be a list")
    criteria = set(case.criteria_by_id())
    observations: List[ReviewObservation] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"answer_key", "labels"}:
            raise SchemaError(f"{case_id}: review observation must contain answer_key and labels")
        answer_key, labels = entry["answer_key"], entry["labels"]
        if not isinstance(answer_key, str) or answer_key not in case.frozen_answers:
            raise SchemaError(f"{case_id}: review observation names an unknown answer")
        if not isinstance(labels, Mapping):
            raise SchemaError(f"{case_id}: review labels must be an object")
        clean: Dict[str, Optional[bool]] = {}
        for criterion_id, label in labels.items():
            if criterion_id not in criteria:
                raise SchemaError(f"{case_id}: review label names an unknown criterion")
            if label is not None and not isinstance(label, bool):
                raise SchemaError(f"{case_id}: review label must be true, false, or null")
            clean[criterion_id] = label
        observations.append(ReviewObservation(answer_key=answer_key, labels=clean))
    return observations


def partition_summary(cases: Iterable[ClinicalCase]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}
    for case in cases:
        bucket = summary.setdefault(
            case.partition.value, {"cases": 0, "settings": 0, "criteria": 0}
        )
        bucket["cases"] += 1
        bucket["criteria"] += len(case.rubric_items)
    for partition in summary:
        summary[partition]["settings"] = len({
            case.setting.value for case in cases if case.partition.value == partition
        })
    return summary


__all__ = [
    "PROVENANCE",
    "SYNTHETIC_CORPUS_PATH",
    "SYNTHETIC_LICENSE",
    "SyntheticCorpus",
    "canonical_cases",
    "conversation_digest",
    "load_review_overlay",
    "load_synthetic_corpus",
    "partition_summary",
    "validate_corpus",
]