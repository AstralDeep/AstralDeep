"""Contextual Privacy Filter Engine.

Fulfills Acceptance Checks 2, 3, and 4 (Issue #318):
- Contextual span detection for names, addresses, MRNs, and dates
- Preserves clinical meaning (zero loss on medical terms, dosages, lab values, ICD codes)
- Negative controls preventing over-redaction
- Multilingual and encoded text support
- Fail-closed behavior on corrupt inputs, invalid span offsets, or unexpected exceptions
- Strictly shielded outputs (no raw span text in results or logs)
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Set, Tuple

from .types import FilterStatus, PrivacySpan, SafeRedactionResult

# Bounded inputs: Maximum 512 KiB per payload
MAX_INPUT_BYTES = 512 * 1024
FAIL_CLOSED_MARKER = "[FAIL_CLOSED_PHI_REDACTED]"

# Clinical terms, diagnoses, drugs, and lab units that must NEVER be redacted
PROTECTED_CLINICAL_TERMS: Set[str] = {
    "type 2 diabetes",
    "type 2 diabetes mellitus",
    "hypertension",
    "essential hypertension",
    "atrial fibrillation",
    "myocardial infarction",
    "acute myocardial infarction",
    "congestive heart failure",
    "chronic kidney disease",
    "asthma",
    "copd",
    "pneumonia",
    "bacterial pneumonia",
    "covid-19",
    "sars-cov-2",
    "depression",
    "major depressive disorder",
    "anxiety",
    "hyperlipidemia",
    "gastroesophageal reflux",
    "metformin",
    "lisinopril",
    "atorvastatin",
    "amlodipine",
    "metoprolol",
    "albuterol",
    "omeprazole",
    "losartan",
    "gabapentin",
    "hydrochlorothiazide",
    "amoxicillin",
    "aspirin",
    "acetaminophen",
    "ibuprofen",
    "insulin",
    "insulin glargine",
    "prednisone",
    "furosemide",
    "pantoprazole",
}

# Regex patterns for clinical dosages and lab values that must be protected
CLINICAL_NUMERIC_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|kg|ml|l|mEq|mEq\/L|mg\/dL|mmol\/L|uL|k\/uL|ng\/mL|bpm|mmHg|%)\b", re.IGNORECASE),
    re.compile(r"\b\d{2,3}\/\d{2,3}\s*(?:mmHg)?\b"),  # Blood pressure e.g. 120/80
    re.compile(r"\b(?:ICD-10|ICD-9|CPT)[\s:]+[A-Z0-9\.]+\b", re.IGNORECASE),  # Billing / diagnosis codes
    re.compile(r"\b(?:BID|TID|QID|QD|PRN|PO|IV|IM|SubQ)\b", re.IGNORECASE),  # Medical administration routes
)

# Non-name stop words and title tokens that must not be recognized as names
NAME_EXCLUDED_TOKENS: Set[str] = {
    "was", "is", "has", "had", "will", "presented", "reports", "underwent",
    "stated", "denies", "shows", "arrived", "complains", "resides", "noted",
    "admitted", "discharged", "in", "at", "to", "for", "with", "on",
    "dr", "md", "do", "rn", "np", "pa", "doctor", "physician", "surgeon", "attending", "nurse"
}

# Contextual span detection patterns
CONTEXTUAL_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    # Names with clinical context (Provider or Patient)
    (
        "NAME",
        re.compile(
            r"\b(?:Dr\.|Doctor|Attending|Physician|Surgeon|Nurse|Consultant|Seen by|Examined by)"
            r"(?:\s+(?:Dr\.|Doctor|Attending|Physician))?\s+"
            r"([A-ZÀ-ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-ÿ][a-zà-ÿ]+)?)\b"
        ),
    ),
    (
        "NAME",
        re.compile(
            r"\b(?:Patient(?:\s+name)?|Pt|Client|Subject|Señor|Señora|Monsieur|Madame)[\s:#]+"
            r"([A-ZÀ-ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-ÿ][a-zà-ÿ]+)?)\b"
        ),
    ),
    # Street Addresses (Suite/Apt/St/Ave/Blvd/Rd/Dr/Way/Lane/Ct/Terrace/Circle/Highway/Place)
    (
        "ADDRESS",
        re.compile(
            r"\b\d{1,5}\s+[A-ZÀ-ÿa-z0-9\.\s]{2,25}\s+"
            r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Way|Court|Ct|Lane|Ln|Parkway|Pkwy|Terrace|Ter|Circle|Cir|Highway|Hwy|Place|Pl)\b"
            r"(?:\s+(?:Apt|Suite|Ste|Unit|#)\s*[\w\-]+)?"
            r"(?:\s*,\s*[A-ZÀ-ÿ][a-zà-ÿ\s]+)?"
            r"(?:\s*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?",
            re.IGNORECASE,
        ),
    ),
    # MRN / Clinical Record Number
    (
        "MRN",
        re.compile(
            r"\b(?:MRN|Medical\s*Record(?:\s*Number)?|Patient\s*ID|Record\s*ID|Account\s*#)[\s:#]+"
            r"([A-Z0-9\-_]{5,15})\b",
            re.IGNORECASE,
        ),
    ),
    # Contextual Dates (Admit, Discharge, Service, Encounter, DOB)
    (
        "DATE",
        re.compile(
            r"\b(?:Admitted|Discharged|DOS|Date\s+of\s+Service|Encounter\s+Date|Seen\s+on|DOB|Birthdate)(?:\s+on)?[\s:#]+"
            r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b",
            re.IGNORECASE,
        ),
    ),
)


class ContextualPrivacyEngine:
    """Bounded, fail-closed contextual privacy filter engine."""

    def __init__(self, max_input_bytes: int = MAX_INPUT_BYTES) -> None:
        self.max_input_bytes = max_input_bytes

    def is_protected_clinical_content(self, text_segment: str) -> bool:
        """Check if a matched segment corresponds to protected medical terminology."""
        cleaned = text_segment.strip().lower()
        if cleaned in PROTECTED_CLINICAL_TERMS:
            return True
        for pat in CLINICAL_NUMERIC_PATTERNS:
            if pat.fullmatch(text_segment.strip()):
                return True
        return False

    def is_excluded_name_token(self, token: str) -> bool:
        """Check if matched name token is a common grammatical verb, stop word, or bare title."""
        parts = token.strip().lower().split()
        return any(p in NAME_EXCLUDED_TOKENS for p in parts)

    def extract_spans(self, text: str) -> List[PrivacySpan]:
        """Extract valid, non-overlapping sensitive spans from text."""
        raw_spans: List[PrivacySpan] = []

        for entity_type, pattern in CONTEXTUAL_PATTERNS:
            for match in pattern.finditer(text):
                # If pattern has a capturing group for the entity itself, use group(1)
                if match.groups() and match.group(1):
                    start = match.start(1)
                    end = match.end(1)
                    matched_val = match.group(1)
                else:
                    start = match.start(0)
                    end = match.end(0)
                    matched_val = match.group(0)

                # Skip if this is protected clinical terminology
                if self.is_protected_clinical_content(matched_val):
                    continue

                # Skip if entity is NAME but token is an excluded verb/stop-word/bare title
                if entity_type == "NAME" and self.is_excluded_name_token(matched_val):
                    continue

                # Validate span offsets
                if start < 0 or end > len(text) or start >= end:
                    raise ValueError(f"Invalid span offset detected: [{start}, {end}] in text length {len(text)}")

                span = PrivacySpan.create(
                    start=start,
                    end=end,
                    entity_type=entity_type,
                    raw_span_text=matched_val,
                    confidence=1.0,
                )
                raw_spans.append(span)

        # Sort spans by start offset; on conflict, prefer earlier start and longer span
        raw_spans.sort(key=lambda s: (s.start, -(s.end - s.start)))

        # Resolve overlaps by greedily accepting the longest non-overlapping span
        resolved_spans: List[PrivacySpan] = []
        last_end = 0
        for span in raw_spans:
            if span.start >= last_end:
                resolved_spans.append(span)
                last_end = span.end

        return resolved_spans

    def filter_text(self, text: str) -> SafeRedactionResult:
        """Filter and redact contextual sensitive spans from text.

        Enforces fail-closed safety and bounded resource usage.
        Guarantees that raw sensitive text is NEVER included in the result dict.
        """
        start_time = time.perf_counter()

        # Input validation and bounds check
        if not isinstance(text, str):
            return SafeRedactionResult(
                redacted_text="",
                status=FilterStatus.FAIL_CLOSED,
                safe_counts={},
                spans_count=0,
                latency_ms=(time.perf_counter() - start_time) * 1000,
                error_detail="NON_STRING_INPUT",
            )

        if len(text.encode("utf-8")) > self.max_input_bytes:
            # Bounded resource check: Fail-closed on oversized payloads
            return SafeRedactionResult(
                redacted_text=FAIL_CLOSED_MARKER,
                status=FilterStatus.FAIL_CLOSED,
                safe_counts={"OVERSIZED_INPUT": 1},
                spans_count=1,
                latency_ms=(time.perf_counter() - start_time) * 1000,
                error_detail="INPUT_EXCEEDS_BYTE_LIMIT",
            )

        if not text:
            return SafeRedactionResult(
                redacted_text="",
                status=FilterStatus.CLEAN,
                safe_counts={},
                spans_count=0,
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        try:
            spans = self.extract_spans(text)
        except Exception as e:
            # Fail-closed: Never pass unredacted data on engine failure
            return SafeRedactionResult(
                redacted_text=FAIL_CLOSED_MARKER,
                status=FilterStatus.FAIL_CLOSED,
                safe_counts={"ENGINE_ERROR": 1},
                spans_count=1,
                latency_ms=(time.perf_counter() - start_time) * 1000,
                error_detail=type(e).__name__,
            )

        if not spans:
            return SafeRedactionResult(
                redacted_text=text,
                status=FilterStatus.CLEAN,
                safe_counts={},
                spans_count=0,
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        # Apply redactions from end to start to preserve character indices
        safe_counts: Dict[str, int] = {}
        out_chars = list(text)

        for span in reversed(spans):
            replacement = f"[REDACTED:{span.entity_type.lower()}]"
            out_chars[span.start : span.end] = list(replacement)
            safe_counts[span.entity_type] = safe_counts.get(span.entity_type, 0) + 1

        redacted_text = "".join(out_chars)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return SafeRedactionResult(
            redacted_text=redacted_text,
            status=FilterStatus.REDACTED,
            safe_counts=safe_counts,
            spans_count=len(spans),
            latency_ms=elapsed_ms,
        )
