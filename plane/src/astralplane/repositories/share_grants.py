"""Immutable-snapshot share-grant persistence keyed by token digest, with revocation and
an open counter. Token hashing, PHI policy, and public HTTP handling stay with the
caller; digest lookups keep inactive states indistinguishable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _required_id,
    _row_value,
    _structured_json,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ShareGrantRevocationState(StrEnum):
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ShareGrantRecord:
    share_id: int
    token_sha256: str = field(repr=False)
    owner_id: str
    chat_id: str
    scope: str
    component_id: str | None
    snapshot_html: str = field(repr=False)
    snapshot_json: Any = field(repr=False)
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    open_count: int


@dataclass(frozen=True, slots=True)
class ShareGrantMetadata:
    share_id: int
    owner_id: str
    chat_id: str
    scope: str
    component_id: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    open_count: int


class ShareGrantRepository:
    _FIELDS = (
        "id, token_sha256, user_id, chat_id, scope, component_id, snapshot_html, "
        "snapshot_json, created_at, expires_at, revoked_at, open_count"
    )
    _METADATA_FIELDS = (
        "id, user_id, chat_id, scope, component_id, created_at, expires_at, "
        "revoked_at, open_count"
    )

    def create_grant(
        self,
        transaction: Transaction,
        *,
        token_sha256: str,
        owner_id: str,
        chat_id: str,
        scope: str,
        component_id: str | None,
        snapshot_html: str,
        snapshot_json: object,
        expires_at: datetime | None,
    ) -> ShareGrantRecord:
        digest = _digest(token_sha256)
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        grant_scope = _bounded_text(scope, "scope", maximum=128)
        component = _optional_text(component_id, "component_id", 512)
        html = _bounded_text(
            snapshot_html,
            "snapshot_html",
            maximum=10_000_000,
            allow_empty=True,
        )
        canonical_snapshot, frozen_snapshot = _snapshot_input(snapshot_json)
        expiry = _optional_aware_datetime(expires_at, "expires_at")
        row = transaction.fetch_one(
            f"""
            INSERT INTO share_grant (
                token_sha256, user_id, chat_id, scope, component_id,
                snapshot_html, snapshot_json, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (token_sha256) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (digest, owner, chat, grant_scope, component, html, canonical_snapshot, expiry),
        )
        if row is None:
            row = transaction.fetch_one(
                f"""
                SELECT {self._FIELDS} FROM share_grant
                 WHERE token_sha256 = %s AND user_id = %s
                """,
                (digest, owner),
            )
        if row is None:
            raise RepositoryConflictError("share digest is bound to another owner")
        record = _grant(row)
        if (
            record.owner_id != owner
            or record.chat_id != chat
            or record.scope != grant_scope
            or record.component_id != component
            or record.snapshot_html != html
            or record.snapshot_json != frozen_snapshot
            or record.expires_at != expiry
        ):
            raise RepositoryConflictError("share grant replay changed immutable semantics")
        return record

    def list_grants(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 200,
    ) -> tuple[ShareGrantMetadata, ...]:
        owner = _required_id(owner_id, "owner_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = transaction.fetch_all(
            f"""
            SELECT {self._METADATA_FIELDS} FROM share_grant
             WHERE user_id = %s
             ORDER BY created_at DESC, id DESC
             LIMIT %s
            """,
            (owner, limit),
        )
        return tuple(_metadata(row) for row in rows)

    def revoke_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        share_id: int,
        revoked_at: datetime,
    ) -> ShareGrantRevocationState:
        owner = _required_id(owner_id, "owner_id")
        identity = _positive_id(share_id, "share_id")
        observed_at = _aware_datetime(revoked_at, "revoked_at")
        result = transaction.execute(
            """
            UPDATE share_grant SET revoked_at = %s
             WHERE id = %s AND user_id = %s AND revoked_at IS NULL
            """,
            (observed_at, identity, owner),
        )
        if result.rowcount == 1:
            return ShareGrantRevocationState.REVOKED
        row = transaction.fetch_one(
            "SELECT revoked_at FROM share_grant WHERE id = %s AND user_id = %s",
            (identity, owner),
        )
        if row is None:
            return ShareGrantRevocationState.MISSING
        return ShareGrantRevocationState.ALREADY_REVOKED

    def resolve_active_by_digest(
        self,
        transaction: Transaction,
        *,
        token_sha256: str,
        as_of: datetime,
    ) -> ShareGrantRecord | None:
        digest = _digest(token_sha256)
        observed_at = _aware_datetime(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            SELECT {self._FIELDS} FROM share_grant
             WHERE token_sha256 = %s AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > %s)
            """,
            (digest, observed_at),
        )
        return None if row is None else _grant(row)

    def record_open(
        self,
        transaction: Transaction,
        *,
        share_id: int,
        token_sha256: str,
        as_of: datetime,
    ) -> ShareGrantRecord | None:
        identity = _positive_id(share_id, "share_id")
        digest = _digest(token_sha256)
        observed_at = _aware_datetime(as_of, "as_of")
        row = transaction.fetch_one(
            f"""
            UPDATE share_grant
               SET open_count = open_count + 1
             WHERE id = %s AND token_sha256 = %s AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > %s)
            RETURNING {self._FIELDS}
            """,
            (identity, digest, observed_at),
        )
        return None if row is None else _grant(row)


def _digest(value: object) -> str:
    digest = _bounded_text(value, "token_sha256", maximum=64)
    if _SHA256.fullmatch(digest) is None:
        raise RepositoryValidationError("token_sha256 must be 64 lowercase hexadecimal characters")
    return digest


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum)


def _snapshot_input(value: object) -> tuple[str, Any]:
    canonical = _canonical_json(value, "snapshot_json")
    try:
        return canonical, _structured_json(canonical, "snapshot_json")
    except RepositoryDataError as exc:
        raise RepositoryValidationError(
            "snapshot_json must be a JSON object or array"
        ) from exc


def _positive_id(value: object, field: str) -> int:
    integer = _non_negative_int(value, field)
    if integer == 0:
        raise RepositoryValidationError(f"{field} must be positive")
    return integer


def _optional_aware_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _aware_datetime(value, field)


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _stored_datetime(value: object, field: str) -> datetime:
    try:
        return _aware_datetime(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted share timestamp is not timezone-aware", metadata={"field": field}
        ) from exc


def _optional_stored_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _stored_datetime(value, field)


def _grant(row: Mapping[str, Any]) -> ShareGrantRecord:
    snapshot = _structured_json(_row_value(row, "snapshot_json"), "snapshot_json")
    return ShareGrantRecord(
        share_id=_positive_stored_id(_row_value(row, "id")),
        token_sha256=_stored_digest(_row_value(row, "token_sha256")),
        owner_id=str(_row_value(row, "user_id")),
        chat_id=str(_row_value(row, "chat_id")),
        scope=str(_row_value(row, "scope")),
        component_id=None if row.get("component_id") is None else str(row["component_id"]),
        snapshot_html=str(_row_value(row, "snapshot_html")),
        snapshot_json=snapshot,
        created_at=_stored_datetime(_row_value(row, "created_at"), "created_at"),
        expires_at=_optional_stored_datetime(row.get("expires_at"), "expires_at"),
        revoked_at=_optional_stored_datetime(row.get("revoked_at"), "revoked_at"),
        open_count=_stored_non_negative_int(_row_value(row, "open_count"), "open_count"),
    )


def _metadata(row: Mapping[str, Any]) -> ShareGrantMetadata:
    return ShareGrantMetadata(
        share_id=_positive_stored_id(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        chat_id=str(_row_value(row, "chat_id")),
        scope=str(_row_value(row, "scope")),
        component_id=None if row.get("component_id") is None else str(row["component_id"]),
        created_at=_stored_datetime(_row_value(row, "created_at"), "created_at"),
        expires_at=_optional_stored_datetime(row.get("expires_at"), "expires_at"),
        revoked_at=_optional_stored_datetime(row.get("revoked_at"), "revoked_at"),
        open_count=_stored_non_negative_int(_row_value(row, "open_count"), "open_count"),
    )


def _stored_digest(value: object) -> str:
    try:
        return _digest(value)
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted share digest is invalid") from exc


def _positive_stored_id(value: object) -> int:
    try:
        return _positive_id(value, "share_id")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted share id is invalid") from exc


def _stored_non_negative_int(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted share counter is invalid", metadata={"field": field}
        ) from exc


__all__ = (
    "ShareGrantMetadata",
    "ShareGrantRecord",
    "ShareGrantRepository",
    "ShareGrantRevocationState",
)
