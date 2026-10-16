"""Federated, owner-scoped text search across the authorized corpus — durable memory,
private notes, and conversations with visible messages — returning source-linked hits
with honest per-source availability; consumed by the Search chrome surface adapter.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from personalization.explicit_note_service import SEARCH_SCAN_LIMIT

logger = logging.getLogger("personalization.source_search")

SOURCES = ("conversations", "notes", "memory")
SOURCE_LABELS = {
    "conversations": "Conversations",
    "notes": "Private notes",
    "memory": "Durable memory",
}
MAX_QUERY_BYTES = 256
MAX_QUERY_CHARS = 200
MAX_PAGE_LIMIT = 50
DEFAULT_PAGE_LIMIT = 20
CONVERSATION_SCAN_LIMIT = 40
MESSAGE_SCAN_LIMIT = 400
_SNIPPET_CHARS = 160
_CONTEXT_CHARS = 32

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SearchQueryInvalid(ValueError):
    pass


class SourceUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SearchHit:
    source: str
    ref: dict
    title: str
    snippet: str
    updated_at: int
    rank: float


@dataclass(frozen=True)
class SourceReport:
    source: str
    available: bool
    complete: bool
    matched: int
    scanned: int


@dataclass(frozen=True)
class SearchOutcome:
    query: str
    source: Optional[str]
    hits: tuple[SearchHit, ...] = field(default_factory=tuple)
    reports: tuple[SourceReport, ...] = field(default_factory=tuple)
    next_offset: Optional[int] = None

    @property
    def any_available(self) -> bool:
        return any(report.available for report in self.reports)

    @property
    def partial(self) -> bool:
        return any(report.available and not report.complete for report in self.reports)


def normalize_query(query) -> str:
    if not isinstance(query, str):
        raise SearchQueryInvalid("Enter a search term.")
    cleaned = " ".join(query.split())
    if not cleaned:
        raise SearchQueryInvalid("Enter a search term.")
    if "\x00" in cleaned:
        raise SearchQueryInvalid("Search terms cannot contain control characters.")
    if len(cleaned.encode("utf-8")) > MAX_QUERY_BYTES or len(cleaned) > MAX_QUERY_CHARS:
        raise SearchQueryInvalid("Search is limited to 200 characters; use a shorter term.")
    return cleaned


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": max(0, int(offset))}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor) -> int:
    if cursor in (None, ""):
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > 64 or not cursor.isascii():
        raise SearchQueryInvalid("That result page is no longer valid; start a new search.")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, TypeError):
        raise SearchQueryInvalid("That result page is no longer valid; start a new search.") from None
    offset = value.get("o") if isinstance(value, dict) else None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > 10_000:
        raise SearchQueryInvalid("That result page is no longer valid; start a new search.")
    return offset


def form_params(params, fields) -> dict:
    params = dict(params) if isinstance(params, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    merged = dict(params)
    raw = fields.get("q", params.get("q", ""))
    if "q" in fields or (isinstance(raw, str) and raw.strip()):
        try:
            merged["q"] = normalize_query(raw)
        except SearchQueryInvalid as exc:
            merged.pop("cursor", None)
            merged["q"] = raw if isinstance(raw, str) else ""
            merged["error"] = str(exc)
            return merged
    elif not isinstance(raw, str):
        merged.pop("q", None)
    source = fields.get("source", params.get("source", ""))
    if isinstance(source, str) and source in SOURCES:
        merged["source"] = source
    else:
        merged.pop("source", None)
    if "cursor" in fields or "cursor" in params or merged.get("q"):
        try:
            offset = decode_cursor(fields.get("cursor", params.get("cursor")))
        except SearchQueryInvalid as exc:
            merged["error"] = str(exc)
            merged.pop("cursor", None)
            return merged
        if offset:
            merged["cursor"] = encode_cursor(offset)
        else:
            merged.pop("cursor", None)
    merged.pop("error", None)
    return merged


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _score(haystack: str, query: str, updated_at: int, newest: int) -> float:
    lowered = (haystack or "").lower()
    tokens = _tokens(query)
    overlap = len(tokens & _tokens(lowered)) / len(tokens) if tokens else 0.0
    phrase = 1.0 if query.lower() in lowered else 0.0
    recency = min(1.0, updated_at / newest) if newest > 0 and updated_at > 0 else 0.0
    return round(0.5 * phrase + 0.3 * overlap + 0.2 * recency, 6)


def _snippet(text: str, query: str) -> str:
    hay = " ".join((text or "").split())
    lowered = hay.lower()
    at = lowered.find(query.lower())
    if at < 0:
        return (hay[:_SNIPPET_CHARS] + "...") if len(hay) > _SNIPPET_CHARS else hay
    start = max(0, at - _CONTEXT_CHARS)
    end = min(len(hay), at + len(query) + _SNIPPET_CHARS - _CONTEXT_CHARS)
    return ("..." if start else "") + hay[start:end] + ("..." if end < len(hay) else "")


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _repo(orch):
    service = getattr(orch, "personalization_service", None)
    return getattr(service, "repo", None) if service is not None else None


def _history_context(orch):
    from orchestrator.plane_repository_context import (
        PlaneRepositoryContext,
        plane_source_from_orchestrator,
        repository_from,
    )

    source = plane_source_from_orchestrator(orch)
    repository, runtime = repository_from(
        "history", plane_runtime=source.plane_runtime, repositories=source.plane_repositories)
    return PlaneRepositoryContext(repository=repository, plane_runtime=runtime)


def _memory_hits(orch, user_id: str, query: str, *, bound: int) -> tuple[list, SourceReport]:
    repo = _repo(orch)
    if repo is None or not hasattr(repo, "list_memory"):
        return [], SourceReport("memory", False, False, 0, 0)
    try:
        records = list(repo.list_memory(user_id) or [])
    except Exception:
        logger.exception("source_search: memory scan failed for user %s", user_id)
        return [], SourceReport("memory", False, False, 0, 0)
    complete = len(records) < SEARCH_SCAN_LIMIT
    records = records[:bound]
    newest = max((int(row.get("updated_at") or 0) for row in records
                  if isinstance(row, dict)), default=0)
    lowered = query.lower()
    hits = []
    for row in records:
        if not isinstance(row, dict):
            continue
        value = " ".join(str(row.get(key) or "") for key in ("value", "keywords", "category"))
        if lowered not in value.lower():
            continue
        memory_id = str(row.get("id") or "")
        if not memory_id:
            continue
        updated = int(row.get("updated_at") or 0)
        hits.append(SearchHit(
            source="memory", ref={"kind": "memory", "memory_id": memory_id},
            title=str(row.get("category") or "memory"),
            snippet=_snippet(value, query), updated_at=updated,
            rank=_score(value, query, updated, newest),
        ))
    return hits, SourceReport("memory", True, complete, len(hits), len(records))


async def _note_hits(orch, caller, query: str, *, bound: int) -> tuple[list, SourceReport]:
    from personalization.explicit_note_service import ExplicitNoteService

    service = getattr(orch, "explicit_notes", None)
    if caller is None or type(service) is not ExplicitNoteService:
        return [], SourceReport("notes", False, False, 0, 0)
    hits: list[SearchHit] = []
    scanned, cursor, complete = 0, None, True
    try:
        while scanned < SEARCH_SCAN_LIMIT and len(hits) < bound:
            page = await service.list(caller=caller, after_id=cursor,
                                      limit=min(100, bound - len(hits)), search=query)
            records = list(page.notes)
            scanned += len(records)
            for note in records:
                metadata = note.metadata
                hits.append(SearchHit(
                    source="notes",
                    ref={"kind": "note", "note_id": metadata.note_id,
                         "expected_revision": metadata.revision},
                    title=str(metadata.category or "note"),
                    snippet=_snippet(note.value, query),
                    updated_at=int(metadata.updated_at or 0),
                    rank=1.0,
                ))
                if len(hits) >= bound:
                    break
            cursor = page.next_cursor
            if cursor is None:
                break
        complete = cursor is None and scanned < SEARCH_SCAN_LIMIT and len(hits) < bound
        return hits[:bound], SourceReport("notes", True, complete, len(hits), scanned)
    except Exception:
        logger.exception("source_search: note scan failed")
        return [], SourceReport("notes", False, False, 0, 0)


def _conversation_hits(orch, user_id: str, query: str, *, bound: int) -> tuple[list, SourceReport]:
    try:
        context = _history_context(orch)
        conversations = list(context.call(
            context.repository.conversations.list_recent_nonempty,
            owner_id=user_id, limit=CONVERSATION_SCAN_LIMIT) or [])
    except Exception:
        logger.exception("source_search: conversation scan failed for user %s", user_id)
        return [], SourceReport("conversations", False, False, 0, 0)
    complete = len(conversations) < CONVERSATION_SCAN_LIMIT
    newest = max((int(getattr(chat, "updated_at", 0) or 0) for chat in conversations), default=0)
    lowered = query.lower()
    hits: list[SearchHit] = []
    scanned = 0
    truncated = False
    for chat in conversations:
        if scanned >= MESSAGE_SCAN_LIMIT or len(hits) >= bound:
            truncated = True
            break
        chat_id = getattr(chat, "conversation_id", None)
        if not chat_id:
            continue
        title = str(getattr(chat, "title", "") or "")
        updated = int(getattr(chat, "updated_at", 0) or 0)
        try:
            messages = list(context.call(
                context.repository.messages.list_visible,
                owner_id=user_id, conversation_id=str(chat_id),
                limit=MESSAGE_SCAN_LIMIT - scanned) or [])
        except Exception:
            logger.debug("source_search: message scan failed for chat %s", chat_id, exc_info=True)
            messages = []
        if len(messages) >= MESSAGE_SCAN_LIMIT - scanned:
            truncated = True
        scanned += len(messages)
        best = None
        for message in messages:
            text = _message_text(getattr(message, "content", None))
            if not text or lowered not in text.lower():
                continue
            stamped = int(getattr(message, "timestamp", 0) or 0)
            candidate = SearchHit(
                source="conversations", ref={"kind": "conversation", "chat_id": str(chat_id)},
                title=title or "Conversation", snippet=_snippet(text, query),
                updated_at=max(stamped, updated),
                rank=_score(text, query, stamped, newest),
            )
            if best is None or (candidate.rank, candidate.updated_at) > (best.rank, best.updated_at):
                best = candidate
            if len(hits) + 1 >= bound and best is not None:
                break
        if best is not None:
            hits.append(best)
        elif lowered in title.lower():
            hits.append(SearchHit(
                source="conversations", ref={"kind": "conversation", "chat_id": str(chat_id)},
                title=title or "Conversation", snippet=_snippet(title, query),
                updated_at=updated, rank=_score(title, query, updated, newest),
            ))
        if len(hits) >= bound:
            truncated = True
            break
    return hits[:bound], SourceReport("conversations", True, complete and not truncated,
                                      len(hits), scanned)


async def federated_search(orch, *, user_id: str, query, source=None,
                           limit: int = DEFAULT_PAGE_LIMIT, cursor=None,
                           notes_caller=None) -> SearchOutcome:
    cleaned = normalize_query(query)
    if source is not None and source not in SOURCES:
        raise SearchQueryInvalid("Unknown source filter.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_PAGE_LIMIT:
        raise SearchQueryInvalid("Invalid page size.")
    offset = decode_cursor(cursor)
    selected = (source,) if source else SOURCES
    merged: list[SearchHit] = []
    reports: list[SourceReport] = []
    for name in selected:
        if name == "notes":
            hits, report = await _note_hits(orch, notes_caller, cleaned, bound=SEARCH_SCAN_LIMIT)
        else:
            try:
                hits, report = await asyncio.to_thread(
                    _conversation_hits if name == "conversations" else _memory_hits,
                    orch, user_id, cleaned, bound=SEARCH_SCAN_LIMIT)
            except Exception:
                logger.exception("source_search: %s scan failed for user %s", name, user_id)
                hits, report = [], SourceReport(name, False, False, 0, 0)
        reports.append(report)
        merged.extend(hits)
    merged.sort(key=lambda hit: (-hit.rank, -hit.updated_at, hit.source,
                                 json.dumps(hit.ref, sort_keys=True)))
    deduped: list[SearchHit] = []
    seen: set[str] = set()
    for hit in merged:
        key = f"{hit.source}:{json.dumps(hit.ref, sort_keys=True)}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    page = tuple(deduped[offset:offset + limit])
    next_offset = offset + limit if offset + limit < len(deduped) else None
    return SearchOutcome(query=cleaned, source=source, hits=page,
                         reports=tuple(reports), next_offset=next_offset)


async def resolve_source(orch, *, user_id: str, ref) -> Optional[str]:
    """Returns an honest unavailability reason, or None while the source still exists."""
    if not isinstance(ref, dict):
        return "This result is no longer available."
    kind = ref.get("kind")
    if kind == "conversation":
        chat_id = ref.get("chat_id")
        chat = None
        if chat_id:
            try:
                context = _history_context(orch)
                chat = context.call(context.repository.conversations.get,
                                    owner_id=user_id, conversation_id=str(chat_id))
            except Exception:
                logger.debug("source_search: conversation resolve failed", exc_info=True)
        return None if chat is not None else "That conversation is no longer available."
    if kind == "memory":
        repo = _repo(orch)
        memory_id = ref.get("memory_id")
        record = None
        if repo is not None and memory_id and hasattr(repo, "get_memory"):
            try:
                record = repo.get_memory(user_id, str(memory_id))
            except Exception:
                logger.debug("source_search: memory resolve failed", exc_info=True)
        return None if record is not None else "That memory is no longer available."
    if kind == "note":
        return None
    return "This result is no longer available."
