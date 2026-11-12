"""Feed chain acceptance, fork and shortened-feed detection, checkpoints, sync planning and message validation.
It consumes event.py for headers and the roster's feed status, and it defines what peers exchange over astral/sync/1.
"""

from __future__ import annotations

from typing import Any

from . import crypto
from .cj import b64url_decode, canonical, strict_loads
from .errors import MeshError
from .event import clock_key, verify_header

MAX_RANGES = 64
MAX_SPAN = 512
MAX_EVENTS = 256
MAX_HASHES = 256
MESSAGE_TYPES = (
    "hello",
    "heads",
    "want",
    "roster",
    "events",
    "bodies_want",
    "bodies",
    "checkpoint",
    "fork",
    "error",
    "bye",
)


class EventStore:
    def __init__(self) -> None:
        self.chains: dict[str, list[dict[str, Any]]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.forked: dict[str, dict[str, Any]] = {}

    def head(self, feed: str) -> tuple[int, str | None]:
        chain = self.chains.get(feed, [])
        return (len(chain), crypto.object_id(chain[-1]) if chain else None)

    def heads(self) -> dict[str, dict[str, Any]]:
        return {
            feed: {"id": crypto.object_id(chain[-1]), "seq": len(chain)}
            for feed, chain in sorted(self.chains.items())
            if chain
        }

    def accept(
        self,
        header: dict[str, Any],
        status: dict[str, Any],
        *,
        now_ms: int | None = None,
    ) -> str:
        identifier = verify_header(header, now_ms=now_ms)
        feed = header["feed"]
        if feed in self.forked:
            raise MeshError("feed.forked", feed)
        state = status.get("state")
        if state not in ("member", "removed"):
            raise MeshError("feed.unknown", feed)
        if state == "removed" and header["seq"] > status.get("cutoff", 0):
            raise MeshError("feed.removed", feed)
        chain = self.chains.setdefault(feed, [])
        seq = header["seq"]
        if seq <= len(chain):
            existing = chain[seq - 1]
            if crypto.object_id(existing) == identifier:
                return "duplicate"
            self._mark_fork(feed, existing, header)
            raise MeshError("feed.fork", f"two events at {feed} seq {seq}")
        if seq > len(chain) + 1:
            raise MeshError("feed.gap", f"have {len(chain)}, got {seq}")
        if seq > 1:
            previous = chain[-1]
            if header["prev"] != crypto.object_id(previous):
                self._mark_fork(feed, previous, header)
                raise MeshError("feed.fork", "prev does not match the held head")
            if clock_key(header["clock"]) <= clock_key(previous["clock"]):
                raise MeshError("feed.clock_regress", "clock must rise within a feed")
        for dependency in header["after"]:
            known = self.by_id.get(dependency)
            if known is not None and clock_key(header["clock"]) <= clock_key(
                known["clock"]
            ):
                raise MeshError("feed.after_clock", dependency)
        chain.append(header)
        self.by_id[identifier] = header
        return "accepted"

    def _mark_fork(
        self, feed: str, first: dict[str, Any], second: dict[str, Any]
    ) -> None:
        self.forked[feed] = {"a": first, "b": second, "feed": feed}

    def missing_after(self) -> list[str]:
        missing = {
            d
            for header in self.by_id.values()
            for d in header["after"]
            if d not in self.by_id
        }
        return sorted(missing)


def build_checkpoint(
    mesh: str,
    feed: str,
    seq: int,
    head: str,
    by_device: str,
    identity_scalar: int,
    clock: dict[str, int],
) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "by": by_device,
        "clock": clock,
        "feed": feed,
        "head": head,
        "mesh": mesh,
        "seq": seq,
        "v": 1,
    }
    checkpoint["sig"] = crypto.sign_object(
        "p256", identity_scalar, "astral.checkpoint/v1", checkpoint
    )
    return checkpoint


def verify_checkpoint(checkpoint: Any, identity_public: bytes, mesh: str) -> None:
    fields = {"by", "clock", "feed", "head", "mesh", "seq", "sig", "v"}
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != fields
        or checkpoint["v"] != 1
    ):
        raise MeshError("checkpoint.malformed", "fields")
    if (
        type(checkpoint["seq"]) is not int
        or checkpoint["seq"] < 1
        or not crypto.is_id(checkpoint["head"])
    ):
        raise MeshError("checkpoint.malformed", "seq or head")
    if checkpoint["mesh"] != mesh:
        raise MeshError("checkpoint.wrong_mesh", checkpoint["mesh"])
    if crypto.device_id(identity_public) != checkpoint["by"]:
        raise MeshError(
            "checkpoint.wrong_signer", "key does not match the signing device"
        )
    if not crypto.verify_object(
        "p256", identity_public, "astral.checkpoint/v1", checkpoint
    ):
        raise MeshError("checkpoint.bad_signature", "signature")


def check_checkpoint_against(store: EventStore, checkpoint: dict[str, Any]) -> str:
    feed = checkpoint["feed"]
    if feed in store.forked:
        return "forked"
    seq, head = store.head(feed)
    if seq < checkpoint["seq"]:
        return "behind"
    held = store.chains[feed][checkpoint["seq"] - 1]
    if crypto.object_id(held) != checkpoint["head"]:
        raise MeshError(
            "checkpoint.contradicts_feed", f"{feed} seq {checkpoint['seq']}"
        )
    return "consistent"


def detect_shortened(
    remote_heads: dict[str, dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    evidence = []
    for checkpoint in checkpoints:
        remote = remote_heads.get(checkpoint["feed"])
        if remote is not None and remote["seq"] < checkpoint["seq"]:
            evidence.append({"checkpoint": checkpoint, "claimed_seq": remote["seq"]})
        elif (
            remote is not None
            and remote["seq"] == checkpoint["seq"]
            and remote["id"] != checkpoint["head"]
        ):
            evidence.append({"checkpoint": checkpoint, "claimed_id": remote["id"]})
    return evidence


def plan_wants(
    local: dict[str, dict[str, Any]], remote: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    ranges = []
    for feed in sorted(remote):
        have = local.get(feed, {"seq": 0})["seq"]
        want = remote[feed]["seq"]
        start = have + 1
        while start <= want:
            end = min(want, start + MAX_SPAN - 1)
            ranges.append({"feed": feed, "from": start, "to": end})
            start = end + 1
    return ranges[:MAX_RANGES]


def plan_sends(
    local: dict[str, dict[str, Any]], wants: list[dict[str, Any]]
) -> list[tuple[str, int]]:
    sends = []
    for item in wants:
        top = local.get(item["feed"], {"seq": 0})["seq"]
        for seq in range(item["from"], min(item["to"], top) + 1):
            sends.append((item["feed"], seq))
    return sends[:MAX_EVENTS]


def bodies_for_peer(
    headers_by_body: dict[str, dict[str, Any]],
    requested: list[str],
    *,
    peer_in_phi_group: bool,
) -> dict[str, Any]:
    send: list[str] = []
    withheld: list[dict[str, str]] = []
    for digest in requested:
        header = headers_by_body.get(digest)
        if header is None:
            withheld.append({"hash": digest, "reason": "unknown"})
            continue
        confidentiality = header["labels"]["confidentiality"]
        if confidentiality == "secret" and header["type"] != "key.credential":
            withheld.append({"hash": digest, "reason": "secret"})
        elif confidentiality == "phi" and not peer_in_phi_group:
            withheld.append({"hash": digest, "reason": "phi"})
        else:
            send.append(digest)
    return {"send": send, "withheld": withheld}


def check_message(message: Any) -> str:
    if not isinstance(message, dict) or message.get("t") not in MESSAGE_TYPES:
        raise MeshError("sync.malformed", "unknown message type")
    kind = message["t"]
    required = {
        "hello": {"t", "v", "mesh", "device", "roster", "stop", "clock", "phi"},
        "heads": {"t", "feeds", "checkpoints"},
        "want": {"t", "ranges", "roster"},
        "roster": {"t", "entries"},
        "events": {"t", "events"},
        "bodies_want": {"t", "hashes"},
        "bodies": {"t", "bodies", "withheld"},
        "checkpoint": {"t", "checkpoint"},
        "fork": {"t", "evidence"},
        "error": {"t", "code"},
        "bye": {"t"},
    }[kind]
    if set(message) != required:
        raise MeshError("sync.malformed", f"{kind} fields")
    if kind == "hello":
        if (
            message["v"] != 1
            or not crypto.is_id(message["mesh"])
            or not crypto.is_id(message["device"])
        ):
            raise MeshError("sync.malformed", "hello identity")
        roster = message["roster"]
        if (
            not isinstance(roster, dict)
            or set(roster) != {"count", "heads"}
            or any(not crypto.is_id(h) for h in roster["heads"])
        ):
            raise MeshError("sync.malformed", "hello roster")
    elif kind == "want":
        ranges = message["ranges"]
        if len(ranges) > MAX_RANGES:
            raise MeshError("sync.too_large", "ranges")
        for item in ranges:
            if (
                set(item) != {"feed", "from", "to"}
                or type(item["from"]) is not int
                or type(item["to"]) is not int
                or not 1 <= item["from"] <= item["to"]
                or item["to"] - item["from"] + 1 > MAX_SPAN
            ):
                raise MeshError("sync.malformed", "range")
            if len(b64url_decode(item["feed"])) != 32:
                raise MeshError("sync.malformed", "range feed")
        if (
            any(not crypto.is_id(r) for r in message["roster"])
            or len(message["roster"]) > MAX_HASHES
        ):
            raise MeshError("sync.malformed", "roster ids")
    elif kind == "events":
        if len(message["events"]) > MAX_EVENTS:
            raise MeshError("sync.too_large", "events")
    elif kind == "bodies_want":
        if len(message["hashes"]) > MAX_HASHES or any(
            not crypto.is_id(h) for h in message["hashes"]
        ):
            raise MeshError("sync.malformed", "hashes")
    elif kind == "bodies":
        for item in message["withheld"]:
            if set(item) != {"hash", "reason"} or item["reason"] not in (
                "phi",
                "secret",
                "unknown",
            ):
                raise MeshError("sync.malformed", "withheld")
    return kind


def frame(message: dict[str, Any]) -> bytes:
    check_message(message)
    body = canonical(message)
    if len(body) > 8 * 1024 * 1024:
        raise MeshError("sync.too_large", "frame")
    return len(body).to_bytes(4, "big") + body


def unframe(data: bytes) -> dict[str, Any]:
    if len(data) < 4 or int.from_bytes(data[:4], "big") != len(data) - 4:
        raise MeshError("sync.malformed", "frame length")
    message = strict_loads(data[4:])
    check_message(message)
    return message
