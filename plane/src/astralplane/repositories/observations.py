"""Opaque owner- and conversation-scoped tool observations with bounded paged recall,
absolute expiry, and tombstoned purge. Authorization, audience, packing, compaction, and
provider policy stay with the host; callers own transactions and encrypt content before
it reaches Plane.
"""

from __future__ import annotations

import hashlib
import json
import math
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

MAX_OBSERVATION_OCTETS: Final = 8388608
MAX_PAGE_OCTETS: Final = 16384
MAX_PAGE_COUNT: Final = 512
MAX_CONVERSATION_OCTETS: Final = 67108864
MAX_RETENTION_SECONDS: Final = 86400
MAX_TIME: Final = 2**53 - 1
MAX_SOURCE_REFS: Final = 8
MAX_SOURCE_REF_CHARACTERS: Final = 512
MAX_IDENTITY_CHARACTERS: Final = 512
MAX_SEAL_CHARACTERS: Final = 512
MAX_OBSERVATION_LIST: Final = 100
PURGE_REASONS: Final = ("deleted", "revoked", "expired")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LOCK_SEED: Final = 97


def _text(value: object, field_name: str, *, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise RepositoryValidationError(f"{field_name} is outside its supported bounds")
    if value != value.strip():
        raise RepositoryValidationError(f"{field_name} must not have edge whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in value
    ):
        raise RepositoryValidationError(f"{field_name} contains invalid characters")
    return value


def _owner(value: object) -> str:
    return _text(value, "owner_id", maximum=MAX_IDENTITY_CHARACTERS)


def _conversation(value: object) -> str:
    return _text(value, "conversation_id", maximum=MAX_IDENTITY_CHARACTERS)


def _operation(value: object) -> str:
    return _text(value, "operation_id", maximum=MAX_IDENTITY_CHARACTERS)


def _seal(value: object) -> str:
    return _text(value, "seal", maximum=MAX_SEAL_CHARACTERS)


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _UUID.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field_name} must be a canonical UUID")
    return value


def _integer(
    value: object, field_name: str, *, minimum: int = 0, maximum: int = MAX_TIME
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError(f"{field_name} is outside its supported bounds")
    return value


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise RepositoryValidationError(f"{field_name} must be boolean")
    return value


def _source_refs(value: object) -> tuple[str, ...]:
    if type(value) not in (tuple, list) or len(value) > MAX_SOURCE_REFS:
        raise RepositoryValidationError("source_refs is outside its supported bounds")
    result = tuple(
        _text(ref, "source ref", maximum=MAX_SOURCE_REF_CHARACTERS) for ref in value
    )
    if len(set(result)) != len(result):
        raise RepositoryValidationError("source_refs must be unique")
    return result


def _content(value: object) -> bytes:
    if type(value) is not bytes or not 1 <= len(value) <= MAX_OBSERVATION_OCTETS:
        raise RepositoryValidationError("content is outside its supported ciphertext bounds")
    return value


def _reason(value: object) -> str:
    if type(value) is not str or value not in PURGE_REASONS:
        raise RepositoryValidationError("purge reason is not supported")
    return value


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class ObservationCaptureCommand:
    owner_id: str = field(repr=False)
    conversation_id: str
    observation_id: str
    operation_id: str
    source_refs: tuple[str, ...]
    seal: str = field(repr=False)
    content: bytes = field(repr=False)
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _conversation(self.conversation_id)
        _identifier(self.observation_id, "observation_id")
        _operation(self.operation_id)
        object.__setattr__(self, "source_refs", _source_refs(self.source_refs))
        _seal(self.seal)
        _content(self.content)
        _integer(self.created_at, "created_at")
        _integer(self.expires_at, "expires_at", minimum=1)
        if not self.created_at < self.expires_at <= self.created_at + MAX_RETENTION_SECONDS:
            raise RepositoryValidationError(
                "observation expiry must be absolute and within its retention ceiling"
            )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def request_digest(self) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "version": 1,
                    "kind": "tool_observation_capture",
                    "owner_id": self.owner_id,
                    "conversation_id": self.conversation_id,
                    "observation_id": self.observation_id,
                    "operation_id": self.operation_id,
                    "source_refs": list(self.source_refs),
                    "seal": self.seal,
                    "content_sha256": self.content_sha256,
                    "created_at": self.created_at,
                    "expires_at": self.expires_at,
                }
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationHead:
    owner_id: str = field(repr=False)
    conversation_id: str
    observation_id: str
    capture_seq: int
    operation_id: str
    source_refs: tuple[str, ...]
    seal: str | None = field(repr=False)
    page_count: int
    observation_octets: int
    content_sha256: str | None
    capture_digest: str
    created_at: int
    expires_at: int
    purged_at: int | None = None
    purged_reason: str | None = None

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _conversation(self.conversation_id)
        _identifier(self.observation_id, "observation_id")
        _integer(self.capture_seq, "capture_seq", minimum=1)
        _operation(self.operation_id)
        object.__setattr__(self, "source_refs", _source_refs(self.source_refs))
        _integer(self.created_at, "created_at")
        _integer(self.expires_at, "expires_at", minimum=1)
        _digest(self.capture_digest, "capture_digest")
        if self.purged_at is None:
            if self.purged_reason is not None:
                raise RepositoryValidationError("live observation must not carry a purge reason")
            _integer(self.page_count, "page_count", minimum=1, maximum=MAX_PAGE_COUNT)
            _integer(
                self.observation_octets,
                "observation_octets",
                minimum=1,
                maximum=MAX_OBSERVATION_OCTETS,
            )
            if self.seal is None or self.content_sha256 is None:
                raise RepositoryValidationError("live observation requires its seal and digest")
            _digest(self.content_sha256, "content_sha256")
            if not self.created_at < self.expires_at <= self.created_at + MAX_RETENTION_SECONDS:
                raise RepositoryValidationError("observation retention window is inconsistent")
        else:
            _integer(self.purged_at, "purged_at", minimum=self.created_at)
            if self.purged_reason not in PURGE_REASONS:
                raise RepositoryValidationError("observation tombstone reason is inconsistent")
            if (self.page_count, self.observation_octets) != (0, 0):
                raise RepositoryValidationError("observation tombstone must not retain extent")
            if self.seal is not None or self.content_sha256 is not None:
                raise RepositoryValidationError("observation tombstone must not retain content")
            if self.source_refs:
                raise RepositoryValidationError("observation tombstone must not retain sources")

    @property
    def purged(self) -> bool:
        return self.purged_at is not None


@dataclass(frozen=True, slots=True)
class ObservationCapture:
    observation: ObservationHead
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.observation) is not ObservationHead:
            raise RepositoryValidationError("observation capture requires a typed head")
        _boolean(self.replayed, "replayed")


@dataclass(frozen=True, slots=True)
class ObservationPage:
    owner_id: str = field(repr=False)
    conversation_id: str
    observation_id: str
    capture_seq: int
    content_sha256: str
    page_no: int
    content: bytes = field(repr=False)
    observation_octets: int
    page_count: int
    continuation: bool

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _conversation(self.conversation_id)
        _identifier(self.observation_id, "observation_id")
        _integer(self.capture_seq, "capture_seq", minimum=1)
        _digest(self.content_sha256, "content_sha256")
        _integer(self.page_no, "page_no", minimum=1, maximum=MAX_PAGE_COUNT + 1)
        _integer(
            self.observation_octets,
            "observation_octets",
            minimum=1,
            maximum=MAX_OBSERVATION_OCTETS,
        )
        _integer(self.page_count, "page_count", minimum=1, maximum=MAX_PAGE_COUNT)
        if type(self.content) is not bytes:
            raise RepositoryValidationError("page content must be bytes")
        if self.page_no == self.page_count + 1:
            if self.content != b"" or self.continuation:
                raise RepositoryValidationError("end-of-source page must be empty and terminal")
        elif self.page_no > self.page_count:
            raise RepositoryValidationError("page number is beyond the observation extent")
        elif not 1 <= len(self.content) <= MAX_PAGE_OCTETS:
            raise RepositoryValidationError("page content is outside its supported bounds")
        _boolean(self.continuation, "continuation")
        if self.continuation != (self.page_no < self.page_count):
            raise RepositoryValidationError("page continuation state is inconsistent")


@dataclass(frozen=True, slots=True)
class ObservationPurge:
    observation: ObservationHead
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.observation) is not ObservationHead:
            raise RepositoryValidationError("observation purge requires a typed head")
        if not self.observation.purged:
            raise RepositoryValidationError("observation purge requires a tombstone")
        _boolean(self.replayed, "replayed")


@dataclass(frozen=True, slots=True)
class ObservationCursor:
    owner_id: str = field(repr=False)
    conversation_id: str
    capture_seq: int

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _conversation(self.conversation_id)
        _integer(self.capture_seq, "capture_seq", minimum=1)


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    records: tuple[ObservationHead, ...]
    next_cursor: ObservationCursor | None

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(
            type(record) is not ObservationHead for record in self.records
        ):
            raise RepositoryValidationError("observation batch requires detached heads")
        if self.next_cursor is not None and type(self.next_cursor) is not ObservationCursor:
            raise RepositoryValidationError("observation batch cursor is invalid")


@dataclass(frozen=True, slots=True)
class ObservationUsage:
    owner_id: str = field(repr=False)
    conversation_id: str
    live_count: int
    retained_octets: int
    expired_count: int
    capacity_octets: int = MAX_CONVERSATION_OCTETS

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _conversation(self.conversation_id)
        _integer(self.live_count, "live_count", maximum=MAX_TIME)
        _integer(self.retained_octets, "retained_octets", maximum=MAX_CONVERSATION_OCTETS)
        _integer(self.expired_count, "expired_count", maximum=MAX_TIME)


def _head(row: Any) -> ObservationHead:
    if row is None:
        return None
    try:
        source_refs = row["source_refs"]
        if isinstance(source_refs, str):
            source_refs = json.loads(source_refs)
        return ObservationHead(
            row["owner_id"],
            row["conversation_id"],
            str(row["observation_id"]),
            row["capture_seq"],
            row["operation_id"],
            tuple(source_refs),
            row["seal"],
            row["page_count"],
            row["observation_octets"],
            row["content_sha256"],
            row["capture_digest"],
            row["created_at"],
            row["expires_at"],
            row["purged_at"],
            row["purged_reason"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("invalid stored tool observation") from exc


def _split_pages(content: bytes) -> tuple[bytes, ...]:
    if len(content) % MAX_PAGE_OCTETS == 0:
        count = len(content) // MAX_PAGE_OCTETS
    else:
        count = math.ceil(len(content) / MAX_PAGE_OCTETS)
    return tuple(
        content[index * MAX_PAGE_OCTETS : (index + 1) * MAX_PAGE_OCTETS]
        for index in range(count)
    )


class ObservationRepository:
    def capture(
        self,
        transaction: Any,
        *,
        command: ObservationCaptureCommand,
    ) -> ObservationCapture:
        if type(command) is not ObservationCaptureCommand:
            raise RepositoryValidationError("typed tool observation capture command required")
        command = ObservationCaptureCommand(
            command.owner_id,
            command.conversation_id,
            command.observation_id,
            command.operation_id,
            command.source_refs,
            command.seal,
            command.content,
            command.created_at,
            command.expires_at,
        )
        self._lock_owner(transaction, command.owner_id)
        stored = _head(
            transaction.fetch_one(
                "SELECT * FROM tool_observation WHERE owner_id=%s AND conversation_id=%s"
                " AND observation_id=%s FOR UPDATE",
                (command.owner_id, command.conversation_id, command.observation_id),
            )
        )
        if stored is not None:
            if stored.capture_digest != command.request_digest:
                raise RepositoryConflictError("tool observation identity was reused")
            return ObservationCapture(stored, True)
        retained = transaction.fetch_one(
            "SELECT COALESCE(SUM(observation_octets),0) AS retained FROM tool_observation"
            " WHERE owner_id=%s AND conversation_id=%s AND purged_at IS NULL",
            (command.owner_id, command.conversation_id),
        )
        if int(retained["retained"]) + len(command.content) > MAX_CONVERSATION_OCTETS:
            raise RepositoryConflictError("tool observation capacity is exhausted")
        capture_seq = int(
            transaction.fetch_one(
                "SELECT COALESCE(MAX(capture_seq),0)+1 AS next FROM tool_observation"
                " WHERE owner_id=%s AND conversation_id=%s",
                (command.owner_id, command.conversation_id),
            )["next"]
        )
        pages = _split_pages(command.content)
        row = transaction.fetch_one(
            "INSERT INTO tool_observation(owner_id,conversation_id,observation_id,capture_seq,"
            "operation_id,seal,source_refs,page_count,observation_octets,content_sha256,"
            "capture_digest,created_at,expires_at,purged_at,purged_reason)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,NULL,NULL) RETURNING *",
            (
                command.owner_id,
                command.conversation_id,
                command.observation_id,
                capture_seq,
                command.operation_id,
                command.seal,
                _canonical(list(command.source_refs)),
                len(pages),
                len(command.content),
                command.content_sha256,
                command.request_digest,
                command.created_at,
                command.expires_at,
            ),
        )
        head = _head(row)
        if head is None:
            raise RepositoryDataError("tool observation insert returned no record")
        for page_no, page in enumerate(pages, start=1):
            transaction.execute(
                "INSERT INTO tool_observation_page(owner_id,conversation_id,observation_id,"
                "page_no,content,content_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    command.owner_id,
                    command.conversation_id,
                    command.observation_id,
                    page_no,
                    page,
                    hashlib.sha256(page).hexdigest(),
                ),
            )
        return ObservationCapture(head, False)

    def get_observation(
        self,
        query: Any,
        *,
        owner_id: str,
        conversation_id: str,
        observation_id: str,
        now: int | None = None,
        include_purged: bool = False,
    ) -> ObservationHead | None:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        observation_id = _identifier(observation_id, "observation_id")
        include_purged = _boolean(include_purged, "include_purged")
        clauses = ["owner_id=%s", "conversation_id=%s", "observation_id=%s"]
        parameters: list[object] = [owner_id, conversation_id, observation_id]
        if not include_purged:
            clauses.append("purged_at IS NULL")
            if now is not None:
                clauses.append("expires_at>%s")
                parameters.append(_integer(now, "now"))
        return _head(
            query.fetch_one(
                "SELECT * FROM tool_observation WHERE " + " AND ".join(clauses),
                tuple(parameters),
            )
        )

    def read_page(
        self,
        query: Any,
        *,
        owner_id: str,
        conversation_id: str,
        observation_id: str,
        page_no: int,
        now: int,
    ) -> ObservationPage | None:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        observation_id = _identifier(observation_id, "observation_id")
        page_no = _integer(page_no, "page_no", minimum=1, maximum=MAX_PAGE_COUNT + 1)
        _integer(now, "now")
        head = _head(
            query.fetch_one(
                "SELECT * FROM tool_observation WHERE owner_id=%s AND conversation_id=%s"
                " AND observation_id=%s AND purged_at IS NULL AND expires_at>%s",
                (owner_id, conversation_id, observation_id, now),
            )
        )
        if head is None:
            return None
        if page_no == head.page_count + 1:
            return ObservationPage(
                owner_id,
                conversation_id,
                observation_id,
                head.capture_seq,
                head.content_sha256,
                page_no,
                b"",
                head.observation_octets,
                head.page_count,
                False,
            )
        if page_no > head.page_count + 1:
            raise RepositoryValidationError("page number is beyond the observation extent")
        row = query.fetch_one(
            "SELECT content,content_sha256 FROM tool_observation_page WHERE owner_id=%s"
            " AND conversation_id=%s AND observation_id=%s AND page_no=%s",
            (owner_id, conversation_id, observation_id, page_no),
        )
        if row is None:
            raise RepositoryDataError("stored tool observation page is missing")
        content = bytes(row["content"])
        if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
            raise RepositoryDataError("stored tool observation page failed its integrity check")
        return ObservationPage(
            owner_id,
            conversation_id,
            observation_id,
            head.capture_seq,
            head.content_sha256,
            page_no,
            content,
            head.observation_octets,
            head.page_count,
            page_no < head.page_count,
        )

    def verify_observation(
        self,
        query: Any,
        *,
        owner_id: str,
        conversation_id: str,
        observation_id: str,
        now: int,
    ) -> ObservationHead | None:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        observation_id = _identifier(observation_id, "observation_id")
        _integer(now, "now")
        head = _head(
            query.fetch_one(
                "SELECT * FROM tool_observation WHERE owner_id=%s AND conversation_id=%s"
                " AND observation_id=%s AND purged_at IS NULL AND expires_at>%s",
                (owner_id, conversation_id, observation_id, now),
            )
        )
        if head is None:
            return None
        rows = query.fetch_all(
            "SELECT page_no,content,content_sha256 FROM tool_observation_page WHERE owner_id=%s"
            " AND conversation_id=%s AND observation_id=%s ORDER BY page_no",
            (owner_id, conversation_id, observation_id),
        )
        if len(rows) != head.page_count:
            raise RepositoryDataError("stored tool observation page count is inconsistent")
        joined = bytearray()
        for expected_page_no, row in enumerate(rows, start=1):
            if int(row["page_no"]) != expected_page_no:
                raise RepositoryDataError("stored tool observation pages are incomplete")
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
                raise RepositoryDataError("stored tool observation page failed its integrity check")
            joined.extend(content)
        if len(joined) != head.observation_octets:
            raise RepositoryDataError("stored tool observation extent is inconsistent")
        if hashlib.sha256(bytes(joined)).hexdigest() != head.content_sha256:
            raise RepositoryDataError("stored tool observation failed its integrity check")
        return head

    def purge_observation(
        self,
        transaction: Any,
        *,
        owner_id: str,
        conversation_id: str,
        observation_id: str,
        reason: str,
        at: int,
    ) -> ObservationPurge:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        observation_id = _identifier(observation_id, "observation_id")
        reason = _reason(reason)
        _integer(at, "at")
        self._lock_owner(transaction, owner_id)
        current = _head(
            transaction.fetch_one(
                "SELECT * FROM tool_observation WHERE owner_id=%s AND conversation_id=%s"
                " AND observation_id=%s FOR UPDATE",
                (owner_id, conversation_id, observation_id),
            )
        )
        if current is None:
            raise RepositoryNotFoundError("tool observation not found")
        if current.purged:
            if current.purged_reason != reason:
                raise RepositoryConflictError("tool observation purge reason changed")
            return ObservationPurge(current, True)
        if at < current.created_at:
            raise RepositoryConflictError("tool observation purge clock regressed")
        row = transaction.fetch_one(
            "UPDATE tool_observation SET page_count=0,observation_octets=0,content_sha256=NULL,"
            "seal=NULL,source_refs='[]'::jsonb,purged_at=%s,purged_reason=%s"
            " WHERE owner_id=%s AND conversation_id=%s AND observation_id=%s"
            " AND purged_at IS NULL RETURNING *",
            (at, reason, owner_id, conversation_id, observation_id),
        )
        purged = _head(row)
        if purged is None:
            raise RepositoryConflictError("tool observation state changed")
        transaction.execute(
            "DELETE FROM tool_observation_page WHERE owner_id=%s AND conversation_id=%s"
            " AND observation_id=%s",
            (owner_id, conversation_id, observation_id),
        )
        return ObservationPurge(purged, False)

    def purge_expired(
        self,
        transaction: Any,
        *,
        owner_id: str,
        conversation_id: str,
        now: int,
        limit: int = 50,
    ) -> tuple[ObservationPurge, ...]:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        _integer(now, "now")
        if type(limit) is not int or not 1 <= limit <= MAX_OBSERVATION_LIST:
            raise RepositoryValidationError("tool observation purge limit is outside its bounds")
        expired = transaction.fetch_all(
            "SELECT observation_id FROM tool_observation WHERE owner_id=%s"
            " AND conversation_id=%s AND purged_at IS NULL AND expires_at<=%s"
            " ORDER BY capture_seq LIMIT %s",
            (owner_id, conversation_id, now, limit),
        )
        return tuple(
            self.purge_observation(
                transaction,
                owner_id=owner_id,
                conversation_id=conversation_id,
                observation_id=str(row["observation_id"]),
                reason="expired",
                at=now,
            )
            for row in expired
        )

    def list_observations(
        self,
        query: Any,
        *,
        owner_id: str,
        conversation_id: str,
        now: int,
        cursor: ObservationCursor | None = None,
        limit: int = 50,
    ) -> ObservationBatch:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        _integer(now, "now")
        if type(limit) is not int or not 1 <= limit <= MAX_OBSERVATION_LIST:
            raise RepositoryValidationError(
                "tool observation limit is outside its supported bounds"
            )
        clauses = ["owner_id=%s", "conversation_id=%s", "purged_at IS NULL", "expires_at>%s"]
        parameters: list[object] = [owner_id, conversation_id, now]
        if cursor is not None:
            if type(cursor) is not ObservationCursor or (
                cursor.owner_id,
                cursor.conversation_id,
            ) != (owner_id, conversation_id):
                raise RepositoryValidationError("tool observation cursor is invalid for this scope")
            _integer(cursor.capture_seq, "cursor capture_seq", minimum=1)
            clauses.append("capture_seq>%s")
            parameters.append(cursor.capture_seq)
        parameters.append(limit + 1)
        rows = query.fetch_all(
            "SELECT * FROM tool_observation WHERE "
            + " AND ".join(clauses)
            + " ORDER BY capture_seq LIMIT %s",
            tuple(parameters),
        )
        records = tuple(_head(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            next_cursor = ObservationCursor(owner_id, conversation_id, last.capture_seq)
        return ObservationBatch(records, next_cursor)

    def conversation_usage(
        self,
        query: Any,
        *,
        owner_id: str,
        conversation_id: str,
        now: int,
    ) -> ObservationUsage:
        owner_id = _owner(owner_id)
        conversation_id = _conversation(conversation_id)
        _integer(now, "now")
        row = query.fetch_one(
            "SELECT COUNT(*) FILTER (WHERE purged_at IS NULL) AS live_count,"
            " COALESCE(SUM(observation_octets) FILTER (WHERE purged_at IS NULL),0)"
            " AS retained_octets,"
            " COUNT(*) FILTER (WHERE purged_at IS NULL AND expires_at<=%s) AS expired_count"
            " FROM tool_observation WHERE owner_id=%s AND conversation_id=%s",
            (now, owner_id, conversation_id),
        )
        return ObservationUsage(
            owner_id,
            conversation_id,
            int(row["live_count"]),
            int(row["retained_octets"]),
            int(row["expired_count"]),
        )

    @staticmethod
    def _lock_owner(transaction: Any, owner_id: str) -> None:
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,97)) AS acquired",
            (owner_id,),
        )


__all__ = (
    "MAX_CONVERSATION_OCTETS",
    "MAX_OBSERVATION_LIST",
    "MAX_OBSERVATION_OCTETS",
    "MAX_PAGE_COUNT",
    "MAX_PAGE_OCTETS",
    "MAX_RETENTION_SECONDS",
    "MAX_SEAL_CHARACTERS",
    "MAX_SOURCE_REFS",
    "MAX_SOURCE_REF_CHARACTERS",
    "MAX_TIME",
    "PURGE_REASONS",
    "ObservationBatch",
    "ObservationCapture",
    "ObservationCaptureCommand",
    "ObservationCursor",
    "ObservationHead",
    "ObservationPage",
    "ObservationPurge",
    "ObservationRepository",
    "ObservationUsage",
)
