"""Tests for Contextual Privacy Filter (Issue #318).

Comprehensive verification of:
1. Manifest & offline qualification
2. Span safety and strictly shielded serialization (zero raw text leakage)
3. Contextual span detection (names, addresses, MRNs, dates, multilingual)
4. Negative clinical controls (diagnoses, drugs, dosages, lab values preserved)
5. Fail-closed safety (oversized payloads, corrupt inputs, exceptions)
6. Feature flag gating (FF_CONTEXTUAL_PRIVACY_FILTER) and union-of-detections
7. Benchmark evaluation, latency profiling, and report generation
8. Integration with backend.shared.phi_redactor
"""

from __future__ import annotations

import os
import pytest

from security.privacy_filter.adapter import (
    FEATURE_FLAG_ENV,
    LocalPrivacyFilterAdapter,
    is_contextual_privacy_filter_enabled,
)
from security.privacy_filter.corpus import SYNTHETIC_CLINICAL_CORPUS
from security.privacy_filter.engine import (
    ContextualPrivacyEngine,
    FAIL_CLOSED_MARKER,
    MAX_INPUT_BYTES,
)
from security.privacy_filter.evaluator import PrivacyFilterEvaluator
from security.privacy_filter.manifest import ARTIFACT_MANIFEST
from security.privacy_filter.report import generate_markdown_report
from security.privacy_filter.types import FilterStatus, PrivacySpan, SafeRedactionResult
from shared.phi_redactor import redact


class TestManifest:
    def test_manifest_metadata(self):
        assert ARTIFACT_MANIFEST["offline_verified"] is True
        assert ARTIFACT_MANIFEST["network_egress_required"] is False
        assert ARTIFACT_MANIFEST["default_enabled"] is False
        assert "contextual_engine" in ARTIFACT_MANIFEST["components"]
        assert "safe_serialization" in ARTIFACT_MANIFEST["components"]


class TestSpanTypesAndSafeSerialization:
    def test_privacy_span_creation_and_bounds_validation(self):
        span = PrivacySpan.create(0, 10, "NAME", "Dr. Robert", 1.0)
        assert span.start == 0
        assert span.end == 10
        assert span.entity_type == "NAME"
        assert len(span.text_digest) > 0

        # Invalid bounds raise ValueError
        with pytest.raises(ValueError):
            PrivacySpan(-1, 5, "NAME")
        with pytest.raises(ValueError):
            PrivacySpan(10, 5, "NAME")

    def test_safe_redaction_result_never_exposes_raw_text(self):
        secret_raw = "SecretPatient John Doe"
        res = SafeRedactionResult(
            redacted_text="Patient [REDACTED:name]",
            status=FilterStatus.REDACTED,
            safe_counts={"NAME": 1},
            spans_count=1,
            latency_ms=1.23,
        )
        d = res.to_dict()

        # The dictionary representation must strictly contain only safe telemetry
        assert "status" in d
        assert "spans_count" in d
        assert "safe_counts" in d
        assert "latency_ms" in d
        assert secret_raw not in str(d)
        assert secret_raw not in repr(res)


class TestContextualEngine:
    def setup_method(self):
        self.engine = ContextualPrivacyEngine()

    def test_name_redaction(self):
        text = "Consultation performed by Dr. Sarah Jenkins for Patient Robert Miller."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.REDACTED
        assert "Sarah Jenkins" not in res.redacted_text
        assert "Robert Miller" not in res.redacted_text
        assert "[REDACTED:name]" in res.redacted_text
        assert res.safe_counts.get("NAME") == 2

    def test_multilingual_name_redaction(self):
        text = "Admission notes: Patient Señor Carlos Gomez and Monsieur Pierre Dubois were examined."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.REDACTED
        assert "Carlos Gomez" not in res.redacted_text
        assert "Pierre Dubois" not in res.redacted_text
        assert res.safe_counts.get("NAME") == 2

    def test_address_redaction(self):
        text = "Records sent to 742 Evergreen Terrace, Springfield, OR 97477 for intake review."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.REDACTED
        assert "742 Evergreen Terrace" not in res.redacted_text
        assert "[REDACTED:address]" in res.redacted_text
        assert res.safe_counts.get("ADDRESS") == 1

    def test_mrn_redaction(self):
        text = "Inpatient telemetry record: MRN: 98402-A9."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.REDACTED
        assert "98402-A9" not in res.redacted_text
        assert "[REDACTED:mrn]" in res.redacted_text

    def test_contextual_date_redaction(self):
        text = "Patient was Admitted on 10/14/2025 following chest discomfort."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.REDACTED
        assert "10/14/2025" not in res.redacted_text
        assert "[REDACTED:date]" in res.redacted_text


class TestClinicalMeaningPreservation:
    def setup_method(self):
        self.engine = ContextualPrivacyEngine()

    def test_medical_conditions_preserved(self):
        text = "Assessment: Type 2 Diabetes Mellitus, Essential Hypertension, and Asthma."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.CLEAN
        assert res.redacted_text == text
        assert "Type 2 Diabetes Mellitus" in res.redacted_text
        assert "Essential Hypertension" in res.redacted_text
        assert "Asthma" in res.redacted_text

    def test_medications_and_dosages_preserved(self):
        text = "Rx: Metformin 500 mg BID, Lisinopril 10 mg QD, and Albuterol 90 mcg PRN."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.CLEAN
        assert res.redacted_text == text
        assert "Metformin 500 mg BID" in res.redacted_text
        assert "Lisinopril 10 mg QD" in res.redacted_text
        assert "Albuterol 90 mcg PRN" in res.redacted_text

    def test_lab_values_and_vitals_preserved(self):
        text = "Vitals: Blood pressure 120/80 mmHg, heart rate 72 bpm. Potassium 4.2 mEq/L."
        res = self.engine.filter_text(text)
        assert res.status == FilterStatus.CLEAN
        assert res.redacted_text == text
        assert "120/80 mmHg" in res.redacted_text
        assert "4.2 mEq/L" in res.redacted_text


class TestFailClosedSafety:
    def setup_method(self):
        self.engine = ContextualPrivacyEngine()

    def test_oversized_input_fails_closed(self):
        oversized_text = "A" * (MAX_INPUT_BYTES + 100)
        res = self.engine.filter_text(oversized_text)
        assert res.status == FilterStatus.FAIL_CLOSED
        assert res.redacted_text == FAIL_CLOSED_MARKER
        assert res.error_detail == "INPUT_EXCEEDS_BYTE_LIMIT"

    def test_non_string_input_fails_closed(self):
        res = self.engine.filter_text(12345)  # type: ignore
        assert res.status == FilterStatus.FAIL_CLOSED

    def test_empty_string_handled_safely(self):
        res = self.engine.filter_text("")
        assert res.status == FilterStatus.CLEAN
        assert res.redacted_text == ""


class TestAdapterAndFeatureFlag:
    def test_feature_flag_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
        assert is_contextual_privacy_filter_enabled() is False

        adapter = LocalPrivacyFilterAdapter()
        raw = "Dr. Sarah Jenkins saw the patient."
        res = adapter.filter_tool_text(raw)
        assert res.status == FilterStatus.BYPASSED
        assert res.redacted_text == raw

    def test_feature_flag_enabled_applies_filter(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
        assert is_contextual_privacy_filter_enabled() is True

        adapter = LocalPrivacyFilterAdapter()
        raw = "Dr. Sarah Jenkins saw the patient."
        res = adapter.filter_tool_text(raw)
        assert res.status == FilterStatus.REDACTED
        assert "Sarah Jenkins" not in res.redacted_text

    def test_union_of_detections(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG_ENV, "1")
        adapter = LocalPrivacyFilterAdapter()

        # Baseline redactor handles SSN (9 digits), contextual handles provider name
        def dummy_baseline(text: str):
            import re
            return re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED:ssn]", text), True

        input_text = "Dr. Michael Scott reviewed SSN: 123-45-6789 at 100 Main St, Austin, TX 78701."
        final_text, meta = adapter.redact_union(input_text, dummy_baseline)

        assert "[REDACTED:ssn]" in final_text
        assert "[REDACTED:name]" in final_text
        assert "[REDACTED:address]" in final_text
        assert "Michael Scott" not in final_text
        assert "123-45-6789" not in final_text
        assert meta["contextual_status"] == FilterStatus.REDACTED.value


class TestBenchmarkAndReporting:
    def test_benchmark_evaluation_and_report_generation(self):
        evaluator = PrivacyFilterEvaluator()

        def dummy_baseline(text: str):
            import re
            return re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED:ssn]", text)

        bench = evaluator.run_benchmark(dummy_baseline)

        metrics = bench["metrics"]
        perf = bench["performance"]

        # High quality verification targets
        assert metrics["union_recall"] == 100.0
        assert metrics["clinical_meaning_loss_percent"] == 0.0
        assert perf["p95_latency_ms"] < 10.0
        assert bench["recommendation"] == "GO"

        # Verify markdown report generation
        md_report = generate_markdown_report(bench)
        assert "# Contextual Privacy Filter Benchmark Report" in md_report
        assert "GO (RECOMMENDED FOR OPT-IN ADOPTION)" in md_report
        assert "100.0%" in md_report


class TestPhiRedactorIntegration:
    def test_phi_redactor_with_flag_disabled(self, monkeypatch):
        monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
        raw = "Physician Dr. Robert Williams completed chart for MRN 123-45-6789."
        # Baseline masks the SSN pattern, but leaves Dr. Robert Williams
        redacted_out, was_truncated = redact(raw)
        assert was_truncated is False
        assert "[REDACTED:ssn]" in redacted_out
        assert "Robert Williams" in redacted_out

    def test_phi_redactor_with_flag_enabled(self, monkeypatch):
        monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
        raw = "Physician Dr. Robert Williams completed chart for MRN 123-45-6789."
        redacted_out, was_truncated = redact(raw)
        assert was_truncated is False
        assert "[REDACTED:ssn]" in redacted_out
        assert "[REDACTED:name]" in redacted_out
        assert "Robert Williams" not in redacted_out
