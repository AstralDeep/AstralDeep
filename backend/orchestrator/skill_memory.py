"""Distills a successful tool-call trace into a reusable Recipe (tool sequence, param
slots, trigger keywords), matches future requests to one by keyword overlap, and
builds a replay plan via parameterize(). Used by turn_hooks.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_TRUE = ("1", "true", "yes", "on")


def skill_memory_enabled() -> bool:
    return os.getenv("FF_SKILL_MEMORY", "false").strip().lower() in _TRUE


@dataclass(frozen=True)
class Recipe:
    name: str
    tools: Tuple[str, ...]
    params: Tuple[str, ...]
    trigger_keywords: Tuple[str, ...]


def induce_recipe(
    name: str,
    trace: List[Dict],
    *,
    trigger_keywords: Optional[Any] = None,
) -> Recipe:
    if not trace:
        raise ValueError("cannot induce a recipe from an empty trace")

    tools: Tuple[str, ...] = tuple(str(step.get("tool", "")) for step in trace)

    param_set = set()
    for step in trace:
        args = step.get("args") or {}
        if isinstance(args, dict):
            param_set.update(str(k) for k in args.keys())
    params: Tuple[str, ...] = tuple(sorted(param_set))

    keywords: Tuple[str, ...] = _normalize_keywords(trigger_keywords)

    return Recipe(name=name, tools=tools, params=params, trigger_keywords=keywords)


def _normalize_keywords(trigger_keywords: Optional[Any]) -> Tuple[str, ...]:
    if not trigger_keywords:
        return ()
    seen: List[str] = []
    for kw in trigger_keywords:
        norm = str(kw).strip().lower()
        if norm and norm not in seen:
            seen.append(norm)
    return tuple(seen)


def _overlap(recipe: Recipe, request_lc: str) -> int:
    return sum(1 for kw in recipe.trigger_keywords if kw and kw in request_lc)


def match_recipe(
    recipes: List[Recipe],
    request: str,
    *,
    min_overlap: int = 1,
) -> Optional[Recipe]:
    request_lc = (request or "").lower()
    best: Optional[Recipe] = None
    best_key: Optional[Tuple[int, int]] = None

    for recipe in recipes:
        score = _overlap(recipe, request_lc)
        if score < min_overlap:
            continue
        key = (score, len(recipe.trigger_keywords))
        if best_key is None or key > best_key:
            best, best_key = recipe, key

    return best


def parameterize(recipe: Recipe, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    filled = {p: args[p] for p in recipe.params if p in args}
    return [{"tool": tool, "args": dict(filled)} for tool in recipe.tools]


def missing_params(recipe: Recipe, args: Dict[str, Any]) -> List[str]:
    return [p for p in recipe.params if p not in args]
