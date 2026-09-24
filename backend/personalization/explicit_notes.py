"""Encrypts and authenticates a user's single current explicit note per category via an
injected Fernet cipher; the host supplies the key, storage, and CAS. Used by
explicit_note_service.py, explicit_note_expansion.py, and selected_guidance.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import unicodedata
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

NOTE_CATEGORIES = frozenset({"profession", "goal", "preference", "workflow_tag", "context"})
MAX_NOTE_VALUE_BYTES = 4096
MAX_NOTE_CIPHERTEXT_BYTES = 16384
MAX_COUNTER = 2**53 - 1
MAX_LIVE_NOTE_REVISION = MAX_COUNTER - 1
_FORMAT = "astral.explicit-note/v1"


class ExplicitNoteUnavailable(ValueError):
    def __init__(self):
        super().__init__("explicit_note_unavailable")


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_COUNTER:
        raise ExplicitNoteUnavailable()


def _owner(value):
    try:
        if (type(value) is not str or not 1 <= len(value) <= 256
                or len(value.encode("utf-8")) > 1024 or value != value.strip()
                or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)):
            raise ExplicitNoteUnavailable()
    except UnicodeError:
        raise ExplicitNoteUnavailable() from None


def _note_id(value):
    try:
        parsed = UUID(value) if type(value) is str else None
        if parsed is None or parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ExplicitNoteUnavailable() from None


def normalize_note_value(value: str) -> str:
    try:
        if type(value) is not str or len(value.encode("utf-8")) > MAX_NOTE_VALUE_BYTES:
            raise ExplicitNoteUnavailable()
        normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
        if (not normalized or len(normalized.encode("utf-8")) > MAX_NOTE_VALUE_BYTES
                or any(unicodedata.category(c) in {"Cc", "Cs"} and c not in "\n\t"
                       for c in normalized)):
            raise ExplicitNoteUnavailable()
        return normalized
    except UnicodeError:
        raise ExplicitNoteUnavailable() from None


@dataclass(frozen=True, slots=True)
class ExplicitNoteMetadata:
    owner_id: str
    note_id: str
    revision: int
    format_version: int
    category: str
    enabled: bool
    created_at: int
    updated_at: int
    expires_at: int | None = None

    def __post_init__(self):
        _owner(self.owner_id)
        _note_id(self.note_id)
        _integer(self.revision, minimum=1)
        _integer(self.format_version, minimum=1)
        _integer(self.created_at)
        _integer(self.updated_at)
        if self.expires_at is not None:
            _integer(self.expires_at)
        if (self.revision > MAX_LIVE_NOTE_REVISION
                or self.format_version != 1 or type(self.enabled) is not bool
                or type(self.category) is not str or self.category not in NOTE_CATEGORIES
                or self.updated_at < self.created_at
                or (self.expires_at is not None and self.expires_at <= self.updated_at)):
            raise ExplicitNoteUnavailable()


@dataclass(frozen=True, slots=True)
class EncryptedExplicitNote:
    metadata: ExplicitNoteMetadata
    ciphertext: bytes = field(repr=False)

    def __post_init__(self):
        _metadata(self.metadata)
        if type(self.ciphertext) is not bytes or not 1 <= len(self.ciphertext) <= MAX_NOTE_CIPHERTEXT_BYTES:
            raise ExplicitNoteUnavailable()


@dataclass(frozen=True, slots=True)
class OpenedExplicitNote:
    metadata: ExplicitNoteMetadata
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExplicitNoteTombstone:
    owner_id: str
    note_id: str
    revision: int
    deleted_at: int
    deleted_reason: str

    def __post_init__(self):
        _owner(self.owner_id)
        _note_id(self.note_id)
        _integer(self.revision, minimum=1)
        _integer(self.deleted_at)
        if type(self.deleted_reason) is not str or self.deleted_reason not in {"forgotten", "expired"}:
            raise ExplicitNoteUnavailable()


def _metadata(value):
    if type(value) is not ExplicitNoteMetadata:
        raise ExplicitNoteUnavailable()
    value.__post_init__()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _payload(metadata, value):
    return _canonical({"format": _FORMAT, "metadata": asdict(metadata), "value": value})


def _current(metadata, owner_id, now_ms, require_enabled):
    _metadata(metadata)
    _owner(owner_id)
    _integer(now_ms)
    if (type(require_enabled) is not bool or owner_id != metadata.owner_id
            or now_ms < metadata.updated_at or (require_enabled and not metadata.enabled)
            or (metadata.expires_at is not None and now_ms >= metadata.expires_at)):
        raise ExplicitNoteUnavailable()


@dataclass(frozen=True, slots=True)
class ExplicitNoteCipher:
    _fernet: Fernet = field(repr=False)

    def __post_init__(self):
        if type(self._fernet) is not Fernet:
            raise ExplicitNoteUnavailable()

    def seal(self, metadata: ExplicitNoteMetadata, value: str) -> EncryptedExplicitNote:
        _metadata(metadata)
        value = normalize_note_value(value)
        return EncryptedExplicitNote(metadata, self._fernet.encrypt(_payload(metadata, value)))

    def open(self, note: EncryptedExplicitNote, *, owner_id: str, now_ms: int,
             require_enabled: bool = True) -> OpenedExplicitNote:
        if type(note) is not EncryptedExplicitNote:
            raise ExplicitNoteUnavailable()
        note.__post_init__()
        _current(note.metadata, owner_id, now_ms, require_enabled)
        try:
            raw = self._fernet.decrypt(note.ciphertext)
            decoded = json.loads(raw)
            if type(decoded) is not dict:
                raise ExplicitNoteUnavailable()
            value = normalize_note_value(decoded.get("value"))
            if raw != _payload(note.metadata, value):
                raise ExplicitNoteUnavailable()
        except (InvalidToken, ValueError, UnicodeError, RecursionError):
            raise ExplicitNoteUnavailable() from None
        return OpenedExplicitNote(note.metadata, value)

    def revise(self, note: EncryptedExplicitNote, metadata: ExplicitNoteMetadata, *,
               owner_id: str, now_ms: int, value: str | None = None) -> EncryptedExplicitNote:
        opened = self.open(note, owner_id=owner_id, now_ms=now_ms, require_enabled=False)
        _metadata(metadata)
        previous = opened.metadata
        if (metadata.owner_id != previous.owner_id or metadata.note_id != previous.note_id
                or metadata.created_at != previous.created_at
                or metadata.revision != previous.revision + 1
                or metadata.updated_at != now_ms):
            raise ExplicitNoteUnavailable()
        return self.seal(metadata, opened.value if value is None else value)


# Erasure happens in the storage transaction, not here
def retire_note(metadata: ExplicitNoteMetadata, *, owner_id: str, now_ms: int,
                reason: str) -> ExplicitNoteTombstone:
    _metadata(metadata)
    _owner(owner_id)
    _integer(now_ms)
    if (owner_id != metadata.owner_id or now_ms < metadata.updated_at
            or (reason == "expired" and (metadata.expires_at is None or now_ms < metadata.expires_at))):
        raise ExplicitNoteUnavailable()
    return ExplicitNoteTombstone(owner_id, metadata.note_id, metadata.revision + 1, now_ms, reason)
