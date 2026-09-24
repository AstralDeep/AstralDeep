"""Pure, deterministic idle-time precompute: derives likely follow-up questions from
recent messages and durable memories, then scores and ranks them. Consumed by
dreaming/consolidation.py's sweep; stdlib only, no I/O.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List

_MESSAGE_BASE_PRIORITY = 1.0
_MESSAGE_RECENCY_STEP = 0.1

_MEMORY_BASE_PRIORITY = 0.5
_MEMORY_SALIENCE_STEP = 0.25

_QUOTED_PHRASE_BONUS = 0.3

_STOPWORDS = frozenset(
    {
        "I",
        "A",
        "The",
        "This",
        "That",
        "These",
        "Those",
        "It",
        "We",
        "You",
        "They",
        "He",
        "She",
        "What",
        "When",
        "Where",
        "Why",
        "How",
        "Who",
        "Which",
        "Can",
        "Could",
        "Would",
        "Should",
        "Do",
        "Does",
        "Did",
        "Is",
        "Are",
        "Was",
        "Were",
        "Will",
        "Please",
        "Thanks",
        "Thank",
        "Ok",
        "Okay",
        "Yes",
        "No",
        "And",
        "But",
        "Or",
        "If",
        "So",
        "Then",
    }
)

_CAP_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)\b")
_QUOTED_RE = re.compile(r"[\"'“‘]([^\"'”’]+?)[\"'”’]")
_WS_RE = re.compile(r"\s+")


def sleeptime_enabled() -> bool:
    return os.getenv("FF_SLEEPTIME_COMPUTE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass(frozen=True)
class Anticipated:
    question: str
    rationale: str
    priority: float


def _normalize(text: str) -> str:
    collapsed = _WS_RE.sub(" ", (text or "").strip()).lower()
    return collapsed.rstrip("?.!,; ")


def _clean_topic(topic: str) -> str:
    return _WS_RE.sub(" ", (topic or "").strip())


def _message_candidates(recent_messages: List[str]) -> List[Anticipated]:
    out: List[Anticipated] = []
    total = len(recent_messages)
    for idx, message in enumerate(recent_messages):
        if not message or not message.strip():
            continue
        recency = max(0.0, total - 1 - idx)
        recency_weight = _MESSAGE_BASE_PRIORITY - (_MESSAGE_RECENCY_STEP * recency)
        if recency_weight <= 0.0:
            recency_weight = _MESSAGE_RECENCY_STEP

        seen_in_message: set[str] = set()

        for match in _QUOTED_RE.finditer(message):
            topic = _clean_topic(match.group(1))
            if not topic or topic.lower() in seen_in_message:
                continue
            seen_in_message.add(topic.lower())
            out.append(
                Anticipated(
                    question=f"Do you want to know more about {topic}?",
                    rationale=f"Quoted phrase {topic!r} in a recent message",
                    priority=round(recency_weight + _QUOTED_PHRASE_BONUS, 6),
                )
            )

        for match in _CAP_TOKEN_RE.finditer(message):
            topic = _clean_topic(match.group(1))
            if not topic:
                continue
            if " " not in topic and topic in _STOPWORDS:
                continue
            if topic.lower() in seen_in_message:
                continue
            seen_in_message.add(topic.lower())
            out.append(
                Anticipated(
                    question=f"Do you want to know more about {topic}?",
                    rationale=f"Topic {topic!r} mentioned in a recent message",
                    priority=round(recency_weight, 6),
                )
            )
    return out


def _memory_candidates(memories: List[Dict]) -> List[Anticipated]:
    out: List[Anticipated] = []
    for mem in memories:
        if not isinstance(mem, dict):
            continue
        category = str(mem.get("category", "") or "").strip().lower()
        value = mem.get("value")
        if value is None:
            value = mem.get("content", "")
        value = _clean_topic(str(value))
        if not value:
            continue

        try:
            salience = float(mem.get("salience", 1.0))
        except (TypeError, ValueError):
            salience = 1.0
        priority = round(_MEMORY_BASE_PRIORITY + (_MEMORY_SALIENCE_STEP * salience), 6)

        if category == "goal":
            out.append(
                Anticipated(
                    question=f"What's the next step toward {value}?",
                    rationale=f"Goal memory: {value}",
                    priority=priority,
                )
            )
        elif category in ("workflow_tag", "workflow"):
            out.append(
                Anticipated(
                    question=f"Want me to run the {value} workflow again?",
                    rationale=f"Workflow-tag memory: {value}",
                    priority=priority,
                )
            )
    return out


def anticipate_questions(
    recent_messages: List[str],
    memories: List[Dict],
    *,
    k: int = 5,
) -> List[Anticipated]:
    if k <= 0:
        return []

    candidates: List[Anticipated] = []
    candidates.extend(_message_candidates(recent_messages or []))
    candidates.extend(_memory_candidates(memories or []))

    candidates.sort(key=lambda a: (-a.priority, _normalize(a.question)))

    deduped: List[Anticipated] = []
    seen: set[str] = set()
    for cand in candidates:
        key = _normalize(cand.question)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(cand)
        if len(deduped) >= k:
            break
    return deduped


def precompute_plan(
    anticipated: List[Anticipated],
    *,
    budget: int = 3,
) -> List[Anticipated]:
    if budget <= 0 or not anticipated:
        return []
    ordered = sorted(anticipated, key=lambda a: (-a.priority, _normalize(a.question)))
    return ordered[:budget]


def is_idle(
    last_activity_ms: int,
    now_ms: int,
    *,
    idle_after_ms: int = 300_000,
) -> bool:
    return (now_ms - last_activity_ms) >= idle_after_ms
