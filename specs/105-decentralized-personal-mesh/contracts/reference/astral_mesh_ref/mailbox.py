"""The mailbox protocol: signed requests, membership statements, envelope and notice checks, and a model of the blind store.
It defines what astral/mailbox/1 carries and what a mailbox keeps, and it adds the operations the vector runner needs through `register`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import crypto
from .cj import b64url_decode, b64url_encode, canonical
from .errors import MeshError

CONTEXT = "astral.mailbox/v1"
SKEW_S = 300
MAX_ENVELOPES = 64
MAX_ENVELOPE_BYTES = 4194304
MAX_FETCH = 256
MAX_MEMBERS = 64
MAX_NOTICE_BYTES = 16384
NOTICE_TYPES = ("remove", "stop", "epoch")
OPS = ("members", "deposit", "fetch", "notice_put", "notice_get", "status")
REQUEST_FIELDS = {"body", "device", "mailbox", "mesh", "op", "sig", "ts", "v"}
STATEMENT_FIELDS = {
    "by",
    "kind",
    "members",
    "mesh",
    "recovery",
    "seq",
    "sig",
    "v",
    "via",
}
ENVELOPE_FIELDS = {"body_ct", "feed", "header_ct", "seq"}
MIN_HEADER_CT = 32 + 24 + 16

DEFAULT_LIMITS = {
    "max_age_s": 2592000,
    "max_mesh_bytes": 134217728,
    "max_mesh_items": 50000,
    "max_meshes": 10000,
    "max_notices": 64,
    "notice_max_age_s": 15552000,
}


def _is_int(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _key(value: Any, length: int) -> bytes:
    try:
        raw = b64url_decode(value)
    except MeshError as exc:
        raise MeshError("mailbox.malformed", "key encoding") from exc
    if len(raw) != length:
        raise MeshError("mailbox.malformed", "key length")
    return raw


def signing_input(obj: dict[str, Any]) -> bytes:
    return (
        CONTEXT.encode("ascii")
        + b"\n"
        + canonical({k: v for k, v in obj.items() if k != "sig"})
    )


def sign(alg: str, secret: Any, obj: dict[str, Any]) -> str:
    message = signing_input(obj)
    raw = (
        crypto.ed25519_sign(secret, message)
        if alg == "ed25519"
        else crypto.p256_sign(secret, message)
    )
    return b64url_encode(raw)


def verify(alg: str, public: bytes, obj: dict[str, Any]) -> bool:
    signature = obj.get("sig")
    if not isinstance(signature, str):
        return False
    try:
        raw = b64url_decode(signature)
    except MeshError:
        return False
    message = signing_input(obj)
    return (
        crypto.ed25519_verify(public, message, raw)
        if alg == "ed25519"
        else crypto.p256_verify(public, message, raw)
    )


def check_envelope(envelope: Any) -> int:
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        raise MeshError("mailbox.malformed", "envelope fields")
    _key(envelope["feed"], 32)
    if not _is_int(envelope["seq"], 1):
        raise MeshError("mailbox.malformed", "envelope seq")
    try:
        header = b64url_decode(envelope["header_ct"])
        body = envelope["body_ct"]
        if body is not None:
            b64url_decode(body)
    except MeshError as exc:
        raise MeshError("mailbox.malformed", "envelope encoding") from exc
    if len(header) < MIN_HEADER_CT:
        raise MeshError("mailbox.malformed", "header_ct length")
    size = len(canonical(envelope))
    if size > MAX_ENVELOPE_BYTES:
        raise MeshError("mailbox.too_large", "envelope")
    return size


def check_notice(notice: Any, mesh: str) -> str:
    if not isinstance(notice, dict):
        raise MeshError("mailbox.malformed", "notice")
    for field in ("author", "mesh", "sig", "type"):
        if not isinstance(notice.get(field), str):
            raise MeshError("mailbox.malformed", "notice fields")
    if notice["type"] not in NOTICE_TYPES:
        raise MeshError("mailbox.notice_type", notice["type"])
    if notice["mesh"] != mesh:
        raise MeshError("mailbox.wrong_mesh", "notice")
    data = canonical(notice)
    if len(data) > MAX_NOTICE_BYTES:
        raise MeshError("mailbox.too_large", "notice")
    return crypto.sha256_id(data)


def check_statement_shape(statement: Any) -> None:
    if (
        not isinstance(statement, dict)
        or set(statement) != STATEMENT_FIELDS
        or statement["v"] != 1
        or statement["kind"] != "members"
        or statement["via"] not in ("presence", "recovery")
    ):
        raise MeshError("mailbox.malformed", "statement fields")
    if not crypto.is_id(statement["mesh"]) or not _is_int(statement["seq"], 1):
        raise MeshError("mailbox.malformed", "statement mesh or seq")
    members = statement["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise MeshError("mailbox.malformed", "statement members")
    seen_ops: list[str] = []
    seen_presence: set[str] = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"ops", "presence"}:
            raise MeshError("mailbox.malformed", "member fields")
        _key(member["ops"], 32)
        presence = _key(member["presence"], 65)
        if presence[0] != 4:
            raise MeshError("mailbox.malformed", "presence key")
        seen_ops.append(member["ops"])
        seen_presence.add(member["presence"])
    if seen_ops != sorted(set(seen_ops)) or len(seen_presence) != len(members):
        raise MeshError("mailbox.malformed", "members order or duplicates")
    _key(statement["recovery"], 32)
    by = _key(statement["by"], 65 if statement["via"] == "presence" else 32)
    if statement["via"] == "presence" and by[0] != 4:
        raise MeshError("mailbox.malformed", "presence key")


def check_statement(statement: Any) -> dict[str, Any]:
    check_statement_shape(statement)
    alg = "p256" if statement["via"] == "presence" else "ed25519"
    if not verify(alg, b64url_decode(statement["by"]), statement):
        raise MeshError("mailbox.bad_signature", "statement")
    return {
        "by": statement["by"],
        "seq": statement["seq"],
        "via": statement["via"],
    }


def _check_body(op: str, body: Any, mesh: str) -> None:
    if not isinstance(body, dict):
        raise MeshError("mailbox.malformed", "body")
    if op == "members":
        if set(body) != {"statement"}:
            raise MeshError("mailbox.malformed", "members body")
        check_statement_shape(body["statement"])
    elif op == "deposit":
        if set(body) != {"envelopes"} or not isinstance(body["envelopes"], list):
            raise MeshError("mailbox.malformed", "deposit body")
        if not body["envelopes"]:
            raise MeshError("mailbox.malformed", "deposit body")
        if len(body["envelopes"]) > MAX_ENVELOPES:
            raise MeshError("mailbox.too_large", "envelopes")
        for envelope in body["envelopes"]:
            check_envelope(envelope)
    elif op == "fetch":
        if (
            set(body) != {"after", "limit"}
            or not _is_int(body["after"], 0)
            or not _is_int(body["limit"], 1)
        ):
            raise MeshError("mailbox.malformed", "fetch body")
        if body["limit"] > MAX_FETCH:
            raise MeshError("mailbox.too_large", "limit")
    elif op == "notice_put":
        if set(body) != {"notice"}:
            raise MeshError("mailbox.malformed", "notice body")
        check_notice(body["notice"], mesh)
    elif body:
        raise MeshError("mailbox.malformed", f"{op} body")


def check_request(
    request: Any, mailbox: bytes, now_s: int, peer: str | None = None
) -> dict[str, Any]:
    if (
        not isinstance(request, dict)
        or set(request) != REQUEST_FIELDS
        or request["v"] != 1
        or request["op"] not in OPS
    ):
        raise MeshError("mailbox.malformed", "request fields")
    if not crypto.is_id(request["mesh"]) or not _is_int(request["ts"], 0):
        raise MeshError("mailbox.malformed", "mesh or ts")
    audience = _key(request["mailbox"], 32)
    device = _key(request["device"], 32)
    if len(b64url_decode_checked(request["sig"])) != 64:
        raise MeshError("mailbox.malformed", "signature length")
    _check_body(request["op"], request["body"], request["mesh"])
    if audience != mailbox:
        raise MeshError("mailbox.wrong_audience", "audience")
    if abs(now_s - request["ts"]) > SKEW_S:
        raise MeshError("mailbox.stale", "timestamp")
    if peer is not None and peer != request["device"]:
        raise MeshError("mailbox.wrong_peer", "peer")
    if not verify("ed25519", device, request):
        raise MeshError("mailbox.bad_signature", "request")
    return {
        "device": request["device"],
        "mesh": request["mesh"],
        "op": request["op"],
    }


def b64url_decode_checked(text: Any) -> bytes:
    try:
        return b64url_decode(text)
    except MeshError as exc:
        raise MeshError("mailbox.malformed", "encoding") from exc


class _Mesh:
    def __init__(self, recovery: str) -> None:
        self.seq = 0
        self.recovery = recovery
        self.members: dict[str, str] = {}
        self.acked: dict[str, int] = {}
        self.removed: set[str] = set()
        self.head = 0
        self.expired_through = 0
        self.items: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []


class Mailbox:
    def __init__(
        self,
        key: bytes,
        limits: dict[str, int] | None = None,
        admission: list[str] | None = None,
    ) -> None:
        self.key = key
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.admission = admission
        self.meshes: dict[str, _Mesh] = {}

    def handle(
        self, request: Any, now_s: int, peer: str | None = None
    ) -> dict[str, Any]:
        facts = check_request(request, self.key, now_s, peer)
        mesh_id, device, op = facts["mesh"], facts["device"], facts["op"]
        body = request["body"]
        if op == "members":
            return self._members(mesh_id, device, body["statement"], now_s)
        mesh = self.meshes.get(mesh_id)
        if mesh is None:
            raise MeshError("mailbox.unknown_mesh", mesh_id)
        if device in mesh.removed:
            raise MeshError("mailbox.removed", device)
        if device not in mesh.members:
            raise MeshError("mailbox.not_a_member", device)
        self._collect(mesh, now_s)
        if op == "deposit":
            result = self._deposit(mesh, device, body["envelopes"], now_s)
        elif op == "fetch":
            result = self._fetch(mesh, device, body["after"], body["limit"], now_s)
        elif op == "notice_put":
            result = self._notice_put(mesh, device, body["notice"], now_s)
        elif op == "notice_get":
            result = self._notice_get(mesh, device)
        else:
            result = self._status(mesh)
        self._collect(mesh, now_s)
        if op == "status":
            result = self._status(mesh)
        return result

    def _members(
        self, mesh_id: str, device: str, statement: dict[str, Any], now_s: int
    ) -> dict[str, Any]:
        check_statement(statement)
        if statement["mesh"] != mesh_id:
            raise MeshError("mailbox.wrong_mesh", "statement")
        pairs = {m["ops"]: m["presence"] for m in statement["members"]}
        mesh = self.meshes.get(mesh_id)
        if mesh is None:
            self._register(mesh_id, device, statement, pairs)
            return {"seq": statement["seq"]}
        if statement["recovery"] != mesh.recovery:
            raise MeshError("mailbox.recovery_mismatch", "recovery")
        if statement["via"] == "recovery":
            allowed = statement["by"] == mesh.recovery
        else:
            allowed = statement["by"] in mesh.members.values()
        if not allowed:
            raise MeshError("mailbox.members_unauthorized", "signer")
        if statement["seq"] == mesh.seq and pairs == mesh.members:
            return {"seq": mesh.seq}
        if statement["seq"] <= mesh.seq:
            raise MeshError("mailbox.members_seq", str(statement["seq"]))
        self._collect(mesh, now_s)
        mesh.removed |= set(mesh.members) - set(pairs)
        mesh.removed -= set(pairs)
        mesh.acked = {ops: mesh.acked.get(ops, 0) for ops in pairs}
        mesh.members = pairs
        mesh.seq = statement["seq"]
        self._collect(mesh, now_s)
        return {"seq": mesh.seq}

    def _register(
        self,
        mesh_id: str,
        device: str,
        statement: dict[str, Any],
        pairs: dict[str, str],
    ) -> None:
        if statement["seq"] != 1:
            raise MeshError("mailbox.members_seq", "registration is seq 1")
        if statement["via"] != "presence" or pairs.get(device) != statement["by"]:
            raise MeshError("mailbox.members_unauthorized", "registration")
        if self.admission is not None and device not in self.admission:
            raise MeshError("mailbox.admission_denied", device)
        if len(self.meshes) >= self.limits["max_meshes"]:
            raise MeshError("mailbox.quota_exceeded", "meshes")
        mesh = _Mesh(statement["recovery"])
        mesh.seq = 1
        mesh.members = pairs
        mesh.acked = {ops: 0 for ops in pairs}
        self.meshes[mesh_id] = mesh

    def _collected(self, mesh: _Mesh, item: dict[str, Any]) -> bool:
        return all(
            mesh.acked[ops] >= item["n"]
            for ops in mesh.members
            if ops != item["depositor"]
        )

    def _collect(self, mesh: _Mesh, now_s: int) -> None:
        kept = []
        for item in mesh.items:
            if self._collected(mesh, item):
                continue
            if item["at_s"] + self.limits["max_age_s"] <= now_s:
                mesh.expired_through = max(mesh.expired_through, item["n"])
                continue
            kept.append(item)
        mesh.items = kept
        everyone = set(mesh.members)
        mesh.notices = [
            notice
            for notice in mesh.notices
            if not everyone <= notice["seen"]
            and notice["at_s"] + self.limits["notice_max_age_s"] > now_s
        ]

    def _deposit(
        self,
        mesh: _Mesh,
        device: str,
        envelopes: list[dict[str, Any]],
        now_s: int,
    ) -> dict[str, Any]:
        held = {item["digest"] for item in mesh.items}
        fresh: list[tuple[str, dict[str, Any], int]] = []
        duplicates = 0
        for envelope in envelopes:
            data = canonical(envelope)
            digest = crypto.sha256_id(data)
            if digest in held:
                duplicates += 1
                continue
            held.add(digest)
            fresh.append((digest, envelope, len(data)))
        total = sum(item["size"] for item in mesh.items) + sum(f[2] for f in fresh)
        if (
            len(mesh.items) + len(fresh) > self.limits["max_mesh_items"]
            or total > self.limits["max_mesh_bytes"]
        ):
            raise MeshError("mailbox.quota_exceeded", "mesh")
        for digest, envelope, size in fresh:
            mesh.head += 1
            mesh.items.append(
                {
                    "at_s": now_s,
                    "depositor": device,
                    "digest": digest,
                    "envelope": envelope,
                    "n": mesh.head,
                    "size": size,
                }
            )
        return {"accepted": len(fresh), "duplicates": duplicates, "head": mesh.head}

    def _fetch(
        self, mesh: _Mesh, device: str, after: int, limit: int, now_s: int
    ) -> dict[str, Any]:
        if after > mesh.head:
            raise MeshError("mailbox.cursor_ahead", str(after))
        mesh.acked[device] = max(mesh.acked[device], after)
        self._collect(mesh, now_s)
        pending = [
            item
            for item in mesh.items
            if item["n"] > after and item["depositor"] != device
        ]
        chosen = pending[:limit]
        more = len(pending) > limit
        next_cursor = chosen[-1]["n"] if more else max(after, mesh.head)
        return {
            "items": [{"envelope": i["envelope"], "n": i["n"]} for i in chosen],
            "more": more,
            "next": next_cursor,
            "truncated": after < mesh.expired_through,
        }

    def _notice_put(
        self, mesh: _Mesh, device: str, notice: dict[str, Any], now_s: int
    ) -> dict[str, Any]:
        identifier = crypto.sha256_id(canonical(notice))
        for held in mesh.notices:
            if held["id"] == identifier:
                held["seen"].add(device)
                return {"id": identifier, "new": False}
        if len(mesh.notices) >= self.limits["max_notices"]:
            raise MeshError("mailbox.quota_exceeded", "notices")
        mesh.notices.append(
            {"at_s": now_s, "id": identifier, "notice": notice, "seen": {device}}
        )
        return {"id": identifier, "new": True}

    def _notice_get(self, mesh: _Mesh, device: str) -> dict[str, Any]:
        out = [{"id": n["id"], "notice": n["notice"]} for n in mesh.notices]
        for notice in mesh.notices:
            notice["seen"].add(device)
        return {"notices": out}

    def _status(self, mesh: _Mesh) -> dict[str, Any]:
        return {
            "bytes": sum(item["size"] for item in mesh.items),
            "expired_through": mesh.expired_through,
            "head": mesh.head,
            "items": len(mesh.items),
            "members": len(mesh.members),
            "notices": len(mesh.notices),
            "seq": mesh.seq,
        }


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("mailbox_sign")
    def _sign(i: dict[str, Any]) -> Any:
        secret = (
            bytes.fromhex(i["secret_hex"])
            if i["alg"] == "ed25519"
            else int(i["secret_hex"], 16)
        )
        return {"sig": sign(i["alg"], secret, i["object"])}

    @op("mailbox_request")
    def _request(i: dict[str, Any]) -> Any:
        return check_request(
            i["request"], b64url_decode(i["mailbox"]), i["now_s"], i.get("peer")
        )

    @op("mailbox_statement")
    def _statement(i: dict[str, Any]) -> Any:
        return check_statement(i["statement"])

    @op("mailbox_notice")
    def _notice(i: dict[str, Any]) -> Any:
        return {"id": check_notice(i["notice"], i["mesh"])}

    @op("mailbox_envelope")
    def _envelope(i: dict[str, Any]) -> Any:
        return {"size": check_envelope(i["envelope"])}

    @op("mailbox_replay")
    def _replay(i: dict[str, Any]) -> Any:
        box = Mailbox(b64url_decode(i["mailbox"]), i.get("limits"), i.get("admission"))
        results = []
        for step in i["steps"]:
            try:
                results.append(
                    {"ok": box.handle(step["request"], step["at_s"], step.get("peer"))}
                )
            except MeshError as exc:
                results.append({"error": exc.code})
        return {"results": results}
