"""Comprehensive test suite for contextual secret detection patterns and evaluation.

Validates:
- Detection of expanded provider secrets (OpenAI project/admin, Anthropic, Google, Slack, AWS, DB URIs)
- 100% preservation of clinical terminology, medical codes, and genomic sequences (0% Meaning Loss)
- Shannon entropy calculations and boundary conditions
- Streamed chunk buffer boundary detection
- Safe fail-closed error handling without plaintext secret logging or traceback leaks
- End-to-end integration with shared.phi_redactor.redact
- Benchmark evaluation reporting and provenance integrity
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from security.secret_detection.corpus import (
    SYNTHETIC_EDGE_CASES,
    SYNTHETIC_NEGATIVES,
    SYNTHETIC_POSITIVES,
    compute_corpus_digest,
    get_all_test_cases,
)
from security.secret_detection.detector import (
    DetectionResult,
    SecretDetector,
    SecretFinding,
    StreamedSecretBuffer,
)
from security.secret_detection.evaluator import BenchmarkEvaluator, EvaluationReport
from security.secret_detection.patterns import (
    calculate_shannon_entropy,
    is_clinical_exemption,
)
from security.secret_detection.report import (
    export_report_json,
    export_report_markdown,
    format_report_markdown,
)
from shared.phi_redactor import redact


class TestSyntheticPositiveDetection:
    """Verifies that all synthetic positive secret categories are detected and redacted."""

    @pytest.fixture
    def detector(self) -> SecretDetector:
        return SecretDetector()

    def test_all_synthetic_positives_detected(self, detector: SecretDetector):
        for tc in SYNTHETIC_POSITIVES:
            res = detector.scan_and_redact(tc.text)
            assert res.has_secrets is True, f"Failed to detect secret in case {tc.case_id}: {tc.text}"
            assert len(res.findings) > 0
            assert res.redacted_text != tc.text

    def test_openai_project_key_redaction(self, detector: SecretDetector):
        text = "Key " + "sk-proj-" + "1234567890abcdefABCDEF1234567890abcdefABCDEF is active"
        res = detector.scan_and_redact(text)
        assert res.has_secrets is True
        assert "[REDACTED:openai_key]" in res.redacted_text
        assert "sk-proj-" not in res.redacted_text

    def test_anthropic_key_redaction(self, detector: SecretDetector):
        text = "claude_key = " + "sk-ant-" + "api03-abcdef1234567890ABCDEF1234567890-test123456"
        res = detector.scan_and_redact(text)
        assert res.has_secrets is True
        assert "[REDACTED:anthropic_key]" in res.redacted_text
        assert "sk-ant-" not in res.redacted_text

    def test_google_ai_key_redaction(self, detector: SecretDetector):
        text = "api_key = " + "AIzaSy" + "D1234567890abcdefABCDEF123456789"
        res = detector.scan_and_redact(text)
        assert res.has_secrets is True
        assert "[REDACTED:google_key]" in res.redacted_text
        assert "AIzaSy" not in res.redacted_text

    def test_database_uri_password_redaction(self, detector: SecretDetector):
        text = "Connect to postgresql://admin_user:SuperSecretP@ss999@db.internal:5432/patients"
        res = detector.scan_and_redact(text)
        assert res.has_secrets is True
        assert "admin_user" in res.redacted_text
        assert "SuperSecretP@ss999" not in res.redacted_text
        assert "[REDACTED:pwd]" in res.redacted_text
        assert "db.internal:5432/patients" in res.redacted_text


class TestClinicalMeaningPreservation:
    """Verifies 100% preservation of clinical meaning and zero false positives."""

    @pytest.fixture
    def detector(self) -> SecretDetector:
        return SecretDetector()

    def test_all_synthetic_negatives_preserved_untouched(self, detector: SecretDetector):
        for tc in SYNTHETIC_NEGATIVES:
            res = detector.scan_and_redact(tc.text)
            assert res.has_secrets is False, (
                f"False positive detected in clinical case {tc.case_id}: {res.redacted_text}"
            )
            assert res.redacted_text == tc.text

    def test_clinical_secretion_words_not_redacted(self, detector: SecretDetector):
        clinical_phrases = [
            "Bronchial secretion increased following procedure.",
            "Endocrine secretions monitored hourly.",
            "Secretin stimulation test returned normal pancreatic values.",
            "Endometrium in late secretory phase.",
            "Gastric secretagogue administered per protocol.",
        ]
        for phrase in clinical_phrases:
            res = detector.scan_and_redact(phrase)
            assert res.has_secrets is False
            assert res.redacted_text == phrase

    def test_genomic_dna_sequences_not_redacted(self, detector: SecretDetector):
        dna = "Oligonucleotide sequence: ATCGATCGATCGATCGATCGATCGATCGATCG"
        res = detector.scan_and_redact(dna)
        assert res.has_secrets is False
        assert res.redacted_text == dna

    def test_medical_identifiers_and_accounting_not_redacted(self, detector: SecretDetector):
        note = "ICD-10 code E11.9, LOINC 2345-7, token_count: 50, prompt_tokens: 30."
        res = detector.scan_and_redact(note)
        assert res.has_secrets is False
        assert res.redacted_text == note


class TestShannonEntropyAndHeuristics:
    """Tests for entropy calculations and contextual matching heuristics."""

    def test_entropy_computation_known_values(self):
        assert calculate_shannon_entropy("") == 0.0
        # Single repeating character has 0 entropy
        assert calculate_shannon_entropy("aaaaaaaaaaaa") == 0.0
        # Highly random characters have high entropy
        high_ent = calculate_shannon_entropy("a1B#9$zL@0!qP*7^")
        assert high_ent >= 3.5

    def test_clinical_exemption_helper(self):
        assert is_clinical_exemption("secretion") is True
        assert is_clinical_exemption("secretin") is True
        assert is_clinical_exemption("prompt_tokens") is True
        assert is_clinical_exemption("ACTGATCGATCGATCGATCGATCG") is True
        assert is_clinical_exemption("sk-proj-randomkey12345") is False


class TestStreamedSecretBuffer:
    """Tests streamed chunk boundary credential detection."""

    def test_secret_spanning_two_chunks(self):
        buf = StreamedSecretBuffer(window_size=32)
        chunk1 = "Here is the key: sk-proj-12345"
        chunk2 = "67890abcdefABCDEF1234567890abcdefABCDEF and other text"

        out1 = buf.append_chunk(chunk1)
        out2 = buf.append_chunk(chunk2)
        out3 = buf.flush()

        full_output = out1 + out2 + out3
        assert "sk-proj-" not in full_output
        assert "[REDACTED:openai_key]" in full_output

    def test_secret_spanning_multiple_tiny_chunks(self):
        buf = StreamedSecretBuffer(window_size=128)
        full_text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.somerandomsignature123456"
        chunks = [full_text[i : i + 5] for i in range(0, len(full_text), 5)]

        reconstructed = ""
        for ch in chunks:
            reconstructed += buf.append_chunk(ch)
        reconstructed += buf.flush()

        assert "eyJhbGciOi" not in reconstructed
        assert "[REDACTED:bearer_token]" in reconstructed or "[REDACTED:jwt]" in reconstructed


class TestFailClosedAndSecurityGuarantees:
    """Tests failure recovery, input edge cases, and zero-leakage compliance."""

    def test_finding_does_not_contain_raw_secret_value(self):
        detector = SecretDetector()
        res = detector.scan_and_redact("token: " + "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz12")
        assert res.has_secrets is True
        for f in res.findings:
            # Audit contract: SecretFinding has pattern_name, start, end, entropy, replacement
            # but must never expose a raw secret attribute or value
            assert not hasattr(f, "raw_secret")
            assert not hasattr(f, "secret_value")
            assert not hasattr(f, "value")

    def test_oversized_input_handled_safely(self):
        detector = SecretDetector(max_scan_size=1024)
        oversized = "A" * 2000
        res = detector.scan_and_redact(oversized)
        assert len(res.redacted_text) <= 1024
        assert res.has_secrets is False

    def test_none_and_edge_inputs(self):
        detector = SecretDetector()
        assert detector.scan_and_redact(None).redacted_text == ""
        assert detector.scan_and_redact("").redacted_text == ""
        
        for edge in SYNTHETIC_EDGE_CASES:
            res = detector.scan_and_redact(edge.text)
            assert isinstance(res.redacted_text, str)


class TestRedactionBoundaryIntegration:
    """Tests integration into shared.phi_redactor.redact."""

    def test_redact_masks_newly_supported_secret_in_dict(self):
        payload = {
            "service": "anthropic",
            "api_key": "sk-ant-" + "api03-abcdef1234567890ABCDEF1234567890-test123456",
            "patient_note": "Normal secretion observed.",
        }
        redacted_str, was_truncated = redact(payload)
        parsed = json.loads(redacted_str)
        assert "sk-ant-" not in redacted_str
        assert "Normal secretion observed." in parsed["patient_note"]

    def test_redact_masks_newly_supported_secret_in_plain_text(self):
        msg = "Using " + "AIzaSy" + "D1234567890abcdefABCDEF123456789 for embeddings"
        redacted_str, _ = redact(msg)
        assert "AIzaSy" not in redacted_str
        assert "[REDACTED:google_key]" in redacted_str


class TestBenchmarkEvaluationAndReport:
    """Tests that benchmark evaluation produces complete and qualified reports."""

    def test_evaluation_produces_qualified_report(self):
        evaluator = BenchmarkEvaluator()
        report = evaluator.evaluate()

        assert isinstance(report, EvaluationReport)
        assert report.enhanced_metrics.recall >= report.baseline_metrics.recall
        assert report.enhanced_metrics.clinical_meaning_loss_rate == 0.0
        assert report.corpus_digest == compute_corpus_digest()
        assert "ADOPT" in report.adoption_recommendation

    def test_report_artifact_generation(self, tmp_path):
        evaluator = BenchmarkEvaluator()
        report = evaluator.evaluate()

        json_out = str(tmp_path / "report.json")
        md_out = str(tmp_path / "report.md")

        export_report_json(report, json_out)
        export_report_markdown(report, md_out)

        assert os.path.exists(json_out)
        assert os.path.exists(md_out)

        with open(json_out, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["enhanced_metrics"]["clinical_meaning_loss_rate"] == 0.0
