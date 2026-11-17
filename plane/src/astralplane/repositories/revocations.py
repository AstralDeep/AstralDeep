"""Owner-attributed queue of encrypted refresh tokens pending revocation, with a
cycle-bounded administrative drain page. Used by AstralDeep's native-logout flow and
repositories/history.py's session rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
    _single_returned,
)
from astralplane.repositories._issuing_identity import _issuing_pair


@dataclass(frozen=True, slots=True)
class RevocationQueueRecord:
    queue_id: int
    owner_id: str
    refresh_token_ciphertext: str = field(repr=False)
    client_id: str | None = None
    enqueued_at: int = 0
    attempts: int = 0
    issuing_issuer: str | None = None


def _page_integer(value: object, field: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError(f"{field} must be an integer in the supported range")
    return value


@dataclass(frozen=True, slots=True)
class RevocationQueueCursor:
    enqueued_at: int
    queue_id: int

    def __post_init__(self) -> None:
        _page_integer(self.enqueued_at, "cursor enqueue time")
        _page_integer(self.queue_id, "cursor queue id", minimum=1)


@dataclass(frozen=True, slots=True)
class RevocationQueuePage:
    records: tuple[RevocationQueueRecord, ...]
    next_cursor: RevocationQueueCursor | None
    ceiling: int | None


def _record(row: Any) -> RevocationQueueRecord:
    try:
        issuer = row["issuing_issuer"]
        if issuer is not None:
            _issuing_pair(issuer, row.get("client_id"))
    except (KeyError, RepositoryValidationError) as error:
        raise RepositoryDataError("stored revocation issuing identity is invalid") from error
    return RevocationQueueRecord(
        queue_id=int(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        refresh_token_ciphertext=str(_row_value(row, "refresh_token_enc")),
        client_id=None if row.get("client_id") is None else str(row["client_id"]),
        enqueued_at=int(_row_value(row, "enqueued_at")),
        attempts=int(row.get("attempts") or 0),
        issuing_issuer=issuer,
    )


def _queue_id(value: object) -> int:
    queue_id = _non_negative_int(value, "queue id")
    if queue_id == 0:
        raise RepositoryNotFoundError("queue id must identify a persisted record")
    return queue_id


class RevocationQueueRepository:
    _FIELDS = "id, user_id, refresh_token_enc, enqueued_at, attempts, client_id, issuing_issuer"

    def enqueue(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        refresh_token_ciphertext: str,
        enqueued_at: int,
        client_id: str | None = None,
        issuing_issuer: str | None = None,
    ) -> RevocationQueueRecord:
        owner = _required_id(owner_id, "owner id")
        ciphertext = _bounded_text(
            refresh_token_ciphertext,
            "refresh token ciphertext",
            maximum=131_072,
        )
        timestamp = _non_negative_int(enqueued_at, "enqueued at")
        if issuing_issuer is not None:
            _, client = _issuing_pair(issuing_issuer, client_id)
        else:
            client = None if client_id is None else _required_id(client_id, "client id")
        result = transaction.execute(
            f"""
            INSERT INTO auth_revocation_queue (
                user_id, refresh_token_enc, enqueued_at, attempts, client_id, issuing_issuer
            ) VALUES (%s, %s, %s, 0, %s, %s)
            RETURNING {self._FIELDS}
            """,
            (owner, ciphertext, timestamp, client, issuing_issuer),
        )
        return _record(_single_returned(result, "enqueue token revocation"))

    def pending_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 20,
    ) -> tuple[RevocationQueueRecord, ...]:
        owner = _required_id(owner_id, "owner id")
        bounded_limit = _bounded_limit(limit, maximum=200)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM auth_revocation_queue
            WHERE user_id = %s
            ORDER BY enqueued_at, id
            LIMIT %s
            """,
            (owner, bounded_limit),
        )
        return tuple(_record(row) for row in rows)

    def pending_for_administration(
        self,
        query: QueryExecutor,
        *,
        limit: int = 20,
    ) -> tuple[RevocationQueueRecord, ...]:
        bounded_limit = _bounded_limit(limit, maximum=200)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM auth_revocation_queue
            ORDER BY enqueued_at, id
            LIMIT %s
            """,
            (bounded_limit,),
        )
        return tuple(_record(row) for row in rows)

    def page_for_administration(
        self,
        query: QueryExecutor,
        *,
        limit: int = 20,
        after: RevocationQueueCursor | None = None,
        ceiling: int | None = None,
    ) -> RevocationQueuePage:
        size = _page_integer(limit, "limit", minimum=1, maximum=200)
        if ceiling is not None:
            _page_integer(ceiling, "cycle ceiling", minimum=1)
        if after is not None:
            if type(after) is not RevocationQueueCursor:
                raise RepositoryValidationError("after must be a revocation queue cursor")
            after.__post_init__()
            if ceiling is None or after.queue_id > ceiling:
                raise RepositoryValidationError("cursor requires its enclosing cycle ceiling")
        rows = query.fetch_all(
            f"""
            WITH cycle AS (
                SELECT COALESCE(%s::bigint, (
                    SELECT id FROM auth_revocation_queue ORDER BY id DESC LIMIT 1
                )) AS ceiling
            )
            SELECT page.*, cycle.ceiling AS cycle_ceiling
            FROM cycle
            LEFT JOIN LATERAL (
                SELECT {self._FIELDS}
                FROM auth_revocation_queue
                WHERE id <= cycle.ceiling
                  AND (%s::bigint IS NULL OR (enqueued_at, id) > (%s, %s))
                ORDER BY enqueued_at, id
                LIMIT %s
            ) AS page ON TRUE
            ORDER BY page.enqueued_at, page.id
            """
            if not dialect_of(query).is_sqlite
            else f"""
            WITH cycle AS (
                SELECT COALESCE(%s, (
                    SELECT id FROM auth_revocation_queue ORDER BY id DESC LIMIT 1
                )) AS ceiling
            ), page AS (
                SELECT {self._FIELDS} FROM auth_revocation_queue CROSS JOIN cycle
                WHERE id<=cycle.ceiling
                  AND (%s IS NULL OR (enqueued_at,id)>(%s,%s))
                ORDER BY enqueued_at,id LIMIT %s
            )
            SELECT page.*,cycle.ceiling AS cycle_ceiling FROM cycle
            LEFT JOIN page ON true ORDER BY page.enqueued_at,page.id
            """,
            (
                ceiling,
                after.enqueued_at if after else None,
                after.enqueued_at if after else None,
                after.queue_id if after else None,
                size + 1,
            ),
        )
        records = []
        previous = (after.enqueued_at, after.queue_id) if after else None
        try:
            if not rows or len(rows) > size + 1:
                raise RepositoryValidationError("invalid page cardinality")
            captured = rows[0]["cycle_ceiling"]
            if captured is not None:
                _page_integer(captured, "stored cycle ceiling", minimum=1)
            if ceiling is not None and captured != ceiling:
                raise RepositoryValidationError("stored cycle ceiling changed")
            for row in rows:
                if row["cycle_ceiling"] != captured:
                    raise RepositoryValidationError("stored page has inconsistent ceilings")
                if row["id"] is None:
                    if len(rows) != 1:
                        raise RepositoryValidationError("empty page marker contains records")
                    break
                key = (
                    _page_integer(row["enqueued_at"], "stored enqueue time"),
                    _page_integer(row["id"], "stored queue id", minimum=1),
                )
                if captured is None or key[1] > captured or (previous and key <= previous):
                    raise RepositoryValidationError("stored page is outside its ordered cycle")
                records.append(_record(row))
                previous = key
        except (KeyError, TypeError, RepositoryValidationError) as error:
            raise RepositoryDataError("stored revocation page is invalid") from error
        selected = tuple(records[:size])
        next_cursor = (
            RevocationQueueCursor(selected[-1].enqueued_at, selected[-1].queue_id)
            if len(records) > size
            else None
        )
        return RevocationQueuePage(selected, next_cursor, captured)

    def resolve(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        queue_id: int,
    ) -> bool:
        owner = _required_id(owner_id, "owner id")
        identifier = _queue_id(queue_id)
        result = transaction.execute(
            "DELETE FROM auth_revocation_queue WHERE id = %s AND user_id = %s",
            (identifier, owner),
        )
        return result.rowcount == 1

    def bump_attempt(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        queue_id: int,
        expected_attempts: int,
    ) -> RevocationQueueRecord:
        owner = _required_id(owner_id, "owner id")
        identifier = _queue_id(queue_id)
        expected = _non_negative_int(expected_attempts, "expected attempts")
        result = transaction.execute(
            f"""
            UPDATE auth_revocation_queue
            SET attempts = attempts + 1
            WHERE id = %s AND user_id = %s AND attempts = %s
            RETURNING {self._FIELDS}
            """,
            (identifier, owner, expected),
        )
        rows = result.returned_records
        if len(rows) != 1:
            raise RepositoryNotFoundError(
                "owner-scoped revocation record or attempt fence was not found",
                metadata={"operation": "bump token revocation attempt"},
            )
        return _record(rows[0])


__all__ = (
    "RevocationQueueCursor",
    "RevocationQueuePage",
    "RevocationQueueRecord",
    "RevocationQueueRepository",
)
