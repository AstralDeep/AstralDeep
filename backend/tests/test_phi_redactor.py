"""Tests for backend/shared/phi_redactor.py: field-key and value-pattern PHI masking,
truncation after redaction, structured logging of applied redactions, and safe
fallback on malformed input.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from shared.phi_redactor import (  # noqa: E402
    PHI_FIELD_PATTERNS,
    TRUNCATION_LIMIT,
    redact,
)


class TestPassThrough:
    def test_none_passes_through_untouched(self):
        assert redact(None) == (None, False)

    def test_safe_string_unchanged(self):
        out, was_truncated = redact("Search for grants matching biomedical research")
        assert out == "Search for grants matching biomedical research"
        assert was_truncated is False

    def test_safe_dict_unchanged(self):
        payload = {"query": "biomedical", "max_results": 10}
        out, was_truncated = redact(payload)
        assert json.loads(out) == payload
        assert was_truncated is False


class TestFieldKeyMasking:
    @pytest.mark.parametrize(
        "field",
        [
            "name",
            "first_name",
            "patient_name",
            "full_name",
            "dob",
            "birthdate",
            "ssn",
            "social_security_number",
            "mrn",
            "medical_record_number",
            "patient_id",
            "address",
            "street_address",
            "phone",
            "telephone",
            "email",
            "ip_address",
            "device_id",
            "vehicle_id",
            "license_plate",
            "license_number",
            "certificate",
            "account_number",
            "credit_card",
            "card_number",
            "health_plan_id",
            "beneficiary_id",
            "insurance_id",
            "biometric_signature",
            "fingerprint",
            "voiceprint",
            "photo_url",
            "image_url",
        ],
    )
    def test_known_phi_field_is_masked(self, field: str):
        out, _ = redact({field: "raw-sensitive-value-12345"})
        parsed = json.loads(out)
        assert parsed[field] == "[REDACTED:phi]", (
            f"field {field!r} should have been masked"
        )

    def test_field_match_is_case_insensitive(self):
        out, _ = redact({"Patient_Name": "Jane Doe"})
        assert json.loads(out)["Patient_Name"] == "[REDACTED:phi]"

    def test_nested_dict_field_match(self):
        out, _ = redact({"metadata": {"patient": {"name": "Jane"}}})
        parsed = json.loads(out)
        assert parsed["metadata"]["patient"]["name"] == "[REDACTED:phi]"

    def test_field_match_replaces_dict_values_too(self):
        out, _ = redact({"address": {"line1": "123 Main", "city": "Springfield"}})
        parsed = json.loads(out)
        for v in parsed["address"].values():
            assert v == "[REDACTED:phi]"

    def test_field_match_replaces_list_values_too(self):
        out, _ = redact({"phone": ["555-1234", "555-5678"]})
        parsed = json.loads(out)
        assert parsed["phone"] == ["[REDACTED:phi]", "[REDACTED:phi]"]

    def test_unmatched_field_passes_through(self):
        out, _ = redact({"query": "hello", "max_results": 10})
        parsed = json.loads(out)
        assert parsed == {"query": "hello", "max_results": 10}


class TestValuePatternMasking:
    def test_ssn_in_string_is_masked(self):
        out, _ = redact("Patient SSN is 123-45-6789, please verify.")
        assert "[REDACTED:ssn]" in out
        assert "123-45-6789" not in out

    def test_email_in_string_is_masked(self):
        out, _ = redact("Email contact: jane.doe@example.com if questions.")
        assert "[REDACTED:email]" in out
        assert "jane.doe@example.com" not in out

    def test_phone_in_string_is_masked(self):
        out, _ = redact("Call (555) 123-4567 between 9 and 5.")
        assert "[REDACTED:phone]" in out
        assert "(555) 123-4567" not in out

    def test_ip_in_string_is_masked(self):
        out, _ = redact("Logged from 192.168.1.42 today.")
        assert "[REDACTED:ip]" in out
        assert "192.168.1.42" not in out

    def test_full_date_is_masked(self):
        out_iso, _ = redact("DOB recorded as 1985-04-12 today.")
        out_us, _ = redact("Date of service: 04/12/1985.")
        assert "[REDACTED:date]" in out_iso
        assert "1985-04-12" not in out_iso
        assert "[REDACTED:date]" in out_us
        assert "04/12/1985" not in out_us

    def test_year_only_is_NOT_masked(self):
        # Safe Harbor allows year-only dates — not redacted
        out, _ = redact("Cohort is from year 1985.")
        assert "1985" in out
        assert "[REDACTED:date]" not in out

    def test_mrn_pattern_in_string_is_masked(self):
        out, _ = redact("Reference MRN: 12345678 in chart.")
        assert "[REDACTED:mrn]" in out
        assert "12345678" not in out

    def test_value_pattern_inside_unmatched_field(self):
        out, _ = redact({"description": "Caller: 555-123-4567 reported issue."})
        parsed = json.loads(out)
        assert "[REDACTED:phone]" in parsed["description"]
        assert "555-123-4567" not in parsed["description"]


class TestTruncation:
    def test_short_input_not_truncated(self):
        out, was_truncated = redact("hello world")
        assert was_truncated is False
        assert out == "hello world"

    def test_long_input_is_truncated_to_limit(self):
        long = "a" * (TRUNCATION_LIMIT + 100)
        out, was_truncated = redact(long)
        assert was_truncated is True
        assert len(out) <= TRUNCATION_LIMIT
        assert out.endswith("…")

    def test_truncation_runs_after_redaction(self):
        prefix = "x" * 480
        raw = f"{prefix} SSN 123-45-6789 contact"
        out, _ = redact(raw)
        assert "123-45-6789" not in out


class TestObservability:
    def test_redaction_applied_log_emitted_on_mask(self, caplog):
        caplog.set_level(logging.INFO, logger="PHIRedactor")
        redact({"name": "Jane Doe"}, kind="args")
        msgs = [r.getMessage() for r in caplog.records]
        assert "phi_redactor.redaction_applied" in msgs

    def test_no_log_when_nothing_redacted(self, caplog):
        caplog.set_level(logging.INFO, logger="PHIRedactor")
        redact({"query": "biomedical"}, kind="args")
        msgs = [r.getMessage() for r in caplog.records]
        assert "phi_redactor.redaction_applied" not in msgs

    def test_kind_propagated_in_log_extras(self, caplog):
        caplog.set_level(logging.INFO, logger="PHIRedactor")
        redact({"email": "j@x.com"}, kind="result")
        match = next(
            (r for r in caplog.records if r.getMessage() == "phi_redactor.redaction_applied"),
            None,
        )
        assert match is not None
        assert getattr(match, "kind", None) == "result"


class TestNeverRaises:
    def test_unserialisable_value_does_not_raise(self):
        class Weird:
            def __repr__(self):
                return "Weird()"

        out, _ = redact({"query": Weird()})
        assert isinstance(out, str)

    def test_self_referential_input_does_not_raise(self):
        d: dict = {"safe": 1}
        d["self"] = d
        out, was_truncated = redact(d)
        assert isinstance(out, str)
        assert was_truncated in (True, False)


class TestSafeHarborCoverage:
    REQUIRED_LABELS = (
        "name",
        "address",
        "dob",
        "phone",
        "fax",
        "email",
        "ssn",
        "mrn",
        "account_number",
        "certificate",
        "vehicle",
        "device",
        "url",
        "ip",
        "biometric",
        "photo",
    )

    def test_every_required_label_has_a_pattern(self):
        for label in self.REQUIRED_LABELS:
            assert any(label in p for p in PHI_FIELD_PATTERNS), (
                f"PHI_FIELD_PATTERNS missing coverage for Safe Harbor label {label!r}"
            )
