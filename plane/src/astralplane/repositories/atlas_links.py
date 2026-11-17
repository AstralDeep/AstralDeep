"""Neutral same-owner Atlas page links and one-hop backlink traversal. Mutations replace a
page's bounded outgoing links under exact page/link revision fences and command receipts;
callers own transactions while content policy, rendering, and authorization stay in the host.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)

MAX_REVISION: Final = 2**53 - 1
MAX_TIME: Final = 2**53 - 1
MAX_LINK_TARGETS: Final = 16
MAX_LINK_LIST: Final = 100
OUTBOUND: Final = "outbound"
BACKLINK: Final = "backlink"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, field_name: str, *, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise RepositoryValidationError(f"{field_name} is outside its supported bounds")
    if value != value.strip():
        raise RepositoryValidationError(f"{field_name} must not have edge whitespace")
    if any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise RepositoryValidationError(f"{field_name} contains invalid characters")
    return value


def _owner(value: object) -> str:
    return _text(value, "owner_id", maximum=512)


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _UUID.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field_name} must be a canonical UUID")
    return value


def _integer(
    value: object, field_name: str, *, minimum: int = 0, maximum: int = MAX_REVISION
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError(f"{field_name} is outside its supported bounds")
    return value


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _direction(value: object) -> str:
    if type(value) is not str or value not in {OUTBOUND, BACKLINK}:
        raise RepositoryValidationError("direction must be outbound or backlink")
    return value


def _targets(value: object, source_page_id: str) -> tuple[str, ...]:
    if type(value) not in (tuple, list) or len(value) > MAX_LINK_TARGETS:
        raise RepositoryValidationError("targets is outside its supported bounds")
    targets = tuple(_identifier(target, "target_page_id") for target in value)
    if len(set(targets)) != len(targets):
        raise RepositoryValidationError("targets must be unique")
    if source_page_id in targets:
        raise RepositoryValidationError("atlas page cannot link to itself")
    return tuple(sorted(targets))


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise RepositoryValidationError(f"{field_name} must be boolean")
    return value


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _request_digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AtlasLinkSetCommand:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    expected_page_revision: int
    expected_link_revision: int
    targets: tuple[str, ...]
    updated_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        _integer(self.expected_page_revision, "expected_page_revision", minimum=1)
        _integer(
            self.expected_link_revision,
            "expected_link_revision",
            maximum=MAX_REVISION - 1,
        )
        object.__setattr__(self, "targets", _targets(self.targets, self.page_id))
        _integer(self.updated_at, "updated_at", maximum=MAX_TIME)

    @property
    def request_digest(self) -> str:
        return _request_digest(
            {
                "version": 1,
                "kind": "atlas_page_link_set",
                "owner_id": self.owner_id,
                "page_id": self.page_id,
                "expected_page_revision": self.expected_page_revision,
                "expected_link_revision": self.expected_link_revision,
                "targets": list(self.targets),
                "updated_at": self.updated_at,
            }
        )


@dataclass(frozen=True, slots=True)
class AtlasLinkRecord:
    owner_id: str = field(repr=False)
    source_page_id: str
    target_page_id: str
    source_revision: int
    target_revision: int
    link_revision: int
    linked_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.source_page_id, "source_page_id")
        _identifier(self.target_page_id, "target_page_id")
        if self.source_page_id == self.target_page_id:
            raise RepositoryValidationError("atlas page link must not be reflexive")
        _integer(self.source_revision, "source_revision", minimum=1)
        _integer(self.target_revision, "target_revision", minimum=1)
        _integer(self.link_revision, "link_revision", minimum=1)
        _integer(self.linked_at, "linked_at", maximum=MAX_TIME)


@dataclass(frozen=True, slots=True)
class AtlasLinkCursor:
    owner_id: str = field(repr=False)
    page_id: str
    direction: str
    linked_at: int
    source_page_id: str
    target_page_id: str

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.page_id, "page_id")
        _direction(self.direction)
        _integer(self.linked_at, "linked_at", maximum=MAX_TIME)
        _identifier(self.source_page_id, "source_page_id")
        _identifier(self.target_page_id, "target_page_id")


@dataclass(frozen=True, slots=True)
class AtlasLinkBatch:
    records: tuple[AtlasLinkRecord, ...]
    next_cursor: AtlasLinkCursor | None

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(
            type(record) is not AtlasLinkRecord for record in self.records
        ):
            raise RepositoryValidationError("atlas link batch requires detached records")
        if self.next_cursor is not None and type(self.next_cursor) is not AtlasLinkCursor:
            raise RepositoryValidationError("atlas link batch cursor is invalid")


@dataclass(frozen=True, slots=True)
class AtlasLinkCommandReceipt:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    request_digest: str
    expected_page_revision: int
    expected_link_revision: int
    result_link_revision: int
    linked_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        _digest(self.request_digest, "request_digest")
        _integer(self.expected_page_revision, "expected_page_revision", minimum=1)
        _integer(
            self.expected_link_revision,
            "expected_link_revision",
            maximum=MAX_REVISION - 1,
        )
        _integer(self.result_link_revision, "result_link_revision", minimum=1)
        _integer(self.linked_at, "linked_at", maximum=MAX_TIME)
        if self.result_link_revision != self.expected_link_revision + 1:
            raise RepositoryValidationError("atlas link receipt revision is inconsistent")


@dataclass(frozen=True, slots=True)
class AtlasLinkChange:
    receipt: AtlasLinkCommandReceipt
    links: tuple[AtlasLinkRecord, ...]
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.receipt) is not AtlasLinkCommandReceipt:
            raise RepositoryValidationError("atlas link change requires a typed receipt")
        if type(self.links) is not tuple or any(
            type(link) is not AtlasLinkRecord for link in self.links
        ):
            raise RepositoryValidationError("atlas link change requires detached links")
        _boolean(self.replayed, "replayed")
        if any(
            (link.owner_id, link.source_page_id) != (self.receipt.owner_id, self.receipt.page_id)
            for link in self.links
        ):
            raise RepositoryValidationError("atlas link change identity is inconsistent")


def _link(row: Any) -> AtlasLinkRecord:
    try:
        return AtlasLinkRecord(
            row["owner_id"],
            str(row["source_page_id"]),
            str(row["target_page_id"]),
            row["source_revision"],
            row["target_revision"],
            row["link_revision"],
            row["linked_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("invalid stored atlas page link") from exc


def _receipt(row: Any) -> AtlasLinkCommandReceipt | None:
    if row is None:
        return None
    try:
        return AtlasLinkCommandReceipt(
            row["owner_id"],
            str(row["command_id"]),
            str(row["page_id"]),
            row["request_digest"],
            row["expected_page_revision"],
            row["expected_link_revision"],
            row["result_link_revision"],
            row["linked_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("invalid stored atlas page link command") from exc


_SELECT_LINKS: Final = (
    "SELECT l.*,source.revision AS source_revision,target.revision AS target_revision"
    " FROM atlas_page_link AS l"
    " JOIN atlas_page AS source ON source.owner_id=l.owner_id"
    " AND source.page_id=l.source_page_id AND source.deleted_at IS NULL"
    " JOIN atlas_page AS target ON target.owner_id=l.owner_id"
    " AND target.page_id=l.target_page_id AND target.deleted_at IS NULL"
)
_ORDER_LINKS: Final = (
    " ORDER BY l.linked_at DESC,l.source_page_id DESC,l.target_page_id DESC LIMIT %s"
)


class AtlasLinkRepository:
    def set_links(self, transaction: Any, *, command: AtlasLinkSetCommand) -> AtlasLinkChange:
        if type(command) is not AtlasLinkSetCommand:
            raise RepositoryValidationError("typed atlas link set command required")
        command = AtlasLinkSetCommand(
            command.owner_id,
            command.command_id,
            command.page_id,
            command.expected_page_revision,
            command.expected_link_revision,
            command.targets,
            command.updated_at,
        )
        self._lock_owner(transaction, command.owner_id)
        stored = _receipt(
            transaction.fetch_one(
                "SELECT * FROM atlas_page_link_command WHERE owner_id=%s AND command_id=%s"
                " FOR UPDATE",
                (command.owner_id, command.command_id),
            )
        )
        if stored is not None:
            if (stored.page_id, stored.request_digest) != (
                command.page_id,
                command.request_digest,
            ):
                raise RepositoryConflictError("atlas link command identity was reused")
            rows = transaction.fetch_all(
                _SELECT_LINKS + " WHERE l.owner_id=%s AND l.source_page_id=%s"
                " ORDER BY l.target_page_id ASC LIMIT %s",
                (command.owner_id, command.page_id, MAX_LINK_TARGETS),
            )
            return AtlasLinkChange(stored, tuple(_link(row) for row in rows), True)

        source = transaction.fetch_one(
            "SELECT * FROM atlas_page WHERE owner_id=%s AND page_id=%s FOR UPDATE",
            (command.owner_id, command.page_id),
        )
        if source is None:
            raise RepositoryNotFoundError("atlas page not found")
        if source["deleted_at"] is not None:
            raise RepositoryConflictError("atlas page is deleted")
        if source["revision"] != command.expected_page_revision:
            raise RepositoryConflictError("atlas page revision changed")
        if command.updated_at < source["updated_at"]:
            raise RepositoryConflictError("atlas link clock regressed")

        head = transaction.fetch_one(
            "SELECT * FROM atlas_page_link_head WHERE owner_id=%s AND page_id=%s FOR UPDATE",
            (command.owner_id, command.page_id),
        )
        current_link_revision = 0 if head is None else int(head["link_revision"])
        if current_link_revision != command.expected_link_revision:
            raise RepositoryConflictError("atlas link revision changed")
        if head is not None and command.updated_at < head["updated_at"]:
            raise RepositoryConflictError("atlas link clock regressed")

        target_revisions: dict[str, int] = {}
        if command.targets:
            rows = transaction.fetch_all(
                "SELECT page_id,revision FROM atlas_page WHERE owner_id=%s"
                " AND page_id=ANY(%s::uuid[]) AND deleted_at IS NULL"
                " ORDER BY page_id FOR SHARE",
                (command.owner_id, list(command.targets)),
            )
            target_revisions = {str(row["page_id"]): int(row["revision"]) for row in rows}
            if set(target_revisions) != set(command.targets):
                raise RepositoryNotFoundError("atlas link endpoint not found")

        result_link_revision = current_link_revision + 1
        transaction.execute(
            "DELETE FROM atlas_page_link WHERE owner_id=%s AND source_page_id=%s",
            (command.owner_id, command.page_id),
        )
        for target_page_id in command.targets:
            transaction.execute(
                "INSERT INTO atlas_page_link(owner_id,source_page_id,target_page_id,"
                "link_revision,linked_at) VALUES(%s,%s,%s,%s,%s)",
                (
                    command.owner_id,
                    command.page_id,
                    target_page_id,
                    result_link_revision,
                    command.updated_at,
                ),
            )
        advanced = transaction.fetch_one(
            "INSERT INTO atlas_page_link_head(owner_id,page_id,link_revision,updated_at)"
            " VALUES(%s,%s,%s,%s) ON CONFLICT(owner_id,page_id) DO UPDATE"
            " SET link_revision=EXCLUDED.link_revision,updated_at=EXCLUDED.updated_at"
            " WHERE atlas_page_link_head.link_revision=%s RETURNING *",
            (
                command.owner_id,
                command.page_id,
                result_link_revision,
                command.updated_at,
                current_link_revision,
            ),
        )
        if advanced is None:
            raise RepositoryConflictError("atlas link revision changed")
        receipt = AtlasLinkCommandReceipt(
            command.owner_id,
            command.command_id,
            command.page_id,
            command.request_digest,
            command.expected_page_revision,
            command.expected_link_revision,
            result_link_revision,
            command.updated_at,
        )
        transaction.execute(
            "INSERT INTO atlas_page_link_command(owner_id,command_id,page_id,request_digest,"
            "expected_page_revision,expected_link_revision,result_link_revision,linked_at)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt.owner_id,
                receipt.command_id,
                receipt.page_id,
                receipt.request_digest,
                receipt.expected_page_revision,
                receipt.expected_link_revision,
                receipt.result_link_revision,
                receipt.linked_at,
            ),
        )
        links = tuple(
            AtlasLinkRecord(
                command.owner_id,
                command.page_id,
                target_page_id,
                int(source["revision"]),
                target_revisions[target_page_id],
                result_link_revision,
                command.updated_at,
            )
            for target_page_id in reversed(command.targets)
        )
        return AtlasLinkChange(receipt, links, False)

    def list_links(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        cursor: AtlasLinkCursor | None = None,
        limit: int = 50,
    ) -> AtlasLinkBatch:
        return self._list(
            query,
            owner_id=owner_id,
            page_id=page_id,
            direction=OUTBOUND,
            cursor=cursor,
            limit=limit,
        )

    def list_backlinks(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        cursor: AtlasLinkCursor | None = None,
        limit: int = 50,
    ) -> AtlasLinkBatch:
        return self._list(
            query,
            owner_id=owner_id,
            page_id=page_id,
            direction=BACKLINK,
            cursor=cursor,
            limit=limit,
        )

    def _list(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        direction: str,
        cursor: AtlasLinkCursor | None,
        limit: int,
    ) -> AtlasLinkBatch:
        owner_id = _owner(owner_id)
        page_id = _identifier(page_id, "page_id")
        direction = _direction(direction)
        if type(limit) is not int or not 1 <= limit <= MAX_LINK_LIST:
            raise RepositoryValidationError("atlas link limit is outside its supported bounds")
        if cursor is not None:
            if type(cursor) is not AtlasLinkCursor or (
                cursor.owner_id,
                cursor.page_id,
                cursor.direction,
            ) != (owner_id, page_id, direction):
                raise RepositoryValidationError("atlas link cursor is invalid for this scope")
            _integer(cursor.linked_at, "cursor linked_at", maximum=MAX_TIME)
            _identifier(cursor.source_page_id, "cursor source_page_id")
            _identifier(cursor.target_page_id, "cursor target_page_id")
        if query.fetch_one(
            "SELECT revision FROM atlas_page WHERE owner_id=%s AND page_id=%s"
            " AND deleted_at IS NULL",
            (owner_id, page_id),
        ) is None:
            raise RepositoryNotFoundError("atlas page not found")
        root_column = "source_page_id" if direction == OUTBOUND else "target_page_id"
        clauses = ["l.owner_id=%s", f"l.{root_column}=%s"]
        parameters: list[object] = [owner_id, page_id]
        if cursor is not None:
            clauses.append("(l.linked_at,l.source_page_id,l.target_page_id)<(%s,%s::uuid,%s::uuid)")
            parameters.extend(
                (cursor.linked_at, cursor.source_page_id, cursor.target_page_id)
            )
        parameters.append(limit + 1)
        rows = query.fetch_all(
            _SELECT_LINKS + " WHERE " + " AND ".join(clauses) + _ORDER_LINKS,
            tuple(parameters),
        )
        records = tuple(_link(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            next_cursor = AtlasLinkCursor(
                owner_id,
                page_id,
                direction,
                last.linked_at,
                last.source_page_id,
                last.target_page_id,
            )
        return AtlasLinkBatch(records, next_cursor)

    @staticmethod
    def _lock_owner(transaction: Any, owner_id: str) -> None:
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,79)) AS acquired",
            (owner_id,),
        )


__all__ = (
    "BACKLINK",
    "MAX_LINK_LIST",
    "MAX_LINK_TARGETS",
    "MAX_REVISION",
    "MAX_TIME",
    "OUTBOUND",
    "AtlasLinkBatch",
    "AtlasLinkChange",
    "AtlasLinkCommandReceipt",
    "AtlasLinkCursor",
    "AtlasLinkRecord",
    "AtlasLinkRepository",
    "AtlasLinkSetCommand",
)