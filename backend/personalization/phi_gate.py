"""Fail-closed PHI screening for personalization and artifact sharing.
The shared analyzer supports strict memory screening and a contextual sharing profile
used by orchestrator.artifact_share without changing personalization write policy.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import List, Optional, Protocol
from urllib.parse import unquote

logger = logging.getLogger("personalization.phi_gate")

PHI_ENTITIES: List[str] = [
    "PERSON",
    "LOCATION",
    "US_SSN",
    "MEDICAL_LICENSE",
    "US_DRIVER_LICENSE",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "MRN",
]

_DEFAULT_SCORE_THRESHOLD = 0.5

_SHARE_IDENTIFIERS = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
    r"|\b(?:MRN|medical\s+record\s+(?:number|no\.?|id)|patient\s+id|"
    r"social\s+security(?:\s+number)?|SSN|health\s+plan\s+id|insurance\s+id|"
    r"(?:driver'?s?|medical)\s+licen[cs]e(?:\s+(?:number|no\.?))?)"
    r"\s*[:#=\s-]+(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{2,}\b"
    r"|\b(?:DOB|date\s+of\s+birth|birth\s*date|born)\s*[:=\s-]+"
    r"(?:\d{1,4}[-/]\d{1,2}[-/]\d{1,4}|[A-Z]{3,9}\s+\d{1,2},?\s+\d{4})\b"
    r"|\b(?:phone|telephone|mobile|fax)\s*[:=\s-]+\+?\d[\d ()+.-]{6,}\d\b"
    r"|\b\d{1,6}\s+(?:[A-Z0-9]+\s+){1,5}(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd)\b",
    re.IGNORECASE,
)
_SHARE_PATIENT_PREFIX = re.compile(
    r"\b(?:patient|subject)(?:\s+(?:name|identifier|id|record))?\s*[:=#]?\s*"
    r"(?:\n\s*(?:content|text|name)\s*:\s*)?$",
    re.IGNORECASE,
)
_SHARE_CLINICAL_SUFFIX = re.compile(
    r"^\s*[,;:]?\s*(?:(?:was|is)\s+)?"
    r"(?:diagnosed\s+with|treated\s+for|admitted\s+for|discharged\s+with|"
    r"has\s+(?:diabetes|cancer|HIV|asthma|hypertension|a\s+history\s+of))\b",
    re.IGNORECASE,
)

_PREFILTER_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\bMRN\b[:#\s-]*\d{3,}", re.IGNORECASE),
    re.compile(r"\bmedical record\b", re.IGNORECASE),
    re.compile(r"\b\d{7,}\b"),
]


def _prefilter_hits(text: str) -> bool:
    return any(p.search(text) for p in _PREFILTER_PATTERNS)


class _AnalyzerLike(Protocol):
    def analyze(self, text: str, language: str, entities: List[str], score_threshold: float): ...


def _build_presidio_analyzer() -> Optional[_AnalyzerLike]:
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
    except Exception as exc:  # pragma: no cover
        logger.warning("phi_gate.presidio_unavailable", extra={"error": str(exc)})
        return None


class PHIGate:
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
        return self._analyzer is not None

    def contains_phi(self, text: Optional[str]) -> bool:
        if text is None:
            return False
        text = str(text)
        if not text.strip():
            return False

        if _prefilter_hits(text):
            return True

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
        except Exception as exc:  # pragma: no cover
            logger.warning("phi_gate.analyze_failed_fail_closed", extra={"error": str(exc)})
            return True

        return bool(results)

    def filter_value(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return None if self.contains_phi(value) else value

    def contains_phi_for_sharing(self, text: str) -> bool:
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > 1_048_576 or self._analyzer is None:
                return True
            text = unescape(text)
            for _ in range(5):
                if not re.search(r"%[0-9a-fA-F]{2}", text):
                    break
                decoded = unquote(text, errors="strict")
                if decoded == text:
                    break
                text = decoded
            else:
                if re.search(r"%[0-9a-fA-F]{2}", text):
                    return True
            urls = re.findall(r"https?://[^\s<>\"']+", text)
            text += "\n" + "\n".join(re.sub(r"[/_.?&=+%:-]+", " ", url) for url in urls)
            if _SHARE_IDENTIFIERS.search(text):
                return True
            findings = self._analyzer.analyze(
                text=text, language="en", entities=PHI_ENTITIES,
                score_threshold=self._score_threshold,
            )
            if not isinstance(findings, (list, tuple)):
                return True
            for finding in findings:
                start, end = getattr(finding, "start", None), getattr(finding, "end", None)
                entity = getattr(finding, "entity_type", None)
                score = getattr(finding, "score", None)
                if (type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(text) or entity not in PHI_ENTITIES
                        or type(score) not in (int, float) or not 0 <= score <= 1):
                    return True
                if entity in {"US_SSN", "EMAIL_ADDRESS", "MRN"}:
                    return True
                if entity == "PERSON":
                    prefix = text[max(0, start - 160):start]
                    suffix = text[end:end + 160]
                    if _SHARE_PATIENT_PREFIX.search(prefix) or _SHARE_CLINICAL_SUFFIX.search(suffix):
                        return True
        except Exception:
            logger.warning("phi_gate.share_screen_failed_closed")
            return True
        return False

    def redact_for_storage(self, text: str) -> tuple[str, bool]:
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
        if text is None:
            return False
        text = str(text)
        if not text.strip():
            return False
        if _prefilter_hits(text):
            return True
        # Fails open, unlike contains_phi — notices must not spam
        if self._analyzer is None:
            return False
        try:
            results = self._analyzer.analyze(
                text=text,
                language="en",
                entities=[entity for entity in PHI_ENTITIES if entity != "LOCATION"],
                score_threshold=self._score_threshold,
            )
        except Exception as exc:
            logger.debug("phi_gate.notice_analyze_failed_fail_open", extra={"error": str(exc)})
            return False
        return any(getattr(result, "entity_type", None) != "LOCATION" for result in results)


_GATE: Optional[PHIGate] = None


def get_phi_gate() -> PHIGate:
    global _GATE
    if _GATE is None:
        _GATE = PHIGate()
    return _GATE


def set_phi_gate(gate: Optional[PHIGate]) -> None:
    global _GATE
    _GATE = gate
