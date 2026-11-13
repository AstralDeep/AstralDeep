"""Event headers, bodies and wire envelopes: building, signing, sealing, opening and structural checks.
The feed chain rules live in feed.py and the transport messages in sync.py, both of which call this module.
"""

from __future__ import annotations

import re
from typing import Any

from . import crypto
from .cj import b64url_decode, b64url_encode, canonical, strict_loads
from .errors import MeshError

MAX_SKEW_MS = 300_000
INTEGRITY = ("owner", "tool-trusted", "untrusted")
CONFIDENTIALITY = ("public", "private", "phi", "secret")
EVENT_TYPES = {
    "chat.create": "chat",
    "chat.user_message": "chat",
    "chat.assistant_message": "chat",
    "chat.model_request": "chat",
    "chat.model_response": "chat",
    "chat.tool_proposed": "chat",
    "chat.tool_authorized": "chat",
    "chat.tool_result": "chat",
    "chat.turn_start": "chat",
    "chat.turn_end": "chat",
    "chat.turn_outcome_unknown": "chat",
    "chat.spoken": "chat",
    "chat.attachment": "chat",
    "request.send": "request",
    "request.claim": "request",
    "request.refuse": "request",
    "request.cancel": "request",
    "ui.surface_put": "ui",
    "ui.patch": "ui",
    "ui.action": "ui",
    "auth.grant": "authority",
    "auth.revoke": "authority",
    "auth.narrow": "authority",
    "auth.policy": "authority",
    "auth.consent": "authority",
    "auth.receipt": "authority",
    "auth.transfer": "authority",
    "run.lease": "run",
    "run.goal": "run",
    "run.schedule": "run",
    "learn.proposal": "learning",
    "learn.adoption": "learning",
    "learn.reversal": "learning",
    "mem.remember": "memory",
    "mem.supersede": "memory",
    "settings.set": "memory",
    "key.content": "key",
    "key.credential": "key",
    "tools.catalog": "tools",
}
HEADER_FIELDS = {
    "v",
    "feed",
    "seq",
    "prev",
    "after",
    "clock",
    "scope",
    "type",
    "labels",
    "key",
    "body",
    "sig",
}
_CK = re.compile(r"^ck:[A-Za-z0-9_-]{22}$")
_EK = re.compile(r"^ek:(personal|phi):(sha256:[0-9a-f]{64})$")


def make_clock(ms: int, n: int = 0) -> dict[str, int]:
    return {"ms": ms, "n": n}


def clock_key(clock: dict[str, int]) -> tuple[int, int]:
    return (clock["ms"], clock["n"])


def event_id(header: dict[str, Any]) -> str:
    return crypto.object_id(header)


def new_content_key_ref(raw16: bytes) -> str:
    return "ck:" + b64url_encode(raw16)


def epoch_key_ref(group: str, epoch_id: str) -> str:
    return f"ek:{group}:{epoch_id}"


def build_header(
    ops_seed: bytes,
    *,
    seq: int,
    prev: str | None,
    after: list[str],
    clock: dict[str, int],
    scope: str,
    type: str,
    labels: dict[str, str],
    key: str,
    body_hash: str,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "v": 1,
        "feed": b64url_encode(crypto.ed25519_public(ops_seed)),
        "seq": seq,
        "prev": prev,
        "after": sorted(after),
        "clock": clock,
        "scope": scope,
        "type": type,
        "labels": labels,
        "key": key,
        "body": body_hash,
    }
    header["sig"] = crypto.sign_object("ed25519", ops_seed, "astral.event/v1", header)
    return header


def check_header_shape(header: Any) -> None:
    if not isinstance(header, dict) or set(header) != HEADER_FIELDS:
        raise MeshError("event.malformed", "header fields")
    if header["v"] != 1:
        raise MeshError("event.malformed", "version")
    try:
        feed = b64url_decode(header["feed"])
    except MeshError as exc:
        raise MeshError("event.malformed", "feed") from exc
    if len(feed) != 32:
        raise MeshError("event.malformed", "feed length")
    seq = header["seq"]
    if type(seq) is not int or seq < 1:
        raise MeshError("event.malformed", "seq")
    if (seq == 1) != (header["prev"] is None) or (
        header["prev"] is not None and not crypto.is_id(header["prev"])
    ):
        raise MeshError("event.malformed", "prev")
    after = header["after"]
    if (
        not isinstance(after, list)
        or any(not crypto.is_id(a) for a in after)
        or after != sorted(set(after))
        or (header["prev"] is not None and header["prev"] in after)
    ):
        raise MeshError("event.malformed", "after")
    clock = header["clock"]
    if (
        not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(type(clock[k]) is not int or clock[k] < 0 for k in clock)
    ):
        raise MeshError("event.malformed", "clock")
    if header["type"] not in EVENT_TYPES:
        raise MeshError("event.unknown_type", str(header["type"]))
    scope = header["scope"]
    if header["type"] == "chat.create":
        if scope != "new":
            raise MeshError("event.malformed", "chat.create scope must be new")
    elif scope != "mesh" and not (crypto.is_id(scope)):
        raise MeshError("event.malformed", "scope")
    if EVENT_TYPES[header["type"]] in ("chat", "request") and scope == "mesh":
        raise MeshError("event.malformed", "chat events need a chat scope")
    if EVENT_TYPES[header["type"]] == "tools" and scope != "mesh":
        raise MeshError("event.malformed", "tool catalogs have the mesh scope")
    labels = header["labels"]
    if (
        not isinstance(labels, dict)
        or set(labels) != {"confidentiality", "integrity"}
        or labels["integrity"] not in INTEGRITY
        or labels["confidentiality"] not in CONFIDENTIALITY
    ):
        raise MeshError("event.malformed", "labels")
    key = header["key"]
    if not isinstance(key, str) or not (_CK.match(key) or _EK.match(key)):
        raise MeshError("event.malformed", "key")
    if not crypto.is_id(header["body"]):
        raise MeshError("event.malformed", "body")


def verify_header(header: Any, *, now_ms: int | None = None) -> str:
    check_header_shape(header)
    if not crypto.verify_object(
        "ed25519", b64url_decode(header["feed"]), "astral.event/v1", header
    ):
        raise MeshError("event.bad_signature", "header signature")
    if now_ms is not None and header["clock"]["ms"] > now_ms + MAX_SKEW_MS:
        raise MeshError("event.future_clock", "ahead by more than five minutes")
    return event_id(header)


def body_aad(header: dict[str, Any]) -> bytes:
    return canonical(
        {
            "feed": header["feed"],
            "key": header["key"],
            "scope": header["scope"],
            "seq": header["seq"],
        }
    )


def seal_body(
    content_key: bytes, header_fields: dict[str, Any], plaintext: bytes, nonce: bytes
) -> bytes:
    aad = canonical(
        {
            "feed": header_fields["feed"],
            "key": header_fields["key"],
            "scope": header_fields["scope"],
            "seq": header_fields["seq"],
        }
    )
    return nonce + crypto.aead_seal(content_key, nonce, plaintext, aad)


def open_body(content_key: bytes, header: dict[str, Any], body_ct: bytes) -> bytes:
    if len(body_ct) < crypto.NONCE_LEN + crypto.TAG_LEN:
        raise MeshError("event.malformed", "body too short")
    if crypto.sha256_id(body_ct) != header["body"]:
        raise MeshError("event.body_mismatch", "body hash")
    try:
        return crypto.aead_open(
            content_key,
            body_ct[: crypto.NONCE_LEN],
            body_ct[crypto.NONCE_LEN :],
            body_aad(header),
        )
    except MeshError as exc:
        raise MeshError("event.decrypt_failed", "body") from exc


def header_aad(feed: str, seq: int, epoch_id: str) -> bytes:
    return canonical({"epoch": epoch_id, "feed": feed, "seq": seq})


def seal_header(
    epoch_key: bytes, epoch_id: str, header: dict[str, Any], nonce: bytes
) -> bytes:
    sealed = crypto.aead_seal(
        epoch_key,
        nonce,
        canonical(header),
        header_aad(header["feed"], header["seq"], epoch_id),
    )
    return crypto.id_bytes(epoch_id) + nonce + sealed


def seal_event(
    ops_seed: bytes,
    *,
    seq: int,
    prev: str | None,
    after: list[str],
    clock: dict[str, int],
    scope: str,
    type: str,
    labels: dict[str, str],
    key: str,
    content_key: bytes,
    body: Any,
    epoch_id: str,
    epoch_key: bytes,
    body_nonce: bytes,
    header_nonce: bytes,
) -> dict[str, Any]:
    feed = b64url_encode(crypto.ed25519_public(ops_seed))
    plain = canonical(body)
    body_ct = seal_body(
        content_key,
        {"feed": feed, "key": key, "scope": scope, "seq": seq},
        plain,
        body_nonce,
    )
    header = build_header(
        ops_seed,
        seq=seq,
        prev=prev,
        after=after,
        clock=clock,
        scope=scope,
        type=type,
        labels=labels,
        key=key,
        body_hash=crypto.sha256_id(body_ct),
    )
    return {
        "feed": feed,
        "seq": seq,
        "header_ct": b64url_encode(
            seal_header(epoch_key, epoch_id, header, header_nonce)
        ),
        "body_ct": b64url_encode(body_ct),
    }


def open_header(
    envelope: Any, epoch_keys: dict[str, bytes], *, now_ms: int | None = None
) -> tuple[dict[str, Any], str]:
    if not isinstance(envelope, dict) or set(envelope) != {
        "feed",
        "seq",
        "header_ct",
        "body_ct",
    }:
        raise MeshError("event.malformed", "envelope fields")
    try:
        raw = b64url_decode(envelope["header_ct"])
    except MeshError as exc:
        raise MeshError("event.malformed", "header_ct") from exc
    if len(raw) < 32 + crypto.NONCE_LEN + crypto.TAG_LEN:
        raise MeshError("event.malformed", "header_ct too short")
    epoch_id = "sha256:" + raw[:32].hex()
    if epoch_id not in epoch_keys:
        raise MeshError("event.epoch_unknown", epoch_id)
    try:
        plain = crypto.aead_open(
            epoch_keys[epoch_id],
            raw[32 : 32 + crypto.NONCE_LEN],
            raw[32 + crypto.NONCE_LEN :],
            header_aad(envelope["feed"], envelope["seq"], epoch_id),
        )
    except MeshError as exc:
        raise MeshError("event.decrypt_failed", "header") from exc
    header = strict_loads(plain)
    verify_header(header, now_ms=now_ms)
    if header["feed"] != envelope["feed"] or header["seq"] != envelope["seq"]:
        raise MeshError(
            "event.envelope_mismatch", "feed or seq differs from the sealed header"
        )
    return header, epoch_id


def open_event(
    envelope: dict[str, Any],
    epoch_keys: dict[str, bytes],
    content_keys: dict[str, bytes],
    *,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], Any]:
    header, _ = open_header(envelope, epoch_keys, now_ms=now_ms)
    body_ct = envelope["body_ct"]
    if body_ct is None:
        return header, None
    key = header["key"]
    secret = content_keys.get(key)
    if secret is None:
        raise MeshError("event.key_unknown", key)
    return header, strict_loads(open_body(secret, header, b64url_decode(body_ct)))
