"""Benchmark Evaluator for Contextual Privacy Filter vs Baseline Redactor.

Fulfills Acceptance Checks 2 and 5 (Issue #318):
- Evaluates baseline pattern redactor vs union-of-detections
- Measures recall across names, addresses, MRNs, dates, multilingual entities
- Measures clinical meaning preservation rate (over-redaction on negative controls)
- Profiles local latency, throughput, and hardware/configuration metrics
"""

from __future__ import annotations

import platform
import statistics
import time
from typing import Any, Callable, Dict, List

from .corpus import SYNTHETIC_CLINICAL_CORPUS, ClinicalTestCase
from .engine import ContextualPrivacyEngine
from .types import FilterStatus, SafeRedactionResult


class PrivacyFilterEvaluator:
    """Evaluates privacy filtering accuracy, safety, and performance."""

    def __init__(self, engine: ContextualPrivacyEngine | None = None) -> None:
        self.engine = engine or ContextualPrivacyEngine()

    def run_benchmark(self, baseline_fn: Callable[[str], str]) -> Dict[str, Any]:
        """Run benchmark across synthetic clinical corpus.

        Compares baseline pattern redactor against union-of-detections.
        """
        positive_cases = [tc for tc in SYNTHETIC_CLINICAL_CORPUS if not tc.is_negative_clinical_control]
        negative_cases = [tc for tc in SYNTHETIC_CLINICAL_CORPUS if tc.is_negative_clinical_control]

        latencies_ms: List[float] = []
        total_bytes = 0

        # 1. Baseline Evaluation
        baseline_pos_hits = 0
        baseline_neg_over_redacted = 0

        for tc in positive_cases:
            res = baseline_fn(tc.text)
            # Baseline is considered a hit if it redacted something
            if "[REDACTED" in res:
                baseline_pos_hits += 1

        for tc in negative_cases:
            res = baseline_fn(tc.text)
            if "[REDACTED" in res:
                baseline_neg_over_redacted += 1

        # 2. Union Evaluation
        union_pos_hits = 0
        union_neg_over_redacted = 0
        category_detections: Dict[str, int] = {}

        for tc in positive_cases:
            t0 = time.perf_counter()
            base_out = baseline_fn(tc.text)
            ctx_res = self.engine.filter_text(base_out)
            elapsed = (time.perf_counter() - t0) * 1000
            latencies_ms.append(elapsed)
            total_bytes += len(tc.text.encode("utf-8"))

            if ctx_res.status == FilterStatus.REDACTED or "[REDACTED" in base_out:
                union_pos_hits += 1

            for cat, count in ctx_res.safe_counts.items():
                category_detections[cat] = category_detections.get(cat, 0) + count

        for tc in negative_cases:
            t0 = time.perf_counter()
            base_out = baseline_fn(tc.text)
            ctx_res = self.engine.filter_text(base_out)
            elapsed = (time.perf_counter() - t0) * 1000
            latencies_ms.append(elapsed)
            total_bytes += len(tc.text.encode("utf-8"))

            # Check if protected clinical terms were mistakenly redacted
            if ctx_res.status == FilterStatus.REDACTED or "[REDACTED" in base_out:
                union_neg_over_redacted += 1

        # Calculate metrics
        baseline_recall = baseline_pos_hits / len(positive_cases) if positive_cases else 0.0
        union_recall = union_pos_hits / len(positive_cases) if positive_cases else 0.0
        recall_boost = (union_recall - baseline_recall) / baseline_recall * 100 if baseline_recall > 0 else 0.0

        clinical_meaning_loss = (union_neg_over_redacted / len(negative_cases)) * 100 if negative_cases else 0.0

        avg_latency = statistics.mean(latencies_ms) if latencies_ms else 0.0
        p50_latency = statistics.median(latencies_ms) if latencies_ms else 0.0
        p95_latency = (
            sorted(latencies_ms)[int(len(latencies_ms) * 0.95)]
            if latencies_ms
            else 0.0
        )

        total_time_s = sum(latencies_ms) / 1000.0
        throughput_kb_s = (total_bytes / 1024.0) / total_time_s if total_time_s > 0 else 0.0

        # Recommendation logic
        go_recommendation = (
            union_recall >= 0.95
            and clinical_meaning_loss == 0.0
            and p95_latency < 10.0
        )

        return {
            "metrics": {
                "positive_cases_count": len(positive_cases),
                "negative_controls_count": len(negative_cases),
                "baseline_recall": round(baseline_recall * 100, 1),
                "union_recall": round(union_recall * 100, 1),
                "recall_boost_percent": round(recall_boost, 1),
                "clinical_meaning_loss_percent": round(clinical_meaning_loss, 1),
                "category_detections": category_detections,
            },
            "performance": {
                "avg_latency_ms": round(avg_latency, 3),
                "p50_latency_ms": round(p50_latency, 3),
                "p95_latency_ms": round(p95_latency, 3),
                "throughput_kb_s": round(throughput_kb_s, 1),
            },
            "hardware_disclosure": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "python_version": platform.python_version(),
            },
            "recommendation": "GO" if go_recommendation else "NO-GO",
        }
