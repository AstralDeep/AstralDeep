"""Checks independently maintained benign clinical and prompt-accounting controls against the redactor.
Synthetic PHI positives must still mask while unaffected content retains its exact bytes and digest.
"""

import hashlib
import json
from pathlib import Path

import pytest

from shared.phi_redactor import redact

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "phi-clinical-controls.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CORPUS["benign"], ids=lambda case: case["id"])
def test_benign_clinical_and_accounting_content_preserves_bytes(case):
    output, truncated = redact(case["input"], kind="result")
    assert output == case["expected"], "benign clinical or accounting content changed"
    assert truncated is False
    assert hashlib.sha256(output.encode("utf-8")).hexdigest() == case["sha256"]


@pytest.mark.parametrize("case", CORPUS["phi"], ids=lambda case: case["id"])
def test_synthetic_phi_controls_are_masked(case):
    output, truncated = redact(case["input"], kind="result")
    assert output == case["expected"], "synthetic PHI escaped masking"
    assert truncated is False
    assert case["sensitive"] not in output


def test_nested_clinical_controls_preserve_nonsensitive_siblings():
    source = {
        "clinical": {"secretions": "Clear secretions without blood."},
        "prompt_accounting": {"token count": 73},
        "subject": {"mrn": "MRN:987654"},
    }
    output, truncated = redact(source)
    parsed = json.loads(output)
    assert parsed["clinical"] == source["clinical"], "clinical sibling changed"
    assert parsed["prompt_accounting"] == source["prompt_accounting"], (
        "accounting sibling changed"
    )
    assert parsed["subject"] == {"mrn": "[REDACTED:phi]"}
    assert source["subject"]["mrn"] == "MRN:987654"
    assert truncated is False
