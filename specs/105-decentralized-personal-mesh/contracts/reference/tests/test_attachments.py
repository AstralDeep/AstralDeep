"""Checks of the attachment rules beyond the committed vectors: sealing and opening at every chunk boundary, detection of any single edited byte, and independence of the peer rule from request order."""

from __future__ import annotations

import random

import pytest

import generate_vectors  # noqa: F401
from astral_mesh_ref import attachments, crypto
from astral_mesh_ref.errors import MeshError
from astral_mesh_ref.testkit import seed

CHAT = crypto.sha256_id(b"test/chat")
KEY_REF = "ck:AAAAAAAAAAAAAAAAAAAAAA"
KEY = seed("test/key")
LABELS = {"confidentiality": "private", "integrity": "owner"}
C = attachments.CHUNK_SIZE


def _seal(content: bytes):
    return attachments.seal(
        content,
        key=KEY,
        key_ref=KEY_REF,
        chat=CHAT,
        name="a.txt",
        mime="text/plain",
        nonces=attachments.nonce_bytes("test", attachments.chunk_count(len(content))),
        labels=LABELS,
    )


@pytest.mark.parametrize("size", [1, 2, C - 1, C, C + 1, 2 * C, 2 * C + 1, 3 * C - 1])
def test_every_boundary_size_seals_and_opens_to_the_same_bytes(size: int) -> None:
    content = attachments.DetRng("t" + str(size)).bytes(size)
    body, chunks = _seal(content)
    assert len(chunks) == attachments.chunk_count(size)
    assert [len(c) for c in chunks] == [
        attachments.chunk_length(size, i) for i in range(len(chunks))
    ]
    assert (
        attachments.open_all(body, chunks, key=KEY, key_ref=KEY_REF, chat=CHAT)
        == content
    )


def test_the_largest_attachment_uses_the_most_chunks_the_contract_allows() -> None:
    assert attachments.chunk_count(attachments.MAX_SIZE) == attachments.MAX_CHUNKS == 64
    assert attachments.chunk_count(attachments.MAX_SIZE + 1) == 65


def test_any_single_edited_byte_of_any_chunk_is_refused() -> None:
    content = attachments.DetRng("flip").bytes(C + 300)
    body, chunks = _seal(content)
    rng = random.Random(3)
    for _ in range(40):
        index = rng.randrange(len(chunks))
        offset = rng.randrange(len(chunks[index]))
        edited = list(chunks)
        flipped = bytearray(edited[index])
        flipped[offset] ^= 1 << rng.randrange(8)
        edited[index] = bytes(flipped)
        with pytest.raises(MeshError) as caught:
            attachments.open_all(body, edited, key=KEY, key_ref=KEY_REF, chat=CHAT)
        assert caught.value.code == "attachment.chunk_hash"


def test_a_resealing_attacker_who_fixes_the_hash_is_still_refused_by_the_tag() -> None:
    content = attachments.DetRng("reseal").bytes(C + 300)
    body, chunks = _seal(content)
    flipped = bytearray(chunks[0])
    flipped[60] ^= 1
    chunks[0] = bytes(flipped)
    body["chunks"][0] = crypto.sha256_id(chunks[0])
    with pytest.raises(MeshError) as caught:
        attachments.open_all(body, chunks, key=KEY, key_ref=KEY_REF, chat=CHAT)
    assert caught.value.code == "attachment.chunk_decrypt"


def test_a_chunk_cannot_move_to_another_chat_key_reference_or_position() -> None:
    content = attachments.DetRng("move").bytes(2 * C)
    body, chunks = _seal(content)
    for chat, key_ref in (
        (crypto.sha256_id(b"other"), KEY_REF),
        (CHAT, "ck:BBBBBBBBBBBBBBBBBBBBBB"),
    ):
        with pytest.raises(MeshError) as caught:
            attachments.open_all(body, chunks, key=KEY, key_ref=key_ref, chat=chat)
        assert caught.value.code == "attachment.chunk_decrypt"
    moved = [chunks[1], chunks[0]]
    swapped = {**body, "chunks": [crypto.sha256_id(c) for c in moved]}
    with pytest.raises(MeshError) as caught:
        attachments.open_all(swapped, moved, key=KEY, key_ref=KEY_REF, chat=CHAT)
    assert caught.value.code == "attachment.chunk_decrypt"


def test_sealing_the_same_content_twice_gives_different_chunks_only_by_nonce() -> None:
    content = attachments.DetRng("twice").bytes(100)
    first, _ = _seal(content)
    again, _ = attachments.seal(
        content,
        key=KEY,
        key_ref=KEY_REF,
        chat=CHAT,
        name="a.txt",
        mime="text/plain",
        nonces=attachments.nonce_bytes("other", 1),
        labels=LABELS,
    )
    assert first["sha256"] == again["sha256"] and first["chunks"] != again["chunks"]


def test_the_peer_rule_does_not_depend_on_the_order_of_the_request() -> None:
    held = {
        crypto.sha256_id(b"e1"): {
            "labels": LABELS,
            "chunks": [crypto.sha256_id(b"a"), crypto.sha256_id(b"b")],
        },
        crypto.sha256_id(b"e2"): {
            "labels": {"confidentiality": "phi", "integrity": "owner"},
            "chunks": [crypto.sha256_id(b"c")],
        },
    }
    asked = [crypto.sha256_id(x) for x in (b"a", b"b", b"c", b"d")]
    rng = random.Random(5)
    for in_phi in (False, True):
        results = []
        for _ in range(10):
            order = list(asked)
            rng.shuffle(order)
            out = attachments.chunks_for_peer(held, order, peer_in_phi_group=in_phi)
            results.append((sorted(out["send"]), sorted(w["hash"] for w in out["withheld"])))
        assert len(set(map(repr, results))) == 1
