"""Feature 055 (US4) — bounded per-component version history (research D10).

``component_version`` archives a component dict immediately BEFORE a
refine/restore overwrites the live ``saved_components`` row, so every live
component keeps up to :data:`RETAIN` restorable prior states. Restores never
delete archived rows — the current dict is archived first and the chosen
version is copied back onto the live row — so pruning is count-based only,
enforced at archive time.

All reads and writes are scoped by ``(chat_id, user_id)`` exactly like the
workspace store (workspace.py). ``version_no`` is monotonic per
``(chat_id, component_id)`` and assigned under AstralPlane's row lock, so
concurrent archives serialize without Deep borrowing a driver connection.

Functions take an explicit application Plane source. ``a``-prefixed async
twins run the sync functions off the event loop (feature 052 loop guard).
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

# FR-024: newest versions retained per (chat_id, component_id).
RETAIN = 5

VALID_REASONS = ("refine", "restore")


def _iso(value: Any) -> Any:
    """TIMESTAMPTZ columns come back as datetimes; return wire-ready strings."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _plain_component(value: Any) -> Dict[str, Any]:
    """Thaw one detached Plane JSON object into Deep's mutable wire shape.

    Plane records deliberately freeze mappings and sequences after their
    transaction closes.  Component consumers in Deep operate on ordinary
    ``dict``/``list`` JSON values, so this boundary performs a strict copy
    without admitting non-JSON values from a malformed repository record.
    """

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
    if not isinstance(component, dict):  # defensive root-shape guard
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
    """Archive one component dict; returns the assigned ``version_no``.

    Called BEFORE a refine/restore overwrites the live row. Prunes rows
    beyond the newest :data:`RETAIN` for this component as a side effect.
    """
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
    """Bounded newest-first metadata list (no component payloads)."""
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
    """One archived version with its full component dict, or ``None``."""
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
    """Cascade: drop all versions of one deleted component. Returns row count."""
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
    """Cascade: drop all versions in a deleted chat (no chats FK on this table)."""
    if not chat_id or not user_id:
        return 0
    context = _context(source)
    return context.call(
        context.repository.versions.delete_for_conversation,
        owner_id=user_id,
        conversation_id=chat_id,
    )


# ── async facade (event-loop-safe twins of the sync functions above) ────────
async def aarchive(source, chat_id: str, user_id: str, component_id: str,
                   component: Dict[str, Any], reason: str = "refine") -> int:
    """Async twin of :func:`archive`, run off the event loop."""
    return await asyncio.to_thread(archive, source, chat_id, user_id,
                                   component_id, component, reason)


async def alist_versions(source, chat_id: str, user_id: str, component_id: str,
                         limit: int = RETAIN) -> List[Dict[str, Any]]:
    """Async twin of :func:`list_versions`, run off the event loop."""
    return await asyncio.to_thread(list_versions, source, chat_id, user_id,
                                   component_id, limit)


async def aget_version(source, chat_id: str, user_id: str, component_id: str,
                       version_no: Any) -> Optional[Dict[str, Any]]:
    """Async twin of :func:`get_version`, run off the event loop."""
    return await asyncio.to_thread(get_version, source, chat_id, user_id,
                                   component_id, version_no)


async def adelete_for_component(source, chat_id: str, user_id: str,
                                component_id: str) -> int:
    """Async twin of :func:`delete_for_component`, run off the event loop."""
    return await asyncio.to_thread(delete_for_component, source, chat_id,
                                   user_id, component_id)


async def adelete_for_chat(source, chat_id: str, user_id: str) -> int:
    """Async twin of :func:`delete_for_chat`, run off the event loop."""
    return await asyncio.to_thread(delete_for_chat, source, chat_id, user_id)
