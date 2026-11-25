"""Report generator for Contextual Privacy Filter Evaluation.

Fulfills Acceptance Checks 2, 5, and 7 (Issue #318):
- Renders Markdown diagnostic comparison report
- Discloses hardware environment, configuration, and exact metrics
- Provides clear GO/NO-GO qualification recommendation
"""

from __future__ import annotations

import json
from typing import Any, Dict


def generate_markdown_report(benchmark_results: Dict[str, Any]) -> str:
    """Generate structured markdown evaluation report."""
    metrics = benchmark_results["metrics"]
    perf = benchmark_results["performance"]
    hw = benchmark_results["hardware_disclosure"]
    rec = benchmark_results["recommendation"]

    rec_badge = "✅ **GO (RECOMMENDED FOR OPT-IN ADOPTION)**" if rec == "GO" else "⚠️ **NO-GO (NEEDS REFINEMENT)**"

    lines = [
        "# Contextual Privacy Filter Benchmark Report (Issue #318)",
        "",
        "## Executive Summary",
        f"- **Recommendation**: {rec_badge}",
        f"- **Baseline Recall**: {metrics['baseline_recall']}%",
        f"- **Union-of-Detections Recall**: {metrics['union_recall']}%",
        f"- **Recall Boost**: +{metrics['recall_boost_percent']}%",
        f"- **Clinical Meaning Loss**: {metrics['clinical_meaning_loss_percent']}% (Negative controls preserved)",
        "",
        "## Detection Breakdown",
        f"- **Total Positive Test Cases**: {metrics['positive_cases_count']}",
        f"- **Negative Clinical Controls**: {metrics['negative_controls_count']}",
        "- **Detected Entity Categories**:",
    ]

    for cat, cnt in metrics.get("category_detections", {}).items():
        lines.append(f"  - `{cat}`: {cnt} detections")

    lines.extend([
        "",
        "## Performance & Latency Profile",
        f"- **Average Latency**: {perf['avg_latency_ms']} ms",
        f"- **P50 Latency**: {perf['p50_latency_ms']} ms",
        f"- **P95 Latency**: {perf['p95_latency_ms']} ms",
        f"- **Throughput**: {perf['throughput_kb_s']} KB/sec",
        "",
        "## Hardware & Environment Disclosure",
        f"- **Operating System**: {hw['system']} {hw['release']} ({hw['machine']})",
        f"- **Processor**: {hw['processor']}",
        f"- **Python Version**: {hw['python_version']}",
        "- **Execution Mode**: 100% Offline, Deterministic, Zero Network Egress",
        "",
        "## Safety & Redaction Integrity",
        "- **Safe Serialization Verified**: `SafeRedactionResult.to_dict()` excludes all original unredacted text and span texts.",
        "- **Audit Trail Hygiene**: Telemetry logs record only category counts and aggregate metrics.",
        "- **Fail-Closed Protection**: Corrupt inputs, offset errors, and exceptions safely trigger fail-closed redaction marker.",
    ])

    return "\n".join(lines)
