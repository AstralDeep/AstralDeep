"""Owner-isolated projects and normalized chat membership use caller-owned transactions.
Deletion keeps identities retired, and scoped keysets enumerate even empty legacy chats.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _required_id,
)

_MAX = 2**53 - 1
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _text(value: object, label: str, maximum: int = 512) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or value != value.strip():
        raise RepositoryValidationError(f"{label} is outside its supported bounds")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise RepositoryValidationError(f"{label} contains invalid characters")
    return value


def _integer(value: object, label: str, minimum: int = 0, maximum: int = _MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError(f"{label} is outside its supported bounds")
    return value


def _identity(owner_id: str, project_id: str) -> None:
    _text(owner_id, "owner_id")
    if type(project_id) is not str or _UUID.fullmatch(project_id) is None:
        raise RepositoryValidationError("project_id must be a canonical UUID")


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    owner_id: str = field(repr=False)
    name: str
    position: int
    revision: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ProjectCursor:
    owner_id: str = field(repr=False)
    position: int
    project_id: str


@dataclass(frozen=True, slots=True)
class ProjectPage:
    records: tuple[ProjectRecord, ...]
    next_cursor: ProjectCursor | None


@dataclass(frozen=True, slots=True)
class ProjectChatRecord:
    chat_id: str
    owner_id: str = field(repr=False)
    title: str | None
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class ProjectChatCursor:
    owner_id: str = field(repr=False)
    project_id: str | None
    cutoff: int | None
    chat_id: str


@dataclass(frozen=True, slots=True)
class ProjectChatPage:
    records: tuple[ProjectChatRecord, ...]
    next_cursor: ProjectChatCursor | None


def _record(row: Any) -> ProjectRecord:
    return ProjectRecord(
        str(row["project_id"]),
        row["owner_id"],
        row["name"],
        row["position"],
        row["revision"],
        row["created_at"],
        row["updated_at"],
    )


class ProjectRepository:
    @staticmethod
    def _lock(transaction: Any, owner_id: str) -> None:
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 91)) AS acquired", (owner_id,)
        )

    def create(
        self,
        transaction: Any,
        *,
        owner_id: str,
        project_id: str,
        name: str,
        position: int,
        created_at: int,
    ) -> ProjectRecord:
        _identity(owner_id, project_id)
        _text(name, "name", 200)
        _integer(position, "position")
        _integer(created_at, "created_at")
        digest = hashlib.sha256(
            json.dumps([name, position], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        self._lock(transaction, owner_id)
        row = transaction.fetch_one(
            "INSERT INTO chat_project(owner_id,project_id,name,position,revision,"
            "created_at,updated_at,create_digest) VALUES(%s,%s,%s,%s,1,%s,%s,%s)"
            " ON CONFLICT(owner_id,project_id) DO NOTHING RETURNING *",
            (owner_id, project_id, name, position, created_at, created_at, digest),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM chat_project WHERE owner_id=%s AND project_id=%s FOR UPDATE",
                (owner_id, project_id),
            )
            if row["deleted"] or row["create_digest"] != digest:
                raise RepositoryConflictError("project identity was reused or retired")
        return _record(row)

    def get(
        self, query: Any, *, owner_id: str, project_id: str, for_update: bool = False
    ) -> ProjectRecord | None:
        _identity(owner_id, project_id)
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be boolean")
        if for_update:
            self._lock(query, owner_id)
        row = query.fetch_one(
            "SELECT * FROM chat_project WHERE owner_id=%s AND project_id=%s AND NOT deleted"
            + (" FOR UPDATE" if for_update else ""),
            (owner_id, project_id),
        )
        return None if row is None else _record(row)

    def list(
        self, query: Any, *, owner_id: str, cursor: ProjectCursor | None = None, limit: int = 50
    ) -> ProjectPage:
        _text(owner_id, "owner_id")
        _integer(limit, "limit", 1, 200)
        parameters: list[object] = [owner_id]
        suffix = ""
        if cursor is not None:
            if type(cursor) is not ProjectCursor or cursor.owner_id != owner_id:
                raise RepositoryValidationError("project cursor is invalid for this scope")
            _identity(owner_id, cursor.project_id)
            _integer(cursor.position, "cursor position")
            suffix = " AND (position,project_id)>(%s,%s::uuid)"
            parameters.extend((cursor.position, cursor.project_id))
        rows = query.fetch_all(
            "SELECT * FROM chat_project WHERE owner_id=%s AND NOT deleted"
            + suffix
            + " ORDER BY position,project_id LIMIT %s",
            (*parameters, limit + 1),
        )
        records = tuple(_record(row) for row in rows[:limit])
        next_cursor = (
            ProjectCursor(owner_id, records[-1].position, records[-1].project_id)
            if len(rows) > limit
            else None
        )
        return ProjectPage(records, next_cursor)

    def rename(
        self,
        transaction: Any,
        *,
        owner_id: str,
        project_id: str,
        expected_revision: int,
        name: str,
        updated_at: int,
    ) -> ProjectRecord:
        _identity(owner_id, project_id)
        _integer(expected_revision, "expected_revision", 1, _MAX - 1)
        _text(name, "name", 200)
        _integer(updated_at, "updated_at")
        self._lock(transaction, owner_id)
        row = transaction.fetch_one(
            "UPDATE chat_project SET name=%s,updated_at=%s,revision=revision+1"
            " WHERE owner_id=%s AND project_id=%s AND revision=%s AND NOT deleted"
            " AND updated_at<=%s RETURNING *",
            (name, updated_at, owner_id, project_id, expected_revision, updated_at),
        )
        if row is None:
            self._write_miss(transaction, owner_id, project_id)
        return _record(row)

    def delete(
        self, transaction: Any, *, owner_id: str, project_id: str, expected_revision: int
    ) -> bool:
        _identity(owner_id, project_id)
        _integer(expected_revision, "expected_revision", 1, _MAX - 1)
        self._lock(transaction, owner_id)
        row = transaction.fetch_one(
            "UPDATE chat_project AS p SET deleted=TRUE,revision=revision+1"
            " WHERE owner_id=%s AND project_id=%s AND revision=%s AND NOT deleted"
            " AND NOT EXISTS(SELECT 1 FROM chat_project_membership AS m"
            " WHERE m.owner_id=p.owner_id AND m.project_id=p.project_id) RETURNING *",
            (owner_id, project_id, expected_revision),
        )
        if row is None:
            self._write_miss(transaction, owner_id, project_id)
        return True

    def _write_miss(self, query: Any, owner_id: str, project_id: str) -> None:
        if self.get(query, owner_id=owner_id, project_id=project_id) is None:
            raise RepositoryNotFoundError("project not found")
        raise RepositoryConflictError("project revision, clock, or membership changed")

    def attach_chat(self, transaction: Any, *, owner_id: str, project_id: str, chat_id: str) -> str:
        _identity(owner_id, project_id)
        _required_id(chat_id, "chat_id")
        self._lock(transaction, owner_id)
        if self.get(transaction, owner_id=owner_id, project_id=project_id, for_update=True) is None:
            raise RepositoryNotFoundError("project not found")
        if (
            transaction.fetch_one(
                "SELECT id FROM chats WHERE user_id=%s AND id=%s FOR UPDATE", (owner_id, chat_id)
            )
            is None
        ):
            raise RepositoryNotFoundError("chat not found")
        row = transaction.fetch_one(
            "INSERT INTO chat_project_membership(owner_id,project_id,chat_id)"
            " SELECT p.owner_id,p.project_id,c.id FROM chat_project AS p JOIN chats AS c"
            " ON c.user_id=p.owner_id WHERE p.owner_id=%s AND p.project_id=%s"
            " AND NOT p.deleted AND c.id=%s ON CONFLICT(owner_id,chat_id) DO UPDATE"
            " SET project_id=EXCLUDED.project_id WHERE"
            " chat_project_membership.owner_id=EXCLUDED.owner_id AND"
            " chat_project_membership.project_id=EXCLUDED.project_id RETURNING project_id",
            (owner_id, project_id, chat_id),
        )
        if row is None:
            raise RepositoryConflictError("chat already belongs to another project")
        return str(row["project_id"])

    def project_for_chat(self, query: Any, *, owner_id: str, chat_id: str) -> ProjectRecord | None:
        _text(owner_id, "owner_id")
        _required_id(chat_id, "chat_id")
        row = query.fetch_one(
            "SELECT p.* FROM chat_project AS p JOIN chat_project_membership AS m"
            " ON m.owner_id=p.owner_id AND m.project_id=p.project_id"
            " WHERE m.owner_id=%s AND m.chat_id=%s AND NOT p.deleted",
            (owner_id, chat_id),
        )
        return None if row is None else _record(row)

    def list_chats(
        self,
        query: Any,
        *,
        owner_id: str,
        project_id: str,
        cursor: ProjectChatCursor | None = None,
        limit: int = 50,
    ) -> ProjectChatPage:
        _identity(owner_id, project_id)
        if self.get(query, owner_id=owner_id, project_id=project_id) is None:
            raise RepositoryNotFoundError("project not found")
        return self._chats(query, owner_id, project_id, None, cursor, limit)

    def list_owner_chats_before(
        self,
        query: Any,
        *,
        owner_id: str,
        cutoff: int,
        cursor: ProjectChatCursor | None = None,
        limit: int = 100,
    ) -> ProjectChatPage:
        _text(owner_id, "owner_id")
        _integer(cutoff, "cutoff")
        return self._chats(query, owner_id, None, cutoff, cursor, limit)

    @staticmethod
    def _chats(
        query: Any,
        owner_id: str,
        project_id: str | None,
        cutoff: int | None,
        cursor: ProjectChatCursor | None,
        limit: int,
    ) -> ProjectChatPage:
        _integer(limit, "limit", 1, 200)
        clauses = ["c.user_id=%s"]
        parameters: list[object] = [owner_id]
        if project_id is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM chat_project_membership AS m"
                " WHERE m.owner_id=c.user_id AND m.chat_id=c.id AND m.project_id=%s)"
            )
            parameters.append(project_id)
        if cutoff is not None:
            clauses.append("COALESCE(c.created_at,0)<%s")
            parameters.append(cutoff)
        if cursor is not None:
            if type(cursor) is not ProjectChatCursor or (
                cursor.owner_id,
                cursor.project_id,
                cursor.cutoff,
            ) != (owner_id, project_id, cutoff):
                raise RepositoryValidationError("chat cursor is invalid for this scope")
            _required_id(cursor.chat_id, "cursor chat_id")
            clauses.append("c.id>%s")
            parameters.append(cursor.chat_id)
        rows = query.fetch_all(
            "SELECT c.id,c.user_id,c.title,c.created_at,c.updated_at FROM chats AS c WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.id LIMIT %s",
            (*parameters, limit + 1),
        )
        records = tuple(
            ProjectChatRecord(
                row["id"], row["user_id"], row["title"], row["created_at"], row["updated_at"]
            )
            for row in rows[:limit]
        )
        next_cursor = (
            ProjectChatCursor(owner_id, project_id, cutoff, records[-1].chat_id)
            if len(rows) > limit
            else None
        )
        return ProjectChatPage(records, next_cursor)


__all__ = (
    "ProjectChatCursor",
    "ProjectChatPage",
    "ProjectChatRecord",
    "ProjectCursor",
    "ProjectPage",
    "ProjectRecord",
    "ProjectRepository",
)
