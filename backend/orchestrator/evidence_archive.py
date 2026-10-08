"""Retains bounded permitted observations in memory under exact host retention grants.
The evidence host adapter owns source authorization, privacy screening, and audit;
this module enforces immutable bindings, absolute expiry, integrity, and exact pages.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock
import time
from typing import Callable, Sequence

MAX_OBSERVATION_BYTES = 8 * 1024 * 1024
MAX_CONVERSATION_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 16 * 1024
MAX_GLOBAL_BYTES = 128 * 1024 * 1024
MAX_RECORDS = 2048
MAX_POLICY_BYTES = 1024 * 1024
MAX_GRANTS = 256
MAX_SOURCE_ARGUMENT_BYTES = 65536
MAX_LIFETIME = timedelta(hours=24)
_BINDING_FIELDS = ("owner_id", "conversation_id", "audience_id", "source_agent", "source_tool")
_GRANT_FIELDS = frozenset((*_BINDING_FIELDS, "expires_at", "source_deadline", "conversation_deadline"))
_GRANT_REQUIRED_FIELDS = frozenset((*_BINDING_FIELDS, "expires_at"))
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_SECRET_FIELDS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "authorization", "credentials",
    "password", "secret", "privatekey", "delegationtoken",
})
_OUTCOMES = frozenset({
    "success", "succeeded", "error", "denial", "denied", "failure", "failed",
    "incomplete", "cancelled", "interrupted",
})


class EvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EvidenceDenied(EvidenceError):
    def __init__(self):
        super().__init__("evidence_unavailable_or_not_authorized")


class EvidenceUnavailable(EvidenceError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"evidence_{reason}")


class EvidencePolicyError(EvidenceError):
    def __init__(self, code: str = "retention_policy_unavailable"):
        super().__init__(code)


class EvidenceCaptureError(EvidenceError):
    def __init__(self, code: str = "evidence_capture_refused"):
        super().__init__(code)


class EvidenceLimit(EvidenceCaptureError):
    def __init__(self, code: str = "evidence_retention_limit"):
        super().__init__(code)


class EvidencePageError(EvidenceError):
    def __init__(self):
        super().__init__("evidence_page_invalid")


def _identifier(value: object) -> str:
    if (type(value) is not str or not value.strip() or len(value) > 512
            or any(ord(character) < 32 for character in value)):
        raise EvidencePolicyError("retention_policy_invalid")
    try:
        if len(value.encode("utf-8")) > 512:
            raise EvidencePolicyError("retention_policy_invalid")
    except UnicodeError:
        raise EvidencePolicyError("retention_policy_invalid") from None
    return value


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidencePolicyError("retention_policy_invalid")
    try:
        if value.utcoffset() is None:
            raise EvidencePolicyError("retention_policy_invalid")
        return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise EvidencePolicyError("retention_policy_invalid") from None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RetentionGrant:
    owner_id: str
    conversation_id: str
    audience_id: str
    source_agent: str
    source_tool: str
    expires_at: datetime
    source_deadline: datetime | None = None
    conversation_deadline: datetime | None = None

    def __post_init__(self) -> None:
        for name in _BINDING_FIELDS:
            _identifier(getattr(self, name))
        for name in ("expires_at", "source_deadline", "conversation_deadline"):
            value = getattr(self, name)
            if value is not None or name == "expires_at":
                object.__setattr__(self, name, _aware(value))

    @property
    def fingerprint(self) -> str:
        return _digest({
            **{name: getattr(self, name) for name in _BINDING_FIELDS},
            **{name: value.isoformat() if value is not None else None
               for name in ("expires_at", "source_deadline", "conversation_deadline")
               for value in (getattr(self, name),)},
        })

    @property
    def deadline(self) -> datetime:
        return min(value for value in (
            self.expires_at, self.source_deadline, self.conversation_deadline,
        ) if value is not None)


def _policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidencePolicyError("retention_policy_invalid")
        result[key] = value
    return result


def _policy_timestamp(value: object) -> datetime:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise EvidencePolicyError("retention_policy_invalid")
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        raise EvidencePolicyError("retention_policy_invalid") from None


def load_grants(path: str | Path) -> list[RetentionGrant]:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise EvidencePolicyError("retention_policy_invalid")
            content = source.read(MAX_POLICY_BYTES + 1)
        if len(content) > MAX_POLICY_BYTES:
            raise EvidencePolicyError("retention_policy_invalid")
        document = json.loads(content.decode("utf-8"), object_pairs_hook=_policy_object)
        if type(document) is not list or len(document) > MAX_GRANTS:
            raise EvidencePolicyError("retention_policy_invalid")
        grants = []
        for item in document:
            if (type(item) is not dict or not _GRANT_REQUIRED_FIELDS <= item.keys()
                    or not item.keys() <= _GRANT_FIELDS):
                raise EvidencePolicyError("retention_policy_invalid")
            values = dict(item)
            for name in ("expires_at", "source_deadline", "conversation_deadline"):
                if name in values:
                    values[name] = _policy_timestamp(values[name])
            grants.append(RetentionGrant(**values))
        return grants
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise EvidencePolicyError() from None


def match_grant(
    grants: Sequence[RetentionGrant], *, owner_id: str, conversation_id: str,
    audience_id: str, source_agent: str, source_tool: str, now: datetime,
) -> RetentionGrant:
    moment = _aware(now)
    bindings = (owner_id, conversation_id, audience_id, source_agent, source_tool)
    for value in bindings:
        _identifier(value)
    if type(grants) not in (list, tuple) or len(grants) > MAX_GRANTS:
        raise EvidencePolicyError()
    matches = []
    for grant in grants:
        if type(grant) is not RetentionGrant:
            raise EvidencePolicyError()
        if tuple(getattr(grant, name) for name in _BINDING_FIELDS) == bindings:
            matches.append(grant)
    if len(matches) != 1 or matches[0].deadline <= moment:
        raise EvidencePolicyError()
    return matches[0]


def _arguments(source_args: object) -> str:
    visited = 0

    def validate(value: object, depth: int = 0) -> None:
        nonlocal visited
        visited += 1
        if depth > 16 or visited > 4096:
            raise EvidenceCaptureError()
        if type(value) is dict:
            for key, child in value.items():
                if (type(key) is not str or key.startswith("_")
                        or re.sub(r"[-_.]", "", key).lower() in _SECRET_FIELDS):
                    raise EvidenceCaptureError()
                validate(key, depth + 1)
                validate(child, depth + 1)
        elif type(value) is list:
            for child in value:
                validate(child, depth + 1)
        elif type(value) is str:
            if len(value) > MAX_SOURCE_ARGUMENT_BYTES:
                raise EvidenceCaptureError()
        elif value is not None and type(value) not in (bool, int, float):
            raise EvidenceCaptureError()

    if type(source_args) is not dict:
        raise EvidenceCaptureError()
    validate(source_args)
    try:
        serialized = _canonical(source_args)
        if len(serialized.encode("utf-8")) > MAX_SOURCE_ARGUMENT_BYTES:
            raise EvidenceCaptureError()
    except (TypeError, ValueError, UnicodeError):
        raise EvidenceCaptureError() from None
    return serialized


@dataclass(frozen=True, slots=True)
class Observation:
    reference: str
    owner_id: str
    conversation_id: str
    audience_id: str
    source_agent: str
    source_tool: str
    operation_id: str
    order: int
    outcome: str
    digest: str
    size_bytes: int
    grant_fingerprint: str
    captured_at: datetime
    expires_at: datetime
    integrity_identity: str
    _source_args_json: str = field(repr=False)

    @property
    def source_args(self) -> dict[str, object]:
        return json.loads(self._source_args_json)


def _observation_identity(observation: Observation) -> str:
    return _digest({
        "reference": observation.reference,
        **{name: getattr(observation, name) for name in _BINDING_FIELDS},
        "operation_id": observation.operation_id, "order": observation.order,
        "outcome": observation.outcome, "digest": observation.digest,
        "size_bytes": observation.size_bytes, "grant_fingerprint": observation.grant_fingerprint,
        "captured_at": observation.captured_at.isoformat(),
        "expires_at": observation.expires_at.isoformat(),
        "source_args_digest": hashlib.sha256(observation._source_args_json.encode("utf-8")).hexdigest(),
    })


@dataclass(frozen=True, slots=True)
class RecallPage:
    reference: str
    digest: str
    start: int
    end: int
    total: int
    text: str = field(repr=False)
    next_offset: int | None
    at_end: bool
    partial: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference, "digest": self.digest,
            "start": self.start, "end": self.end, "total": self.total,
            "text": self.text, "next_offset": self.next_offset,
            "at_end": self.at_end, "partial": self.partial,
        }


@dataclass(slots=True)
class _StoredObservation:
    metadata: Observation
    content: bytes = field(repr=False)
    expires_monotonic: float
    bindings: tuple[str, str, str]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _Tombstone:
    owner_id: str
    conversation_id: str
    audience_id: str
    reason: str


def _limit(value: object, maximum: int, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_evidence_limit")
    return value


class EvidenceArchive:
    def __init__(
        self, *, clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        observation_limit_bytes: int = MAX_OBSERVATION_BYTES,
        conversation_limit_bytes: int = MAX_CONVERSATION_BYTES,
        page_limit_bytes: int = MAX_PAGE_BYTES,
        global_limit_bytes: int = MAX_GLOBAL_BYTES, max_records: int = MAX_RECORDS,
    ):
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._monotonic = monotonic_clock if monotonic_clock is not None else time.monotonic
        if not callable(self._clock) or not callable(self._monotonic):
            raise ValueError("invalid_evidence_clock")
        self.observation_limit_bytes = _limit(observation_limit_bytes, MAX_OBSERVATION_BYTES)
        self.conversation_limit_bytes = _limit(conversation_limit_bytes, MAX_CONVERSATION_BYTES)
        self.page_limit_bytes = _limit(page_limit_bytes, MAX_PAGE_BYTES, minimum=4)
        self.global_limit_bytes = _limit(global_limit_bytes, MAX_GLOBAL_BYTES)
        self.max_records = _limit(max_records, MAX_RECORDS)
        self._lock = RLock()
        self._records: dict[str, _StoredObservation] = {}
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self._conversation_bytes: dict[tuple[str, str], int] = {}
        self._retained_bytes = 0
        self._order = 0

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    @property
    def observation_count(self) -> int:
        with self._lock:
            return len(self._records)

    def _times(self) -> tuple[datetime, float]:
        try:
            now, monotonic = _aware(self._clock()), self._monotonic()
            if type(monotonic) not in (int, float) or not math.isfinite(monotonic) or monotonic < 0:
                raise ValueError()
            return now, monotonic
        except Exception:
            raise EvidenceUnavailable("clock_unavailable") from None

    def capture(
        self, text: str, *, grant: RetentionGrant, operation_id: str,
        source_args: dict[str, object], outcome: str,
    ) -> Observation:
        if type(grant) is not RetentionGrant:
            raise EvidencePolicyError()
        if type(text) is not str:
            raise EvidenceCaptureError()
        if len(text) > self.observation_limit_bytes:
            raise EvidenceLimit()
        try:
            content = text.encode("utf-8")
            _identifier(operation_id)
        except (UnicodeError, EvidencePolicyError):
            raise EvidenceCaptureError() from None
        if type(outcome) is not str or outcome not in _OUTCOMES:
            raise EvidenceCaptureError()
        arguments = _arguments(source_args)
        size = len(content)
        if size > self.observation_limit_bytes:
            raise EvidenceLimit()
        with self._lock:
            now, monotonic = self._times()
            if grant.deadline <= now:
                raise EvidencePolicyError()
            lifetime = min((grant.deadline - now).total_seconds(), MAX_LIFETIME.total_seconds())
            expiry = now + timedelta(seconds=lifetime)
            expires_monotonic = monotonic + lifetime
            if not math.isfinite(expires_monotonic) or expires_monotonic <= monotonic:
                raise EvidenceUnavailable("clock_unavailable")
            conversation = (grant.owner_id, grant.conversation_id)
            retained = self._conversation_bytes.get(conversation, 0)
            if (len(self._records) >= self.max_records
                    or retained + size > self.conversation_limit_bytes
                    or self._retained_bytes + size > self.global_limit_bytes):
                raise EvidenceLimit()
            reference = f"obs_{secrets.token_urlsafe(32)}"
            if reference in self._records or reference in self._tombstones:
                raise EvidenceCaptureError()
            self._order += 1
            observation = Observation(
                reference=reference,
                **{name: getattr(grant, name) for name in _BINDING_FIELDS},
                operation_id=operation_id, order=self._order, outcome=outcome,
                digest=hashlib.sha256(content).hexdigest(), size_bytes=size,
                grant_fingerprint=grant.fingerprint, captured_at=now, expires_at=expiry,
                integrity_identity="", _source_args_json=arguments,
            )
            object.__setattr__(observation, "integrity_identity", _observation_identity(observation))
            self._records[reference] = _StoredObservation(
                observation, content, expires_monotonic,
                (grant.owner_id, grant.conversation_id, grant.audience_id), size,
            )
            self._retained_bytes += size
            self._conversation_bytes[conversation] = retained + size
            return observation

    def _remove(self, reference: str, reason: str) -> Observation:
        stored = self._records.pop(reference)
        observation = stored.metadata
        conversation = stored.bindings[:2]
        self._retained_bytes -= stored.size_bytes
        remaining = self._conversation_bytes.get(conversation, 0) - stored.size_bytes
        if remaining:
            self._conversation_bytes[conversation] = remaining
        else:
            self._conversation_bytes.pop(conversation, None)
        self._tombstones[reference] = _Tombstone(
            *stored.bindings, reason,
        )
        while len(self._tombstones) > self.max_records:
            self._tombstones.popitem(last=False)
        stored.content = b""
        return observation

    def _resolve(
        self, reference: str, owner_id: str, conversation_id: str, audience_id: str,
    ) -> _StoredObservation:
        if type(reference) is not str or not reference or len(reference) > 512:
            raise EvidenceDenied()
        stored = self._records.get(reference)
        metadata = stored.metadata if stored is not None else self._tombstones.get(reference)
        bindings = stored.bindings if stored is not None else (
            (metadata.owner_id, metadata.conversation_id, metadata.audience_id)
            if metadata is not None else None
        )
        if bindings != (owner_id, conversation_id, audience_id):
            raise EvidenceDenied()
        if stored is None:
            raise EvidenceUnavailable(metadata.reason)
        try:
            intact = hmac.compare_digest(_observation_identity(metadata), metadata.integrity_identity)
        except (TypeError, ValueError, AttributeError):
            intact = False
        if not intact:
            self._remove(reference, "integrity_failed")
            raise EvidenceUnavailable("integrity_failed")
        now, monotonic = self._times()
        if stored.metadata.expires_at <= now or stored.expires_monotonic <= monotonic:
            self._remove(reference, "expired")
            raise EvidenceUnavailable("expired")
        return stored

    def inspect(
        self, reference: str, *, owner_id: str, conversation_id: str, audience_id: str,
    ) -> Observation:
        with self._lock:
            return self._resolve(reference, owner_id, conversation_id, audience_id).metadata

    def read(
        self, reference: str, offset: int = 0, *, grant: RetentionGrant,
        owner_id: str, conversation_id: str, audience_id: str,
    ) -> RecallPage:
        with self._lock:
            stored = self._resolve(reference, owner_id, conversation_id, audience_id)
            observation, content = stored.metadata, stored.content
            if type(grant) is not RetentionGrant or not hmac.compare_digest(
                observation.grant_fingerprint, grant.fingerprint,
            ):
                raise EvidenceDenied()
            try:
                intact = (len(content) == observation.size_bytes
                          and hmac.compare_digest(hashlib.sha256(content).hexdigest(), observation.digest)
                          and hmac.compare_digest(_observation_identity(observation), observation.integrity_identity))
            except (TypeError, ValueError, AttributeError):
                intact = False
            if not intact:
                self._remove(reference, "integrity_failed")
                raise EvidenceUnavailable("integrity_failed")
            total = len(content)
            if (type(offset) is not int or not 0 <= offset <= total
                    or (offset < total and content[offset] & 0xC0 == 0x80)):
                raise EvidencePageError()
            end = min(offset + self.page_limit_bytes, total)
            while end < total and content[end] & 0xC0 == 0x80:
                end -= 1
            text = content[offset:end].decode("utf-8")
            return RecallPage(
                reference, observation.digest, offset, end, total, text,
                end if end < total else None, end == total, offset != 0 or end != total,
            )

    def delete(
        self, reference: str, *, owner_id: str, conversation_id: str, audience_id: str,
    ) -> Observation:
        with self._lock:
            self._resolve(reference, owner_id, conversation_id, audience_id)
            return self._remove(reference, "deleted")

    def revoke(
        self, *, owner_id: str, conversation_id: str | None = None,
        grant_fingerprint: str | None = None,
    ) -> tuple[Observation, ...]:
        with self._lock:
            references = [
                reference for reference, stored in self._records.items()
                if stored.metadata.owner_id == owner_id
                and (conversation_id is None or stored.metadata.conversation_id == conversation_id)
                and (grant_fingerprint is None or stored.metadata.grant_fingerprint == grant_fingerprint)
            ]
            return tuple(self._remove(reference, "revoked") for reference in references)

    def cleanup(self) -> tuple[Observation, ...]:
        now, monotonic = self._times()
        with self._lock:
            expired = [
                reference for reference, stored in self._records.items()
                if stored.metadata.expires_at <= now or stored.expires_monotonic <= monotonic
            ]
            return tuple(self._remove(reference, "expired") for reference in expired)
