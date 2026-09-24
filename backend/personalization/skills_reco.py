"""Ranks a user's authorized agent tools by textual relevance to their profession and
goals for onboarding suggestions; a pure function used by onboarding/api.py and
panels.py's skills panel.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "with", "in", "on", "my",
    "i", "want", "need", "help", "track", "tracking", "work", "working", "use",
}


def _tokens(text: Optional[str]) -> set:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def recommend_skills(
    profession: Optional[str],
    goals: Optional[List[str]],
    available_tools: List[Dict[str, Any]],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    profile_tokens = _tokens(profession)
    for g in goals or []:
        profile_tokens |= _tokens(g)

    ranked: List[Dict[str, Any]] = []
    for idx, tool in enumerate(available_tools):
        text = f"{tool.get('tool_name', '')} {tool.get('description', '')}"
        overlap = profile_tokens & _tokens(text)
        score = len(overlap)
        ranked.append({**tool, "score": score, "_idx": idx})

    def _sort_key(t: Dict[str, Any]):
        return (
            -t["score"],
            0 if t.get("available", True) else 1,
            t["_idx"],
        )

    ranked.sort(key=_sort_key)
    for t in ranked:
        t.pop("_idx", None)
    return ranked[:limit]
