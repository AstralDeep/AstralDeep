"""Federates owner-scoped notes, memory, conversations, and Atlas pages into one bounded,
deterministically ranked result stream with sanitized snippets and owner/query-bound cursors.
search_api.py exposes it over authenticated HTTP and search-time delivery rechecks each page.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import re
import time
import unicodedata
from typing import Any

from personalization.atlas_pages import parse_envelope
from personalization.explicit_notes import (
    EncryptedExplicitNote,
    ExplicitNoteCipher,
    ExplicitNoteMetadata,
    ExplicitNoteUnavailable,
)
from personalization.project_scope import GLOBAL as GLOBAL_MEMORY_PROJECT

logger = logging.getLogger("personalization.federated_search")

SOURCES = ("notes", "memory", "conversations", "atlas")
SOURCE_LABELS = {
    "notes": "Private notes",
    "memory": "Durable memory",
    "conversations": "Conversations",
    "atlas": "Atlas",
}
SOURCE_ORDER = {name: index for index, name in enumerate(SOURCES)}
ATLAS_TYPE = "atlas_page"
CONVERSATION_TYPE = "conversation"
MAX_QUERY_BYTES = 256
MAX_QUERY_CHARS = 200
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 20
MAX_HITS_PER_SOURCE = 1000
NOTE_SCAN_LIMIT = 1000
MEMORY_SCAN_LIMIT = 1000
CONVERSATION_SCAN_LIMIT = 40
MESSAGE_SCAN_LIMIT = 400
ATLAS_SCAN_LIMIT = 500
MAX_CURSOR_BYTES = 2048
MAX_SOURCE_CHARS = 64 * 1024
_SNIPPET_CHARS = 180
_CONTEXT_CHARS = 40
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TAG_RE = re.compile(r"<[^>]{0,2048}>")
_CURSOR_VERSION = 2


class SearchQueryInvalid(ValueError):
    def __init__(self, code: str = "search_query_invalid"):
        super().__init__(code)
        self.code = code


class SourceUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SearchHit:
    source: str
    type: str
    ref: dict[str, Any]
    title: str
    snippet: str
    updated_at: int
    score: int
    rank_key: tuple = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SourceReport:
    source: str
    available: bool
    complete: bool
    scanned: int
    matched: int
    returned: int = 0
    stale: int = 0


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    query: str
    sources: tuple[str, ...]
    types: tuple[str, ...]
    project_id: str | None
    hits: tuple[SearchHit, ...]
    reports: tuple[SourceReport, ...]
    total: int
    next_cursor: str | None

    @property
    def any_available(self) -> bool:
        return any(report.available for report in self.reports)

    @property
    def partial(self) -> bool:
        return any(not report.available or not report.complete for report in self.reports)


@dataclass(frozen=True, slots=True)
class SearchSource:
    source: str
    ref: dict[str, Any]
    title: str
    text: str
    complete: bool


def normalize_query(value: object) -> str:
    if not isinstance(value, str):
        raise SearchQueryInvalid("search_query_required")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise SearchQueryInvalid("search_query_required")
    if any(unicodedata.category(character).startswith("C") for character in cleaned):
        raise SearchQueryInvalid("search_query_control_characters")
    if len(cleaned) > MAX_QUERY_CHARS or len(cleaned.encode("utf-8")) > MAX_QUERY_BYTES:
        raise SearchQueryInvalid("search_query_too_long")
    return cleaned


def normalize_sources(value: object) -> tuple[str, ...]:
    if value is None or value == "" or value == ():
        return SOURCES
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise SearchQueryInvalid("search_source_invalid")
    selected: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise SearchQueryInvalid("search_source_invalid")
        name = part.strip()
        if not name:
            continue
        if name not in SOURCES:
            raise SearchQueryInvalid("search_source_invalid")
        if name not in selected:
            selected.append(name)
    if not selected:
        raise SearchQueryInvalid("search_source_invalid")
    return tuple(sorted(selected, key=SOURCE_ORDER.__getitem__))


def known_types() -> frozenset[str]:
    from personalization.explicit_notes import NOTE_CATEGORIES
    from personalization.repository import MEMORY_CATEGORIES

    return frozenset({*NOTE_CATEGORIES, *MEMORY_CATEGORIES, CONVERSATION_TYPE, ATLAS_TYPE})


def normalize_types(value: object) -> tuple[str, ...]:
    if value is None or value == "" or value == ():
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise SearchQueryInvalid("search_type_invalid")
    allowed = known_types()
    selected: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise SearchQueryInvalid("search_type_invalid")
        name = part.strip()
        if not name:
            continue
        if name not in allowed:
            raise SearchQueryInvalid("search_type_invalid")
        if name not in selected:
            selected.append(name)
    return tuple(sorted(selected))


def normalize_project_filter(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SearchQueryInvalid("search_project_invalid")
    project = value.strip()
    if not project:
        raise SearchQueryInvalid("search_project_invalid")
    if project == "-":
        return "-"
    if len(project.encode("utf-8")) > 128 or any(
        unicodedata.category(character).startswith("C") for character in project
    ):
        raise SearchQueryInvalid("search_project_invalid")
    return project


def sanitize_text(value: object) -> str:
    if value is None:
        return ""
    try:
        text = unicodedata.normalize("NFC", str(value))
    except (TypeError, ValueError, UnicodeError):
        return ""
    kept: list[str] = []
    for character in text:
        if character in "\n\t":
            kept.append(" ")
        elif unicodedata.category(character).startswith("C"):
            continue
        else:
            kept.append(character)
    return " ".join(_TAG_RE.sub(" ", "".join(kept)).split())


def make_snippet(*values: object, query: str) -> str:
    text = sanitize_text(" ".join(
        value for value in values if isinstance(value, str) and value
    ))
    if not text:
        return ""
    at = text.casefold().find(query.casefold())
    if at < 0:
        return text[:_SNIPPET_CHARS] + ("..." if len(text) > _SNIPPET_CHARS else "")
    start = max(0, at - _CONTEXT_CHARS)
    end = min(len(text), at + len(query) + _SNIPPET_CHARS - _CONTEXT_CHARS)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def _tokens(text: object) -> set[str]:
    return set(_TOKEN_RE.findall(str(text or "").casefold()))


def _owner_digest(owner_id: str) -> str:
    return hashlib.sha256(("owner\0" + owner_id).encode("utf-8")).hexdigest()[:32]


def _query_digest(query: str) -> str:
    return hashlib.sha256(("query\0" + query.casefold()).encode("utf-8")).hexdigest()[:32]


def _filter_digest(sources: tuple[str, ...], types: tuple[str, ...],
                   project_id: str | None) -> str:
    raw = json.dumps(
        {"sources": list(sources), "types": list(types), "project": project_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(("filters\0" + raw).encode("utf-8")).hexdigest()[:32]


def encode_cursor(*, owner_id: str, query: str, sources: tuple[str, ...],
                  types: tuple[str, ...], project_id: str | None,
                  rank_key: tuple) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "o": _owner_digest(owner_id),
        "q": _query_digest(query),
        "f": _filter_digest(sources, types, project_id),
        "k": list(rank_key),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: object, *, owner_id: str, query: str,
                  sources: tuple[str, ...], types: tuple[str, ...],
                  project_id: str | None) -> tuple | None:
    if cursor in (None, ""):
        return None
    if (not isinstance(cursor, str) or not cursor.isascii()
            or len(cursor) > MAX_CURSOR_BYTES
            or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None):
        raise SearchQueryInvalid("search_cursor_invalid")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError, TypeError, RecursionError):
        raise SearchQueryInvalid("search_cursor_invalid") from None
    if not isinstance(value, dict) or set(value) != {"v", "o", "q", "f", "k"}:
        raise SearchQueryInvalid("search_cursor_invalid")
    if (value.get("v") != _CURSOR_VERSION
            or value.get("o") != _owner_digest(owner_id)
            or value.get("q") != _query_digest(query)
            or value.get("f") != _filter_digest(sources, types, project_id)):
        raise SearchQueryInvalid("search_cursor_scope_invalid")
    key = value.get("k")
    if (not isinstance(key, list) or len(key) != 7
            or any(type(item) is not int for item in key[:6])
            or not isinstance(key[6], str)
            or re.fullmatch(r"[0-9a-f]{64}", key[6]) is None):
        raise SearchQueryInvalid("search_cursor_invalid")
    return tuple(key)


def _stable_ref(ref: dict[str, Any]) -> str:
    return json.dumps(ref, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _matched_parts(query: str, parts: list[str], title: str,
                   updated_at: int, source: str, ref: dict[str, Any]) -> SearchHit | None:
    haystack = "\n".join(part for part in parts if part)
    folded = haystack.casefold()
    query_folded = query.casefold()
    tokens = _tokens(query)
    phrase = query_folded in folded
    title_folded = title.casefold()
    title_phrase = query_folded in title_folded
    title_overlap = len(tokens & _tokens(title))
    overlap = len(tokens & _tokens(haystack))
    if not phrase and (not tokens or overlap == 0):
        return None
    score = int(phrase) * 4000 + int(title_phrase) * 2000 + title_overlap * 200 + overlap * 100
    stable = hashlib.sha256(_stable_ref(ref).encode("utf-8")).hexdigest()
    rank_key = (-int(phrase), -int(title_phrase), -title_overlap, -overlap,
                -updated_at, SOURCE_ORDER[source], stable)
    return SearchHit(
        source=source,
        type=str(ref.get("type") or source),
        ref={key: value for key, value in ref.items() if key != "type"},
        title=title,
        snippet=make_snippet(*parts, query=query),
        updated_at=updated_at,
        score=score,
        rank_key=rank_key,
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _updated_at(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class FederatedSearchService:
    def __init__(self, orchestrator):
        from orchestrator.plane_repository_context import plane_source_from_orchestrator

        self.orch = orchestrator
        self.source = plane_source_from_orchestrator(orchestrator)
        self.runtime = self.source.plane_runtime
        self.repositories = self.source.plane_repositories
        service = getattr(orchestrator, "personalization_service", None)
        self.repo = getattr(service, "repo", None) if service is not None else None
        self.credentials = getattr(orchestrator, "credential_manager", None)
        self.atlas = getattr(orchestrator, "atlas_pages", None)

    async def open_source(self, *, owner_id: str, ref: object) -> SearchSource | None:
        if not isinstance(owner_id, str) or not owner_id.strip() or not isinstance(ref, dict):
            raise SearchQueryInvalid("search_source_reference_invalid")
        definitions = {
            "note": ("notes", "note_id", {"expected_revision"}),
            "memory": ("memory", "memory_id", set()),
            "conversation": ("conversations", "conversation_id", {"message_id"}),
            "atlas_page": ("atlas", "page_id", {"project_id", "revision"}),
        }
        definition = definitions.get(ref.get("kind")) if isinstance(ref.get("kind"), str) else None
        if definition is None:
            raise SearchQueryInvalid("search_source_reference_invalid")
        source, identity, extra = definition
        value = ref.get(identity)
        if (set(ref) - {"kind", identity, *extra} or not isinstance(value, str)
                or not 1 <= len(value) <= 512 or any(unicodedata.category(c).startswith("C") for c in value)):
            raise SearchQueryInvalid("search_source_reference_invalid")
        for key in ("expected_revision", "revision"):
            if key in extra and (type(ref.get(key)) is not int or not 1 <= ref[key] <= 2**53 - 1):
                raise SearchQueryInvalid("search_source_reference_invalid")
        if "message_id" in ref and ref["message_id"] is not None and (
                type(ref["message_id"]) is not int or ref["message_id"] < 1):
            raise SearchQueryInvalid("search_source_reference_invalid")
        if "project_id" in ref:
            normalize_project_filter(ref["project_id"])
        copied = dict(ref)
        hit = SearchHit(source, "", copied, "", "", 0, 0, ())
        return await asyncio.to_thread(self._open_source, hit, owner_id)

    def _open_source(self, hit, owner_id):
        if hit.source == "notes":
            current = self._current_note(hit, owner_id)
            if current is None:
                return None
            metadata, opened = current
            title, text = metadata.category, opened.value
        elif hit.source == "memory":
            current = self._current_memory(hit, owner_id)
            if current is None:
                return None
            title, text = str(current.get("category") or "Memory"), str(current.get("value") or "")
        elif hit.source == "conversations":
            current = self._current_conversation(hit, owner_id)
            if current is None:
                return None
            chat, message = current
            title = str(getattr(chat, "title", "") or "Conversation")
            text = _message_text(message.content) if message is not None else title
        else:
            current = self._current_atlas(hit, owner_id)
            if current is None:
                return None
            title, text = current.title, current.body or ""
        return SearchSource(hit.source, dict(hit.ref), title, text[:MAX_SOURCE_CHARS], len(text) <= MAX_SOURCE_CHARS)

    async def search(self, *, owner_id: str, query: object, sources: object = None,
                     types: object = None, project_id: object = None,
                     limit: int = DEFAULT_PAGE_LIMIT, cursor: object = None) -> SearchOutcome:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise SearchQueryInvalid("search_owner_invalid")
        cleaned = normalize_query(query)
        selected = normalize_sources(sources)
        selected_types = normalize_types(types)
        project = normalize_project_filter(project_id)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise SearchQueryInvalid("search_limit_invalid")
        after = decode_cursor(cursor, owner_id=owner_id, query=cleaned,
                              sources=selected, types=selected_types, project_id=project)
        tasks = []
        task_sources: list[str] = []
        reports: dict[str, SourceReport] = {}
        for name in selected:
            if not self._source_supports_types(name, selected_types):
                reports[name] = SourceReport(name, True, True, 0, 0)
                continue
            task_sources.append(name)
            tasks.append(self._scan_source(name, owner_id, cleaned, selected_types, project))
        scanned = await asyncio.gather(*tasks) if tasks else []
        merged: list[SearchHit] = []
        for name, (hits, report) in zip(task_sources, scanned):
            reports[name] = report
            merged.extend(hits)
        deduped: dict[tuple[str, str], SearchHit] = {}
        for hit in merged:
            key = (hit.source, _stable_ref(hit.ref))
            current = deduped.get(key)
            if current is None or hit.rank_key < current.rank_key:
                deduped[key] = hit
        ordered = sorted(deduped.values(), key=lambda hit: hit.rank_key)
        available = [hit for hit in ordered if after is None or hit.rank_key > after]
        failed_sources: set[str] = set()
        stale: dict[str, int] = {}
        page: list[SearchHit] = []
        index = 0
        while index < len(available) and len(page) < limit:
            hit = available[index]
            index += 1
            if hit.source in failed_sources:
                continue
            try:
                current = await self._recheck(hit, owner_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("federated_search_recheck_failed source=%s", hit.source,
                               exc_info=True)
                failed_sources.add(hit.source)
                page = [existing for existing in page if existing.source != hit.source]
                continue
            if not current:
                stale[hit.source] = stale.get(hit.source, 0) + 1
                continue
            page.append(hit)
        page.sort(key=lambda row: row.rank_key)
        final_reports = []
        for name in selected:
            report = reports.get(name, SourceReport(name, False, False, 0, 0))
            if name in failed_sources:
                report = SourceReport(name, False, False, report.scanned, 0, 0,
                                      stale.get(name, 0))
            else:
                matched = max(0, sum(1 for hit in ordered if hit.source == name)
                              - stale.get(name, 0))
                returned = sum(1 for hit in page if hit.source == name)
                report = SourceReport(name, report.available, report.complete,
                                      report.scanned, matched, returned,
                                      stale.get(name, 0))
            final_reports.append(report)
        remaining = len(available) - index
        next_cursor = None
        if remaining > 0 and page and len(page) >= limit:
            next_cursor = encode_cursor(
                owner_id=owner_id, query=cleaned, sources=selected,
                types=selected_types, project_id=project, rank_key=page[-1].rank_key,
            )
        return SearchOutcome(
            query=cleaned,
            sources=selected,
            types=selected_types,
            project_id=project,
            hits=tuple(page),
            reports=tuple(final_reports),
            total=sum(report.matched for report in final_reports),
            next_cursor=next_cursor,
        )

    @staticmethod
    def _source_supports_types(source: str, types: tuple[str, ...]) -> bool:
        if not types:
            return True
        if source == "atlas":
            return ATLAS_TYPE in types
        if source == "conversations":
            return CONVERSATION_TYPE in types
        return any(name in types for name in _category_types(source))

    async def _scan_source(self, source: str, owner_id: str, query: str,
                           types: tuple[str, ...], project_id: str | None):
        try:
            if source == "notes":
                return await asyncio.to_thread(self._scan_notes, owner_id, query, types)
            if source == "memory":
                return await asyncio.to_thread(self._scan_memory, owner_id, query, types,
                                               project_id)
            if source == "conversations":
                return await asyncio.to_thread(self._scan_conversations, owner_id, query, types)
            return await asyncio.to_thread(self._scan_atlas, owner_id, query, types, project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("federated_search_source_failed source=%s", source, exc_info=True)
            return [], SourceReport(source, False, False, 0, 0)

    def _note_cipher(self) -> ExplicitNoteCipher | None:
        from cryptography.fernet import Fernet
        from orchestrator.credential_manager import CredentialManager

        if type(self.credentials) is not CredentialManager:
            return None
        fernet = getattr(self.credentials, "_fernet", None)
        if type(fernet) is not Fernet:
            return None
        try:
            return ExplicitNoteCipher(fernet)
        except ExplicitNoteUnavailable:
            return None

    def _note_repository(self):
        return getattr(getattr(self.repositories, "preferences", None), "personalization", None)

    def _scan_notes(self, owner_id: str, query: str,
                    types: tuple[str, ...]) -> tuple[list[SearchHit], SourceReport]:
        cipher = self._note_cipher()
        repository = self._note_repository()
        if cipher is None or repository is None:
            raise SourceUnavailable("notes_unavailable")
        hits: list[SearchHit] = []
        cursor = None
        scanned = 0
        complete = False
        while scanned < NOTE_SCAN_LIMIT and len(hits) < MAX_HITS_PER_SOURCE:
            page_size = min(100, NOTE_SCAN_LIMIT - scanned)
            with self.runtime.transaction() as transaction:
                records = repository.list_explicit_notes(
                    transaction, owner_id=owner_id, after_id=cursor,
                    limit=page_size, include_disabled=True,
                )
            if not records:
                complete = True
                break
            now = time.time_ns() // 1_000_000
            for record in records:
                scanned += 1
                try:
                    metadata = ExplicitNoteMetadata(**{
                        key: value for key, value in asdict(record).items()
                        if key != "ciphertext"
                    })
                    opened = cipher.open(
                        EncryptedExplicitNote(metadata, record.ciphertext),
                        owner_id=owner_id, now_ms=now, require_enabled=False,
                    )
                except (ExplicitNoteUnavailable, TypeError, ValueError):
                    raise SourceUnavailable("notes_unreadable") from None
                if types and metadata.category not in types:
                    continue
                ref = {"kind": "note", "note_id": metadata.note_id,
                       "expected_revision": metadata.revision, "type": metadata.category}
                hit = _matched_parts(
                    query, [metadata.category, opened.value], sanitize_text(metadata.category),
                    metadata.updated_at, "notes", ref,
                )
                if hit is not None:
                    hits.append(hit)
                if len(hits) >= MAX_HITS_PER_SOURCE:
                    break
            if len(records) < page_size:
                complete = True
                break
            if len(hits) >= MAX_HITS_PER_SOURCE:
                break
            cursor = records[-1].note_id
        if len(hits) >= MAX_HITS_PER_SOURCE or scanned >= NOTE_SCAN_LIMIT:
            complete = False
        return hits[:MAX_HITS_PER_SOURCE], SourceReport("notes", True, complete, scanned,
                                                        len(hits[:MAX_HITS_PER_SOURCE]))

    def _memory_items(self, owner_id: str, project_id: str | None):
        if self.repo is None:
            raise SourceUnavailable("memory_unavailable")
        scope = GLOBAL_MEMORY_PROJECT if project_id == "-" else project_id
        items = list(self.repo.list_memory(owner_id, project_id=scope) or ())
        raw_count = len(items)
        return self._apply_memory_policy(items), raw_count

    @staticmethod
    def _apply_memory_policy(items):
        from personalization import living_memory as lm
        from personalization import memory_guard

        if lm.temporal_enabled():
            items = lm.as_of(items, int(time.time() * 1000))
        if memory_guard.guard_enabled():
            kept = []
            for item in items:
                if (isinstance(item, dict) and parse_envelope(item.get("value")) is None
                        and memory_guard.trust_of(item) == "tampered"):
                    logger.warning("federated_search_tampered_memory_excluded")
                    continue
                kept.append(item)
            items = kept
        return items

    def _scan_memory(self, owner_id: str, query: str, types: tuple[str, ...],
                     project_id: str | None) -> tuple[list[SearchHit], SourceReport]:
        items, raw_count = self._memory_items(owner_id, project_id)
        scanned = raw_count
        hits: list[SearchHit] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if parse_envelope(value) is not None:
                continue
            category = str(item.get("category") or "context")
            if types and category not in types:
                continue
            memory_id = str(item.get("id") or "")
            if not memory_id:
                continue
            ref = {"kind": "memory", "memory_id": memory_id, "type": category}
            hit = _matched_parts(
                query, [category, str(value or ""), str(item.get("keywords") or "")],
                sanitize_text(category), _updated_at(item.get("updated_at")), "memory", ref,
            )
            if hit is not None:
                hits.append(hit)
            if len(hits) >= MAX_HITS_PER_SOURCE:
                break
        complete = raw_count < MEMORY_SCAN_LIMIT
        if len(hits) >= MAX_HITS_PER_SOURCE:
            complete = False
        return hits[:MAX_HITS_PER_SOURCE], SourceReport("memory", True, complete, scanned,
                                                        len(hits[:MAX_HITS_PER_SOURCE]))

    def _history_context(self):
        from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

        repository, runtime = repository_from(
            "history", plane_runtime=self.runtime, repositories=self.repositories,
        )
        return PlaneRepositoryContext(repository=repository, plane_runtime=runtime)

    def _scan_conversations(self, owner_id: str, query: str,
                            types: tuple[str, ...]) -> tuple[list[SearchHit], SourceReport]:
        if types and CONVERSATION_TYPE not in types:
            return [], SourceReport("conversations", True, True, 0, 0)
        try:
            context = self._history_context()
            conversations = list(context.call(
                context.repository.conversations.list_recent_nonempty,
                owner_id=owner_id, limit=CONVERSATION_SCAN_LIMIT,
            ) or ())
        except Exception:
            raise SourceUnavailable("conversations_unavailable") from None
        hits: list[SearchHit] = []
        scanned = 0
        complete = len(conversations) < CONVERSATION_SCAN_LIMIT
        truncated = False
        failed = False
        for chat in conversations:
            if scanned >= MESSAGE_SCAN_LIMIT or len(hits) >= MAX_HITS_PER_SOURCE:
                truncated = True
                break
            chat_id = str(getattr(chat, "conversation_id", "") or "")
            if not chat_id:
                continue
            title = sanitize_text(getattr(chat, "title", "") or "") or "Conversation"
            updated = _updated_at(getattr(chat, "updated_at", 0))
            title_hit = _matched_parts(
                query, [title], title, updated, "conversations",
                {"kind": "conversation", "conversation_id": chat_id,
                 "type": CONVERSATION_TYPE},
            )
            chat_hits: list[SearchHit] = []
            remaining = MESSAGE_SCAN_LIMIT - scanned
            try:
                messages = list(context.call(
                    context.repository.messages.list_visible,
                    owner_id=owner_id, conversation_id=chat_id, limit=remaining,
                ) or ())
            except Exception:
                logger.warning("federated_search_messages_failed conversation=%s", chat_id,
                               exc_info=True)
                failed = True
                messages = []
            if len(messages) >= remaining:
                truncated = True
            scanned += len(messages)
            for message in messages:
                text = _message_text(getattr(message, "content", None))
                if not text:
                    continue
                message_id = getattr(message, "message_id", None)
                stamped = _updated_at(getattr(message, "timestamp", 0))
                ref = {"kind": "conversation", "conversation_id": chat_id,
                       "message_id": message_id, "type": CONVERSATION_TYPE}
                hit = _matched_parts(
                    query, [text], title, max(stamped, updated), "conversations", ref,
                )
                if hit is not None:
                    chat_hits.append(hit)
                if len(hits) + len(chat_hits) >= MAX_HITS_PER_SOURCE:
                    truncated = True
                    break
            if chat_hits:
                hits.extend(chat_hits)
            elif title_hit is not None:
                hits.append(title_hit)
            if len(hits) >= MAX_HITS_PER_SOURCE:
                break
        if truncated or failed or len(hits) >= MAX_HITS_PER_SOURCE:
            complete = False
        return hits[:MAX_HITS_PER_SOURCE], SourceReport(
            "conversations", True, complete, scanned, len(hits[:MAX_HITS_PER_SOURCE]),
        )

    def _atlas_service(self):
        if self.atlas is not None:
            return self.atlas
        from personalization.atlas_service import AtlasPageService

        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        fernet = getattr(getattr(self.orch, "credential_manager", None), "_fernet", None)
        self.atlas = AtlasPageService(
            plane_runtime=getattr(plane, "runtime", None),
            plane_repositories=getattr(plane, "repositories", None),
            audit_repo=getattr(self.orch, "audit_repo", None),
            fernet=fernet,
        )
        return self.atlas

    def _scan_atlas(self, owner_id: str, query: str, types: tuple[str, ...],
                    project_id: str | None) -> tuple[list[SearchHit], SourceReport]:
        if types and ATLAS_TYPE not in types:
            return [], SourceReport("atlas", True, True, 0, 0)
        service = self._atlas_service()
        scan = service.scan_pages(
            owner_id=owner_id, project_id=project_id, all_projects=project_id is None,
            limit=ATLAS_SCAN_LIMIT,
        )
        hits: list[SearchHit] = []
        for page in scan.pages:
            ref = {"kind": "atlas_page", "page_id": page.page_id,
                   "project_id": page.project_id, "revision": page.revision,
                   "type": ATLAS_TYPE}
            hit = _matched_parts(
                query, [page.title, page.slug, page.body or "", " ".join(page.sources)],
                sanitize_text(page.title), _updated_at(page.updated_at), "atlas", ref,
            )
            if hit is not None:
                hits.append(hit)
            if len(hits) >= MAX_HITS_PER_SOURCE:
                break
        complete = scan.complete and len(hits) < MAX_HITS_PER_SOURCE
        if len(hits) >= MAX_HITS_PER_SOURCE:
            complete = False
        return hits[:MAX_HITS_PER_SOURCE], SourceReport(
            "atlas", True, complete, scan.scanned, len(hits[:MAX_HITS_PER_SOURCE]),
        )

    async def _recheck(self, hit: SearchHit, owner_id: str) -> bool:
        if hit.source == "notes":
            return await asyncio.to_thread(self._recheck_note, hit, owner_id)
        if hit.source == "memory":
            return await asyncio.to_thread(self._recheck_memory, hit, owner_id)
        if hit.source == "conversations":
            return await asyncio.to_thread(self._recheck_conversation, hit, owner_id)
        return await asyncio.to_thread(self._recheck_atlas, hit, owner_id)

    def _recheck_note(self, hit: SearchHit, owner_id: str) -> bool:
        return self._current_note(hit, owner_id) is not None

    def _current_note(self, hit: SearchHit, owner_id: str):
        cipher = self._note_cipher()
        repository = self._note_repository()
        if cipher is None or repository is None:
            raise SourceUnavailable("notes_unavailable")
        with self.runtime.transaction() as transaction:
            record = repository.get_explicit_note(
                transaction, owner_id=owner_id, note_id=hit.ref.get("note_id"),
                include_disabled=True,
            )
        if record is None:
            return None
        try:
            metadata = ExplicitNoteMetadata(**{
                key: value for key, value in asdict(record).items() if key != "ciphertext"
            })
            opened = cipher.open(EncryptedExplicitNote(metadata, record.ciphertext),
                                 owner_id=owner_id, now_ms=time.time_ns() // 1_000_000,
                                 require_enabled=False)
        except (ExplicitNoteUnavailable, TypeError, ValueError):
            return None
        return (metadata, opened) if metadata.revision == hit.ref.get("expected_revision") else None

    def _recheck_memory(self, hit: SearchHit, owner_id: str) -> bool:
        return self._current_memory(hit, owner_id) is not None

    def _current_memory(self, hit: SearchHit, owner_id: str):
        if self.repo is None:
            raise SourceUnavailable("memory_unavailable")
        record = self.repo.get_memory(owner_id, str(hit.ref.get("memory_id") or ""))
        if not isinstance(record, dict) or record.get("superseded_at") is not None:
            return None
        if parse_envelope(record.get("value")) is not None:
            return None
        return record if self._apply_memory_policy([record]) else None

    def _recheck_conversation(self, hit: SearchHit, owner_id: str) -> bool:
        return self._current_conversation(hit, owner_id) is not None

    def _current_conversation(self, hit: SearchHit, owner_id: str):
        context = self._history_context()
        chat_id = str(hit.ref.get("conversation_id") or "")
        chat = context.call(context.repository.conversations.get,
                            owner_id=owner_id, conversation_id=chat_id)
        if chat is None:
            return None
        message_id = hit.ref.get("message_id")
        if message_id is None:
            return chat, None
        if type(message_id) is not int:
            return None
        message = context.call(context.repository.messages.get,
                               owner_id=owner_id, conversation_id=chat_id,
                               message_id=message_id)
        return (chat, message) if message is not None else None

    def _recheck_atlas(self, hit: SearchHit, owner_id: str) -> bool:
        return self._current_atlas(hit, owner_id) is not None

    def _current_atlas(self, hit: SearchHit, owner_id: str):
        from personalization.atlas_service import AtlasNotFound

        service = self._atlas_service()
        try:
            page = service.get_page(
                owner_id=owner_id, project_id=hit.ref.get("project_id"),
                reference=str(hit.ref.get("page_id") or ""),
            )
        except AtlasNotFound:
            return None
        return page if page.revision == hit.ref.get("revision") else None


def get_search_service(orchestrator) -> FederatedSearchService:
    service = getattr(orchestrator, "_federated_search_service", None)
    if service is None:
        service = FederatedSearchService(orchestrator)
        orchestrator._federated_search_service = service
    return service


def _category_types(source: str) -> frozenset[str]:
    if source == "notes":
        from personalization.explicit_notes import NOTE_CATEGORIES

        return NOTE_CATEGORIES
    from personalization.repository import MEMORY_CATEGORIES

    return frozenset(MEMORY_CATEGORIES)


def hit_payload(hit: SearchHit) -> dict[str, Any]:
    return {
        "source": hit.source,
        "type": hit.type,
        "ref": dict(hit.ref),
        "title": hit.title,
        "snippet": hit.snippet,
        "updated_at": hit.updated_at,
        "rank": hit.score,
    }


def report_payload(report: SourceReport) -> dict[str, Any]:
    return {
        "source": report.source,
        "available": report.available,
        "complete": report.complete,
        "scanned": report.scanned,
        "matched": report.matched,
        "returned": report.returned,
        "stale": report.stale,
    }
