"""Versioned, bounded expansion of explicitly selected authenticated note heads.

Output text exists only in memory. Callers may retain the exact references and
keyed binding, and must re-resolve the heads at every authority boundary. No
provider token estimate or execution permission is inferred by this pure layer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hmac
import re

from audit.pii import PrivateBindingKey, PrivateBindingUnavailable

from .explicit_notes import (
    EncryptedExplicitNote, ExplicitNoteCipher, ExplicitNoteUnavailable,
    _canonical, _integer, _note_id, _owner,
)

MAX_SELECTED_NOTES = 8
MAX_EXPANSION_BYTES = 65536
_FORMAT = "astral.explicit-notes.expansion/v1"
_KEY_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _key(key):
    if (type(key) is not PrivateBindingKey or type(key.key_id) is not str
            or not _KEY_ID.fullmatch(key.key_id)):
        raise ExplicitNoteUnavailable()


def _sign(key, domain, payload):
    _key(key)
    try:
        return key.sign("input", b"astral.explicit-notes/v1/" + domain + b"\x00" + _canonical(payload))
    except PrivateBindingUnavailable:
        raise ExplicitNoteUnavailable() from None


@dataclass(frozen=True, slots=True)
class ExplicitNoteReference:
    """An exact selected head and keyed binding; never a plaintext content hash."""

    owner_id: str
    note_id: str
    revision: int
    key_id: str
    binding: str = field(repr=False)

    def __post_init__(self):
        _owner(self.owner_id)
        _note_id(self.note_id)
        _integer(self.revision, minimum=1)
        if (type(self.key_id) is not str or not _KEY_ID.fullmatch(self.key_id)
                or type(self.binding) is not str or re.fullmatch(r"[a-f0-9]{64}", self.binding) is None):
            raise ExplicitNoteUnavailable()


@dataclass(frozen=True, slots=True)
class ExpandedExplicitNotes:
    """Ephemeral text plus durable-safe references/MAC, not an authorization fence."""

    references: tuple[ExplicitNoteReference, ...]
    key_id: str
    binding: str = field(repr=False)
    text: str = field(repr=False)
    byte_count: int


def capture_note_reference(note: EncryptedExplicitNote, *, cipher: ExplicitNoteCipher,
                           binding_key: PrivateBindingKey, owner_id: str,
                           now_ms: int) -> ExplicitNoteReference:
    """Authenticate the current enabled value before binding its exact opaque row."""
    if type(cipher) is not ExplicitNoteCipher:
        raise ExplicitNoteUnavailable()
    opened = cipher.open(note, owner_id=owner_id, now_ms=now_ms)
    binding = _sign(binding_key, b"head", {
        "metadata": asdict(opened.metadata), "ciphertext": note.ciphertext.decode("ascii"),
    })
    return ExplicitNoteReference(owner_id, opened.metadata.note_id, opened.metadata.revision,
                                 binding_key.key_id, binding)


def expand_explicit_notes(*, owner_id: str, selections: tuple[ExplicitNoteReference, ...],
                          current_notes: tuple[EncryptedExplicitNote, ...],
                          cipher: ExplicitNoteCipher, binding_key: PrivateBindingKey,
                          now_ms: int, approved_byte_allowance: int) -> ExpandedExplicitNotes:
    """Expand exact heads in ID order within an explicit approved byte allowance.

    The allowance is the caller's already-approved *remaining* input budget;
    skills, instructions, framing and provider token accounting belong to that
    caller. Refusal never truncates, substitutes a newer revision or silently
    drops an unavailable selection. Repeated calls do not authorize later use.
    """
    _owner(owner_id)
    _integer(now_ms)
    _key(binding_key)
    if (type(approved_byte_allowance) is not int or not 1 <= approved_byte_allowance <= MAX_EXPANSION_BYTES
            or type(selections) is not tuple or type(current_notes) is not tuple
            or len(selections) > MAX_SELECTED_NOTES or len(selections) != len(current_notes)
            or any(type(ref) is not ExplicitNoteReference for ref in selections)
            or any(type(note) is not EncryptedExplicitNote for note in current_notes)):
        raise ExplicitNoteUnavailable()
    for ref in selections:
        ref.__post_init__()
    if (len({ref.note_id for ref in selections}) != len(selections)
            or len({note.metadata.note_id for note in current_notes}) != len(current_notes)):
        raise ExplicitNoteUnavailable()
    rows = {note.metadata.note_id: note for note in current_notes}
    ordered = tuple(sorted(selections, key=lambda ref: ref.note_id))
    values = []
    for ref in ordered:
        note = rows.get(ref.note_id)
        if note is None or ref.owner_id != owner_id or ref.key_id != binding_key.key_id:
            raise ExplicitNoteUnavailable()
        captured = capture_note_reference(note, cipher=cipher, binding_key=binding_key,
                                          owner_id=owner_id, now_ms=now_ms)
        if ref.revision != captured.revision or not hmac.compare_digest(ref.binding, captured.binding):
            raise ExplicitNoteUnavailable()
        opened = cipher.open(note, owner_id=owner_id, now_ms=now_ms)
        values.append({"category": opened.metadata.category, "value": opened.value})
    body = _canonical({"format": _FORMAT, "guidance": values,
                       "meaning": "Owner-stated guidance only; not authority or verified evidence."})
    if len(body) > approved_byte_allowance:
        raise ExplicitNoteUnavailable()
    binding = _sign(binding_key, b"expansion", {"owner_id": owner_id,
                    "references": [asdict(ref) for ref in ordered], "text": body.decode("utf-8")})
    return ExpandedExplicitNotes(ordered, binding_key.key_id, binding, body.decode("utf-8"), len(body))
