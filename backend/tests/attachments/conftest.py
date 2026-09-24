"""Shared pytest fixtures for the attachments test suite: an isolated blob-root
filesystem, an in-memory StubDatabase, and helpers to seed or soft-delete attachment
rows through the explicit Plane repository double.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest
from astralplane.repositories.artifacts import AttachmentRecord

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


@pytest.fixture
def upload_root(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


class _StubCursor:
    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount


class _AttachmentPlaneRepository:
    def __init__(self) -> None:
        self.records: list[AttachmentRecord] = []
        self.materializations = self
        self.attachments = self

    def seed_for_test(self, record: AttachmentRecord) -> AttachmentRecord:
        if any(item.attachment_id == record.attachment_id for item in self.records):
            raise ValueError("attachment fixture identity already exists")
        self.records.append(record)
        return record

    def register(self, _transaction, record: AttachmentRecord) -> AttachmentRecord:
        self.records.append(record)
        return record

    def get(
        self,
        _transaction,
        *,
        owner_id: str,
        attachment_id: str,
        include_deleted: bool = False,
    ) -> Optional[AttachmentRecord]:
        return next(
            (
                record
                for record in self.records
                if record.owner_id == owner_id
                and record.attachment_id == attachment_id
                and (include_deleted or record.deleted_at is None)
            ),
            None,
        )

    def list_live(
        self,
        _transaction,
        *,
        owner_id: str,
        category: Optional[str],
        limit: int,
        before_created_at: Optional[int],
        before_attachment_id: Optional[str],
    ) -> tuple[AttachmentRecord, ...]:
        records = [
            record
            for record in self.records
            if record.owner_id == owner_id
            and record.deleted_at is None
            and (category is None or record.category == category)
        ]
        if before_created_at is not None and before_attachment_id is not None:
            records = [
                record
                for record in records
                if (record.created_at, record.attachment_id)
                < (before_created_at, before_attachment_id)
            ]
        records.sort(
            key=lambda record: (record.created_at, record.attachment_id),
            reverse=True,
        )
        return tuple(records[:limit])

    def soft_delete(
        self,
        _transaction,
        *,
        owner_id: str,
        attachment_id: str,
        deleted_at: int,
    ) -> Optional[AttachmentRecord]:
        for index, record in enumerate(self.records):
            if (
                record.owner_id == owner_id
                and record.attachment_id == attachment_id
                and record.deleted_at is None
            ):
                updated = replace(record, deleted_at=deleted_at)
                self.records[index] = updated
                return updated
        return None

    def soft_delete_all(
        self,
        _transaction,
        *,
        owner_id: str,
        deleted_at: int,
    ) -> int:
        count = 0
        for index, record in enumerate(self.records):
            if record.owner_id == owner_id and record.deleted_at is None:
                self.records[index] = replace(record, deleted_at=deleted_at)
                count += 1
        return count


class StubDatabase:
    def __init__(self) -> None:
        self.rows: List[dict] = []
        self.plane_repositories = SimpleNamespace(
            artifacts=_AttachmentPlaneRepository()
        )

    def execute(self, query: str, params: Tuple = ()) -> _StubCursor:
        q = query.strip().lower()
        if q.startswith("insert into user_attachments"):
            (
                attachment_id, user_id, filename, content_type, category,
                extension, size_bytes, sha256, storage_path, created_at,
            ) = params
            self.rows.append({
                "attachment_id": attachment_id,
                "user_id": user_id,
                "filename": filename,
                "content_type": content_type,
                "category": category,
                "extension": extension,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "storage_path": storage_path,
                "created_at": created_at,
                "deleted_at": None,
            })
            return _StubCursor(rowcount=1)
        if q.startswith("update user_attachments"):
            if "attachment_id = ?" in q:
                deleted_at, attachment_id, user_id = params
                count = 0
                for r in self.rows:
                    if (
                        r["attachment_id"] == attachment_id
                        and r["user_id"] == user_id
                        and r["deleted_at"] is None
                    ):
                        r["deleted_at"] = deleted_at
                        count += 1
                return _StubCursor(rowcount=count)
            else:
                deleted_at, user_id = params
                count = 0
                for r in self.rows:
                    if r["user_id"] == user_id and r["deleted_at"] is None:
                        r["deleted_at"] = deleted_at
                        count += 1
                return _StubCursor(rowcount=count)
        raise NotImplementedError(query)

    def fetch_one(self, query: str, params: Tuple = ()) -> Optional[dict]:
        q = query.strip().lower()
        if "where attachment_id = ?" in q and "and user_id = ?" not in q:
            (attachment_id,) = params
            for r in self.rows:
                if r["attachment_id"] == attachment_id:
                    return dict(r)
            return None
        if "where attachment_id = ? and user_id = ? and deleted_at is null" in q:
            attachment_id, user_id = params
            for r in self.rows:
                if (
                    r["attachment_id"] == attachment_id
                    and r["user_id"] == user_id
                    and r["deleted_at"] is None
                ):
                    return dict(r)
            return None
        raise NotImplementedError(query)

    def fetch_all(self, query: str, params: Tuple = ()) -> List[dict]:
        q = query.strip().lower()
        if not q.startswith("select * from user_attachments"):
            raise NotImplementedError(query)
        params_list = list(params)
        user_id = params_list.pop(0)
        category = None
        cursor_created_at = None
        cursor_id = None
        if "and category = ?" in q:
            category = params_list.pop(0)
        if "or (created_at = ?" in q:
            cursor_created_at = params_list.pop(0)
            _ = params_list.pop(0)
            cursor_id = params_list.pop(0)
        limit = params_list.pop(0)
        rows = [r for r in self.rows
                if r["user_id"] == user_id and r["deleted_at"] is None]
        if category is not None:
            rows = [r for r in rows if r["category"] == category]
        if cursor_created_at is not None:
            rows = [
                r for r in rows
                if (r["created_at"] < cursor_created_at) or (
                    r["created_at"] == cursor_created_at
                    and r["attachment_id"] < cursor_id
                )
            ]
        rows.sort(key=lambda r: (-r["created_at"], r["attachment_id"]), reverse=False)
        rows = sorted(rows, key=lambda r: (r["created_at"], r["attachment_id"]), reverse=True)
        return [dict(r) for r in rows[:limit]]


@pytest.fixture
def stub_db() -> StubDatabase:
    return StubDatabase()


class AttachmentPlaneRuntime:
    def __init__(self, repositories) -> None:
        self.repositories = repositories

    @contextmanager
    def transaction(self, isolation=None):
        del isolation
        yield object()


def attachment_plane_source(database):
    runtime = AttachmentPlaneRuntime(database.plane_repositories)
    return SimpleNamespace(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def seed_attachment_for_test(
    repo,
    *,
    attachment_id: str,
    user_id: str,
    filename: str,
    content_type: str,
    category: str,
    extension: str,
    size_bytes: int,
    sha256: str,
    storage_path: str | None = None,
    created_at: int | None = None,
) -> AttachmentRecord:
    fake = repo._artifacts.repository.materializations
    seed = getattr(fake, "seed_for_test", None)
    if seed is None:
        raise TypeError("attachment fixture requires the explicit attachment test fake")
    locator = storage_path or f"{user_id}/{attachment_id}/{filename}"
    return seed(
        AttachmentRecord(
            attachment_id=attachment_id,
            owner_id=user_id,
            filename=filename,
            content_type=content_type,
            category=category,
            extension=extension,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_locator=locator,
            created_at=(int(time.time() * 1000) if created_at is None else created_at),
        )
    )


def soft_delete_attachment_for_test(
    repo,
    *,
    attachment_id: str,
    user_id: str,
    deleted_at: int | None = None,
) -> bool:
    fake = repo._artifacts.repository.attachments
    record = fake.soft_delete(
        object(),
        owner_id=user_id,
        attachment_id=attachment_id,
        deleted_at=(int(time.time() * 1000) if deleted_at is None else deleted_at),
    )
    return record is not None

def insert_sample(repo, *, user_id: str, category: str = "document",
                  extension: str = "pdf", filename: Optional[str] = None) -> str:
    aid = str(uuid.uuid4())
    stored_filename = filename or f"{aid[:8]}.{extension}"
    seed_attachment_for_test(
        repo,
        attachment_id=aid,
        user_id=user_id,
        filename=stored_filename,
        content_type="application/pdf",
        category=category,
        extension=extension,
        size_bytes=1234,
        sha256="0" * 64,
    )
    time.sleep(0.001)
    return aid
