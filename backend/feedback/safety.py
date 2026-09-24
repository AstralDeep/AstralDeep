"""Pure-Python inline screen for feedback comments: flags jailbreak/role-override
phrasing and hidden Unicode controls before a comment can reach an LLM. A second,
LLM-based pass runs later in knowledge_synthesis.py.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .schemas import COMMENT_MAX_CHARS

logger = logging.getLogger("Feedback.Safety")

REASON_JAILBREAK_PHRASE = "jailbreak_phrase"
REASON_ROLE_OVERRIDE_MARKER = "role_override_marker"
REASON_UNICODE_CONTROL = "unicode_control"
REASON_OVER_LENGTH = "over_length"
REASON_PRE_PASS_FLAG = "pre_pass_flag"
REASON_PRE_PASS_DISAGREEMENT = "pre_pass_disagreement"


_JAILBREAK_PATTERNS: Tuple[str, ...] = (
    r"ignore\s+(all\s+)?(the\s+)?previous\s+(instruction|prompt|rule|context)s?",
    r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above)\s+(instruction|prompt)s?",
    r"disregard\s+(all\s+)?(the\s+)?(above|prior)\b",
    r"forget\s+(all\s+)?(the\s+)?previous\s+(instruction|prompt|rule)s?",
    r"you\s+are\s+now\s+(?!feeling|happy|sad|able)",
    r"act\s+as\s+(if\s+you\s+(were|are)|though\s+you\s+(were|are))",
    r"pretend\s+(to\s+be|you\s+are|you'?re)",
    r"from\s+now\s+on\s+you\s+(are|will|must|should)",
    r"\bsystem\s+prompt\b",
    r"new\s+(instruction|directive)s?\s*[:.\-]",
    r"override\s+(your|the)\s+(instruction|rule|guideline)s?",
    r"\bdan\s+mode\b",
    r"\bdeveloper\s+mode\b",
    r"jailbreak\b",
    r"reveal\s+(your|the)\s+(system\s+)?prompt",
    r"print\s+(your|the)\s+(system\s+)?(prompt|instruction)s?",
    r"(what\s+were|what\s+are)\s+(your|the)\s+(original|initial|system|first)\s+(instruction|prompt|directive)s?",
    r"show\s+me\s+(the\s+)?(entire|whole|full|original|system)\s+(prompt|instruction)s?",
    r"tell\s+me\s+(your|the)\s+(training\s+data|system\s+prompt|initial\s+(instruction|prompt))",
    r"\b(dear|hi|hello|attention)\s+(admin|reviewer|moderator|operator)\b",
    r"\bto\s+the\s+(admin|reviewer|moderator|developer)\b",
    r"(modify|change|update|rewrite|delete)\s+(the\s+)?tool",
    r"(modify|change|update|rewrite|delete)\s+(the\s+)?(prompt|knowledge|policy)",
)

_JAILBREAK_RE = re.compile("|".join(_JAILBREAK_PATTERNS), re.IGNORECASE)


_ROLE_OVERRIDE_MARKERS: Tuple[str, ...] = (
    "<|system|>",
    "<|user|>",
    "<|assistant|>",
    "<|im_start|>",
    "<|im_end|>",
    "### system",
    "### instruction",
    "[system]",
    "[/system]",
    "<system>",
    "</system>",
    "[INST]",
    "[/INST]",
    "<<SYS>>",
    "<</SYS>>",
)


# Cc/Cf catch zero-width and bidi injection characters
_FORBIDDEN_UNICODE_CATEGORIES = {"Cc", "Cf"}
_ALLOWED_UNICODE_CHARS = {"\n", "\r", "\t"}


_OVERLAY_PATH = Path(__file__).parent / "safety_patterns.json"


def _load_overlay() -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    if not _OVERLAY_PATH.exists():
        return (), ()
    try:
        data = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        logger.warning("safety_patterns.json failed to load: %s", exc)
        return (), ()
    extra_jb = tuple(data.get("jailbreak_patterns", []))
    extra_mk = tuple(data.get("role_override_markers", []))
    return extra_jb, extra_mk


_overlay_jb, _overlay_mk = _load_overlay()
if _overlay_jb:
    _JAILBREAK_RE = re.compile(
        "|".join(list(_JAILBREAK_PATTERNS) + list(_overlay_jb)), re.IGNORECASE
    )
_ROLE_OVERRIDE_MARKERS_FULL = _ROLE_OVERRIDE_MARKERS + _overlay_mk


def classify(text: Optional[str]) -> Tuple[str, Optional[str]]:
    if text is None or text == "":
        return "clean", None
    if not isinstance(text, str):
        return "quarantined", REASON_JAILBREAK_PHRASE

    if len(text) > COMMENT_MAX_CHARS:
        return "quarantined", REASON_OVER_LENGTH

    for ch in text:
        if ch in _ALLOWED_UNICODE_CHARS:
            continue
        cat = unicodedata.category(ch)
        if cat in _FORBIDDEN_UNICODE_CATEGORIES:
            return "quarantined", REASON_UNICODE_CONTROL

    lower = text.lower()
    for marker in _ROLE_OVERRIDE_MARKERS_FULL:
        if marker.lower() in lower:
            return "quarantined", REASON_ROLE_OVERRIDE_MARKER

    if _JAILBREAK_RE.search(text):
        return "quarantined", REASON_JAILBREAK_PHRASE

    return "clean", None


def classify_many(texts: Iterable[Optional[str]]) -> List[Tuple[str, Optional[str]]]:
    return [classify(t) for t in texts]
