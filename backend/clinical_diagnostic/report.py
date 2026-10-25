"""Aggregates clinical diagnostic results into per-source, per-partition, failure-class,
review-status, and human/grader-agreement summaries and renders an explicitly diagnostic
markdown report that excludes infrastructure failures from every score denominator.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Mapping

from clinical_diagnostic.models import BANDS, CaseResult, Partition
from clinical_diagnostic.run_record import RunRecord
from clinical_diagnostic.scoring import is_complete


def summarize(results: Iterable[CaseResult], review_counts: Mapping[str, int]) -> Dict[str, object]:
    results = list(results)
    sources = sorted({result.answer_source.value for result in results})
    by_source: Dict[str, dict] = {}
    for source in sources:
        selected = [result for result in results if result.answer_source.value == source]
        scored = [result for result in selected if result.status == "scored"]
        failures = [result for result in selected if result.status == "infrastructure_failure"]
        values = [result.score.normalized for result in scored if result.score is not None]
        bands = {band: 0 for band in BANDS}
        for result in scored:
            if result.score is not None:
                bands[result.score.band] = bands.get(result.score.band, 0) + 1
        by_source[source] = {
            "scored": len(scored),
            "infrastructure_failures": len(failures),
            "mean_normalized": (
                round(sum(value for value in values if value is not None) / len(values), 6)
                if values else None
            ),
            "bands": bands,
            "complete_cases": sum(
                1 for result in scored
                if result.score is not None and is_complete(result.score)
            ),
            "met_harmful": sum(
                result.score.met_harmful for result in scored if result.score is not None
            ),
            "missed_required": sum(
                result.score.missed_required for result in scored if result.score is not None
            ),
            "missed_omissions": sum(
                result.score.missed_omissions for result in scored if result.score is not None
            ),
            "met_abstention": sum(
                result.score.met_abstention for result in scored if result.score is not None
            ),
            "abstention_criteria": sum(
                result.score.abstention_criteria for result in scored if result.score is not None
            ),
            "agreement": _agreement(selected),
        }
    failure_codes = Counter(
        result.failure.code.value for result in results
        if result.status == "infrastructure_failure" and result.failure is not None
    )
    by_partition: Dict[str, Dict[str, dict]] = {}
    for partition in (Partition.DEVELOPMENT, Partition.HELD_OUT):
        selected = [result for result in results if result.partition is partition]
        if not selected:
            continue
        by_partition[partition.value] = {
            source: _source_partition(selected, source) for source in sources
        }
    by_setting: Dict[str, int] = Counter(
        result.setting.value for result in results if result.status == "scored"
    )
    label_sources = Counter(result.labels_source.value for result in results)
    return {
        "by_source": by_source,
        "by_partition": by_partition,
        "by_setting": dict(by_setting),
        "by_official_use_case": dict(Counter(
            result.official_use_case for result in results if result.official_use_case)),
        "label_sources": dict(sorted(label_sources.items())),
        "failure_codes": dict(sorted(failure_codes.items())),
        "review_counts": dict(review_counts),
        "clinician_reviewed": int(review_counts.get("clinician_reviewed", 0)),
        "infrastructure_failures": sum(1 for result in results
                                       if result.status == "infrastructure_failure"),
        "scored": sum(1 for result in results if result.status == "scored"),
        "unknown_review_labels": sum(
            _agreement([result])["unknown"] for result in results
        ),
    }


def _source_partition(results: List[CaseResult], source: str) -> dict:
    selected = [
        result for result in results
        if result.answer_source.value == source and result.status == "scored"
    ]
    values = [
        result.score.normalized for result in selected
        if result.score is not None and result.score.normalized is not None
    ]
    return {
        "scored": len(selected),
        "mean_normalized": (
            round(sum(values) / len(values), 6) if values else None
        ),
    }


def _agreement(results: List[CaseResult]) -> dict:
    matched = disagreed = unknown = 0
    for result in results:
        if result.agreement is None:
            continue
        matched += result.agreement.matched
        disagreed += result.agreement.disagreed
        unknown += result.agreement.unknown
    decided = matched + disagreed
    return {
        "matched": matched,
        "disagreed": disagreed,
        "unknown": unknown,
        "decided": decided,
        "rate": round(matched / decided, 6) if decided else None,
    }


def render_markdown(record: RunRecord) -> str:
    identity = record.identity
    summary = record.summary if isinstance(record.summary, dict) else {}
    by_source = summary.get("by_source", {})
    lines: List[str] = []
    lines.append("# Clinical Answer Diagnostic Report")
    lines.append("")
    lines.append(
        f"Run `{record.run_id}` · mode `{identity.mode}` · suite `{identity.suite_version}`"
        f" · schema `{identity.schema}` · config `{identity.config_digest[:16]}`"
    )
    lines.append("")
    corpus = identity.corpus
    lines.append(
        f"Corpus file `{str(corpus.get('file_digest', ''))[:16]}` · content "
        f"`{str(corpus.get('content_digest', ''))[:16]}` · license "
        f"`{corpus.get('license_name', '')}` · producers "
        f"`{', '.join(identity.producers)}` · grader `{identity.grader}`"
    )
    review_digest = str(corpus.get("review_file_digest", ""))
    if review_digest:
        lines.append(f"Review overlay `{review_digest[:16]}`")
    asset = corpus.get("asset") if isinstance(corpus.get("asset"), Mapping) else {}
    if asset:
        lines.append(
            f"Reviewed Professional asset `{str(asset.get('sha256', ''))[:16]}` · "
            f"license `{asset.get('license', '')}` · retrieved `{asset.get('retrieved', '')}`"
        )
    lines.append("")
    lines.append(
        "> This report scores visible answers against frozen synthetic rubric items. "
        "It does not measure a real model, clinical safety, physician equivalence, or "
        "benchmark improvement unless a separately recorded public run measured that "
        "exact model and configuration. Infrastructure failures are excluded from all "
        "score aggregates and are never converted into zero scores."
    )
    lines.append("")
    review = summary.get("review_counts", {})
    lines.append("## Clinical review status")
    lines.append("")
    lines.append(
        "| draft | clinician_reviewed | disputed | retired | clinician-reviewed cases |"
    )
    lines.append("|---:|---:|---:|---:|---:|")
    lines.append(
        "| {draft} | {clinician_reviewed} | {disputed} | {retired} | {clinician} |".format(
            draft=review.get("draft", 0),
            clinician_reviewed=review.get("clinician_reviewed", 0),
            disputed=review.get("disputed", 0),
            retired=review.get("retired", 0),
            clinician=summary.get("clinician_reviewed", 0),
        )
    )
    lines.append("")
    if not summary.get("clinician_reviewed"):
        lines.append(
            "No clinician review is recorded for this corpus, so this run carries no "
            "clinical-validity claim. Author-draft labels are reported only as author/grader "
            "agreement, never as clinician agreement."
        )
        lines.append("")
    lines.append("## Per-producer results")
    lines.append("")
    lines.append(
        "| Producer | scored | infrastructure failures | mean normalized | complete | "
        "met harmful | missed required | missed omission | abstention met/total |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for source in identity.producers:
        row = by_source.get(source, {})
        mean = row.get("mean_normalized")
        lines.append(
            f"| {source} | {row.get('scored', 0)} | "
            f"{row.get('infrastructure_failures', 0)} | "
            + ("—" if mean is None else f"{mean:+.3f}") + " | "
            f"{row.get('complete_cases', 0)} | {row.get('met_harmful', 0)} | "
            f"{row.get('missed_required', 0)} | {row.get('missed_omissions', 0)} | "
            f"{row.get('met_abstention', 0)}/{row.get('abstention_criteria', 0)} |"
        )
    lines.append("")
    lines.append("## Partition means (scored cases only)")
    lines.append("")
    lines.append("| Partition | " + " | ".join(identity.producers) + " |")
    lines.append("|---|" + "---:|" * len(identity.producers))
    for partition, values in summary.get("by_partition", {}).items():
        cells = []
        for source in identity.producers:
            mean = values.get(source, {}).get("mean_normalized")
            cells.append("—" if mean is None else f"{mean:+.3f}")
        lines.append(f"| {partition} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Review-label/grader agreement")
    lines.append("")
    label_sources = summary.get("label_sources", {})
    lines.append(
        "Label sources: "
        + (", ".join(f"`{source}` ({count})" for source, count in label_sources.items())
           or "none")
        + f". Unknown labels observed: {summary.get('unknown_review_labels', 0)}."
    )
    lines.append("")
    lines.append(
        "| Producer | matched | disagreed | unknown labels | decided | agreement |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|")
    for source in identity.producers:
        agreement = by_source.get(source, {}).get("agreement", {})
        rate = agreement.get("rate")
        lines.append(
            f"| {source} | {agreement.get('matched', 0)} | "
            f"{agreement.get('disagreed', 0)} | {agreement.get('unknown', 0)} | "
            f"{agreement.get('decided', 0)} | "
            + ("—" if rate is None else f"{rate:.3f}") + " |"
        )
    lines.append("")
    lines.append("## Infrastructure failures (never scored)")
    lines.append("")
    failures = summary.get("failure_codes", {})
    if failures:
        lines.append("| Failure code | count |")
        lines.append("|---|---:|")
        for code, count in failures.items():
            lines.append(f"| {code} | {count} |")
    else:
        lines.append("None.")
    lines.append("")
    failing = [result for result in record.results
               if result.status == "infrastructure_failure"]
    if failing:
        lines.append("| Case | Producer | Code | Detail |")
        lines.append("|---|---|---|---|")
        for result in failing:
            detail = result.failure.detail.replace("|", "\\|") if result.failure else ""
            lines.append(
                f"| {result.case_id} | {result.answer_source.value} | "
                f"{result.failure.code.value if result.failure else ''} | {detail} |"
            )
        lines.append("")
    if record.gate_offenders:
        lines.append("## Frozen regression gate")
        lines.append("")
        for offender in record.gate_offenders:
            lines.append(f"- {offender}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
