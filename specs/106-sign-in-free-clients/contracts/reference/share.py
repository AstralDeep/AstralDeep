"""Reference for the share session: its messages and limits, admission, the served-type selection over a chat, the wraps, the share envelope seal and open, the chunk policy and the receiver steps.
It reuses the contacts reference for rosters, following and reader checks, and the preserved 105 helpers for events, attachments and wrapping, and vectors/share-v1.json pins it for every other implementation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "105-decentralized-personal-mesh/contracts/reference"
    ),
)
import contacts
from astral_mesh_ref import attachments, crypto
from astral_mesh_ref import event as mesh_event
from astral_mesh_ref import fold as mesh_fold
from astral_mesh_ref import testkit
from astral_mesh_ref.cj import b64url_decode, b64url_encode, canonical, strict_loads
from astral_mesh_ref.errors import MeshError

ALPN = "astral/share/1"
FRAME_LIMIT = 8 * 1024 * 1024
HEADER_INFO = b"astral.share.header/v1"
SESSION_KINDS = ("contact", "read", "verify", "nudge")
SERVER_KINDS = ("desktop", "pocket", "node")
MAX_RANGES = 64
MAX_SPAN = 512
MAX_ENVELOPES = 256
MAX_ROSTER_IDS = 256
MAX_ROSTER_ENTRIES = 256
MAX_HASHES = 256
MAX_GRANTS_PER_MESSAGE = 256
MAX_KEYS = 64
MAX_FEEDS = 256
MAX_TITLE = 256
PRE_ADMISSION = {"rounds": 4, "frames": 16, "bytes": 1024 * 1024}
NONCE_BYTES = 16
CONFIDENTIALITY = mesh_event.CONFIDENTIALITY
INTEGRITY = mesh_event.INTEGRITY
CK = re.compile(r"ck:[A-Za-z0-9_-]{22}")
EK = re.compile(r"ek:(personal|phi):sha256:[0-9a-f]{64}")

SERVED_FIELDS: dict[str, frozenset[str] | None] = {
    "chat.create": frozenset({"title"}),
    "chat.user_message": frozenset({"text", "base", "attachments", "steered"}),
    "chat.assistant_message": frozenset({"turn", "text", "notice"}),
    "chat.spoken": frozenset({"kind", "text", "turn"}),
    "chat.attachment": frozenset({"name", "mime", "size", "sha256", "chunks"}),
    "chat.turn_start": frozenset({"continues", "message", "cancelled"}),
    "chat.turn_end": frozenset({"turn", "reason", "code"}),
    "chat.turn_outcome_unknown": frozenset({"turn", "calls"}),
    "ui.surface_put": frozenset({"document", "turn", "source"}),
    "ui.patch": frozenset({"surface", "ops"}),
    "chat.tool_result": None,
}
SURFACE_TYPES = ("ui.surface_put", "ui.patch")
TOOL_RESULT = "chat.tool_result"

HELLO_BASE = {"t", "v", "kind", "mesh", "device", "roster", "nonce"}
HELLO_FIELDS = {
    "contact": HELLO_BASE | {"invite", "name"},
    "read": HELLO_BASE | {"phi"},
    "verify": HELLO_BASE | {"name"},
    "nudge": set(HELLO_BASE),
}
GRANT_ENTRY_FIELDS = {
    "id",
    "chat",
    "title",
    "phi",
    "withheld_phi",
    "withheld_fields",
    "withheld_keys",
    "supersedes",
    "heads",
    "wraps",
}
ENVELOPE_FIELDS = {"grant", "key", "feed", "seq", "header_ct", "body_ct"}
HEADER_FIELDS = mesh_event.HEADER_FIELDS
SENDERS = {
    "hello": "both",
    "roster_want": "both",
    "roster": "both",
    "grants_want": "reader",
    "grants": "server",
    "want": "reader",
    "events": "server",
    "chunks_want": "reader",
    "chunks": "server",
    "revoked": "server",
    "error": "both",
    "bye": "both",
}
OPEN_MESSAGES = ("hello", "roster_want", "roster", "error", "bye")
CHUNK_REASONS = ("phi", "unknown", "secret")

FRAME_CODES = ("share.malformed", "share.too_large")
ADMISSION_CODES = (
    "share.invite_unknown",
    "share.contact_unknown",
    "share.contact_unverified",
    "share.roster_incomplete",
    "share.peer_unknown",
    "share.server_kind",
    "share.unexpected_message",
    "contact.self",
)
SERVING_CODES = (
    "share.phi_withheld",
    "share.body_field_refused",
    "share.too_many_keys",
    "share.chunk_not_named",
)
RECEIVER_CODES = (
    "share.malformed",
    "share.grant_unknown",
    "share.key_unknown",
    "wrap.decrypt_failed",
    "share.decrypt_failed",
    "event.malformed",
    "share.envelope_mismatch",
    "share.bad_signature",
    "share.feed_unknown",
    "share.scope_mismatch",
    "share.type_not_allowed",
    "share.label_unknown",
    "share.secret_refused",
    "event.body_mismatch",
    "event.decrypt_failed",
)
RECEIVER_STEPS = (
    "share.malformed",
    "share.grant_unknown",
    "share.key_unknown",
    "share.decrypt_failed",
    "event.malformed",
    "share.envelope_mismatch",
    "share.bad_signature",
    "share.feed_unknown",
    "share.scope_mismatch",
    "share.type_not_allowed",
    "share.label_unknown",
    "event.body_mismatch",
    "event.decrypt_failed",
    "event.malformed",
    "share.body_field_refused",
)
CODES = tuple(
    dict.fromkeys(
        FRAME_CODES
        + ADMISSION_CODES
        + contacts.READER_CODES
        + contacts.FOLLOW_CODES
        + SERVING_CODES
        + RECEIVER_CODES
        + ("attachment.chunk_hash",)
    )
)


def _fail(code: str, detail: str = "") -> MeshError:
    return MeshError(code, detail)


def _int(value: Any, low: int = 0) -> bool:
    return type(value) is int and value >= low


def _bytes_b64(value: Any, length: int | None = None) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        raw = b64url_decode(value)
    except MeshError:
        return None
    return raw if length is None or len(raw) == length else None


def _hex32(value: Any) -> bool:
    return isinstance(value, str) and bool(contacts.HEX32.fullmatch(value))


def _sorted_unique(items: Any, limit: int, check: Any) -> bool:
    return (
        isinstance(items, list)
        and all(check(i) for i in items)
        and all(a < b for a, b in zip(items, items[1:]))
        and len(items) <= limit
    )


def derive_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    kept = "".join(c for c in title if not contacts.caller_ticket.refused_in_label(c))
    return kept[:MAX_TITLE]


def _title_ok(title: Any) -> bool:
    return (
        isinstance(title, str)
        and len(title) <= MAX_TITLE
        and not any(contacts.caller_ticket.refused_in_label(c) for c in title)
    )


def _wrap_ok(wrap: Any) -> bool:
    return (
        isinstance(wrap, dict)
        and set(wrap) == {"epk", "nonce", "ct"}
        and _bytes_b64(wrap["epk"], 32) is not None
        and _bytes_b64(wrap["nonce"], crypto.NONCE_LEN) is not None
        and _bytes_b64(wrap["ct"], crypto.KEY_LEN + crypto.TAG_LEN) is not None
    )


def _check_hello(message: dict[str, Any]) -> None:
    fields = HELLO_FIELDS.get(message.get("kind"))
    if fields is None or set(message) != fields or message["v"] != 1:
        raise _fail("share.malformed", "hello fields")
    roster = message["roster"]
    if (
        not crypto.is_id(message["mesh"])
        or not crypto.is_id(message["device"])
        or not isinstance(roster, dict)
        or set(roster) != {"heads", "count"}
        or not _sorted_unique(roster["heads"], contacts.MAX_MARK_HEADS, crypto.is_id)
        or not _int(roster["count"])
        or roster["count"] < len(roster["heads"])
        or _bytes_b64(message["nonce"], NONCE_BYTES) is None
    ):
        raise _fail("share.malformed", "hello values")
    if "phi" in message and type(message["phi"]) is not bool:
        raise _fail("share.malformed", "hello phi")
    if "invite" in message and _bytes_b64(message["invite"], NONCE_BYTES) is None:
        raise _fail("share.malformed", "hello invite")
    if "name" in message and not contacts._name_ok(message["name"]):
        raise _fail("share.malformed", "hello name")


def _check_range_list(ranges: Any) -> None:
    if not isinstance(ranges, list):
        raise _fail("share.malformed", "ranges")
    if len(ranges) > MAX_RANGES:
        raise _fail("share.too_large", "ranges")
    last: tuple[str, int] | None = None
    for item in ranges:
        if (
            not isinstance(item, dict)
            or set(item) != {"feed", "from", "to"}
            or _bytes_b64(item["feed"], 32) is None
            or not _int(item["from"], 1)
            or not _int(item["to"])
            or item["to"] < item["from"]
        ):
            raise _fail("share.malformed", "range")
        if item["to"] - item["from"] + 1 > MAX_SPAN:
            raise _fail("share.too_large", "range span")
        if last is not None and (item["feed"], item["from"]) <= last:
            raise _fail("share.malformed", "range order")
        last = (item["feed"], item["to"])


def _check_heads(heads: Any) -> None:
    if (
        not isinstance(heads, dict)
        or len(heads) > MAX_FEEDS
        or any(_bytes_b64(f, 32) is None or not _int(s, 1) for f, s in heads.items())
    ):
        raise _fail("share.malformed", "heads")


def _check_wraps(wraps: Any) -> None:
    if not isinstance(wraps, list):
        raise _fail("share.malformed", "wraps")
    if len(wraps) > MAX_KEYS:
        raise _fail("share.too_many_keys", str(len(wraps)))
    keys = []
    for item in wraps:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "wrap"}
            or not isinstance(item["key"], str)
            or not CK.fullmatch(item["key"])
            or not _wrap_ok(item["wrap"])
        ):
            raise _fail("share.malformed", "wrap")
        keys.append(item["key"])
    if keys != sorted(set(keys)):
        raise _fail("share.malformed", "wrap order")


def check_grant_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != GRANT_ENTRY_FIELDS:
        raise _fail("share.malformed", "grant entry fields")
    supersedes = entry["supersedes"]
    if (
        not _hex32(entry["id"])
        or not crypto.is_id(entry["chat"])
        or not _title_ok(entry["title"])
        or type(entry["phi"]) is not bool
        or not all(
            _int(entry[f]) for f in ("withheld_phi", "withheld_fields", "withheld_keys")
        )
        or (supersedes is not None and (not _hex32(supersedes) or supersedes == entry["id"]))
    ):
        raise _fail("share.malformed", "grant entry values")
    _check_heads(entry["heads"])
    _check_wraps(entry["wraps"])
    return entry


def check_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        raise _fail("share.malformed", "envelope fields")
    if (
        not _hex32(envelope["grant"])
        or not isinstance(envelope["key"], str)
        or not CK.fullmatch(envelope["key"])
        or _bytes_b64(envelope["feed"], 32) is None
        or not _int(envelope["seq"], 1)
        or _bytes_b64(envelope["header_ct"]) is None
        or _bytes_b64(envelope["body_ct"]) is None
    ):
        raise _fail("share.malformed", "envelope values")
    return envelope


def _check_list(message: dict[str, Any], field: str, limit: int, check: Any) -> None:
    items = message[field]
    if not isinstance(items, list):
        raise _fail("share.malformed", field)
    if len(items) > limit:
        raise _fail("share.too_large", field)
    for item in items:
        check(item)


def _id_check(item: Any) -> None:
    if not crypto.is_id(item):
        raise _fail("share.malformed", "id")


def _hex_check(item: Any) -> None:
    if not _hex32(item):
        raise _fail("share.malformed", "grant id")


def _chunk_check(item: Any) -> None:
    if (
        not isinstance(item, dict)
        or set(item) != {"hash", "ct"}
        or not crypto.is_id(item["hash"])
        or _bytes_b64(item["ct"]) is None
    ):
        raise _fail("share.malformed", "chunk")


def _withheld_check(item: Any) -> None:
    if (
        not isinstance(item, dict)
        or set(item) != {"hash", "reason"}
        or not crypto.is_id(item["hash"])
        or item["reason"] not in CHUNK_REASONS
    ):
        raise _fail("share.malformed", "withheld")


def _roster_entry_check(item: Any) -> None:
    if not isinstance(item, str):
        raise _fail("share.malformed", "roster entry")


def _check_unique(message: dict[str, Any], field: str) -> None:
    items = message[field]
    if items != sorted(set(items)):
        raise _fail("share.malformed", field + " order")


def check_message(message: Any) -> str:
    if not isinstance(message, dict) or message.get("t") not in SENDERS:
        raise _fail("share.malformed", "kind")
    t = message["t"]
    if t == "hello":
        _check_hello(message)
        return t
    shapes: dict[str, tuple[set[str], Any]] = {
        "roster_want": ({"t", "ids"}, lambda: _id_list(message)),
        "roster": ({"t", "entries"}, lambda: _check_list(message, "entries", MAX_ROSTER_ENTRIES, _roster_entry_check)),
        "grants_want": ({"t", "grants"}, lambda: _grant_ids(message)),
        "grants": ({"t", "grants"}, lambda: _grants_list(message)),
        "want": ({"t", "grant", "ranges"}, lambda: _want(message)),
        "events": ({"t", "grant", "events"}, lambda: _events(message)),
        "chunks_want": ({"t", "grant", "hashes"}, lambda: _hash_list(message)),
        "chunks": ({"t", "grant", "chunks", "withheld"}, lambda: _chunks(message)),
        "revoked": ({"t", "grants"}, lambda: _grant_ids(message)),
        "error": ({"t", "code"}, lambda: _error(message)),
        "bye": ({"t"}, lambda: None),
    }
    fields, check = shapes[t]
    if set(message) != fields:
        raise _fail("share.malformed", t + " fields")
    check()
    return t


def _id_list(message: dict[str, Any]) -> None:
    _check_list(message, "ids", MAX_ROSTER_IDS, _id_check)
    _check_unique(message, "ids")


def _grant_ids(message: dict[str, Any]) -> None:
    _check_list(message, "grants", MAX_GRANTS_PER_MESSAGE, _hex_check)
    _check_unique(message, "grants")


def _grants_list(message: dict[str, Any]) -> None:
    _check_list(message, "grants", MAX_GRANTS_PER_MESSAGE, check_grant_entry)
    ids = [g["id"] for g in message["grants"]]
    if ids != sorted(set(ids)):
        raise _fail("share.malformed", "grants order")


def _want(message: dict[str, Any]) -> None:
    _hex_check(message["grant"])
    _check_range_list(message["ranges"])


def _events(message: dict[str, Any]) -> None:
    _hex_check(message["grant"])
    _check_list(message, "events", MAX_ENVELOPES, check_envelope)


def _hash_list(message: dict[str, Any]) -> None:
    _hex_check(message["grant"])
    _check_list(message, "hashes", MAX_HASHES, _id_check)
    _check_unique(message, "hashes")


def _chunks(message: dict[str, Any]) -> None:
    _hex_check(message["grant"])
    _check_list(message, "chunks", MAX_HASHES, _chunk_check)
    _check_list(message, "withheld", MAX_HASHES, _withheld_check)


def _error(message: dict[str, Any]) -> None:
    if message["code"] not in CODES:
        raise _fail("share.malformed", "error code")


def frame(message: dict[str, Any]) -> bytes:
    check_message(message)
    body = canonical(message)
    if len(body) > FRAME_LIMIT:
        raise _fail("share.too_large", "frame")
    return len(body).to_bytes(4, "big") + body


def unframe(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise _fail("share.malformed", "frame length")
    length = int.from_bytes(data[:4], "big")
    if length > FRAME_LIMIT:
        raise _fail("share.too_large", "frame")
    if length != len(data) - 4:
        raise _fail("share.malformed", "frame length")
    try:
        message = strict_loads(data[4:])
        canonical_text = canonical(message)
    except MeshError as error:
        raise _fail("share.malformed", error.code) from error
    if canonical_text != data[4:]:
        raise _fail("share.malformed", "not canonical")
    check_message(message)
    return message


def message_allowed(kind: str, admitted: bool, sender: str, t: str) -> None:
    if kind not in SESSION_KINDS or t not in SENDERS or sender not in ("reader", "server"):
        raise _fail("share.malformed", "session")
    direction = SENDERS[t]
    if direction not in ("both", sender):
        raise _fail("share.unexpected_message", t)
    if t in OPEN_MESSAGES:
        return
    if not admitted or kind != "read":
        raise _fail("share.unexpected_message", t)


def pre_admission(rounds: list[dict[str, int]]) -> dict[str, Any]:
    if len(rounds) > PRE_ADMISSION["rounds"]:
        raise _fail("share.too_large", "rounds")
    for item in rounds:
        if item["frames"] > PRE_ADMISSION["frames"] or item["bytes"] > PRE_ADMISSION["bytes"]:
            raise _fail("share.too_large", "round")
    return {}


def admit(kind: str, hello: dict[str, Any], peer_feed: str, view: dict[str, Any]) -> dict[str, Any]:
    if kind not in SESSION_KINDS:
        raise _fail("share.malformed", "session kind")
    check_message(hello)
    if hello["kind"] != kind:
        raise _fail("share.malformed", "hello kind")
    if hello["mesh"] == view["own_mesh"]:
        raise _fail("contact.self")
    if kind == "contact":
        return _admit_contact(hello, view)
    record = view["contact"]
    if record is None or record["mesh"] != hello["mesh"]:
        raise _fail("share.contact_unknown", hello["mesh"])
    if kind != "verify" and record["state"] != "verified":
        raise _fail("share.contact_unverified", hello["mesh"])
    roster = contacts.roster_state(view["entries"], hello["mesh"])
    missing = [h for h in hello["roster"]["heads"] if h not in roster["usable"]]
    if missing:
        raise _fail("share.roster_incomplete", ",".join(missing))
    members = roster["fold"]["members"]
    member = members.get(hello["device"])
    if member is None or peer_feed not in member["feeds"]:
        raise _fail("share.peer_unknown", hello["device"])
    result = {
        "admitted": True,
        "kind": kind,
        "mesh": hello["mesh"],
        "device": hello["device"],
        "member_kind": member["kind"],
        "phi": False,
    }
    if kind == "verify":
        return result
    followed = contacts.follow(hello["mesh"], record, view["entries"])
    if not followed["follows"]:
        raise _fail(followed["reason"] or "contact.roster_rejected")
    if kind == "nudge" and member["kind"] not in SERVER_KINDS:
        raise _fail("share.server_kind", member["kind"])
    reader = {"device": hello["device"], "kind": member["kind"], "entries": roster["usable"]}
    if kind == "read":
        contacts.reader_check(view["mark"], reader)
    else:
        _mark_check(view["mark"], reader)
    if kind == "read":
        result["phi"] = bool(hello["phi"]) and hello["device"] in roster["fold"]["phi_members"]
    return result


def _mark_check(mark: dict[str, Any] | None, reader: dict[str, Any]) -> None:
    if mark is None:
        return
    if reader["device"] in mark["removed"]:
        raise _fail("share.device_removed", reader["device"])
    if not set(mark["heads"]) <= set(reader["entries"]):
        raise _fail("share.roster_stale")


def _admit_contact(hello: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    open_nonces = {i["nonce"] for i in view["invites"] if view["now_s"] < i["expires_s"]}
    if hello["invite"] not in open_nonces:
        raise _fail("share.invite_unknown")
    return {
        "admitted": True,
        "kind": "contact",
        "mesh": hello["mesh"],
        "device": hello["device"],
        "member_kind": None,
        "phi": False,
    }


def chat_keys(chat: str, create: dict[str, Any], key_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    held: dict[str, dict[str, Any]] = {}
    for item in sorted(key_events, key=_order):
        body = item["body"]
        header = item["header"]
        match = EK.fullmatch(header["key"])
        if header["type"] != "key.content" or match is None:
            continue
        if not (
            isinstance(body, dict)
            and all(isinstance(body.get(f), str) for f in ("id", "key", "scope"))
        ):
            raise _fail("event.malformed", "key.content body")
        if body["scope"] == chat or (body["scope"] == "new" and body["id"] == create["header"]["key"]):
            held[body["id"]] = {
                "ref": body["id"],
                "secret": body["key"],
                "group": match.group(1),
                "order": list(_order(item)),
            }
    return sorted(held.values(), key=lambda k: k["ref"])


def _order(item: dict[str, Any]) -> tuple[int, int, str, int]:
    return mesh_fold.order_key(item["header"])


def _event_checks(
    item: dict[str, Any], chat: str, event_id: str, phi: bool, wrapped: set[str]
) -> str | None:
    header, body = item["header"], item["body"]
    allowed_fields = SERVED_FIELDS.get(header["type"], False)
    if header["scope"] != chat and not (header["type"] == "chat.create" and event_id == chat):
        return "share.scope_mismatch"
    if allowed_fields is False:
        return "share.type_not_allowed"
    labels = header["labels"]
    if labels["confidentiality"] not in CONFIDENTIALITY or labels["integrity"] not in INTEGRITY:
        return "share.label_unknown"
    if labels["confidentiality"] == "secret":
        return "share.secret_refused"
    if labels["confidentiality"] == "phi" and not phi:
        return "share.phi_withheld"
    if allowed_fields is not None and not set(body) <= allowed_fields:
        return "share.body_field_refused"
    if header["key"] not in wrapped:
        return "share.key_unknown"
    return None


def _walk(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        bind = value.get("$bind")
        if set(value) == {"$bind"} and isinstance(bind, dict) and isinstance(bind.get("event"), str):
            found.add(bind["event"])
        for child in value.values():
            _walk(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk(child, found)


def bound_results(surfaces: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for item in surfaces:
        _walk(item["body"], found)
        source = item["body"].get("source") if item["header"]["type"] == "ui.surface_put" else None
        if isinstance(source, dict) and isinstance(source.get("result"), str):
            found.add(source["result"])
    return found


def select_served(chat: str, events: list[dict[str, Any]], phi: bool, wrapped: list[str]) -> dict[str, Any]:
    refused: dict[str, str] = {}
    served: list[dict[str, Any]] = []
    held = set(wrapped)
    ordered = sorted(events, key=_order)
    pending = []
    for item in ordered:
        event_id = crypto.object_id(item["header"])
        if item["header"]["type"] == TOOL_RESULT:
            pending.append((event_id, item))
            continue
        code = _event_checks(item, chat, event_id, phi, held)
        if code is None:
            served.append(item)
        else:
            refused[event_id] = code
    bound = bound_results([i for i in served if i["header"]["type"] in SURFACE_TYPES])
    for event_id, item in pending:
        code = _event_checks(item, chat, event_id, phi, held)
        if code is None and event_id not in bound:
            code = "share.type_not_allowed"
        if code is None:
            served.append(item)
        else:
            refused[event_id] = code
    served.sort(key=_order)
    return {
        "served": [crypto.object_id(i["header"]) for i in served],
        "refused": dict(sorted(refused.items())),
        "withheld_phi": sum(1 for c in refused.values() if c == "share.phi_withheld"),
        "withheld_fields": sum(1 for c in refused.values() if c == "share.body_field_refused"),
        "heads": _heads(served),
    }


def _heads(served: list[dict[str, Any]]) -> dict[str, int]:
    heads: dict[str, int] = {}
    for item in served:
        header = item["header"]
        heads[header["feed"]] = max(heads.get(header["feed"], 0), header["seq"])
    return dict(sorted(heads.items()))


def wrap_set(keys: list[dict[str, Any]], phi: bool) -> dict[str, Any]:
    eligible = sorted(
        (k for k in keys if phi or k["group"] != "phi"),
        key=lambda k: (k["order"], k["ref"]),
        reverse=True,
    )
    kept = eligible[:MAX_KEYS]
    return {
        "keys": sorted(kept, key=lambda k: k["ref"]),
        "withheld_keys": len(eligible) - len(kept),
    }


def wrap_aad(grant: str, key: str, recipient: str) -> dict[str, str]:
    return {"grant": grant, "key": key, "purpose": "share", "recipient": recipient}


def make_wraps(
    grant: str, recipient: str, wrap_public: bytes, keys: list[dict[str, Any]], rng: Any
) -> list[dict[str, Any]]:
    return [
        {
            "key": k["ref"],
            "wrap": crypto.wrap_key(
                wrap_public,
                b64url_decode(k["secret"]),
                wrap_aad(grant, k["ref"], recipient),
                rng.bytes(32),
                rng.bytes(crypto.NONCE_LEN),
            ),
        }
        for k in sorted(keys, key=lambda k: k["ref"])
    ]


def open_wrap(
    grant: str, key: str, recipient: str, wrap_private: bytes, wrap: dict[str, str]
) -> bytes:
    return crypto.unwrap_key(wrap_private, wrap, wrap_aad(grant, key, recipient))


def header_key(content_key: bytes, grant: str) -> bytes:
    return crypto.hkdf(content_key, HEADER_INFO, salt=bytes.fromhex(grant))


def header_aad(envelope: dict[str, Any]) -> bytes:
    return canonical(
        {
            "feed": envelope["feed"],
            "grant": envelope["grant"],
            "key": envelope["key"],
            "seq": envelope["seq"],
        }
    )


def seal_envelope(
    grant: str, content_key: bytes, header: Any, body_ct: bytes, nonce: bytes, *, key: str, feed: str, seq: int
) -> dict[str, Any]:
    envelope = {
        "grant": grant,
        "key": key,
        "feed": feed,
        "seq": seq,
        "header_ct": "",
        "body_ct": b64url_encode(body_ct),
    }
    sealed = crypto.aead_seal(
        header_key(content_key, grant), nonce, canonical(header), header_aad(envelope)
    )
    envelope["header_ct"] = b64url_encode(nonce + sealed)
    return envelope


def serve_envelopes(
    grant: str, items: list[dict[str, Any]], keys: dict[str, bytes], rng: Any
) -> list[dict[str, Any]]:
    return [
        seal_envelope(
            grant,
            keys[i["header"]["key"]],
            i["header"],
            b64url_decode(i["body_ct"]),
            rng.bytes(crypto.NONCE_LEN),
            key=i["header"]["key"],
            feed=i["header"]["feed"],
            seq=i["header"]["seq"],
        )
        for i in items
    ]


def _parse_header(plain: bytes) -> dict[str, Any]:
    try:
        header = strict_loads(plain)
    except MeshError as error:
        raise _fail("event.malformed", error.code) from error
    if not isinstance(header, dict) or set(header) != HEADER_FIELDS or header["v"] != 1:
        raise _fail("event.malformed", "header fields")
    labels, clock = header["labels"], header["clock"]
    if (
        _bytes_b64(header["feed"], 32) is None
        or not _int(header["seq"], 1)
        or (header["prev"] is not None and not crypto.is_id(header["prev"]))
        or (header["seq"] == 1) != (header["prev"] is None)
        or not isinstance(header["after"], list)
        or not all(crypto.is_id(a) for a in header["after"])
        or header["after"] != sorted(set(header["after"]))
        or not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or not _int(clock["ms"])
        or not _int(clock["n"])
        or not isinstance(header["scope"], str)
        or not isinstance(header["type"], str)
        or not isinstance(labels, dict)
        or set(labels) != {"confidentiality", "integrity"}
        or not all(isinstance(v, str) for v in labels.values())
        or not isinstance(header["key"], str)
        or not crypto.is_id(header["body"])
        or not isinstance(header["sig"], str)
    ):
        raise _fail("event.malformed", "header values")
    return header


def feed_allowed(feed: str, seq: int, members: dict[str, Any], removed: dict[str, Any]) -> bool:
    if any(feed in m["feeds"] for m in members.values()):
        return True
    for record in removed.values():
        if feed in record["feeds"]:
            return seq <= record.get("cutoff", {}).get(feed, 0)
    return False


def granter_roster(roster: dict[str, Any]) -> dict[str, Any]:
    state = roster["fold"]
    cut: dict[str, dict[str, int]] = {}
    for entry in roster["entries"].values():
        if entry["type"] == "remove" and entry["body"]["device"] in state["removed"]:
            seen = cut.setdefault(entry["body"]["device"], {})
            for feed, seq in entry["body"]["cutoff"].items():
                seen[feed] = min(seq, seen.get(feed, seq))
    return {
        "members": {d: {"feeds": list(m["feeds"])} for d, m in sorted(state["members"].items())},
        "removed": {d: {"feeds": sorted(c), "cutoff": dict(sorted(c.items()))} for d, c in sorted(cut.items())},
    }


def receive(envelope: Any, context: dict[str, Any]) -> dict[str, Any]:
    check_envelope(envelope)
    grant = context["grants"].get(envelope["grant"])
    if grant is None:
        raise _fail("share.grant_unknown", envelope["grant"])
    wrap = grant["wraps"].get(envelope["key"])
    if wrap is None:
        raise _fail("share.key_unknown", envelope["key"])
    content_key = open_wrap(
        envelope["grant"],
        envelope["key"],
        context["reader"]["device"],
        bytes.fromhex(context["reader"]["wrap_private_hex"]),
        wrap,
    )
    raw = b64url_decode(envelope["header_ct"])
    if len(raw) < crypto.NONCE_LEN + crypto.TAG_LEN:
        raise _fail("share.decrypt_failed", "header_ct too short")
    try:
        plain = crypto.aead_open(
            header_key(content_key, envelope["grant"]),
            raw[: crypto.NONCE_LEN],
            raw[crypto.NONCE_LEN :],
            header_aad(envelope),
        )
    except MeshError as error:
        raise _fail("share.decrypt_failed", "header") from error
    header = _parse_header(plain)
    if (
        header["key"] != envelope["key"]
        or header["feed"] != envelope["feed"]
        or header["seq"] != envelope["seq"]
    ):
        raise _fail("share.envelope_mismatch")
    if not crypto.verify_object(
        "ed25519", b64url_decode(header["feed"]), "astral.event/v1", header
    ):
        raise _fail("share.bad_signature")
    if not feed_allowed(header["feed"], header["seq"], context["members"], context["removed"]):
        raise _fail("share.feed_unknown", header["feed"])
    event_id = crypto.object_id(header)
    chat = grant["chat"]
    if header["scope"] != chat and not (header["type"] == "chat.create" and event_id == chat):
        raise _fail("share.scope_mismatch")
    if header["type"] not in SERVED_FIELDS:
        raise _fail("share.type_not_allowed", header["type"])
    labels = header["labels"]
    if labels["confidentiality"] not in CONFIDENTIALITY or labels["integrity"] not in INTEGRITY:
        raise _fail("share.label_unknown")
    if labels["confidentiality"] == "secret":
        raise _fail("share.secret_refused")
    body = mesh_event.open_body(content_key, header, b64url_decode(envelope["body_ct"]))
    try:
        value = strict_loads(body)
    except MeshError as error:
        raise _fail("event.malformed", error.code) from error
    allowed = SERVED_FIELDS[header["type"]]
    if allowed is not None and (not isinstance(value, dict) or not set(value) <= allowed):
        raise _fail("share.body_field_refused")
    return {
        "id": event_id,
        "feed": header["feed"],
        "seq": header["seq"],
        "type": header["type"],
        "clock": header["clock"],
        "labels": {"confidentiality": labels["confidentiality"], "integrity": "untrusted"},
        "header_labels": labels,
        "body": value,
    }


def require_in_effect(in_effect: list[dict[str, Any]], contact: str, grant: str) -> None:
    if not any(g["id"] == grant and g["contact"] == contact for g in in_effect):
        raise _fail("share.grant_unknown", grant)


def answer_want(
    held: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    in_effect: list[dict[str, Any]],
    contact: str,
    grant: str,
) -> dict[str, Any]:
    require_in_effect(in_effect, contact, grant)
    _check_range_list(ranges)
    by_feed: dict[str, list[dict[str, Any]]] = {}
    for item in held:
        by_feed.setdefault(item["feed"], []).append(item)
    out: list[str] = []
    for span in ranges:
        for item in sorted(by_feed.get(span["feed"], []), key=lambda i: i["seq"]):
            if span["from"] <= item["seq"] <= span["to"]:
                out.append(item["id"])
    return {"ids": out[:MAX_ENVELOPES], "more": len(out) > MAX_ENVELOPES}


def answer_revoked(named: list[str], folded: dict[str, Any]) -> dict[str, Any]:
    _check_list({"x": named}, "x", MAX_GRANTS_PER_MESSAGE, _hex_check)
    return {"revoked": sorted(set(named) & set(folded["revoked"]))}


def serve_chunks(
    attachments_served: dict[str, dict[str, Any]],
    requested: list[str],
    phi: bool,
    in_effect: list[dict[str, Any]],
    contact: str,
    grant: str,
) -> dict[str, Any]:
    require_in_effect(in_effect, contact, grant)
    if len(requested) > MAX_HASHES:
        raise _fail("share.too_large", "hashes")
    return attachments.chunks_for_peer(attachments_served, requested, peer_in_phi_group=phi)


def receive_chunks(message: dict[str, Any], named: list[str], requested: list[str]) -> dict[str, Any]:
    check_message(message)
    held = set(named)
    asked = set(requested)
    for item in message["withheld"]:
        if item["hash"] not in asked:
            raise _fail("share.malformed", "withheld hash not requested")
    kept: list[str] = []
    for chunk in message["chunks"]:
        if chunk["hash"] not in held or chunk["hash"] not in asked:
            raise _fail("share.chunk_not_named", chunk["hash"])
        if crypto.sha256_id(b64url_decode(chunk["ct"])) != chunk["hash"]:
            raise _fail("attachment.chunk_hash", chunk["hash"])
        kept.append(chunk["hash"])
    return {"kept": kept, "withheld": [w["hash"] for w in message["withheld"]]}


def grants_message(given: dict[str, Any], rng: Any) -> dict[str, Any]:
    reader = given["reader"]
    entries: list[dict[str, Any]] = []
    served: dict[str, list[str]] = {}
    refused: dict[str, dict[str, str]] = {}
    for grant in sorted(given["grants"], key=lambda g: g["id"]):
        if grant["contact"] != reader["mesh"]:
            continue
        chat = given["chats"][grant["chat"]]
        phi = bool(grant["phi"] and reader["phi"] and given["destination_allowed"])
        keys = chat_keys(grant["chat"], chat["create"], chat["key_events"])
        wraps = wrap_set(keys, phi)
        picked = select_served(
            grant["chat"],
            [chat["create"]] + chat["events"],
            phi,
            [k["ref"] for k in wraps["keys"]],
        )
        entries.append(
            {
                "id": grant["id"],
                "chat": grant["chat"],
                "title": derive_title(chat["title"]),
                "phi": phi,
                "withheld_phi": picked["withheld_phi"],
                "withheld_fields": picked["withheld_fields"],
                "withheld_keys": wraps["withheld_keys"],
                "supersedes": grant["supersedes"],
                "heads": picked["heads"],
                "wraps": make_wraps(
                    grant["id"],
                    reader["device"],
                    b64url_decode(reader["wrap"]),
                    wraps["keys"],
                    rng,
                ),
            }
        )
        served[grant["id"]] = picked["served"]
        refused[grant["id"]] = picked["refused"]
    message = {"t": "grants", "grants": entries}
    check_message(message)
    return {"message": message, "served": served, "refused": refused}


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as error:
        return {"error": error.code}


def run(kind: str, given: dict[str, Any]) -> dict[str, Any]:
    calls = {
        "share_message": lambda: {"t": check_message(given["message"])},
        "share_frame": lambda: {"hex": frame(given["message"]).hex()},
        "share_unframe": lambda: unframe(bytes.fromhex(given["hex"])),
        "share_allowed": lambda: message_allowed(
            given["kind"], given["admitted"], given["sender"], given["t"]
        ),
        "share_pre_admission": lambda: pre_admission(given["rounds"]),
        "share_admit": lambda: admit(given["kind"], given["hello"], given["peer_feed"], given["view"]),
        "share_select": lambda: select_served(
            given["chat"], given["events"], given["phi"], given["wrapped"]
        ),
        "share_keys": lambda: _keys_result(given),
        "share_grants": lambda: grants_message(given, testkit.DetRng(given["rng"])),
        "share_receive": lambda: receive(given["envelope"], given["context"]),
        "share_open_wrap": lambda: {
            "secret": open_wrap(
                given["grant"],
                given["key"],
                given["recipient"],
                bytes.fromhex(given["wrap_private_hex"]),
                given["wrap"],
            ).hex()
        },
        "share_header_key": lambda: {"key": header_key(bytes.fromhex(given["content_key_hex"]), given["grant"]).hex()},
        "share_want": lambda: answer_want(
            given["held"], given["ranges"], given["in_effect"], given["contact"], given["grant"]
        ),
        "share_granter_roster": lambda: granter_roster(given["roster"]),
        "share_title": lambda: {"title": derive_title(given["title"])},
        "share_revoked": lambda: answer_revoked(given["named"], given["fold"]),
        "share_chunks": lambda: serve_chunks(
            given["attachments"],
            given["requested"],
            given["phi"],
            given["in_effect"],
            given["contact"],
            given["grant"],
        ),
        "share_chunks_receive": lambda: receive_chunks(given["message"], given["named"], given["requested"]),
    }
    return _guard(calls[kind])


def _keys_result(given: dict[str, Any]) -> dict[str, Any]:
    keys = chat_keys(given["chat"], given["create"], given["key_events"])
    wrapped = wrap_set(keys, given["phi"])
    return {
        "keys": [k["ref"] for k in keys],
        "wrapped": [k["ref"] for k in wrapped["keys"]],
        "withheld_keys": wrapped["withheld_keys"],
    }
