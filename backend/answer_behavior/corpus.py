"""Loads and validates the answer-behavior corpus: the tracked module literal by default
or a JSON file override; enforces schema, bounds, provenance pins, declared identities,
and per-actor frozen answers, failing closed on any malformed entry.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from answer_behavior import corpus_data
from answer_behavior.models import (
    ACTIONS,
    DIMENSIONS,
    ActionClaim,
    AnswerTranscript,
    BehaviorCase,
    Environment,
    PermissionDenial,
    Question,
    Receipt,
    Resource,
    UntrustedContent,
)

SCHEMA_VERSION = 1
MAX_CASES = 32


@dataclass(frozen=True)
class Corpus:
    schema_version: int
    suite: str
    suite_version: str
    provenance: Dict[str, Any]
    identities: Dict[str, List[str]]
    cases: List[BehaviorCase] = field(default_factory=list)

    def cases_for_identity_scope(self, scope: str) -> List[BehaviorCase]:
        allowed = set(self.identities.get(scope, []))
        return [case for case in self.cases if case.identity in allowed]


def load_corpus(path: Optional[str] = None) -> Corpus:
    if path is None:
        return parse_corpus(copy.deepcopy(corpus_data.CORPUS))
    with open(path, "r", encoding="utf-8") as handle:
        return parse_corpus(json.load(handle))


def parse_corpus(payload: Dict[str, Any]) -> Corpus:
    _require(payload, "schema_version", int)
    _require(payload, "suite", str)
    _require(payload, "suite_version", str)
    _require(payload, "provenance", dict)
    _require(payload, "identities", dict)
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported corpus schema_version: {payload['schema_version']!r}")
    for scope, names in payload["identities"].items():
        if scope not in ("development", "held_out") or not isinstance(names, list):
            raise ValueError(f"identities must declare development and held_out lists: {scope!r}")
    cases = [_parse_case(entry) for entry in payload.get("cases", [])]
    if not cases or len(cases) > MAX_CASES:
        raise ValueError(f"corpus must hold 1..{MAX_CASES} bounded cases, got {len(cases)}")
    _validate_case_ids(cases)
    _validate_identities(cases, payload["identities"])
    _validate_provenance(payload["provenance"])
    return Corpus(
        schema_version=payload["schema_version"],
        suite=payload["suite"],
        suite_version=payload["suite_version"],
        provenance=payload["provenance"],
        identities=payload["identities"],
        cases=cases,
    )


def _parse_case(entry: Dict[str, Any]) -> BehaviorCase:
    _require(entry, "case_id", str)
    _require(entry, "dimension", str)
    _require(entry, "category", str)
    _require(entry, "identity", str)
    _require(entry, "authority", str)
    _require(entry, "user_prompt", str)
    if entry["dimension"] not in DIMENSIONS:
        raise ValueError(f"{entry['case_id']}: unknown dimension {entry['dimension']!r}")
    environment = _parse_environment(entry.get("environment", {}))
    _require_scorable_axis(entry, environment)
    case = BehaviorCase(
        case_id=entry["case_id"],
        dimension=entry["dimension"],
        category=entry["category"],
        identity=entry["identity"],
        authority=entry["authority"],
        user_prompt=entry["user_prompt"],
        environment=environment,
        warrant_markers=list(entry.get("warrant_markers", [])),
        fabrication_markers=list(entry.get("fabrication_markers", [])),
        evidence_markers=list(entry.get("evidence_markers", [])),
        report_required=list(entry.get("report_required", [])),
        unscorable_reason=str(entry.get("unscorable_reason", "")),
        frozen_answers={
            actor: _parse_transcript(actor, transcript)
            for actor, transcript in entry.get("frozen_answers", {}).items()
        },
        expected_verdicts=dict(entry.get("expected_verdicts", {})),
    )
    for actor in ACTIONS:
        if actor not in case.frozen_answers:
            raise ValueError(f"{case.case_id}: missing frozen answer for actor {actor!r}")
    return case


def _parse_environment(payload: Dict[str, Any]) -> Environment:
    return Environment(
        receipts=[Receipt(**receipt) for receipt in payload.get("receipts", [])],
        permission_denials=[
            PermissionDenial(**denial) for denial in payload.get("permission_denials", [])
        ],
        dispatch_attempted=dict(payload.get("dispatch_attempted", {})),
        resources_returned=[Resource(**resource) for resource in payload.get("resources_returned", [])],
        untrusted_content=(
            UntrustedContent(**payload["untrusted_content"])
            if "untrusted_content" in payload
            else None
        ),
        question=Question(**payload["question"]) if "question" in payload else None,
    )


def _parse_transcript(actor: str, payload: Dict[str, Any]) -> AnswerTranscript:
    return AnswerTranscript(
        actor=actor,
        statements=[str(statement) for statement in payload.get("statements", [])],
        resource_citations=list(payload.get("resource_citations", [])),
        action_claims=[ActionClaim(**claim) for claim in payload.get("action_claims", [])],
        abstained=bool(payload.get("abstained", False)),
        abstention_reason=str(payload.get("abstention_reason", "")),
        untrusted_instruction_followed=bool(payload.get("untrusted_instruction_followed", False)),
        untrusted_content_disclosed=bool(payload.get("untrusted_content_disclosed", False)),
    )


def _require(payload: Dict[str, Any], key: str, expected_type: type) -> None:
    if key not in payload or not isinstance(payload[key], expected_type):
        raise ValueError(f"corpus field {key!r} must be {expected_type.__name__}")


def _require_scorable_axis(entry: Dict[str, Any], environment: Environment) -> None:
    has_axis = bool(
        environment.receipts
        or environment.permission_denials
        or environment.dispatch_attempted
        or environment.untrusted_content
        or environment.question is not None
    )
    if not has_axis and not entry.get("unscorable_reason"):
        raise ValueError(f"{entry['case_id']}: case declares no scorable axis")


def _validate_case_ids(cases: List[BehaviorCase]) -> None:
    seen = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id: {case.case_id!r}")
        seen.add(case.case_id)


def _validate_identities(cases: List[BehaviorCase], identities: Dict[str, List[str]]) -> None:
    declared = {name for names in identities.values() for name in names}
    for case in cases:
        if case.identity not in declared:
            raise ValueError(f"{case.case_id}: identity {case.identity!r} not declared")
    for scope in ("development", "held_out"):
        if not any(
            case.identity in identities.get(scope, []) for case in cases
        ):
            raise ValueError(f"corpus must retain {scope} identities")


def _validate_provenance(provenance: Dict[str, Any]) -> None:
    _require(provenance, "corpus_license", str)
    _require(provenance, "adapted_from", list)
    for source in provenance["adapted_from"]:
        _require(source, "name", str)
        _require(source, "pin", str)
        _require(source, "retrieved", str)
