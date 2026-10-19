"""Tests for clinical_diagnostic.report and run_record: aggregate summaries, failure and
gate rendering, review honesty, and the reproducibility record's identity and artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinical_diagnostic.models import (
    Agreement,
    AnswerSource,
    CaseResult,
    FailureCode,
    InfrastructureFailure,
    LabelSource,
    Partition,
    ReviewStatus,
    Score,
    Setting,
)
from clinical_diagnostic.report import render_markdown, summarize
from clinical_diagnostic.run_record import RunIdentity, RunRecord, canonical_json


def identity(**over):
    fields = dict(
        mode="frozen",
        corpus={
            "file_digest": "a" * 64,
            "content_digest": "b" * 64,
            "license_name": "Apache-2.0",
            "asset": {"sha256": "c" * 64, "license": "licensed", "retrieved": "2026-10-06"},
        },
        producers=("frozen_baseline", "frozen_candidate"),
        grader="marker-grader/1",
        config={"limits": {}},
        models={"frozen_candidate": "frozen-corpus/candidate"},
    )
    fields.update(over)
    return RunIdentity(**fields)


def scored(source, normalized=1.0, harmful=0, missed_required=0, missed_omissions=0,
           agreement=None, partition=Partition.DEVELOPMENT):
    return CaseResult(
        "case-1", partition, Setting.CONSULTATION, "primary care",
        ReviewStatus.DRAFT, LabelSource.AUTHOR_DRAFT, source, "d" * 64, "scored",
        score=Score(
            total_points=normalized, possible_points=1.0, normalized=normalized,
            band="excellent" if normalized >= 0.9 else "poor",
            met_required=1 - missed_required, missed_required=missed_required,
            met_harmful=harmful, harmful_criteria=1, met_abstention=1,
            abstention_criteria=1, met_omissions=1 - missed_omissions,
            missed_omissions=missed_omissions,
        ),
        agreement=agreement,
    )


def failure(source=AnswerSource.FROZEN_CANDIDATE):
    return CaseResult(
        "case-2", Partition.HELD_OUT, Setting.RESEARCH, "primary care",
        ReviewStatus.DRAFT, LabelSource.NONE, source, "", "infrastructure_failure",
        failure=InfrastructureFailure(FailureCode.INVALID_GRADE, "bad | grade"),
    )


def test_summary_separates_infrastructure_failures_and_counts_agreement():
    results = [
        scored(AnswerSource.FROZEN_CANDIDATE, agreement=Agreement(2, 1, 3)),
        scored(AnswerSource.FROZEN_BASELINE, normalized=-0.5),
        failure(),
    ]
    summary = summarize(results, {"draft": 3, "clinician_reviewed": 0})
    assert summary["scored"] == 2
    assert summary["infrastructure_failures"] == 1
    assert summary["failure_codes"] == {"invalid_grade": 1}
    assert summary["by_source"]["frozen_candidate"]["scored"] == 1
    assert summary["by_source"]["frozen_candidate"]["agreement"]["unknown"] == 3
    assert summary["by_source"]["frozen_baseline"]["mean_normalized"] == -0.5
    assert summary["by_partition"]["development"]["frozen_candidate"]["mean_normalized"] == 1.0
    assert summary["by_partition"]["held_out"]["frozen_candidate"]["scored"] == 0
    assert summary["by_setting"] == {"consultation": 2}
    assert summary["clinician_reviewed"] == 0


def test_summary_with_no_results_is_empty_not_zero_scored():
    summary = summarize([], {})
    assert summary["scored"] == 0
    assert summary["by_source"] == {}
    assert summary["failure_codes"] == {}
    assert summary["by_partition"] == {}


def test_render_includes_failure_and_gate_sections():
    record = RunRecord(run_id="r1", identity=identity(), review_counts={"draft": 2})
    record.results = [scored(AnswerSource.FROZEN_CANDIDATE), failure()]
    record.summary = summarize(record.results, record.review_counts)
    record.gate_offenders = ("candidate is incomplete",)
    report = render_markdown(record)
    assert "Invalid" not in report
    assert "| invalid_grade | 1 |" in report
    assert "bad \\| grade" in report
    assert "Frozen regression gate" in report
    assert "candidate is incomplete" in report
    assert "No clinician review is recorded" in report
    assert "frozen_baseline" in report


def test_render_without_asset_or_failures_is_explicit():
    record = RunRecord(
        run_id="r2",
        identity=identity(corpus={"file_digest": "", "content_digest": "",
                                  "license_name": "licensed"}),
        review_counts={"clinician_reviewed": 1},
    )
    record.results = [scored(AnswerSource.FROZEN_CANDIDATE)]
    record.summary = summarize(record.results, record.review_counts)
    report = render_markdown(record)
    assert "Reviewed Professional asset" not in report
    assert "None." in report
    assert "No clinician review is recorded" not in report


def test_render_includes_a_review_overlay_digest():
    record = RunRecord(
        run_id="r4",
        identity=identity(corpus={"file_digest": "", "content_digest": "",
                                  "license_name": "licensed",
                                  "review_file_digest": "d" * 64}),
        review_counts={"clinician_reviewed": 1},
    )
    record.results = [scored(AnswerSource.FROZEN_CANDIDATE)]
    record.summary = summarize(record.results, record.review_counts)
    assert "Review overlay `dddddddddddddddd`" in render_markdown(record)


def test_render_handles_missing_summary_entries():
    record = RunRecord(run_id="r3", identity=identity(), review_counts={})
    record.summary = {}
    report = render_markdown(record)
    assert "| frozen_baseline | 0 | 0 | — |" in report
    assert "Partition means" in report


def test_run_identity_validates_and_digests_configuration():
    with pytest.raises(ValueError, match="mode"):
        identity(mode="other")
    with pytest.raises(ValueError, match="producer"):
        identity(producers=())
    with pytest.raises(ValueError, match="grader"):
        identity(grader="")
    first = identity()
    second = identity(config={"limits": {"max_calls": 1}})
    assert first.config_digest != second.config_digest
    assert first.to_dict()["models"]["frozen_candidate"] == "frozen-corpus/candidate"
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_run_record_writes_and_refuses_unsafe_run_ids(tmp_path):
    record = RunRecord(run_id="safe-run", identity=identity())
    path = record.write_json(str(tmp_path))
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["run_id"] == "safe-run"
    assert payload["identity"]["config_digest"]
    with pytest.raises(ValueError, match="identifier"):
        RunRecord(run_id="../escape", identity=identity()).write_json(str(tmp_path))
    with pytest.raises(ValueError, match="identifier"):
        RunRecord(run_id="", identity=identity()).write_json(str(tmp_path))