"""Tests for personalization/phi_gate.py: the pre-filter catches obvious identifiers
without Presidio, and the gate fails closed when the analyzer is unavailable or
raises, using an injected fake analyzer.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from personalization.phi_gate import PHIGate


class _FakeAnalyzer:
    def __init__(self, results=None, raises: bool = False):
        self._results = results or []
        self._raises = raises

    def analyze(self, text, language, entities, score_threshold):  # noqa: D401
        if self._raises:
            raise RuntimeError("analyzer boom")
        return list(self._results)


def _clean_gate() -> PHIGate:
    return PHIGate(analyzer=_FakeAnalyzer(results=[]))


@pytest.mark.parametrize(
    "text",
    [
        "Patient SSN is 123-45-6789",
        "email me at jane.doe@example.com",
        "call 555-123-4567",
        "DOB 1980-04-12",
        "born 4/12/1980",
        "MRN: 0099123",
        "medical record number 4456789",
        "account 12345678",
    ],
)
def test_prefilter_blocks_obvious_phi(text):
    gate = _clean_gate()
    assert gate.contains_phi(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Prefers concise, bullet-point summaries",
        "Works as a clinical researcher",
        "Goal: track grant deadlines",
        "Likes dark mode and terse answers",
    ],
)
def test_clean_personalization_passes(text):
    gate = _clean_gate()
    assert gate.contains_phi(text) is False
    assert gate.filter_value(text) == text


def test_analyzer_detected_entity_blocks():
    gate = PHIGate(analyzer=_FakeAnalyzer(results=[object()]))
    assert gate.contains_phi("met with the new lead") is True
    assert gate.filter_value("met with the new lead") is None


def test_fail_closed_when_analyzer_unavailable():
    gate = PHIGate(analyzer=None, build_if_missing=False)
    assert gate.available is False
    assert gate.contains_phi("just some preference text") is True


def test_fail_closed_when_analyzer_raises():
    gate = PHIGate(analyzer=_FakeAnalyzer(raises=True))
    assert gate.contains_phi("some non-obvious value") is True


def test_empty_is_clean():
    gate = PHIGate(analyzer=None, build_if_missing=False)
    assert gate.contains_phi("") is False
    assert gate.contains_phi("   ") is False
    assert gate.contains_phi(None) is False


class _SharingAnalyzer:
    def __init__(self, entities=()):
        self.entities = entities
        self.calls = []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return [
            SimpleNamespace(entity_type=entity, start=match.start(), end=match.end(), score=0.85)
            for entity, value in self.entities
            for match in re.finditer(re.escape(value), kwargs["text"])
        ]


@pytest.mark.parametrize("text,entities", [
    ("Forecast 2026-09-23 and 09/24/2026, population 12345678, rainfall 72.1234567", ()),
    ("Electronic medical record systems; MRN: unavailable; dataset 1234567890", (("PHONE_NUMBER", "1234567890"),)),
    ("Weather forecast for Lexington, Kentucky", (("LOCATION", "Lexington"), ("LOCATION", "Kentucky"))),
    ("Research by John Smith on patient outcomes", (("PERSON", "John Smith"),)),
    ("John Smith studies patients diagnosed with diabetes", (("PERSON", "John Smith"),)),
    ("Biography of Ada Lovelace", (("PERSON", "Ada Lovelace"),)),
    ("<div style='color:#12345678'>2026-09-23</div>", ()),
])
def test_sharing_allows_dashboard_data_and_public_names(text, entities):
    analyzer = _SharingAnalyzer(entities)
    gate = PHIGate(analyzer=analyzer)
    assert gate.contains_phi_for_sharing(text) is False
    assert analyzer.calls[0]["score_threshold"] == 0.5
    assert analyzer.calls[0]["language"] == "en"


@pytest.mark.parametrize("text", [
    "SSN 123-45-6789", "SSN: 123456789", "MRN: A0099123", "Patient ID: P1008",
    "medical record number 4456789", "Health plan ID: ABC1234", "Insurance ID: 1881234",
    "Driver license: A12345", "Medical license no. 981231",
    "DOB 1980-04-12", "date of birth: April 12, 1980", "born 4/12/1980",
    "jane.doe@example.com", "call 555-123-4567", "telephone: 5551234567",
    "Address: 123 Main Street", "https://example.org/?patient_id=P1008",
    "https://example.org/?contact=jane%2Edoe%40example%2Ecom",
    "https://example.org/?mrn%3D0099123", "SSN 123&#45;45&#45;6789",
])
def test_sharing_still_blocks_direct_identifiers(text):
    assert _clean_gate().contains_phi_for_sharing(text) is True


@pytest.mark.parametrize("text", [
    "Patient: John Smith", "Patient name: John Smith", "Patient John Smith",
    "Subject: John Smith", "title: Patient record\ncontent: John Smith",
    "John Smith has diabetes", "John Smith was diagnosed with asthma",
    "https://example.org/patient/John_Smith",
])
def test_sharing_blocks_names_tied_to_patient_records(text):
    gate = PHIGate(analyzer=_SharingAnalyzer((("PERSON", "John Smith"),)))
    assert gate.contains_phi_for_sharing(text) is True


@pytest.mark.parametrize("entity", ["US_SSN", "EMAIL_ADDRESS", "MRN"])
def test_sharing_blocks_validated_direct_analyzer_findings(entity):
    gate = PHIGate(analyzer=_SharingAnalyzer(((entity, "sensitive value"),)))
    assert gate.contains_phi_for_sharing("sensitive value") is True


@pytest.mark.parametrize("finding", [
    {}, object(), SimpleNamespace(entity_type="UNKNOWN", start=0, end=4, score=0.8),
    SimpleNamespace(entity_type="PERSON", start=-1, end=4, score=0.8),
    SimpleNamespace(entity_type="PERSON", start=0, end=99, score=0.8),
    SimpleNamespace(entity_type="PERSON", start=True, end=4, score=0.8),
    SimpleNamespace(entity_type="PERSON", start=0, end=4, score=float("nan")),
    SimpleNamespace(entity_type="PERSON", start=0, end=4, score=True),
])
def test_sharing_invalid_findings_fail_closed(finding):
    assert PHIGate(analyzer=_FakeAnalyzer([finding])).contains_phi_for_sharing("John Smith") is True


@pytest.mark.parametrize("text", [None, 123, "\ud800", "a" * 1_048_577, "%FF", "%25252525252541"])
def test_sharing_invalid_or_unbounded_input_fails_closed(text):
    assert _clean_gate().contains_phi_for_sharing(text) is True


def test_sharing_analyzer_failure_and_invalid_result_fail_closed():
    unavailable = PHIGate(analyzer=None, build_if_missing=False)
    assert unavailable.contains_phi_for_sharing("ordinary dashboard") is True
    assert PHIGate(analyzer=_FakeAnalyzer(raises=True)).contains_phi_for_sharing("ordinary dashboard") is True
    analyzer = _SharingAnalyzer()
    analyzer.analyze = lambda **kwargs: None
    assert PHIGate(analyzer=analyzer).contains_phi_for_sharing("ordinary dashboard") is True


def test_sharing_policy_does_not_weaken_memory_screening():
    gate = PHIGate(analyzer=_SharingAnalyzer((("PERSON", "Ada Lovelace"),)))
    for text in ("Forecast 2026-09-23", "population 12345678", "Ada Lovelace"):
        assert gate.contains_phi_for_sharing(text) is False
        assert gate.contains_phi(text) is True
