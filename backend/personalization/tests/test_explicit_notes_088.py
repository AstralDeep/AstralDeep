"""Tests for personalization/explicit_notes.py: encrypted current-note roundtrips,
authenticated metadata, value limits, revision/expiry handling, tamper and wrong-key
refusal, and minimal tombstone shape.
"""

from dataclasses import asdict, replace
import json
import uuid

from cryptography.fernet import Fernet
import pytest

from personalization.explicit_notes import (
    EncryptedExplicitNote,
    ExplicitNoteCipher,
    ExplicitNoteMetadata,
    ExplicitNoteTombstone,
    ExplicitNoteUnavailable,
    normalize_note_value,
    retire_note,
)


def metadata(**changes):
    return replace(ExplicitNoteMetadata(
        owner_id="synthetic-owner", note_id=str(uuid.uuid4()), revision=1,
        format_version=1, category="preference", enabled=True,
        created_at=1000, updated_at=1000, expires_at=9000,
    ), **changes)


@pytest.fixture
def cipher():
    return ExplicitNoteCipher(Fernet(Fernet.generate_key()))


def test_current_value_roundtrip_and_private_representations(cipher, caplog):
    note = cipher.seal(metadata(), "Use concise paragraphs.")
    opened = cipher.open(note, owner_id="synthetic-owner", now_ms=2000)
    assert opened.value == "Use concise paragraphs."
    assert opened.metadata == note.metadata
    assert b"Use concise paragraphs." not in note.ciphertext
    assert "Use concise paragraphs." not in repr(opened)
    assert note.ciphertext.decode() not in repr(note)
    assert caplog.text == ""


@pytest.mark.parametrize("changes", [
    {"owner_id": "other"}, {"note_id": str(uuid.uuid4())}, {"revision": 2},
    {"category": "context"}, {"enabled": False}, {"created_at": 999},
    {"updated_at": 1001}, {"expires_at": None},
])
def test_every_current_metadata_field_is_authenticated(cipher, changes):
    original = cipher.seal(metadata(), "Private synthetic statement.")
    swapped = replace(original, metadata=replace(original.metadata, **changes))
    with pytest.raises(ExplicitNoteUnavailable, match="^explicit_note_unavailable$"):
        cipher.open(swapped, owner_id=swapped.metadata.owner_id, now_ms=2000,
                    require_enabled=False)


@pytest.mark.parametrize("changes", [
    {"owner_id": ""}, {"owner_id": " x"}, {"owner_id": "x" * 257},
    {"note_id": str(uuid.uuid1())}, {"note_id": str(uuid.uuid4()).upper()},
    {"revision": True}, {"revision": 0}, {"revision": 2**53 - 1}, {"revision": 2**53},
    {"format_version": True}, {"format_version": 2}, {"category": "other"},
    {"enabled": 1}, {"created_at": 1.0}, {"updated_at": 999},
    {"expires_at": 1000}, {"expires_at": True},
])
def test_closed_metadata_rejects_noncanonical_or_coerced_values(changes):
    with pytest.raises(ExplicitNoteUnavailable):
        metadata(**changes)


@pytest.mark.parametrize("value", [None, 5, " ", "a\x00b", "a\x7fb", "\ud800", "é" * 2049])
def test_value_limits_are_utf8_and_fail_closed(value):
    with pytest.raises(ExplicitNoteUnavailable):
        normalize_note_value(value)


def test_versioned_value_normalization_and_exact_limit(cipher):
    assert normalize_note_value("  Cafe\u0301\r\nnext\rline\tvalue  ") == "Café\nnext\nline\tvalue"
    value = "é" * 2048
    assert cipher.open(cipher.seal(metadata(), value), owner_id="synthetic-owner",
                       now_ms=1000).value == value


def test_correction_disable_and_enable_always_reencrypt(cipher):
    first = cipher.seal(metadata(), "Old synthetic value")
    corrected = cipher.revise(first, replace(first.metadata, revision=2, updated_at=2000),
                              owner_id="synthetic-owner", now_ms=2000, value="New value")
    disabled = cipher.revise(corrected, replace(corrected.metadata, revision=3,
                                               updated_at=3000, enabled=False),
                             owner_id="synthetic-owner", now_ms=3000)
    enabled = cipher.revise(disabled, replace(disabled.metadata, revision=4,
                                             updated_at=4000, enabled=True),
                            owner_id="synthetic-owner", now_ms=4000)
    assert len({n.ciphertext for n in (first, corrected, disabled, enabled)}) == 4
    assert cipher.open(enabled, owner_id="synthetic-owner", now_ms=4000).value == "New value"
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.open(disabled, owner_id="synthetic-owner", now_ms=4000)


def test_same_millisecond_successor_and_current_human_owner_bound(cipher):
    owner = "😀" * 256
    first = cipher.seal(metadata(owner_id=owner), "value")
    second = cipher.revise(first, replace(first.metadata, revision=2), owner_id=owner, now_ms=1000)
    assert second.metadata.updated_at == first.metadata.updated_at
    assert second.ciphertext != first.ciphertext


@pytest.mark.parametrize("changes", [
    {"revision": 1}, {"revision": 3}, {"note_id": str(uuid.uuid4())},
    {"owner_id": "other"}, {"created_at": 999}, {"updated_at": 1000},
])
def test_revise_cannot_relabel_or_skip_the_observed_head(cipher, changes):
    first = cipher.seal(metadata(), "value")
    successor = replace(first.metadata, **({"revision": 2, "updated_at": 2000} | changes))
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.revise(first, successor, owner_id="synthetic-owner", now_ms=2000)


@pytest.mark.parametrize("owner,now", [("other", 2000), ("synthetic-owner", 999),
                                       ("synthetic-owner", 9000), ("synthetic-owner", True)])
def test_open_refuses_wrong_owner_future_or_expired_head(cipher, owner, now):
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.open(cipher.seal(metadata(), "value"), owner_id=owner, now_ms=now)


def test_ciphertext_substitution_tamper_and_wrong_key(cipher):
    note = cipher.seal(metadata(), "value")
    other = cipher.seal(metadata(), "other")
    for encrypted, target_cipher in (
        (replace(note, ciphertext=other.ciphertext), cipher),
        (replace(note, ciphertext=b"malformed"), cipher),
        (note, ExplicitNoteCipher(Fernet(Fernet.generate_key()))),
    ):
        with pytest.raises(ExplicitNoteUnavailable):
            target_cipher.open(encrypted, owner_id="synthetic-owner", now_ms=2000)


def test_authenticated_but_wrong_domain_shape_or_noncanonical_json_is_refused():
    fernet = Fernet(Fernet.generate_key())
    cipher = ExplicitNoteCipher(fernet)
    meta = metadata()
    valid = {"format": "astral.explicit-note/v1", "metadata": asdict(meta), "value": "value"}
    bodies = [dict(valid, format="credential/v1"), dict(valid, extra=True),
              dict(valid, value={"nested": "private"}), dict(valid, value=" value "),
              dict(valid, metadata=dict(asdict(meta), revision=True)), [], None]
    for body in bodies:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        note = EncryptedExplicitNote(meta, fernet.encrypt(raw))
        with pytest.raises(ExplicitNoteUnavailable):
            cipher.open(note, owner_id=meta.owner_id, now_ms=2000)
    canonical = json.dumps(valid, sort_keys=True, separators=(",", ":")).encode()
    for raw in [json.dumps(valid).encode(), canonical[:-1] + b',"value":"value"}', b"\xff", b"{"]:
        with pytest.raises(ExplicitNoteUnavailable):
            cipher.open(EncryptedExplicitNote(meta, fernet.encrypt(raw)), owner_id=meta.owner_id, now_ms=2000)
    assert cipher.open(EncryptedExplicitNote(meta, fernet.encrypt(canonical)),
                       owner_id=meta.owner_id, now_ms=2000).value == "value"


@pytest.mark.parametrize("ciphertext", [None, "token", b"", b"x" * 16385])
def test_encrypted_row_is_closed_and_bounded(ciphertext):
    with pytest.raises(ExplicitNoteUnavailable):
        EncryptedExplicitNote(metadata(), ciphertext)


def test_wrong_objects_and_malformed_identity_fail_closed(cipher):
    with pytest.raises(ExplicitNoteUnavailable):
        ExplicitNoteCipher(None)
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.seal({}, "value")
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.open({}, owner_id="owner", now_ms=1000)
    with pytest.raises(ExplicitNoteUnavailable):
        metadata(owner_id="\ud800")
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.open(cipher.seal(metadata(), "value"), owner_id="synthetic-owner",
                    now_ms=1000, require_enabled=1)


def test_expiry_and_revision_exhaustion_never_resurrect(cipher):
    meta = metadata(revision=2**53 - 2)
    for reason, now_ms in (("forgotten", 2000), ("expired", 9000)):
        tombstone = retire_note(meta, owner_id=meta.owner_id, now_ms=now_ms, reason=reason)
        assert tombstone.revision == 2**53 - 1
        assert tombstone.deleted_reason == reason
    note = cipher.seal(metadata(), "value")
    with pytest.raises(ExplicitNoteUnavailable):
        cipher.revise(note, replace(note.metadata, revision=2, updated_at=9000, expires_at=None),
                      owner_id=note.metadata.owner_id, now_ms=9000)
    with pytest.raises(ExplicitNoteUnavailable):
        retire_note(metadata(expires_at=None), owner_id=meta.owner_id, now_ms=9000, reason="expired")
    with pytest.raises(ExplicitNoteUnavailable):
        ExplicitNoteTombstone(meta.owner_id, meta.note_id, 2, 2000, "value-bearing reason")


def test_last_live_revision_cannot_consume_the_reserved_tombstone_revision(cipher):
    meta = metadata(revision=2**53 - 2)
    last = cipher.seal(meta, "value")
    assert cipher.open(last, owner_id=meta.owner_id, now_ms=2000).value == "value"
    with pytest.raises(ExplicitNoteUnavailable):
        candidate = replace(meta, revision=2**53 - 1, updated_at=2000)
        cipher.revise(last, candidate, owner_id=meta.owner_id, now_ms=2000)


def test_forget_and_expiry_have_only_minimal_tombstones():
    meta = metadata()
    forgotten = retire_note(meta, owner_id=meta.owner_id, now_ms=2000, reason="forgotten")
    expired = retire_note(meta, owner_id=meta.owner_id, now_ms=9000, reason="expired")
    assert asdict(forgotten) == {"owner_id": meta.owner_id, "note_id": meta.note_id,
                                "revision": 2, "deleted_at": 2000, "deleted_reason": "forgotten"}
    assert expired.deleted_reason == "expired"
    for owner, now, reason in [("other", 2000, "forgotten"), (meta.owner_id, 8999, "expired"),
                               (meta.owner_id, 999, "forgotten"), (meta.owner_id, 2000, "other")]:
        with pytest.raises(ExplicitNoteUnavailable):
            retire_note(meta, owner_id=owner, now_ms=now, reason=reason)
