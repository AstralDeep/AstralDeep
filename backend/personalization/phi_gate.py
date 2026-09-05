"""PHI gate for durable personalization memory (feature 025).

Guarantees that no protected health information is written to durable memory
or short-term signals (FR-017, FR-028, SC-005). PHI may still be *used* in a
live turn; this gate only governs what is *persisted*.

Design (research.md R3, lead-dev-approved dependency):
- Primary detector: Microsoft Presidio (local, in-process — no PHI egress),
  with a custom MRN / medical-record-number recognizer.
- Pure-Python pre-filter: a fast regex pass that catches the obvious
  identifiers (SSN, phone, email, full dates, long digit runs, MRN context)
  before invoking the heavier analyzer.
- **Fail-closed**: if the analyzer cannot be initialized or raises, the gate
  treats the input as PHI and blocks the write. This means durable memory is
  disabled rather than leaking when the detector is unavailable.

The analyzer is injectable so unit tests can exercise both the "clean passes"
and "fail-closed" paths without loading the spaCy model.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Protocol

logger = logging.getLogger("personalization.phi_gate")

# Entities the gate treats as PHI when reported by the analyzer.
PHI_ENTITIES: List[str] = [
    "PERSON",
    "LOCATION",
    "US_SSN",
    "MEDICAL_LICENSE",
    "US_DRIVER_LICENSE",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "MRN",  # custom recognizer registered below
]

_DEFAULT_SCORE_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Pure-Python pre-filter (fast path; also a partial fallback)
# ---------------------------------------------------------------------------

_PREFILTER_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                      # SSN
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),  # phone
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                      # ISO date (possible DOB)
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                # US date (possible DOB)
    re.compile(r"\bMRN\b[:#\s-]*\d{3,}", re.IGNORECASE),       # MRN context
    re.compile(r"\bmedical record\b", re.IGNORECASE),          # MRN context
    re.compile(r"\b\d{7,}\b"),                                 # long digit run (account/MRN)
]


def _prefilter_hits(text: str) -> bool:
    return any(p.search(text) for p in _PREFILTER_PATTERNS)


class _AnalyzerLike(Protocol):
    def analyze(self, text: str, language: str, entities: List[str], score_threshold: float): ...


def _build_presidio_analyzer() -> Optional[_AnalyzerLike]:
    """Build a Presidio AnalyzerEngine with a custom MRN recognizer.

    Returns ``None`` if Presidio (or its spaCy model) is unavailable, which
    causes the gate to fail closed.
    """
    try:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer

        analyzer = AnalyzerEngine()
        mrn_recognizer = PatternRecognizer(
            supported_entity="MRN",
            patterns=[
                Pattern(name="mrn_context", regex=r"\bMRN[:#\s-]*\d{3,}\b", score=0.85),
                Pattern(name="mrn_label", regex=r"\bmedical record (?:number|no\.?)\b[:#\s-]*\d{3,}", score=0.85),
            ],
        )
        analyzer.registry.add_recognizer(mrn_recognizer)
        return analyzer
    except Exception as exc:  # pragma: no cover - exercised via fail-closed test
        logger.warning("phi_gate.presidio_unavailable", extra={"error": str(exc)})
        return None


class PHIGate:
    """Decides whether a candidate value may be persisted to durable memory."""

    def __init__(
        self,
        analyzer: Optional[_AnalyzerLike] = None,
        *,
        build_if_missing: bool = True,
        score_threshold: float = _DEFAULT_SCORE_THRESHOLD,
    ) -> None:
        self._score_threshold = score_threshold
        if analyzer is not None:
            self._analyzer: Optional[_AnalyzerLike] = analyzer
        elif build_if_missing:
            self._analyzer = _build_presidio_analyzer()
        else:
            self._analyzer = None

    @property
    def available(self) -> bool:
        """True when the Presidio analyzer is loaded and usable."""
        return self._analyzer is not None

    def contains_phi(self, text: Optional[str]) -> bool:
        """Return True if ``text`` should be blocked from durable storage.

        Fail-closed: empty/None is treated as clean (nothing to store), but any
        analyzer error or unavailability is treated as PHI (block).
        """
        if text is None:
            return False
        text = str(text)
        if not text.strip():
            return False

        # Fast path: obvious identifiers.
        if _prefilter_hits(text):
            return True

        # The analyzer is required to catch names / locations / licenses that
        # regex cannot. If it is unavailable, fail closed.
        if self._analyzer is None:
            logger.warning("phi_gate.fail_closed_no_analyzer")
            return True

        try:
            results = self._analyzer.analyze(
                text=text,
                language="en",
                entities=PHI_ENTITIES,
                score_threshold=self._score_threshold,
            )
        except Exception as exc:  # pragma: no cover - exercised via fail-closed test
            logger.warning("phi_gate.analyze_failed_fail_closed", extra={"error": str(exc)})
            return True

        return bool(results)

    def filter_value(self, value: Optional[str]) -> Optional[str]:
        """Return ``value`` if safe to persist, else ``None`` (dropped)."""
        if value is None:
            return None
        return None if self.contains_phi(value) else value

    def redact_for_storage(self, text: str) -> tuple[str, bool]:
        """Redact bounded source evidence, then enforce the unchanged gate.

        This does not authorize or rewrite instructions or action arguments.
        Missing/invalid detector output and identifiers that cannot be safely
        redacted refuse the entire observation before any durable write.
        """
        from shared.phi_redactor import PHI_VALUE_PATTERNS

        if not isinstance(text, str) or len(text.encode("utf-8")) > 65536 or self._analyzer is None:
            raise ValueError("phi_redaction_unavailable")
        original = text
        for pattern, replacement in PHI_VALUE_PATTERNS:
            text = pattern.sub(replacement, text)
        try:
            findings = self._analyzer.analyze(
                text=text, language="en", entities=PHI_ENTITIES,
                score_threshold=self._score_threshold,
            )
        except Exception as exc:
            raise ValueError("phi_redaction_unavailable") from exc
        if not isinstance(findings, (list, tuple)):
            raise ValueError("phi_redaction_invalid")
        spans = []
        for finding in findings:
            start, end = getattr(finding, "start", None), getattr(finding, "end", None)
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(text)
                    or getattr(finding, "entity_type", None) not in PHI_ENTITIES):
                raise ValueError("phi_redaction_invalid")
            spans.append((start, end))
        # Merge overlaps before replacement so offsets remain tied to the
        # detector input, even when recognizers report the same span twice.
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in reversed(merged):
            text = text[:start] + "[REDACTED:phi]" + text[end:]
        if self.contains_phi(text):
            raise ValueError("phi_redaction_refused")
        return text, text != original

    def detect_for_notice(self, text: Optional[str]) -> bool:
        """Notify-only detection: True only on a POSITIVE PHI signal.

        Unlike :meth:`contains_phi` (fail-closed, correct for durable
        storage), this is for user-facing awareness notices (feature 030
        chat banner) and FAILS OPEN: an unavailable or erroring analyzer
        returns False so the notice never fires on every message in a
        deployment without Presidio. The regex prefilter (MRN/SSN-style
        identifiers) still fires without the analyzer.
        """
        if text is None:
            return False
        text = str(text)
        if not text.strip():
            return False
        if _prefilter_hits(text):
            return True
        if self._analyzer is None:
            return False
        try:
            results = self._analyzer.analyze(
                text=text,
                language="en",
                entities=PHI_ENTITIES,
                score_threshold=self._score_threshold,
            )
        except Exception as exc:
            logger.debug("phi_gate.notice_analyze_failed_fail_open", extra={"error": str(exc)})
            return False
        return bool(results)


# ---------------------------------------------------------------------------
# Process-wide singleton (analyzer is expensive to build)
# ---------------------------------------------------------------------------

_GATE: Optional[PHIGate] = None


def get_phi_gate() -> PHIGate:
    """Return the process-wide PHI gate, building the analyzer on first use."""
    global _GATE
    if _GATE is None:
        _GATE = PHIGate()
    return _GATE


def set_phi_gate(gate: Optional[PHIGate]) -> None:
    """Override the singleton (used by tests)."""
    global _GATE
    _GATE = gate
