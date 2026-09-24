"""Consolidates proven duplicate table presentations without changing stored workspace data.
The orchestrator applies this before adapting completed canvases for clients.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from orchestrator.source_details import present_source_details, prioritize_completed_dashboard

_CHILD_KEYS = ("content", "children")
_SOURCE_KEYS = ("_source_agent", "_source_tool", "_source_params", "_source_correlation_id", "provenance")
_WRAPPERS = {"card", "container", "grid", "collapsible"}
_INTERACTIONS = {"action", "actions", "events", "on_click", "editable", "selectable"}
_LIMIT_NODES = 4000
_LIMIT_TABLES = 128
_LIMIT_CELLS = 100000
_HEADER_ALIASES = {"% of ytd budget spent": "% of ytd spent"}


@dataclass
class _Table:
    node: dict[str, Any]
    headers: tuple[str, ...]
    rows: list[tuple[str, ...]]
    title: str
    source: dict[str, Any]
    preview: bool
    depth: int


def _label(value: Any) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _cell(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _header(value: str) -> str:
    normalized = _label(value)
    return _HEADER_ALIASES.get(normalized, normalized)


def _table(node: dict[str, Any], source: dict[str, Any], depth: int, context: str) -> _Table | None:
    headers, rows = node.get("headers"), node.get("rows")
    if not isinstance(headers, list) or not headers or not all(isinstance(h, str) for h in headers):
        return None
    labels = tuple(_header(header) for header in headers)
    if len(set(labels)) != len(labels) or not all(labels):
        return None
    if not isinstance(rows, list) or not rows:
        return None
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        return None
    if any(isinstance(cell, (dict, list)) for row in rows for cell in row):
        return None
    if _INTERACTIONS.intersection(node):
        return None
    title = node.get("title") or context
    try:
        normalized = [tuple(_cell(cell) for cell in row) for row in rows]
    except (TypeError, ValueError):
        return None
    preview = node.get("id") == "modify-data-preview" and str(source.get("_source_tool", "")).endswith("modify_data")
    return _Table(node, labels, normalized, _label(title), source, preview, depth)


def _compatible(left: _Table, right: _Table) -> bool:
    if not left.preview and not right.preview and left.title and right.title and left.title != right.title:
        return False
    left_source = left.source.get("_source_agent"), left.source.get("_source_tool")
    right_source = right.source.get("_source_agent"), right.source.get("_source_tool")
    copied_spreadsheet = _spreadsheet_dashboard_pair(left, right)
    if all(left_source) and all(right_source) and left_source != right_source:
        if not (left.preview or right.preview or copied_spreadsheet):
            return False
    if not left.preview and not right.preview:
        for key in ("description", "caption", "footer", "footnotes"):
            if left.node.get(key) != right.node.get(key):
                return False
        left_params, right_params = left.source.get("_source_params"), right.source.get("_source_params")
        if isinstance(left_params, dict) and isinstance(right_params, dict) and not copied_spreadsheet:
            context_keys = (set(left_params) | set(right_params)) - {"title", "description", "rows", "columns", "data"}
            if any(left_params.get(key) != right_params.get(key) for key in context_keys if not key.startswith("_")):
                return False
    return True


def _spreadsheet_dashboard_pair(left: _Table, right: _Table) -> bool:
    return (left.source.get("_source_agent") == right.source.get("_source_agent") == "connectors-1"
            and {str(item.source.get("_source_tool", "")).rsplit("__", 1)[-1] for item in (left, right)}
            == {"excel_generate", "interactive_artifacts"})


def _header_indices(full: _Table, part: _Table) -> list[int] | None:
    indices = []
    for header in part.headers:
        if header in full.headers:
            indices.append(full.headers.index(header))
            continue
        return None
    return indices if len(set(indices)) == len(indices) else None


def _contains(full: _Table, part: _Table) -> bool:
    if not _compatible(full, part) or len(full.rows) < len(part.rows):
        return False
    if not part.preview and len(part.headers) != len(full.headers):
        return False
    indices = _header_indices(full, part)
    if indices is None:
        return False
    full_rows = Counter(tuple(row[index] for index in indices) for row in full.rows)
    part_rows = Counter(part.rows)
    return not (part_rows - full_rows) if part.preview else part_rows == full_rows


def consolidate_canvas(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = copy.deepcopy(components)
    tables: list[_Table] = []
    nodes = cells = 0

    def collect(items: Any, source: dict[str, Any], depth: int, context: str = "") -> None:
        nonlocal nodes, cells
        if not isinstance(items, list):
            return
        for node in items:
            if not isinstance(node, dict):
                continue
            node.update(present_source_details(node))
            nodes += 1
            if nodes > _LIMIT_NODES or depth > 40:
                raise ValueError("canvas bound exceeded")
            inherited = dict(source)
            inherited.update({key: node[key] for key in _SOURCE_KEYS if key in node})
            if node.get("type") == "table":
                entry = _table(node, inherited, depth, context)
                if entry is not None:
                    tables.append(entry)
                    cells += len(entry.headers) * len(entry.rows)
                    if len(tables) > _LIMIT_TABLES or cells > _LIMIT_CELLS:
                        raise ValueError("table bound exceeded")
            for key in _CHILD_KEYS:
                collect(node.get(key), inherited, depth + 1, node.get("title") or context)
            tabs = node.get("tabs")
            if isinstance(tabs, list):
                for tab in tabs:
                    if isinstance(tab, dict):
                        collect(tab.get("content"), inherited, depth + 1, tab.get("label") or context)

    try:
        collect(output, {}, 0)
    except ValueError:
        return output
    removed: set[int] = set()
    for position, candidate in enumerate(tables):
        if id(candidate.node) in removed:
            continue
        for other in tables[position + 1:]:
            if id(other.node) in removed:
                continue
            candidate_contains = _contains(candidate, other)
            other_contains = _contains(other, candidate)
            if not candidate_contains and not other_contains:
                continue
            if other_contains:
                winner, loser = other, candidate
            else:
                winner, loser = candidate, other
            if loser.source.get("_source_tool") and not loser.preview and not winner.source.get("_source_tool"):
                winner, loser = loser, winner
            if _spreadsheet_dashboard_pair(winner, loser) and str(loser.source.get("_source_tool", "")).endswith("excel_generate"):
                winner, loser = loser, winner
            if not winner.title and loser.title and not loser.preview:
                winner.node["title"] = loser.node.get("title") or (loser.source.get("_source_params") or {}).get("title") or loser.title
                winner.title = loser.title
            indices = _header_indices(winner, loser)
            if indices is not None:
                for old_index, new_index in enumerate(indices):
                    if len(loser.node["headers"][old_index]) > len(winner.node["headers"][new_index]):
                        winner.node["headers"][new_index] = loser.node["headers"][old_index]
                winner.headers = tuple(_header(header) for header in winner.node["headers"])
            removed.add(id(loser.node))
            if loser is candidate:
                break

    downloads: set[str] = set()

    def prune(items: list[Any]) -> list[Any]:
        kept = []
        for node in items:
            if not isinstance(node, dict):
                kept.append(node)
                continue
            if id(node) in removed:
                continue
            if node.get("type") == "file_download" and isinstance(node.get("url"), str):
                public = {key: value for key, value in node.items() if key not in _SOURCE_KEYS and key not in {"id", "component_id", "_presentation"}}
                signature = json.dumps(public, sort_keys=True, ensure_ascii=False)
                if signature in downloads:
                    continue
                downloads.add(signature)
            had_children = False
            for key in _CHILD_KEYS:
                if isinstance(node.get(key), list):
                    had_children = had_children or bool(node[key])
                    node[key] = prune(node[key])
            tabs = node.get("tabs")
            if isinstance(tabs, list):
                for tab in tabs:
                    if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                        tab["content"] = prune(tab["content"])
            if had_children and node.get("type") in _WRAPPERS and not _INTERACTIONS.intersection(node) and not any(node.get(key) for key in _CHILD_KEYS):
                continue
            kept.append(node)
        return kept

    return prioritize_completed_dashboard(prune(output))
