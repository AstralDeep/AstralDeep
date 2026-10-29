"""Defines the fixed reviewed note-extraction schema (version, field names, enums,
quantity ranges, approved units, and failure codes) shared by the extraction tool,
its deterministic validator, prompts, and the clinician-review diagnostic; anything
here is a reviewed schema change, never a per-call option.
"""

from __future__ import annotations

import re

SCHEMA_VERSION = "note-extract-v1"

MAX_NOTE_CHARS = 20_000
MIN_NOTE_CHARS = 1
MAX_NOTE_ID_CHARS = 120
NOTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/\-]{0,119}$")
MAX_RESPONSE_CHARS = 64_000
MAX_FINDINGS = 12
MAX_QUOTE_CHARS = 300
MAX_TERM_CHARS = 120
MAX_STRING_VALUE_CHARS = 120

TOP_LEVEL_KEYS = frozenset({"schema_version", "note_id", "note_digest", "patient_ref", "note_date", "findings"})
SLOT_KEYS = frozenset({"value", "evidence", "abstention_reason"})
FINDINGS_KEYS = frozenset({"entries", "abstention_reason"})
FINDING_KEYS = frozenset({
    "term", "evidence", "assertion", "experiencer", "temporality",
    "value_type", "value", "unit",
})
EVIDENCE_KEYS = frozenset({"start", "end", "quote"})

ABSTENTION_REASONS = ("not_documented", "segment_unreadable", "outside_field_scope")
ASSERTIONS = ("asserted", "negated", "uncertain")
EXPERIENCERS = ("patient", "family")
TEMPORALITIES = ("current", "historical", "planned")
VALUE_TYPES = ("none", "temperature", "heart_rate", "glucose", "a1c", "systolic_bp", "weight", "height")

SCALAR_FIELDS = ("patient_ref", "note_date")

QUANTITIES = {
    "temperature": {"C": (25.0, 45.0, "Cel")},
    "heart_rate": {"bpm": (20.0, 250.0, "{/min}")},
    "glucose": {"mg/dL": (0.0, 2000.0, "mg/dL")},
    "a1c": {"%": (0.0, 25.0, "%")},
    "systolic_bp": {"mmHg": (30.0, 300.0, "mm[Hg]")},
    "weight": {"kg": (0.2, 700.0, "kg")},
    "height": {"cm": (10.0, 260.0, "cm")},
}

UNIT_CODES = {
    unit: ucum
    for units in QUANTITIES.values()
    for unit, (_, _, ucum) in units.items()
}

CHECK_SCHEMA = "E_SCHEMA"
CHECK_SCHEMA_VERSION = "E_SCHEMA_VERSION"
CHECK_NOTE_ID = "E_NOTE_ID"
CHECK_DIGEST = "E_DIGEST"
CHECK_UNKNOWN_KEY = "E_UNKNOWN_KEY"
CHECK_OVERSIZE = "E_OUTPUT_OVERSIZE"
CHECK_SPAN_RANGE = "E_SPAN_RANGE"
CHECK_SPAN_MISMATCH = "E_SPAN_MISMATCH"
CHECK_SPAN_AMBIGUOUS = "E_SPAN_AMBIGUOUS"
CHECK_SPAN_EMPTY = "E_SPAN_EMPTY"
CHECK_SPAN_OVERSIZE = "E_SPAN_OVERSIZE"
CHECK_EVIDENCE_REQUIRED = "E_EVIDENCE_REQUIRED"
CHECK_TYPE = "E_TYPE"
CHECK_ENUM = "E_ENUM"
CHECK_UNIT = "E_UNIT"
CHECK_VALUE_RANGE = "E_VALUE_RANGE"
CHECK_VALUE_REQUIRED = "E_VALUE_REQUIRED"
CHECK_DATE = "E_DATE"
CHECK_ABSTENTION_CONFLICT = "E_ABSTENTION_CONFLICT"
CHECK_DUPLICATE = "E_DUPLICATE"
CHECK_FINDING_CAP = "E_FINDING_CAP"

STATUS_EXTRACTED = "extracted"
STATUS_ABSENT = "absent"
STATUS_FAILED_CHECK = "failed_check"
STATUS_NOT_EVALUATED = "not_evaluated"

ABSENCE_BASIS = "model_abstention"

OUTCOME_REJECTED = "rejected_output"
OUTCOME_VALIDATED = "validated"
OUTCOME_EXTRACTED = "extracted"
OUTCOME_PARTIAL = "partial"
OUTCOME_ABSENT = "absent"
OUTCOME_NO_VALID_EXTRACTION = "no_valid_extraction"
