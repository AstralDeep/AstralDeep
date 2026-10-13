"""Retains bounded generated answer views in memory with exact owner and source bindings.
The evidence host owns fresh authorization, privacy screening, and transient literal delivery;
this store enforces immutable metadata, integrity, fixed expiry, and capacity.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
import re
import secrets
from threading import RLock
import time
from typing import Callable, Sequence

MAX_VIEW_BYTES = 64 * 1024
MAX_CONVERSATION_BYTES = 1024 * 1024
MAX_CONVERSATION_RECORDS = 64
MAX_GLOBAL_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 256
MAX_DEPENDENCIES = 256
MAX_LIFETIME = timedelta(minutes=10)
_SOURCE_REFERENCE = re.compile(r"obs_[A-Za-z0-9_-]{43}")
_VIEW_REFERENCE = re.compile(r"view_[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


class ViewError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ViewDenied(ViewError):
    def __init__(self):
        super().__init__("context_view_unavailable_or_not_authorized")


class ViewUnavailable(ViewError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"context_view_{reason}")


class ViewCaptureError(ViewError):
    def __init__(self):
        super().__init__("context_view_capture_refused")


class ViewLimit(ViewCaptureError):
    def __init__(self):
        ViewError.__init__(self, "context_view_retention_limit")


def _identifier(value: object) -> str:
    try:
        if (type(value) is not str or not value.strip() or len(value) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or len(value.encode("utf-8")) > 512):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ViewCaptureError() from None


def _aware(value: object) -> datetime:
    try:
        if type(value) is not datetime or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(UTC)
    except Exception:
        raise ViewCaptureError() from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ViewCaptureError()
    return value


def _bindings(owner_id: object, conversation_id: object, audience_id: object) -> tuple[str, str, str]:
    return tuple(_identifier(value) for value in (owner_id, conversation_id, audience_id))


@dataclass(frozen=True, slots=True)
class SourceDependency:
    reference: str
    integrity_identity: str
    grant_fingerprint: str
    expires_at: datetime

    def __post_init__(self):
        if type(self.reference) is not str or _SOURCE_REFERENCE.fullmatch(self.reference) is None:
            raise ViewCaptureError()
        _digest(self.integrity_identity)
        _digest(self.grant_fingerprint)
        object.__setattr__(self, "expires_at", _aware(self.expires_at))


def _dependencies(values: object) -> tuple[SourceDependency, ...]:
    if type(values) not in (tuple, list) or len(values) > MAX_DEPENDENCIES:
        raise ViewCaptureError()
    result, references = [], set()
    for value in values:
        if type(value) is not SourceDependency:
            raise ViewCaptureError()
        try:
            source = SourceDependency(value.reference, value.integrity_identity, value.grant_fingerprint, value.expires_at)
        except AttributeError:
            raise ViewCaptureError() from None
        if source.reference in references:
            raise ViewCaptureError()
        result.append(source)
        references.add(source.reference)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ContextView:
    reference: str
    owner_id: str = field(repr=False)
    conversation_id: str = field(repr=False)
    audience_id: str = field(repr=False)
    digest: str
    size_bytes: int
    captured_at: datetime
    expires_at: datetime
    dependencies: tuple[SourceDependency, ...] = field(repr=False)
    integrity_identity: str


def _identity(view: ContextView) -> str:
    if type(view) is not ContextView or _VIEW_REFERENCE.fullmatch(view.reference) is None:
        raise ViewCaptureError()
    bindings = _bindings(view.owner_id, view.conversation_id, view.audience_id)
    dependencies = _dependencies(view.dependencies)
    captured, expires = _aware(view.captured_at), _aware(view.expires_at)
    if (type(view.size_bytes) is not int or not 0 <= view.size_bytes <= MAX_VIEW_BYTES
            or expires <= captured
            or expires != min((captured + MAX_LIFETIME, *(value.expires_at for value in dependencies)))):
        raise ViewCaptureError()
    value = {"reference": view.reference, "bindings": bindings, "digest": _digest(view.digest),
        "size_bytes": view.size_bytes, "captured_at": captured.isoformat(), "expires_at": expires.isoformat(),
        "dependencies": [{"reference": source.reference, "integrity_identity": source.integrity_identity,
                          "grant_fingerprint": source.grant_fingerprint, "expires_at": source.expires_at.isoformat()}
                         for source in dependencies]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextViewContent:
    reference: str
    digest: str
    text: str = field(repr=False)
    size_bytes: int
    expires_at: datetime
    dependencies: tuple[SourceDependency, ...] = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {"reference": self.reference, "digest": self.digest, "text": self.text,
                "size_bytes": self.size_bytes, "expires_at": self.expires_at.isoformat()}


@dataclass(slots=True)
class _StoredView:
    metadata: ContextView
    content: bytes = field(repr=False)
    bindings: tuple[str, str, str]
    size_bytes: int
    expires_at: datetime
    expires_monotonic: float
    source_references: tuple[str, ...]
    identity: str


@dataclass(frozen=True, slots=True)
class _Tombstone:
    bindings: tuple[str, str, str]
    reason: str


def _limit(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid_context_view_limit")
    return value


class ContextViewStore:
    def __init__(
        self, *, clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        view_limit_bytes: int = MAX_VIEW_BYTES,
        conversation_limit_bytes: int = MAX_CONVERSATION_BYTES,
        conversation_max_records: int = MAX_CONVERSATION_RECORDS,
        global_limit_bytes: int = MAX_GLOBAL_BYTES, max_records: int = MAX_RECORDS,
    ):
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._monotonic = monotonic_clock if monotonic_clock is not None else time.monotonic
        if not callable(self._clock) or not callable(self._monotonic):
            raise ValueError("invalid_context_view_clock")
        self.view_limit_bytes = _limit(view_limit_bytes, MAX_VIEW_BYTES)
        self.conversation_limit_bytes = _limit(conversation_limit_bytes, MAX_CONVERSATION_BYTES)
        self.conversation_max_records = _limit(conversation_max_records, MAX_CONVERSATION_RECORDS)
        self.global_limit_bytes = _limit(global_limit_bytes, MAX_GLOBAL_BYTES)
        self.max_records = _limit(max_records, MAX_RECORDS)
        self._lock = RLock()
        self._records: dict[str, _StoredView] = {}
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self._conversation_bytes: dict[tuple[str, str], int] = {}
        self._conversation_counts: dict[tuple[str, str], int] = {}
        self._retained_bytes = 0
        self._last_wall: datetime | None = None
        self._last_monotonic: float | None = None
        self._clock_failed = False

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    @property
    def view_count(self) -> int:
        with self._lock:
            return len(self._records)

    def _remove(self, reference: str, reason: str) -> ContextView:
        stored = self._records.pop(reference)
        conversation = stored.bindings[:2]
        self._retained_bytes -= stored.size_bytes
        remaining = self._conversation_bytes[conversation] - stored.size_bytes
        count = self._conversation_counts[conversation] - 1
        if count:
            self._conversation_bytes[conversation] = remaining
            self._conversation_counts[conversation] = count
        else:
            self._conversation_bytes.pop(conversation)
            self._conversation_counts.pop(conversation)
        self._tombstones[reference] = _Tombstone(stored.bindings, reason)
        while len(self._tombstones) > self.max_records:
            self._tombstones.popitem(last=False)
        stored.content = b""
        return stored.metadata

    def _clock_failure(self):
        self._clock_failed = True
        for reference in tuple(self._records):
            self._remove(reference, "clock_unavailable")
        raise ViewUnavailable("clock_unavailable") from None

    def _times(self) -> tuple[datetime, float]:
        if self._clock_failed:
            raise ViewUnavailable("clock_unavailable")
        try:
            now, monotonic = _aware(self._clock()), self._monotonic()
            if (type(monotonic) not in (int, float) or not math.isfinite(monotonic) or monotonic < 0
                    or self._last_wall is not None and now < self._last_wall
                    or self._last_monotonic is not None and monotonic < self._last_monotonic):
                raise ValueError()
        except Exception:
            self._clock_failure()
        self._last_wall, self._last_monotonic = now, monotonic
        return now, monotonic

    def _expire(self, now: datetime, monotonic: float) -> tuple[ContextView, ...]:
        references = [reference for reference, stored in self._records.items()
                      if stored.expires_at <= now or stored.expires_monotonic <= monotonic]
        return tuple(self._remove(reference, "expired") for reference in references)

    def capture(
        self, text: str, *, owner_id: str, conversation_id: str, audience_id: str,
        dependencies: Sequence[SourceDependency] = (), expected_digest: str | None = None,
    ) -> ContextView:
        bindings, sources = _bindings(owner_id, conversation_id, audience_id), _dependencies(dependencies)
        if type(text) is not str:
            raise ViewCaptureError()
        if len(text) > self.view_limit_bytes:
            raise ViewLimit()
        try:
            content = text.encode("utf-8")
        except UnicodeError:
            raise ViewCaptureError() from None
        size = len(content)
        if size > self.view_limit_bytes:
            raise ViewLimit()
        digest = hashlib.sha256(content).hexdigest()
        if expected_digest is not None and not hmac.compare_digest(digest, _digest(expected_digest)):
            raise ViewCaptureError()
        with self._lock:
            now, monotonic = self._times()
            if any(source.expires_at <= now for source in sources):
                raise ViewCaptureError()
            try:
                expires = min((now + MAX_LIFETIME, *(source.expires_at for source in sources)))
                expires_monotonic = monotonic + (expires - now).total_seconds()
                if not math.isfinite(expires_monotonic) or expires_monotonic <= monotonic:
                    raise ValueError()
            except (ValueError, OverflowError):
                self._clock_failure()
            self._expire(now, monotonic)
            conversation = bindings[:2]
            retained = self._conversation_bytes.get(conversation, 0)
            count = self._conversation_counts.get(conversation, 0)
            if (len(self._records) >= self.max_records or count >= self.conversation_max_records
                    or retained + size > self.conversation_limit_bytes
                    or self._retained_bytes + size > self.global_limit_bytes):
                raise ViewLimit()
            reference = f"view_{secrets.token_urlsafe(32)}"
            if (_VIEW_REFERENCE.fullmatch(reference) is None
                    or reference in self._records or reference in self._tombstones):
                raise ViewCaptureError()
            view = ContextView(reference, *bindings, digest, size, now, expires, sources, "")
            identity = _identity(view)
            object.__setattr__(view, "integrity_identity", identity)
            self._records[reference] = _StoredView(view, content, bindings, size, expires, expires_monotonic,
                                                 tuple(source.reference for source in sources), identity)
            self._retained_bytes += size
            self._conversation_bytes[conversation] = retained + size
            self._conversation_counts[conversation] = count + 1
            return view

    def _resolve(self, reference: str, owner_id: str, conversation_id: str, audience_id: str) -> _StoredView:
        try:
            bindings = _bindings(owner_id, conversation_id, audience_id)
            if type(reference) is not str or _VIEW_REFERENCE.fullmatch(reference) is None:
                raise ViewCaptureError()
        except ViewCaptureError:
            raise ViewDenied() from None
        stored = self._records.get(reference)
        tombstone = self._tombstones.get(reference)
        if (stored.bindings if stored is not None else tombstone.bindings if tombstone is not None else None) != bindings:
            raise ViewDenied()
        if stored is None:
            raise ViewUnavailable(tombstone.reason)
        try:
            metadata = stored.metadata
            intact = (hmac.compare_digest(_identity(metadata), stored.identity)
                      and hmac.compare_digest(metadata.integrity_identity, stored.identity)
                      and metadata.size_bytes == stored.size_bytes == len(stored.content)
                      and hmac.compare_digest(metadata.digest, hashlib.sha256(stored.content).hexdigest()))
        except Exception:
            intact = False
        if not intact:
            self._remove(reference, "integrity_failed")
            raise ViewUnavailable("integrity_failed")
        now, monotonic = self._times()
        if stored.expires_at <= now or stored.expires_monotonic <= monotonic:
            self._remove(reference, "expired")
            raise ViewUnavailable("expired")
        return stored

    @staticmethod
    def _validate_proof(view: ContextView, dependencies: object, expected_digest: str | None):
        try:
            if dependencies is not None and _dependencies(dependencies) != view.dependencies:
                raise ViewCaptureError()
            if expected_digest is not None and not hmac.compare_digest(view.digest, _digest(expected_digest)):
                raise ViewCaptureError()
        except ViewCaptureError:
            raise ViewDenied() from None

    def inspect(
        self, reference: str, *, owner_id: str, conversation_id: str, audience_id: str,
        dependencies: Sequence[SourceDependency] | None = None, expected_digest: str | None = None,
    ) -> ContextView:
        with self._lock:
            view = self._resolve(reference, owner_id, conversation_id, audience_id).metadata
            self._validate_proof(view, dependencies, expected_digest)
            return view

    def read(
        self, reference: str, *, owner_id: str, conversation_id: str, audience_id: str,
        dependencies: Sequence[SourceDependency], expected_digest: str | None = None,
    ) -> ContextViewContent:
        with self._lock:
            stored = self._resolve(reference, owner_id, conversation_id, audience_id)
            if dependencies is None:
                raise ViewDenied()
            view = stored.metadata
            self._validate_proof(view, dependencies, expected_digest)
            return ContextViewContent(view.reference, view.digest, stored.content.decode("utf-8"),
                                      view.size_bytes, view.expires_at, view.dependencies)

    def delete(self, reference: str, *, owner_id: str, conversation_id: str, audience_id: str) -> ContextView:
        with self._lock:
            self._resolve(reference, owner_id, conversation_id, audience_id)
            return self._remove(reference, "deleted")

    def revoke(
        self, *, owner_id: str, conversation_id: str | None = None, source_reference: str | None = None,
    ) -> tuple[ContextView, ...]:
        try:
            _identifier(owner_id)
            if conversation_id is not None:
                _identifier(conversation_id)
            if source_reference is not None and (type(source_reference) is not str
                                                or _SOURCE_REFERENCE.fullmatch(source_reference) is None):
                raise ViewCaptureError()
        except ViewCaptureError:
            raise ViewDenied() from None
        with self._lock:
            references = [reference for reference, stored in self._records.items()
                          if stored.bindings[0] == owner_id
                          and (conversation_id is None or stored.bindings[1] == conversation_id)
                          and (source_reference is None or source_reference in stored.source_references)]
            return tuple(self._remove(reference, "revoked") for reference in references)

    def cleanup(self) -> tuple[ContextView, ...]:
        with self._lock:
            return self._expire(*self._times())

    def clear(self) -> tuple[ContextView, ...]:
        with self._lock:
            return tuple(self._remove(reference, "deleted") for reference in tuple(self._records))
