"""Opaque owner- and project-scoped Atlas page heads, immutable content revisions, terminal
tombstones, and exact command receipts. Revision bytes, edits, pagination, and replay stay
neutral; callers own transactions and encrypt page content before it reaches Plane.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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
MAX_CONTENT_BYTES: Final = 16384
MAX_SOURCES: Final = 8
MAX_SOURCE_CHARACTERS: Final = 512
MAX_PROJECT_CHARACTERS: Final = 128
MAX_SLUG_CHARACTERS: Final = 128
MAX_PAGE_LIST: Final = 100
MAX_REVISION_LIST: Final = 100
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, field_name: str, *, maximum: int, multiline: bool = False) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise RepositoryValidationError(f"{field_name} is outside its supported bounds")
    if value != value.strip():
        raise RepositoryValidationError(f"{field_name} must not have edge whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} and not (multiline and character in "\n\t")
        for character in value
    ):
        raise RepositoryValidationError(f"{field_name} contains invalid characters")
    return value


def _owner(value: object) -> str:
    return _text(value, "owner_id", maximum=512)


def _project(value: object) -> str | None:
    if value is None:
        return None
    return _text(value, "project_id", maximum=MAX_PROJECT_CHARACTERS)


def _slug(value: object) -> str:
    if type(value) is not str or _SLUG.fullmatch(value) is None:
        raise RepositoryValidationError("slug is not canonical")
    return value


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


def _sources(value: object) -> tuple[str, ...]:
    if type(value) not in (tuple, list) or len(value) > MAX_SOURCES:
        raise RepositoryValidationError("sources is outside its supported bounds")
    result = tuple(
        _text(source, "source", maximum=MAX_SOURCE_CHARACTERS, multiline=True) for source in value
    )
    if len(set(result)) != len(result):
        raise RepositoryValidationError("sources must be unique")
    return result


def _content(value: object) -> bytes:
    if type(value) is not bytes or not 1 <= len(value) <= MAX_CONTENT_BYTES:
        raise RepositoryValidationError("content is outside its supported ciphertext bounds")
    return value


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
class AtlasPageCreateCommand:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    project_id: str | None
    slug: str
    sources: tuple[str, ...]
    content: bytes = field(repr=False)
    created_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        _project(self.project_id)
        _slug(self.slug)
        object.__setattr__(self, "sources", _sources(self.sources))
        _content(self.content)
        _integer(self.created_at, "created_at", maximum=MAX_TIME)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def request_digest(self) -> str:
        return _request_digest(
            {
                "version": 1,
                "kind": "atlas_page_create",
                "owner_id": self.owner_id,
                "page_id": self.page_id,
                "project_id": self.project_id,
                "slug": self.slug,
                "sources": list(self.sources),
                "content_sha256": self.content_sha256,
                "created_at": self.created_at,
            }
        )


@dataclass(frozen=True, slots=True)
class AtlasPageDeleteCommand:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    expected_revision: int
    deleted_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        _integer(self.expected_revision, "expected_revision", minimum=1, maximum=MAX_REVISION - 1)
        _integer(self.deleted_at, "deleted_at", maximum=MAX_TIME)

    @property
    def request_digest(self) -> str:
        return _request_digest(
            {
                "version": 1,
                "kind": "atlas_page_delete",
                "owner_id": self.owner_id,
                "page_id": self.page_id,
                "expected_revision": self.expected_revision,
                "deleted_at": self.deleted_at,
            }
        )


@dataclass(frozen=True, slots=True)
class AtlasPageEditCommand:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    expected_revision: int
    sources: tuple[str, ...]
    content: bytes = field(repr=False)
    updated_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        _integer(self.expected_revision, "expected_revision", minimum=1, maximum=MAX_REVISION - 1)
        object.__setattr__(self, "sources", _sources(self.sources))
        _content(self.content)
        _integer(self.updated_at, "updated_at", maximum=MAX_TIME)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def request_digest(self) -> str:
        return _request_digest(
            {
                "version": 1,
                "kind": "atlas_page_edit",
                "owner_id": self.owner_id,
                "page_id": self.page_id,
                "expected_revision": self.expected_revision,
                "sources": list(self.sources),
                "content_sha256": self.content_sha256,
                "updated_at": self.updated_at,
            }
        )


@dataclass(frozen=True, slots=True)
class AtlasPageRecord:
    owner_id: str = field(repr=False)
    page_id: str
    project_id: str | None
    slug: str
    revision: int
    sources: tuple[str, ...]
    content: bytes | None = field(repr=False)
    content_sha256: str | None
    created_at: int
    updated_at: int
    deleted_at: int | None

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.page_id, "page_id")
        _project(self.project_id)
        _slug(self.slug)
        _integer(self.revision, "revision", minimum=1)
        object.__setattr__(self, "sources", _sources(self.sources))
        _integer(self.created_at, "created_at", maximum=MAX_TIME)
        _integer(self.updated_at, "updated_at", minimum=self.created_at, maximum=MAX_TIME)
        if self.deleted_at is None:
            _content(self.content)
            if self.content_sha256 is None:
                raise RepositoryValidationError("live atlas page requires a content digest")
            _digest(self.content_sha256, "content_sha256")
            if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
                raise RepositoryValidationError("live atlas page digest does not match content")
        else:
            _integer(self.deleted_at, "deleted_at", minimum=self.created_at, maximum=MAX_TIME)
            if self.deleted_at != self.updated_at:
                raise RepositoryValidationError("atlas page tombstone time is inconsistent")
            if self.content is not None or self.content_sha256 is not None:
                raise RepositoryValidationError("atlas page tombstone must not retain content")
            if self.sources:
                raise RepositoryValidationError("atlas page tombstone must not retain sources")
            if self.revision < 2:
                raise RepositoryValidationError("atlas page tombstone revision is incomplete")

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True, slots=True)
class AtlasPageCommandReceipt:
    owner_id: str = field(repr=False)
    command_id: str
    page_id: str
    command: str
    request_digest: str
    result_revision: int
    created_at: int
    expected_revision: int | None = None

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.command_id, "command_id")
        _identifier(self.page_id, "page_id")
        if type(self.command) is not str or self.command not in {"create", "edit", "delete"}:
            raise RepositoryValidationError("command must be create, edit, or delete")
        _digest(self.request_digest, "request_digest")
        _integer(self.result_revision, "result_revision", minimum=1)
        _integer(self.created_at, "created_at", maximum=MAX_TIME)
        if self.expected_revision is not None:
            _integer(
                self.expected_revision,
                "expected_revision",
                minimum=1,
                maximum=MAX_REVISION - 1,
            )
        if self.command == "create":
            if self.result_revision != 1 or self.expected_revision is not None:
                raise RepositoryValidationError("command receipt revision is inconsistent")
            return
        if self.result_revision < 2:
            raise RepositoryValidationError("command receipt revision is inconsistent")
        if (
            self.expected_revision is not None
            and self.result_revision != self.expected_revision + 1
        ):
            raise RepositoryValidationError("command receipt revision is inconsistent")


@dataclass(frozen=True, slots=True)
class AtlasPageChange:
    page: AtlasPageRecord
    receipt: AtlasPageCommandReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.page) is not AtlasPageRecord:
            raise RepositoryValidationError("atlas page change requires a typed page")
        if type(self.receipt) is not AtlasPageCommandReceipt:
            raise RepositoryValidationError("atlas page change requires a typed receipt")
        _boolean(self.replayed, "replayed")
        if (self.page.owner_id, self.page.page_id) != (
            self.receipt.owner_id,
            self.receipt.page_id,
        ):
            raise RepositoryValidationError("atlas page change identity is inconsistent")


@dataclass(frozen=True, slots=True)
class AtlasPageCursor:
    owner_id: str = field(repr=False)
    project_id: str | None
    created_at: int
    page_id: str

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _project(self.project_id)
        _integer(self.created_at, "created_at", maximum=MAX_TIME)
        _identifier(self.page_id, "page_id")


@dataclass(frozen=True, slots=True)
class AtlasPageBatch:
    records: tuple[AtlasPageRecord, ...]
    next_cursor: AtlasPageCursor | None

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(
            type(record) is not AtlasPageRecord for record in self.records
        ):
            raise RepositoryValidationError("atlas page batch requires detached records")
        if self.next_cursor is not None and type(self.next_cursor) is not AtlasPageCursor:
            raise RepositoryValidationError("atlas page batch cursor is invalid")


@dataclass(frozen=True, slots=True)
class AtlasPageRevisionRecord:
    owner_id: str = field(repr=False)
    page_id: str
    revision: int
    sources: tuple[str, ...]
    content: bytes = field(repr=False)
    content_sha256: str
    created_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.page_id, "page_id")
        _integer(self.revision, "revision", minimum=1)
        object.__setattr__(self, "sources", _sources(self.sources))
        _content(self.content)
        _digest(self.content_sha256, "content_sha256")
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise RepositoryValidationError("atlas page revision digest does not match content")
        _integer(self.created_at, "created_at", maximum=MAX_TIME)


@dataclass(frozen=True, slots=True)
class AtlasPageRevisionCursor:
    owner_id: str = field(repr=False)
    page_id: str
    revision: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _identifier(self.page_id, "page_id")
        _integer(self.revision, "revision", minimum=1)


@dataclass(frozen=True, slots=True)
class AtlasPageRevisionBatch:
    records: tuple[AtlasPageRevisionRecord, ...]
    next_cursor: AtlasPageRevisionCursor | None

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(
            type(record) is not AtlasPageRevisionRecord for record in self.records
        ):
            raise RepositoryValidationError("atlas revision batch requires detached records")
        if (
            self.next_cursor is not None
            and type(self.next_cursor) is not AtlasPageRevisionCursor
        ):
            raise RepositoryValidationError("atlas revision batch cursor is invalid")


@dataclass(frozen=True, slots=True)
class AtlasPageEditChange:
    page: AtlasPageRecord
    revision: AtlasPageRevisionRecord
    receipt: AtlasPageCommandReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.page) is not AtlasPageRecord:
            raise RepositoryValidationError("atlas page edit requires a typed page")
        if type(self.revision) is not AtlasPageRevisionRecord:
            raise RepositoryValidationError("atlas page edit requires a typed revision")
        if type(self.receipt) is not AtlasPageCommandReceipt:
            raise RepositoryValidationError("atlas page edit requires a typed receipt")
        _boolean(self.replayed, "replayed")
        if self.receipt.command != "edit":
            raise RepositoryValidationError("atlas page edit receipt is inconsistent")
        if (self.page.owner_id, self.page.page_id) != (
            self.receipt.owner_id,
            self.receipt.page_id,
        ) or (self.revision.owner_id, self.revision.page_id) != (
            self.receipt.owner_id,
            self.receipt.page_id,
        ):
            raise RepositoryValidationError("atlas page edit identity is inconsistent")
        if self.revision.revision != self.receipt.result_revision:
            raise RepositoryValidationError("atlas page edit revision is inconsistent")
        if self.page.deleted or self.page.revision < self.revision.revision:
            raise RepositoryValidationError("atlas page edit result is inconsistent")


AtlasPageAppendCommand = AtlasPageEditCommand
AtlasPageAppendChange = AtlasPageEditChange


def _page(row: Any) -> AtlasPageRecord | None:
    if row is None:
        return None
    try:
        sources = row["sources"]
        if isinstance(sources, str):
            sources = json.loads(sources)
        content = None if row["content"] is None else bytes(row["content"])
        return AtlasPageRecord(
            row["owner_id"],
            str(row["page_id"]),
            row["project_id"],
            row["slug"],
            row["revision"],
            tuple(sources),
            content,
            row["content_sha256"],
            row["created_at"],
            row["updated_at"],
            row["deleted_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("invalid stored atlas page") from exc


def _receipt(row: Any) -> AtlasPageCommandReceipt | None:
    if row is None:
        return None
    try:
        return AtlasPageCommandReceipt(
            row["owner_id"],
            str(row["command_id"]),
            str(row["page_id"]),
            row["command"],
            row["request_digest"],
            row["result_revision"],
            row["created_at"],
            row.get("expected_revision"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDataError("invalid stored atlas page command receipt") from exc


def _revision(row: Any) -> AtlasPageRevisionRecord | None:
    if row is None:
        return None
    try:
        sources = row["sources"]
        if isinstance(sources, str):
            sources = json.loads(sources)
        return AtlasPageRevisionRecord(
            row["owner_id"],
            str(row["page_id"]),
            row["revision"],
            tuple(sources),
            bytes(row["content"]),
            row["content_sha256"],
            row["created_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("invalid stored atlas page revision") from exc


class AtlasPageRepository:
    def create_page(self, transaction: Any, *, command: AtlasPageCreateCommand) -> AtlasPageChange:
        if type(command) is not AtlasPageCreateCommand:
            raise RepositoryValidationError("typed atlas page create command required")
        command = AtlasPageCreateCommand(
            command.owner_id,
            command.command_id,
            command.page_id,
            command.project_id,
            command.slug,
            command.sources,
            command.content,
            command.created_at,
        )
        self._lock_owner(transaction, command.owner_id)
        stored_receipt = _receipt(
            transaction.fetch_one(
                "SELECT * FROM atlas_page_command WHERE owner_id=%s AND command_id=%s FOR UPDATE",
                (command.owner_id, command.command_id),
            )
        )
        if stored_receipt is not None:
            if (
                stored_receipt.page_id,
                stored_receipt.command,
                stored_receipt.request_digest,
            ) != (command.page_id, "create", command.request_digest):
                raise RepositoryConflictError("atlas page command identity was reused")
            page = self.get_page(
                transaction,
                owner_id=command.owner_id,
                page_id=command.page_id,
                include_deleted=True,
            )
            if page is None:
                raise RepositoryDataError("atlas page command receipt has no page")
            return AtlasPageChange(page, stored_receipt, True)
        if transaction.fetch_one(
            "SELECT page_id FROM atlas_page WHERE owner_id=%s AND page_id=%s",
            (command.owner_id, command.page_id),
        ):
            raise RepositoryConflictError("atlas page identity already exists")
        if transaction.fetch_one(
            "SELECT page_id FROM atlas_page WHERE owner_id=%s"
            " AND COALESCE(project_id,'')=%s AND slug=%s AND deleted_at IS NULL",
            (command.owner_id, command.project_id or "", command.slug),
        ):
            raise RepositoryConflictError("atlas page slug is already used")
        row = transaction.fetch_one(
            "INSERT INTO atlas_page(owner_id,page_id,project_id,slug,revision,sources,"
            "content,content_sha256,created_at,updated_at,deleted_at)"
            " VALUES(%s,%s,%s,%s,1,%s::jsonb,%s,%s,%s,%s,NULL) RETURNING *",
            (
                command.owner_id,
                command.page_id,
                command.project_id,
                command.slug,
                _canonical(list(command.sources)),
                command.content,
                command.content_sha256,
                command.created_at,
                command.created_at,
            ),
        )
        page = _page(row)
        if page is None:
            raise RepositoryDataError("atlas page insert returned no record")
        transaction.execute(
            "INSERT INTO atlas_page_revision(owner_id,page_id,revision,sources,content,"
            "content_sha256,created_at) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (
                command.owner_id,
                command.page_id,
                1,
                _canonical(list(command.sources)),
                command.content,
                command.content_sha256,
                command.created_at,
            ),
        )
        receipt = AtlasPageCommandReceipt(
            command.owner_id,
            command.command_id,
            command.page_id,
            "create",
            command.request_digest,
            1,
            command.created_at,
        )
        transaction.execute(
            "INSERT INTO atlas_page_command(owner_id,command_id,page_id,command,"
            "request_digest,result_revision,created_at,expected_revision)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt.owner_id,
                receipt.command_id,
                receipt.page_id,
                receipt.command,
                receipt.request_digest,
                receipt.result_revision,
                receipt.created_at,
                receipt.expected_revision,
            ),
        )
        return AtlasPageChange(page, receipt, False)

    def delete_page(self, transaction: Any, *, command: AtlasPageDeleteCommand) -> AtlasPageChange:
        if type(command) is not AtlasPageDeleteCommand:
            raise RepositoryValidationError("typed atlas page delete command required")
        command = AtlasPageDeleteCommand(
            command.owner_id,
            command.command_id,
            command.page_id,
            command.expected_revision,
            command.deleted_at,
        )
        self._lock_owner(transaction, command.owner_id)
        stored_receipt = _receipt(
            transaction.fetch_one(
                "SELECT * FROM atlas_page_command WHERE owner_id=%s AND command_id=%s FOR UPDATE",
                (command.owner_id, command.command_id),
            )
        )
        if stored_receipt is not None:
            if (
                stored_receipt.page_id,
                stored_receipt.command,
                stored_receipt.request_digest,
            ) != (command.page_id, "delete", command.request_digest):
                raise RepositoryConflictError("atlas page command identity was reused")
            page = self.get_page(
                transaction,
                owner_id=command.owner_id,
                page_id=command.page_id,
                include_deleted=True,
            )
            if page is None:
                raise RepositoryDataError("atlas page command receipt has no page")
            return AtlasPageChange(page, stored_receipt, True)
        current = _page(
            transaction.fetch_one(
                "SELECT * FROM atlas_page WHERE owner_id=%s AND page_id=%s FOR UPDATE",
                (command.owner_id, command.page_id),
            )
        )
        if current is None:
            raise RepositoryNotFoundError("atlas page not found")
        if current.deleted:
            raise RepositoryConflictError("atlas page is already deleted")
        if current.revision != command.expected_revision:
            raise RepositoryConflictError("atlas page revision changed")
        if command.deleted_at < current.updated_at:
            raise RepositoryConflictError("atlas page deletion clock regressed")
        row = transaction.fetch_one(
            "UPDATE atlas_page SET revision=revision+1,sources='[]'::jsonb,content=NULL,"
            "content_sha256=NULL,updated_at=%s,deleted_at=%s WHERE owner_id=%s AND page_id=%s"
            " AND revision=%s AND deleted_at IS NULL RETURNING *",
            (
                command.deleted_at,
                command.deleted_at,
                command.owner_id,
                command.page_id,
                command.expected_revision,
            ),
        )
        page = _page(row)
        if page is None:
            raise RepositoryConflictError("atlas page revision changed")
        receipt = AtlasPageCommandReceipt(
            command.owner_id,
            command.command_id,
            command.page_id,
            "delete",
            command.request_digest,
            page.revision,
            command.deleted_at,
            command.expected_revision,
        )
        transaction.execute(
            "INSERT INTO atlas_page_command(owner_id,command_id,page_id,command,"
            "request_digest,result_revision,created_at,expected_revision)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt.owner_id,
                receipt.command_id,
                receipt.page_id,
                receipt.command,
                receipt.request_digest,
                receipt.result_revision,
                receipt.created_at,
                receipt.expected_revision,
            ),
        )
        transaction.execute(
            "UPDATE atlas_page_link_head SET link_revision=link_revision+1,"
            "updated_at=GREATEST(updated_at,%s) WHERE owner_id=%s AND page_id IN ("
            "SELECT source_page_id FROM atlas_page_link WHERE owner_id=%s"
            " AND target_page_id=%s AND source_page_id<>%s)",
            (
                command.deleted_at,
                command.owner_id,
                command.owner_id,
                command.page_id,
                command.page_id,
            ),
        )
        transaction.execute(
            "DELETE FROM atlas_page_link WHERE owner_id=%s"
            " AND (source_page_id=%s OR target_page_id=%s)",
            (command.owner_id, command.page_id, command.page_id),
        )
        transaction.execute(
            "DELETE FROM atlas_page_link_head WHERE owner_id=%s AND page_id=%s",
            (command.owner_id, command.page_id),
        )
        return AtlasPageChange(page, receipt, False)

    def edit_page(
        self, transaction: Any, *, command: AtlasPageEditCommand
    ) -> AtlasPageEditChange:
        if type(command) is not AtlasPageEditCommand:
            raise RepositoryValidationError("typed atlas page edit command required")
        command = AtlasPageEditCommand(
            command.owner_id,
            command.command_id,
            command.page_id,
            command.expected_revision,
            command.sources,
            command.content,
            command.updated_at,
        )
        self._lock_owner(transaction, command.owner_id)
        stored_receipt = _receipt(
            transaction.fetch_one(
                "SELECT * FROM atlas_page_command WHERE owner_id=%s AND command_id=%s FOR UPDATE",
                (command.owner_id, command.command_id),
            )
        )
        if stored_receipt is not None:
            if (
                stored_receipt.page_id,
                stored_receipt.command,
                stored_receipt.request_digest,
            ) != (command.page_id, "edit", command.request_digest):
                raise RepositoryConflictError("atlas page command identity was reused")
            page = self.get_page(
                transaction,
                owner_id=command.owner_id,
                page_id=command.page_id,
                include_deleted=True,
            )
            if page is None:
                raise RepositoryDataError("atlas page command receipt has no page")
            if page.deleted:
                raise RepositoryConflictError("atlas page is deleted")
            revision = self.get_revision(
                transaction,
                owner_id=command.owner_id,
                page_id=command.page_id,
                revision=stored_receipt.result_revision,
                include_deleted=True,
            )
            if revision is None:
                raise RepositoryDataError("atlas page edit receipt has no revision")
            return AtlasPageEditChange(page, revision, stored_receipt, True)
        current = _page(
            transaction.fetch_one(
                "SELECT * FROM atlas_page WHERE owner_id=%s AND page_id=%s FOR UPDATE",
                (command.owner_id, command.page_id),
            )
        )
        if current is None:
            raise RepositoryNotFoundError("atlas page not found")
        if current.deleted:
            raise RepositoryConflictError("atlas page is deleted")
        if current.revision != command.expected_revision:
            raise RepositoryConflictError("atlas page revision changed")
        if command.updated_at < current.updated_at:
            raise RepositoryConflictError("atlas page edit clock regressed")
        revision = AtlasPageRevisionRecord(
            command.owner_id,
            command.page_id,
            current.revision + 1,
            command.sources,
            command.content,
            command.content_sha256,
            command.updated_at,
        )
        transaction.execute(
            "INSERT INTO atlas_page_revision(owner_id,page_id,revision,sources,content,"
            "content_sha256,created_at) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (
                revision.owner_id,
                revision.page_id,
                revision.revision,
                _canonical(list(revision.sources)),
                revision.content,
                revision.content_sha256,
                revision.created_at,
            ),
        )
        row = transaction.fetch_one(
            "UPDATE atlas_page SET revision=%s,sources=%s::jsonb,content=%s,"
            "content_sha256=%s,updated_at=%s WHERE owner_id=%s AND page_id=%s"
            " AND revision=%s AND deleted_at IS NULL RETURNING *",
            (
                revision.revision,
                _canonical(list(revision.sources)),
                revision.content,
                revision.content_sha256,
                revision.created_at,
                command.owner_id,
                command.page_id,
                command.expected_revision,
            ),
        )
        page = _page(row)
        if page is None:
            raise RepositoryConflictError("atlas page revision changed")
        receipt = AtlasPageCommandReceipt(
            command.owner_id,
            command.command_id,
            command.page_id,
            "edit",
            command.request_digest,
            revision.revision,
            command.updated_at,
            command.expected_revision,
        )
        transaction.execute(
            "INSERT INTO atlas_page_command(owner_id,command_id,page_id,command,"
            "request_digest,result_revision,created_at,expected_revision)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                receipt.owner_id,
                receipt.command_id,
                receipt.page_id,
                receipt.command,
                receipt.request_digest,
                receipt.result_revision,
                receipt.created_at,
                receipt.expected_revision,
            ),
        )
        return AtlasPageEditChange(page, revision, receipt, False)

    def append_revision(
        self, transaction: Any, *, command: AtlasPageEditCommand
    ) -> AtlasPageEditChange:
        return self.edit_page(transaction, command=command)

    def get_revision(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        revision: int,
        include_deleted: bool = False,
    ) -> AtlasPageRevisionRecord | None:
        owner_id = _owner(owner_id)
        page_id = _identifier(page_id, "page_id")
        revision = _integer(revision, "revision", minimum=1)
        include = _boolean(include_deleted, "include_deleted")
        statement = (
            "SELECT stored.* FROM atlas_page_revision AS stored"
            " JOIN atlas_page AS page ON page.owner_id=stored.owner_id"
            " AND page.page_id=stored.page_id"
            " WHERE stored.owner_id=%s AND stored.page_id=%s AND stored.revision=%s"
        )
        if not include:
            statement += " AND page.deleted_at IS NULL"
        return _revision(query.fetch_one(statement, (owner_id, page_id, revision)))

    def list_revisions(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        include_deleted: bool = False,
        cursor: AtlasPageRevisionCursor | None = None,
        limit: int = 50,
    ) -> AtlasPageRevisionBatch:
        owner_id = _owner(owner_id)
        page_id = _identifier(page_id, "page_id")
        include = _boolean(include_deleted, "include_deleted")
        if type(limit) is not int or not 1 <= limit <= MAX_REVISION_LIST:
            raise RepositoryValidationError("atlas revision limit is outside its supported bounds")
        clauses = ["stored.owner_id=%s", "stored.page_id=%s"]
        parameters: list[object] = [owner_id, page_id]
        if not include:
            clauses.append("page.deleted_at IS NULL")
        if cursor is not None:
            if type(cursor) is not AtlasPageRevisionCursor or (
                cursor.owner_id,
                cursor.page_id,
            ) != (owner_id, page_id):
                raise RepositoryValidationError("atlas revision cursor is invalid for this scope")
            clauses.append("stored.revision<%s")
            parameters.append(cursor.revision)
        parameters.append(limit + 1)
        rows = query.fetch_all(
            "SELECT stored.* FROM atlas_page_revision AS stored"
            " JOIN atlas_page AS page ON page.owner_id=stored.owner_id"
            " AND page.page_id=stored.page_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY stored.revision DESC LIMIT %s",
            tuple(parameters),
        )
        records = tuple(_revision(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            next_cursor = AtlasPageRevisionCursor(owner_id, page_id, last.revision)
        return AtlasPageRevisionBatch(records, next_cursor)

    def get_page_revision(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        revision: int,
        include_deleted: bool = False,
    ) -> AtlasPageRevisionRecord | None:
        return self.get_revision(
            query,
            owner_id=owner_id,
            page_id=page_id,
            revision=revision,
            include_deleted=include_deleted,
        )

    def list_page_revisions(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        include_deleted: bool = False,
        cursor: AtlasPageRevisionCursor | None = None,
        limit: int = 50,
    ) -> AtlasPageRevisionBatch:
        return self.list_revisions(
            query,
            owner_id=owner_id,
            page_id=page_id,
            include_deleted=include_deleted,
            cursor=cursor,
            limit=limit,
        )

    def get_page(
        self,
        query: Any,
        *,
        owner_id: str,
        page_id: str,
        include_deleted: bool = False,
    ) -> AtlasPageRecord | None:
        owner_id = _owner(owner_id)
        page_id = _identifier(page_id, "page_id")
        include = _boolean(include_deleted, "include_deleted")
        statement = "SELECT * FROM atlas_page WHERE owner_id=%s AND page_id=%s"
        if not include:
            statement += " AND deleted_at IS NULL"
        return _page(query.fetch_one(statement, (owner_id, page_id)))

    def get_page_by_slug(
        self,
        query: Any,
        *,
        owner_id: str,
        project_id: str | None,
        slug: str,
    ) -> AtlasPageRecord | None:
        owner_id = _owner(owner_id)
        project_id = _project(project_id)
        slug = _slug(slug)
        if project_id is None:
            project_clause = "project_id IS NULL"
            parameters: tuple[object, ...] = (owner_id, slug)
        else:
            project_clause = "project_id=%s"
            parameters = (owner_id, project_id, slug)
        return _page(
            query.fetch_one(
                "SELECT * FROM atlas_page WHERE owner_id=%s AND "
                + project_clause
                + " AND slug=%s AND deleted_at IS NULL",
                parameters,
            )
        )

    def list_pages(
        self,
        query: Any,
        *,
        owner_id: str,
        project_id: str | None,
        cursor: AtlasPageCursor | None = None,
        limit: int = 50,
    ) -> AtlasPageBatch:
        owner_id = _owner(owner_id)
        project_id = _project(project_id)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIST:
            raise RepositoryValidationError("atlas page limit is outside its supported bounds")
        clauses = ["owner_id=%s", "deleted_at IS NULL"]
        parameters: list[object] = [owner_id]
        if project_id is None:
            clauses.append("project_id IS NULL")
        else:
            clauses.append("project_id=%s")
            parameters.append(project_id)
        if cursor is not None:
            if type(cursor) is not AtlasPageCursor or (
                cursor.owner_id,
                cursor.project_id,
            ) != (owner_id, project_id):
                raise RepositoryValidationError("atlas page cursor is invalid for this scope")
            _integer(cursor.created_at, "cursor created_at", maximum=MAX_TIME)
            _identifier(cursor.page_id, "cursor page_id")
            clauses.append("(created_at,page_id)<(%s,%s::uuid)")
            parameters.extend((cursor.created_at, cursor.page_id))
        parameters.append(limit + 1)
        rows = query.fetch_all(
            "SELECT * FROM atlas_page WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC,page_id DESC LIMIT %s",
            tuple(parameters),
        )
        records = tuple(_page(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            next_cursor = AtlasPageCursor(owner_id, project_id, last.created_at, last.page_id)
        return AtlasPageBatch(records, next_cursor)

    @staticmethod
    def _lock_owner(transaction: Any, owner_id: str) -> None:
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,79)) AS acquired",
            (owner_id,),
        )


__all__ = (
    "MAX_CONTENT_BYTES",
    "MAX_PAGE_LIST",
    "MAX_PROJECT_CHARACTERS",
    "MAX_REVISION",
    "MAX_REVISION_LIST",
    "MAX_SLUG_CHARACTERS",
    "MAX_SOURCES",
    "MAX_SOURCE_CHARACTERS",
    "MAX_TIME",
    "AtlasPageAppendChange",
    "AtlasPageAppendCommand",
    "AtlasPageBatch",
    "AtlasPageChange",
    "AtlasPageCommandReceipt",
    "AtlasPageCreateCommand",
    "AtlasPageCursor",
    "AtlasPageDeleteCommand",
    "AtlasPageEditChange",
    "AtlasPageEditCommand",
    "AtlasPageRecord",
    "AtlasPageRepository",
    "AtlasPageRevisionBatch",
    "AtlasPageRevisionCursor",
    "AtlasPageRevisionRecord",
)