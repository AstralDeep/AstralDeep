"""Pure helpers for the optional project_id memory boundary: namespacing keys, filtering
rows to a project-or-global slice, and layering project instructions over global
ones. Consumed by memory_tools.py and repository.py; inert when its flag is off.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

GLOBAL = "__global__"


def project_scope_enabled() -> bool:
    return os.getenv("FF_PROJECT_MEMORY", "false").strip().lower() in ("1", "true", "yes", "on")


def normalize_project(project_id: Optional[str]) -> str:
    pid = (project_id or "").strip()
    return pid or GLOBAL


def scope_key(user_id: str, project_id: Optional[str]) -> str:
    return f"{user_id}\x1f{normalize_project(project_id)}"


def filter_to_project(items: List[Dict[str, Any]], project_id: Optional[str], *,
                      key: str = "project_id", include_global: bool = True) -> List[Dict[str, Any]]:
    target = normalize_project(project_id)
    out: List[Dict[str, Any]] = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        item_pid = normalize_project(it.get(key))
        if item_pid == target:
            out.append(it)
        elif include_global and item_pid == GLOBAL and target != GLOBAL:
            out.append(it)
    return out


def visible_in(item: Dict[str, Any], project_id: Optional[str], *, key: str = "project_id") -> bool:
    target = normalize_project(project_id)
    item_pid = normalize_project(item.get(key) if isinstance(item, dict) else None)
    return item_pid == target or (item_pid == GLOBAL and target != GLOBAL)


def layer_instructions(global_instructions: str, project_instructions: str) -> str:
    g = (global_instructions or "").strip()
    p = (project_instructions or "").strip()
    if not p:
        return g
    if not g:
        return p
    return f"{g}\n\n## Project context\n{p}"
