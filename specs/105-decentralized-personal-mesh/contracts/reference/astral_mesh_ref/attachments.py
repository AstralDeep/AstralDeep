"""Attachments: the body of `chat.attachment`, fixed-size chunks sealed under the chat's content key and addressed by hash, the checks a receiver makes on each chunk, and the rule that decides which peers are sent a chunk.
The kernel writes and reads these events, the vectors in attachments.json pin them, crypto.py supplies the AEAD, and sync.py's body rule is extended here to chunks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import crypto
from .cj import canonical
from .errors import MeshError
from .testkit import DetRng

TYPE = "chat.attachment"
CHUNK_SIZE = 262144
MAX_SIZE = 16 * 1024 * 1024
MAX_CHUNKS = MAX_SIZE // CHUNK_SIZE
MAX_NAME = 255
MAX_PER_MESSAGE = 8
SEALED_OVERHEAD = crypto.NONCE_LEN + crypto.TAG_LEN
MIME_TYPES = (
    "application/json",
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/markdown",
    "text/plain",
)
BODY_FIELDS = {"chunks", "mime", "name", "sha256", "size"}
CONFIDENTIALITY = ("public", "private", "phi")


def _bad(detail: str) -> MeshError:
    return MeshError("attachment.malformed", detail)


def chunk_count(size: int) -> int:
    return -(-size // CHUNK_SIZE)


def chunk_length(size: int, index: int) -> int:
    """Length of the sealed chunk at `index`: the plaintext length plus nonce and tag."""
    last = chunk_count(size) - 1
    plain = size - last * CHUNK_SIZE if index == last else CHUNK_SIZE
    return plain + SEALED_OVERHEAD


def chunk_aad(chat: str, key_ref: str, index: int) -> bytes:
    return canonical(
        {"chat": chat, "index": index, "key": key_ref, "purpose": "attachment"}
    )


def valid_name(name: Any) -> bool:
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME:
        return False
    if name in (".", "..") or "/" in name or "\\" in name:
        return False
    return all(ord(c) >= 0x20 and ord(c) != 0x7F for c in name)


def check_body(body: Any, labels: dict[str, str]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        raise _bad("fields")
    if labels.get("confidentiality") not in CONFIDENTIALITY:
        raise MeshError("attachment.label", "attachments are public, private or phi")
    if not valid_name(body["name"]):
        raise MeshError("attachment.bad_name", "name")
    if body["mime"] not in MIME_TYPES:
        raise MeshError("attachment.mime_not_allowed", str(body["mime"]))
    size = body["size"]
    if type(size) is not int:
        raise _bad("size")
    if size < 1:
        raise MeshError("attachment.empty", "size")
    if size > MAX_SIZE:
        raise MeshError("attachment.too_large", "size")
    if not crypto.is_id(body["sha256"]):
        raise _bad("sha256")
    chunks = body["chunks"]
    if not isinstance(chunks, list) or not all(crypto.is_id(c) for c in chunks):
        raise _bad("chunks")
    if len(chunks) != chunk_count(size):
        raise MeshError("attachment.chunk_count", "chunks")
    if len(set(chunks)) != len(chunks):
        raise _bad("duplicate chunk")
    return {"chunks": len(chunks), "mime": body["mime"], "size": size}


def check_message(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise _bad("message body")
    refs = body.get("attachments")
    if refs is None:
        return {"attachments": 0}
    if not isinstance(refs, list) or not all(crypto.is_id(r) for r in refs):
        raise _bad("attachments")
    if len(set(refs)) != len(refs):
        raise _bad("duplicate attachment")
    if len(refs) > MAX_PER_MESSAGE:
        raise MeshError("attachment.too_many", "attachments")
    return {"attachments": len(refs)}


def seal(
    content: bytes,
    *,
    key: bytes,
    key_ref: str,
    chat: str,
    name: str,
    mime: str,
    nonces: bytes,
    labels: dict[str, str],
) -> tuple[dict[str, Any], list[bytes]]:
    if not content:
        raise MeshError("attachment.empty", "content")
    if len(content) > MAX_SIZE:
        raise MeshError("attachment.too_large", "content")
    sealed: list[bytes] = []
    for index in range(chunk_count(len(content))):
        plain = content[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
        nonce = nonces[index * crypto.NONCE_LEN : (index + 1) * crypto.NONCE_LEN]
        sealed.append(
            nonce + crypto.aead_seal(key, nonce, plain, chunk_aad(chat, key_ref, index))
        )
    body = {
        "chunks": [crypto.sha256_id(ct) for ct in sealed],
        "mime": mime,
        "name": name,
        "sha256": crypto.sha256_id(content),
        "size": len(content),
    }
    check_body(body, labels)
    return body, sealed


def receive_chunk(
    body: dict[str, Any],
    index: int,
    ct: bytes,
    *,
    key: bytes | None,
    key_ref: str,
    chat: str,
) -> bytes | None:
    """Checks one received chunk against the attachment's body and returns its plaintext when the key is held."""
    if ct is None:
        raise MeshError("attachment.chunk_missing", str(index))
    if crypto.sha256_id(ct) != body["chunks"][index]:
        raise MeshError("attachment.chunk_hash", str(index))
    if len(ct) != chunk_length(body["size"], index):
        raise MeshError("attachment.chunk_size", str(index))
    if key is None:
        return None
    try:
        return crypto.aead_open(
            key,
            ct[: crypto.NONCE_LEN],
            ct[crypto.NONCE_LEN :],
            chunk_aad(chat, key_ref, index),
        )
    except MeshError as exc:
        raise MeshError("attachment.chunk_decrypt", str(index)) from exc


def open_all(
    body: dict[str, Any],
    chunks: list[bytes | None],
    *,
    key: bytes,
    key_ref: str,
    chat: str,
) -> bytes:
    plain = bytearray()
    for index in range(len(body["chunks"])):
        ct = chunks[index] if index < len(chunks) else None
        plain += receive_chunk(
            body, index, ct, key=key, key_ref=key_ref, chat=chat
        )  # type: ignore[operator]
    if len(plain) != body["size"] or crypto.sha256_id(bytes(plain)) != body["sha256"]:
        raise MeshError("attachment.content_mismatch", "content")
    return bytes(plain)


def chunks_for_peer(
    attachments: dict[str, dict[str, Any]],
    requested: list[str],
    *,
    peer_in_phi_group: bool,
) -> dict[str, Any]:
    owner: dict[str, str] = {}
    for event_id, attachment in sorted(attachments.items()):
        for digest in attachment["chunks"]:
            held = owner.get(digest)
            if held is None or _rank(attachment) > _rank(attachments[held]):
                owner[digest] = event_id
    send: list[str] = []
    withheld: list[dict[str, str]] = []
    for digest in requested:
        event_id = owner.get(digest)
        if event_id is None:
            withheld.append({"hash": digest, "reason": "unknown"})
            continue
        confidentiality = attachments[event_id]["labels"]["confidentiality"]
        if confidentiality == "secret":
            withheld.append({"hash": digest, "reason": "secret"})
        elif confidentiality == "phi" and not peer_in_phi_group:
            withheld.append({"hash": digest, "reason": "phi"})
        else:
            send.append(digest)
    return {"send": send, "withheld": withheld}


def _rank(attachment: dict[str, Any]) -> int:
    return ("public", "private", "phi", "secret").index(
        attachment["labels"]["confidentiality"]
    )


def content_bytes(spec: dict[str, Any]) -> bytes:
    stream = DetRng("attachment/" + spec["label"])
    return stream.bytes(spec["length"])


def nonce_bytes(label: str, count: int) -> bytes:
    return DetRng("attachment-nonce/" + label).bytes(count * crypto.NONCE_LEN)


def _tamper(
    body: dict[str, Any], chunks: list[bytes | None], steps: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[bytes | None]]:
    body = {**body, "chunks": list(body["chunks"])}
    chunks = list(chunks)
    for step in steps:
        kind = step["op"]
        if kind == "flip":
            ct = bytearray(chunks[step["chunk"]])  # type: ignore[arg-type]
            ct[step["offset"]] ^= 1
            chunks[step["chunk"]] = bytes(ct)
            if step.get("rehash"):
                body["chunks"][step["chunk"]] = crypto.sha256_id(chunks[step["chunk"]])  # type: ignore[arg-type]
        elif kind == "truncate":
            chunks[step["chunk"]] = chunks[step["chunk"]][: step["to"]]  # type: ignore[index]
            if step.get("rehash"):
                body["chunks"][step["chunk"]] = crypto.sha256_id(chunks[step["chunk"]])  # type: ignore[arg-type]
        elif kind == "drop":
            chunks[step["chunk"]] = None
        elif kind == "swap":
            a, b = step["chunks"]
            chunks[a], chunks[b] = chunks[b], chunks[a]
        elif kind == "forge_sha256":
            body["sha256"] = crypto.sha256_id(b"forged")
        else:
            raise _bad("tamper " + str(kind))
    return body, chunks


def _sealed(i: dict[str, Any]) -> tuple[dict[str, Any], list[bytes], bytes]:
    content = content_bytes(i["content"])
    key = bytes.fromhex(i["key_hex"])
    body, chunks = seal(
        content,
        key=key,
        key_ref=i["key"],
        chat=i["chat"],
        name=i.get("name", "scan.png"),
        mime=i.get("mime", "image/png"),
        nonces=nonce_bytes(i["nonces"], chunk_count(max(len(content), 1))),
        labels=i.get("labels", {"confidentiality": "private", "integrity": "owner"}),
    )
    return body, chunks, key


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("attachment_check")
    def _check(i: dict[str, Any]) -> Any:
        return check_body(i["body"], i["labels"])

    @op("attachment_message_check")
    def _message(i: dict[str, Any]) -> Any:
        return check_message(i["body"])

    @op("attachment_seal")
    def _seal(i: dict[str, Any]) -> Any:
        body, chunks, _ = _sealed(i)
        return {"body": body, "lengths": [len(c) for c in chunks]}

    @op("attachment_receive")
    def _receive(i: dict[str, Any]) -> Any:
        body, chunks, key = _sealed(i)
        body, chunks = _tamper(body, chunks, i.get("tamper", []))
        key_held = None if i.get("key_held") is False else key
        if i.get("wrong_key"):
            key_held = bytes.fromhex(i["wrong_key"])
        plain = receive_chunk(
            body,
            i["index"],
            chunks[i["index"]],  # type: ignore[arg-type]
            key=key_held,
            key_ref=i["key"],
            chat=i.get("open_chat", i["chat"]),
        )
        return {
            "decrypted": plain is not None,
            "length": None if plain is None else len(plain),
        }

    @op("attachment_open")
    def _open(i: dict[str, Any]) -> Any:
        body, chunks, key = _sealed(i)
        body, chunks = _tamper(body, chunks, i.get("tamper", []))
        if i.get("wrong_key"):
            key = bytes.fromhex(i["wrong_key"])
        plain = open_all(
            body, chunks, key=key, key_ref=i["key"], chat=i.get("open_chat", i["chat"])
        )
        return {"length": len(plain), "sha256": crypto.sha256_id(plain)}

    @op("attachment_serve")
    def _serve(i: dict[str, Any]) -> Any:
        return chunks_for_peer(
            i["attachments"], i["requested"], peer_in_phi_group=i["peer_in_phi_group"]
        )

