"""Comparative evaluation engine between baseline and enhanced secret detectors.

Runs deterministic benchmarks across synthetic corpora, computes precision, recall,
and clinical meaning preservation metrics, and generates an inert evaluation report.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

from security.secret_detection.corpus import (
    CORPUS_VERSION,
    SYNTHETIC_EDGE_CASES,
    SYNTHETIC_NEGATIVES,
    SYNTHETIC_POSITIVES,
    TestCase,
    compute_corpus_digest,
    get_all_test_cases,
)
from security.secret_detection.detector import SecretDetector
from security.secret_detection.patterns import LICENSE_NOTICE, SOURCE_PIN
from shared.phi_redactor import SECRET_VALUE_PATTERNS


class BaselineDetector:
    """Represents the existing pattern-only detector in backend/shared/phi_redactor.py."""

    def __init__(self):
        self._patterns = SECRET_VALUE_PATTERNS

    def scan_and_redact(self, text: str) -> bool:
        """Return True if any baseline pattern matches."""
        if not text:
            return False
        for pattern, _ in self._patterns:
            if pattern.search(text):
                return True
        return False


@dataclass(frozen=True, slots=True)
class MetricSummary:
    detector_name: str
    total_cases: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float
    recall: float
    f1_score: float
    clinical_meaning_loss_rate: float
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    timestamp: str
    corpus_version: str
    corpus_digest: str
    source_attribution: str
    license_notice: str
    baseline_metrics: MetricSummary
    enhanced_metrics: MetricSummary
    incremental_recall_gain: float
    incremental_precision_gain: float
    adoption_recommendation: str


class BenchmarkEvaluator:
    """Executes comparative evaluation across all benchmark corpora."""

    def __init__(self):
        self._baseline = BaselineDetector()
        self._enhanced = SecretDetector()

    def evaluate(self) -> EvaluationReport:
        all_cases = get_all_test_cases()
        corpus_digest = compute_corpus_digest()

        # Evaluate Baseline
        start_time = time.perf_counter()
        base_tp = base_fp = base_tn = base_fn = 0
        for tc in all_cases:
            detected = self._baseline.scan_and_redact(tc.text)
            if tc.is_positive:
                if detected:
                    base_tp += 1
                else:
                    base_fn += 1
            else:
                if detected:
                    base_fp += 1
                else:
                    base_tn += 1
        base_elapsed = (time.perf_counter() - start_time) * 1000.0

        base_prec = round(base_tp / (base_tp + base_fp), 4) if (base_tp + base_fp) > 0 else 0.0
        base_rec = round(base_tp / (base_tp + base_fn), 4) if (base_tp + base_fn) > 0 else 0.0
        base_f1 = (
            round(2 * base_prec * base_rec / (base_prec + base_rec), 4)
            if (base_prec + base_rec) > 0
            else 0.0
        )
        base_cml = round(base_fp / (base_fp + base_tn), 4) if (base_fp + base_tn) > 0 else 0.0

        base_summary = MetricSummary(
            detector_name="Baseline (phi_redactor current regexes)",
            total_cases=len(all_cases),
            true_positives=base_tp,
            false_positives=base_fp,
            true_negatives=base_tn,
            false_negatives=base_fn,
            precision=base_prec,
            recall=base_rec,
            f1_score=base_f1,
            clinical_meaning_loss_rate=base_cml,
            elapsed_ms=round(base_elapsed, 2),
        )

        # Evaluate Enhanced
        start_time = time.perf_counter()
        enh_tp = enh_fp = enh_tn = enh_fn = 0
        for tc in all_cases:
            res = self._enhanced.scan_and_redact(tc.text)
            detected = res.has_secrets
            if tc.is_positive:
                if detected:
                    enh_tp += 1
                else:
                    enh_fn += 1
            else:
                if detected:
                    enh_fp += 1
                else:
                    enh_tn += 1
        enh_elapsed = (time.perf_counter() - start_time) * 1000.0

        enh_prec = round(enh_tp / (enh_tp + enh_fp), 4) if (enh_tp + enh_fp) > 0 else 0.0
        enh_rec = round(enh_tp / (enh_tp + enh_fn), 4) if (enh_tp + enh_fn) > 0 else 0.0
        enh_f1 = (
            round(2 * enh_prec * enh_rec / (enh_prec + enh_rec), 4)
            if (enh_prec + enh_rec) > 0
            else 0.0
        )
        enh_cml = round(enh_fp / (enh_fp + enh_tn), 4) if (enh_fp + enh_tn) > 0 else 0.0

        enh_summary = MetricSummary(
            detector_name="Enhanced (Contextual SecretDetector)",
            total_cases=len(all_cases),
            true_positives=enh_tp,
            false_positives=enh_fp,
            true_negatives=enh_tn,
            false_negatives=enh_fn,
            precision=enh_prec,
            recall=enh_rec,
            f1_score=enh_f1,
            clinical_meaning_loss_rate=enh_cml,
            elapsed_ms=round(enh_elapsed, 2),
        )

        recall_gain = round(enh_rec - base_rec, 4)
        precision_gain = round(enh_prec - base_prec, 4)

        if enh_rec >= base_rec and enh_cml == 0.0:
            rec_text = "ADOPT: Enhanced detector achieves higher recall with 0% clinical meaning loss."
        else:
            rec_text = "RETAIN_BASELINE: Enhanced detector did not demonstrate net positive qualification."

        return EvaluationReport(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            corpus_version=CORPUS_VERSION,
            corpus_digest=corpus_digest,
            source_attribution=SOURCE_PIN,
            license_notice=LICENSE_NOTICE,
            baseline_metrics=base_summary,
            enhanced_metrics=enh_summary,
            incremental_recall_gain=recall_gain,
            incremental_precision_gain=precision_gain,
            adoption_recommendation=rec_text,
        )
