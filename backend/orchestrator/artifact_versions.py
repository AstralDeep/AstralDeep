"""Bounded per-component version history: archive() snapshots a component into
component_version immediately before a refine/restore overwrites the live
saved_components row, keeping the newest 5 versions per component.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

RETAIN = 5

VALID_REASONS = ("refine", "restore")


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _plain_component(value: Any) -> Dict[str, Any]:
    def thaw(item: Any, path: str, seen: frozenset[int]) -> Any:
        if item is None or type(item) in {str, bool, int}:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return item
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise ValueError(f"{path} contains a reference cycle")
            nested_seen = seen | {identity}
            plain: Dict[str, Any] = {}
            for key, nested in item.items():
                if type(key) is not str:
                    raise ValueError(f"{path} contains a non-string object key")
                plain[key] = thaw(nested, f"{path}.{key}", nested_seen)
            return plain
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen:
                raise ValueError(f"{path} contains a reference cycle")
            nested_seen = seen | {identity}
            return [
                thaw(nested, f"{path}[{index}]", nested_seen)
                for index, nested in enumerate(item)
            ]
        raise ValueError(f"{path} contains a non-JSON value")

    if not isinstance(value, Mapping):
        raise ValueError("artifact version component must be a JSON object")
    component = thaw(value, "component", frozenset())
    if not isinstance(component, dict):
        raise ValueError("artifact version component must be a JSON object")
    return component


def _context(source) -> PlaneRepositoryContext:
    runtime = getattr(source, "plane_runtime", None)
    repositories = getattr(source, "plane_repositories", None)
    repository, runtime = repository_from(
        "artifacts",
        plane_runtime=runtime,
        repositories=repositories,
        legacy_database=None,
    )
    return PlaneRepositoryContext(
        repository=repository,
        plane_runtime=runtime,
    )


def archive(source, chat_id: str, user_id: str, component_id: str,
            component: Dict[str, Any], reason: str = "refine") -> int:
    if not chat_id or not user_id or not component_id:
        raise ValueError("archive requires chat_id, user_id and component_id")
    if not isinstance(component, dict):
        raise ValueError("archive requires a component dict")
    if reason not in VALID_REASONS:
        raise ValueError(f"unknown archive reason {reason!r}")

    context = _context(source)
    record = context.call(
        context.repository.versions.archive,
        owner_id=user_id,
        conversation_id=chat_id,
        component_id=component_id,
        component=component,
        reason=reason,
        retain=RETAIN,
    )
    return record.version_number


def list_versions(source, chat_id: str, user_id: str, component_id: str,
                  limit: int = RETAIN) -> List[Dict[str, Any]]:
    if not chat_id or not user_id or not component_id:
        return []
    try:
        limit = max(1, min(int(limit), RETAIN))
    except (TypeError, ValueError):
        limit = RETAIN
    context = _context(source)
    records = context.call(
        context.repository.versions.list_for_component,
        owner_id=user_id,
        conversation_id=chat_id,
        component_id=component_id,
        limit=limit,
    )
    return [
        {
            "id": record.version_id,
            "version_no": record.version_number,
            "reason": record.reason,
            "created_at": _iso(record.created_at),
            "title": record.component.get("title"),
            "component_type": record.component.get("type"),
        }
        for record in records
    ]


def get_version(source, chat_id: str, user_id: str, component_id: str,
                version_no: Any) -> Optional[Dict[str, Any]]:
    if not chat_id or not user_id or not component_id:
        return None
    try:
        version_no = int(version_no)
    except (TypeError, ValueError):
        return None
    context = _context(source)
    record = context.call(
        context.repository.versions.get,
        owner_id=user_id,
        conversation_id=chat_id,
        component_id=component_id,
        version_number=version_no,
    )
    if record is None:
        return None
    return {
        "id": record.version_id,
        "chat_id": chat_id,
        "component_id": component_id,
        "version_no": record.version_number,
        "reason": record.reason,
        "created_at": _iso(record.created_at),
        "component": _plain_component(record.component),
    }


def delete_for_component(source, chat_id: str, user_id: str, component_id: str) -> int:
    if not chat_id or not user_id or not component_id:
        return 0
    context = _context(source)
    return context.call(
        context.repository.versions.delete_for_component,
        owner_id=user_id,
        conversation_id=chat_id,
        component_id=component_id,
    )


def delete_for_chat(source, chat_id: str, user_id: str) -> int:
    if not chat_id or not user_id:
        return 0
    context = _context(source)
    return context.call(
        context.repository.versions.delete_for_conversation,
        owner_id=user_id,
        conversation_id=chat_id,
    )


async def aarchive(source, chat_id: str, user_id: str, component_id: str,
                   component: Dict[str, Any], reason: str = "refine") -> int:
    return await asyncio.to_thread(archive, source, chat_id, user_id,
                                   component_id, component, reason)


async def alist_versions(source, chat_id: str, user_id: str, component_id: str,
                         limit: int = RETAIN) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(list_versions, source, chat_id, user_id,
                                   component_id, limit)


async def aget_version(source, chat_id: str, user_id: str, component_id: str,
                       version_no: Any) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_version, source, chat_id, user_id,
                                   component_id, version_no)


async def adelete_for_component(source, chat_id: str, user_id: str,
                                component_id: str) -> int:
    return await asyncio.to_thread(delete_for_component, source, chat_id,
                                   user_id, component_id)


async def adelete_for_chat(source, chat_id: str, user_id: str) -> int:
    return await asyncio.to_thread(delete_for_chat, source, chat_id, user_id)
