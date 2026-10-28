"""Frozen synthetic planning-task corpus: three disjoint partitions (train,
selection-validation, untouched final holdout) of small non-PHI decomposition cases with
independently declared labels, plus canonical digests and fail-closed partition
validation that refuses empty or overlapping partitions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

SCHEMA_VERSION = 1
FIXTURE_ID = "coordinator-planning-synthetic-v1"
PARTITION_NAMES = ("train", "validation", "holdout")
MIN_CASES_PER_PARTITION = 2


@dataclass(frozen=True)
class PlannerCase:
    case_id: str
    request: str
    available_tools: Tuple[str, ...]
    min_subtasks: int
    max_subtasks: int
    expected_tools: FrozenSet[str]
    simple: bool = False
    expect_dependency: bool = False
    forbidden_tools: FrozenSet[str] = frozenset()

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "request": self.request,
            "available_tools": list(self.available_tools),
            "min_subtasks": self.min_subtasks,
            "max_subtasks": self.max_subtasks,
            "expected_tools": sorted(self.expected_tools),
            "simple": self.simple,
            "expect_dependency": self.expect_dependency,
            "forbidden_tools": sorted(self.forbidden_tools),
        }


_TRAIN: Tuple[PlannerCase, ...] = (
    PlannerCase(
        case_id="t-simple-projects",
        request="List the projects for team Atlas.",
        available_tools=("list_projects", "fetch_record", "build_report"),
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"list_projects"}),
        simple=True,
    ),
    PlannerCase(
        case_id="t-simple-chart",
        request="Render a chart of the monthly incidents.",
        available_tools=("render_chart", "fetch_record", "list_projects"),
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"render_chart"}),
        simple=True,
    ),
    PlannerCase(
        case_id="t-pair-translate",
        request="Fetch the Q3 record and translate its summary text.",
        available_tools=("fetch_record", "translate_text", "list_projects"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"fetch_record", "translate_text"}),
        expect_dependency=True,
    ),
    PlannerCase(
        case_id="t-broad-audit",
        request=(
            "Audit the catalog, check the schedule, build the report, "
            "and list the projects."
        ),
        available_tools=(
            "search_catalog",
            "check_schedule",
            "build_report",
            "list_projects",
        ),
        min_subtasks=2,
        max_subtasks=3,
        expected_tools=frozenset({"search_catalog", "build_report"}),
    ),
    PlannerCase(
        case_id="t-broad-notes",
        request=(
            "Summarize the meeting notes, check the schedule for follow-ups, "
            "and build a report."
        ),
        available_tools=("summarize_notes", "check_schedule", "build_report", "translate_text"),
        min_subtasks=2,
        max_subtasks=4,
        expected_tools=frozenset({"summarize_notes", "build_report"}),
    ),
)

_VALIDATION: Tuple[PlannerCase, ...] = (
    PlannerCase(
        case_id="v-simple-record",
        request="Fetch the incident record for unit seven.",
        available_tools=("fetch_record", "list_projects", "build_report"),
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"fetch_record"}),
        simple=True,
    ),
    PlannerCase(
        case_id="v-broad-chart",
        request="Render a chart from the catalog and build a report.",
        available_tools=("render_chart", "search_catalog", "build_report", "fetch_record"),
        min_subtasks=2,
        max_subtasks=3,
        expected_tools=frozenset({"render_chart", "build_report"}),
    ),
    PlannerCase(
        case_id="v-broad-translate",
        request="Fetch the record, translate the text, and summarize the notes.",
        available_tools=("fetch_record", "translate_text", "summarize_notes", "build_report"),
        min_subtasks=2,
        max_subtasks=3,
        expected_tools=frozenset({"translate_text", "summarize_notes"}),
    ),
    PlannerCase(
        case_id="v-exclusion",
        request="List the projects and render a chart, but do not build any report.",
        available_tools=("list_projects", "render_chart", "build_report", "fetch_record"),
        min_subtasks=1,
        max_subtasks=3,
        expected_tools=frozenset({"list_projects", "render_chart"}),
        forbidden_tools=frozenset({"build_report"}),
    ),
)

_HOLDOUT: Tuple[PlannerCase, ...] = (
    PlannerCase(
        case_id="h-simple-schedule",
        request="Check the schedule for tomorrow.",
        available_tools=("check_schedule", "list_projects", "fetch_record"),
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"check_schedule"}),
        simple=True,
    ),
    PlannerCase(
        case_id="h-pair-report",
        request="Fetch the record and build a report from it.",
        available_tools=("fetch_record", "build_report", "translate_text"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"fetch_record", "build_report"}),
        expect_dependency=True,
    ),
    PlannerCase(
        case_id="h-broad-full",
        request=(
            "Audit the catalog, summarize the notes, translate the text, "
            "check the schedule, and build the report."
        ),
        available_tools=(
            "search_catalog",
            "summarize_notes",
            "translate_text",
            "check_schedule",
            "build_report",
        ),
        min_subtasks=2,
        max_subtasks=3,
        expected_tools=frozenset({"search_catalog", "summarize_notes", "build_report"}),
    ),
)


class FixtureError(ValueError):
    pass


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    schema_version: int
    partitions: Dict[str, Tuple[PlannerCase, ...]]
    partition_digests: Dict[str, str]
    digest: str

    def cases(self, partition: str) -> Tuple[PlannerCase, ...]:
        return self.partitions[partition]

    @property
    def holdout_input_digest(self) -> str:
        return hashlib.sha256(_canonical(sorted(
            render_case_block(case) for case in self.cases("holdout")
        )).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "holdout_input_digest": self.holdout_input_digest,
            "schema_version": self.schema_version,
            "fixture_id": self.fixture_id,
            "partition_digests": dict(self.partition_digests),
            "digest": self.digest,
            "case_counts": {
                name: len(cases) for name, cases in self.partitions.items()
            },
        }


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _partition_digest(cases: Tuple[PlannerCase, ...]) -> str:
    payload = [case.to_dict() for case in sorted(cases, key=lambda c: c.case_id)]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate(
    partitions: Dict[str, Tuple[PlannerCase, ...]], partition_names: Tuple[str, ...]
) -> None:
    for name in partition_names:
        if name not in partitions or not partitions[name]:
            raise FixtureError(f"partition {name!r} is missing or empty")
    seen: Dict[str, str] = {}
    seen_inputs: set = set()
    for name in partition_names:
        for case in partitions[name]:
            if not case.case_id or not case.request.strip():
                raise FixtureError(f"partition {name!r} contains an unlabeled case")
            if case.min_subtasks < 1 or case.max_subtasks < case.min_subtasks:
                raise FixtureError(f"case {case.case_id!r} has an invalid subtask range")
            if case.case_id in seen:
                raise FixtureError(
                    f"case {case.case_id!r} appears in {seen[case.case_id]!r} "
                    f"and {name!r}; partitions must be disjoint"
                )
            seen[case.case_id] = name
            input_text = render_case_block(case)
            if input_text in seen_inputs:
                raise FixtureError("case inputs must be disjoint even when labels or identities differ")
            seen_inputs.add(input_text)
            unknown = case.expected_tools - set(case.available_tools)
            if unknown:
                raise FixtureError(
                    f"case {case.case_id!r} expects tools outside its available list"
                )


def build_fixture(
    train: Tuple[PlannerCase, ...] = _TRAIN,
    validation: Tuple[PlannerCase, ...] = _VALIDATION,
    holdout: Tuple[PlannerCase, ...] = _HOLDOUT,
) -> Fixture:
    partitions = {
        "train": tuple(train),
        "validation": tuple(validation),
        "holdout": tuple(holdout),
    }
    _validate(partitions, PARTITION_NAMES)
    for name, cases in partitions.items():
        if len(cases) < MIN_CASES_PER_PARTITION:
            raise FixtureError(
                f"partition {name!r} needs at least {MIN_CASES_PER_PARTITION} cases"
            )
    digests = {
        name: _partition_digest(cases) for name, cases in partitions.items()
    }
    identity = _canonical(
        {
            "schema_version": SCHEMA_VERSION,
            "fixture_id": FIXTURE_ID,
            "partition_digests": digests,
        }
    )
    return Fixture(
        fixture_id=FIXTURE_ID,
        schema_version=SCHEMA_VERSION,
        partitions=partitions,
        partition_digests=digests,
        digest=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )


def render_case_block(case: PlannerCase) -> str:
    lines = ["[available tools]"]
    lines.extend(case.available_tools)
    lines.append("")
    lines.append("[request]")
    lines.append(case.request)
    return "\n".join(lines)


def case_ids(cases: List[PlannerCase]) -> List[str]:
    return [case.case_id for case in cases]


def load_holdout(path: str | Path) -> Fixture:
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise FixtureError("holdout file exceeds 65536 bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
        if (type(payload) is not dict or set(payload) != {"schema_version", "holdout"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION
                or type(payload["holdout"]) is not list or not 2 <= len(payload["holdout"]) <= 32):
            raise ValueError
        cases = []
        fields = set(PlannerCase.__dataclass_fields__)
        for item in payload["holdout"]:
            if type(item) is not dict or set(item) != fields:
                raise ValueError
            for name, maximum in (("case_id", 128), ("request", 4000)):
                if type(item[name]) is not str or not 1 <= len(item[name]) <= maximum:
                    raise ValueError
            for name in ("min_subtasks", "max_subtasks"):
                if type(item[name]) is not int or not 1 <= item[name] <= 32:
                    raise ValueError
            if any(type(item[name]) is not bool for name in ("simple", "expect_dependency")):
                raise ValueError
            values = dict(item)
            for name in ("available_tools", "expected_tools", "forbidden_tools"):
                tools = item[name]
                if (type(tools) is not list or len(tools) > 32 or any(
                        type(tool) is not str or not 1 <= len(tool) <= 128 for tool in tools)):
                    raise ValueError
                values[name] = tuple(tools) if name == "available_tools" else frozenset(tools)
            cases.append(PlannerCase(**values))
        return build_fixture(holdout=tuple(cases))
    except (ValueError, TypeError, KeyError):
        raise FixtureError("invalid explicit synthetic holdout fixture") from None
