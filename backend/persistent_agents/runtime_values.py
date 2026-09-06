"""Bounded deterministic interpretation of untrusted observations/proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any

from persistent_agents.dispatch_context import canonical


def thaw(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: thaw(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(thaw(value)).encode("utf-8")).hexdigest()


def bounded_context(context: dict[str, Any]) -> dict[str, Any]:
    """Fit evidence into a durable request, retaining owner instructions intact.

    An explicit excerpt identifies omitted evidence. Original completed results
    and their digests remain in Plane; this does not silently summarize them.
    """
    return _bounded_context(context, (4096, 2048, 1024, 512, 256, 128, 64, 32))


def legacy_bounded_context(context: dict[str, Any]) -> dict[str, Any]:
    """Preserve original intent binding and the fallback for serialized requests."""
    return _bounded_context(context, (512, 256, 128, 64, 32))


def _bounded_context(context: dict[str, Any], limits: tuple[int, ...]) -> dict[str, Any]:
    """Apply reviewed excerpt sizes while preserving the 5,500-byte ceiling."""
    context = thaw(context)
    if len(canonical(context).encode("utf-8")) <= 5500:
        return context

    def excerpt(value, limit):
        if isinstance(value, str) and len(value.encode("utf-8")) > limit:
            prefix = value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            return prefix + " [evidence excerpt]"
        if isinstance(value, dict):
            return {key: child if key.endswith(("_id", "_digest"))
                    else excerpt(child, limit) for key, child in value.items()}
        if isinstance(value, list):
            return [excerpt(child, limit) for child in value]
        return value

    for limit in limits:
        bounded = {key: value if key in {"instructions", "instruction", "completion_condition", "tools"}
                   else excerpt(value, limit) for key, value in context.items()}
        if len(canonical(bounded).encode("utf-8")) <= 5500:
            return bounded
    raise ValueError("assignment_model_input_limit")


def extract_result(response: Any) -> dict[str, Any]:
    if response is None or getattr(response, "error", None):
        raise ValueError("assignment_source_failed")
    parts: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 16:
            raise ValueError("assignment_source_limit")
        if isinstance(value, Mapping):
            if str(value.get("type", "")).lower() == "alert" and value.get("variant") == "error":
                raise ValueError("assignment_source_failed")
            for key, entry in value.items():
                if key in {"text", "content", "markdown", "message"} and isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, (Mapping, list, tuple)):
                    walk(entry, depth + 1)
        elif isinstance(value, (list, tuple)):
            for entry in value:
                walk(entry, depth + 1)
        if sum(map(len, parts)) > 65536:
            raise ValueError("assignment_source_limit")

    result = getattr(response, "result", None)
    walk(getattr(response, "ui_components", None))
    if isinstance(result, str):
        parts.append(result)
    elif isinstance(result, Mapping):
        walk(result)
    text = "\n".join(dict.fromkeys(parts)).strip()
    # Reader tools may return structured records without UI. Keep their actual
    # data in the revision too, omitting UI-only metadata with unstable IDs.
    data = thaw(result.get("_data", result)) if isinstance(result, Mapping) else None
    if not text and not data:
        raise ValueError("assignment_source_empty")
    normalized = {"text": text, "data": data}
    if len(canonical(normalized).encode("utf-8")) > 65536:
        raise ValueError("assignment_source_limit")
    return normalized


def _object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 32768:
        raise ValueError("assignment_model_output_limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("assignment_model_duplicate_key")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if not isinstance(value, dict):
        raise TypeError("assignment_model_object_required")
    return value


def parse_plan(text: str, allowed: set[str], maximum: int) -> list[dict[str, Any]]:
    value = _object(text)
    tasks = value.get("tasks")
    if set(value) != {"tasks"} or not isinstance(tasks, list) or not 1 <= len(tasks) <= maximum:
        raise ValueError("assignment_task_plan_invalid")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"id", "instruction", "tools", "depends_on"}:
            raise ValueError("assignment_task_plan_invalid")
        identity, instruction = task["id"], task["instruction"]
        tools, dependencies = task["tools"], task["depends_on"]
        if (not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", identity)
                or identity in seen or not isinstance(instruction, str)
                or not 1 <= len(instruction) <= 4096
                or not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)
                or not set(tools) <= allowed or len(set(tools)) != len(tools)
                or not isinstance(dependencies, list)
                or any(not isinstance(d, str) for d in dependencies)
                or not set(dependencies) <= seen or len(set(dependencies)) != len(dependencies)):
            raise ValueError("assignment_task_plan_invalid")
        # Topological order is part of this bounded contract: cycles, forward
        # references and missing dependencies all fail before a plan is stored.
        seen.add(identity)
    return tasks


def parse_step(text: str, allowed: set[str]) -> dict[str, Any]:
    value = _object(text)
    if value.get("kind") == "result":
        if (set(value) != {"kind", "text"} or not isinstance(value["text"], str)
                or not 1 <= len(value["text"]) <= 8192):
            raise ValueError("assignment_step_invalid")
    elif value.get("kind") == "tool":
        if (set(value) != {"kind", "tool", "arguments"}
                or value.get("tool") not in allowed or not isinstance(value["arguments"], dict)
                or any(str(k).startswith("_") or k in {"user_id", "session_id", "consent"}
                       for k in value["arguments"])
                or len(canonical(value["arguments"]).encode("utf-8")) > 16384):
            raise ValueError("assignment_step_invalid")
        from persistent_agents.models import SourceSelection
        SourceSelection.bounded_arguments(value["arguments"])
    else:
        raise ValueError("assignment_step_invalid")
    return value


def parse_completion(text: str, completion_condition: str | None) -> dict[str, Any]:
    value = _object(text)
    if (set(value) != {"kind", "text", "completed"} or value["kind"] != "result"
            or not isinstance(value["text"], str) or not 1 <= len(value["text"]) <= 8192
            or type(value["completed"]) is not bool
            or (value["completed"] and not completion_condition)):
        raise ValueError("assignment_completion_invalid")
    return value
