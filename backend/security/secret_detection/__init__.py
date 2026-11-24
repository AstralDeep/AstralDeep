"""Contextual secret detection and benchmark evaluation module."""

from security.secret_detection.detector import (
    DetectionResult,
    SecretDetector,
    SecretFinding,
    StreamedSecretBuffer,
)
from security.secret_detection.evaluator import (
    BenchmarkEvaluator,
    EvaluationReport,
    MetricSummary,
)
from security.secret_detection.patterns import (
    calculate_shannon_entropy,
    is_clinical_exemption,
)

__all__ = [
    "SecretDetector",
    "StreamedSecretBuffer",
    "SecretFinding",
    "DetectionResult",
    "BenchmarkEvaluator",
    "EvaluationReport",
    "MetricSummary",
    "calculate_shannon_entropy",
    "is_clinical_exemption",
]
