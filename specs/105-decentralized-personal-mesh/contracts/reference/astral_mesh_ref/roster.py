"""The roster: signed entries that form a DAG, the shape and signature checks for each entry type, and the deterministic merge into membership, keys, issuers, PHI approvals and the stop counter.
It uses attestation.py for outside statements and crypto.py for signatures, and sync.py asks it for each feed's status.
"""

from __future__ import annotations

from typing import Any

from . import attestation, crypto
from .cj import b64url_decode, b64url_encode
from .errors import MeshError

KINDS = ("desktop", "pocket", "satellite", "node")
GROUPS = ("personal", "phi")
TYPES = (
    "genesis",
    "add",
    "remove",
    "rotate",
    "attest",
    "issuer",
    "phi_authority",
    "phi_approval",
    "epoch",
    "wrap",
    "stop",
)
PRESENCE_REQUIRED = {
    "genesis",
    "add",
    "remove",
    "issuer",
    "phi_authority",
    "phi_approval",
}
RECOVERY_ALLOWED = {"add", "remove", "epoch", "wrap", "issuer", "stop", "phi_authority"}
ENTRY_FIELDS = {"v", "type", "mesh", "follows", "clock", "author", "body", "sig"}
DEVICE_FIELDS = {"cert", "identity", "kind", "label", "ops", "presence", "wrap"}
WRAP_FIELDS = {"ct", "epk", "nonce"}


def entry_id(entry: dict[str, Any]) -> str:
    return crypto.object_id(
        {k: v for k, v in entry.items() if k not in ("sig", "presence_sig")}
    )


def _signing(entry: dict[str, Any]) -> bytes:
    return crypto.signing_input(
        "astral.roster/v1", entry, exclude=("sig", "presence_sig")
    )


def build_entry(
    type: str,
    mesh: str | None,
    follows: list[str],
    clock: dict[str, int],
    author: str,
    body: dict[str, Any],
    *,
    identity_scalar: int | None = None,
    presence_scalar: int | None = None,
    recovery_seed: bytes | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "author": author,
        "body": body,
        "clock": clock,
        "follows": sorted(follows),
        "mesh": mesh,
        "type": type,
        "v": 1,
    }
    message = _signing(entry)
    if recovery_seed is not None:
        entry["sig"] = b64url_encode(crypto.ed25519_sign(recovery_seed, message))
    else:
        assert identity_scalar is not None
        entry["sig"] = b64url_encode(crypto.p256_sign(identity_scalar, message))
        if presence_scalar is not None:
            entry["presence_sig"] = b64url_encode(
                crypto.p256_sign(presence_scalar, message)
            )
    return entry


def _bytes(value: Any, length: int, what: str) -> bytes:
    try:
        raw = b64url_decode(value)
    except MeshError as exc:
        raise MeshError("roster.malformed", what) from exc
    if len(raw) != length:
        raise MeshError("roster.malformed", what)
    return raw


def check_wrap(blob: Any, what: str = "wrap") -> None:
    if not isinstance(blob, dict) or set(blob) != WRAP_FIELDS:
        raise MeshError("roster.malformed", what)
    _bytes(blob["epk"], 32, what)
    _bytes(blob["nonce"], crypto.NONCE_LEN, what)
    _bytes(blob["ct"], 48, what)


def check_device_record(record: Any, mesh: str | None) -> str:
    if not isinstance(record, dict) or set(record) != DEVICE_FIELDS:
        raise MeshError("roster.malformed", "device record fields")
    if (
        record["kind"] not in KINDS
        or not isinstance(record["label"], str)
        or not 0 < len(record["label"]) <= 64
    ):
        raise MeshError("roster.malformed", "device kind or label")
    identity = _bytes(record["identity"], 65, "identity key")
    presence = _bytes(record["presence"], 65, "presence key")
    _bytes(record["ops"], 32, "operations key")
    _bytes(record["wrap"], 32, "wrap key")
    if identity[0] != 4 or presence[0] != 4:
        raise MeshError("roster.malformed", "P-256 points must be uncompressed")
    payload = {"device": {k: v for k, v in record.items() if k != "cert"}, "mesh": mesh}
    if not crypto.verify_detached(
        "p256", identity, "astral.pairing/v1", payload, record["cert"]
    ):
        raise MeshError(
            "roster.bad_device_cert", "identity key does not certify the device keys"
        )
    return crypto.device_id(identity)


def _check_issuer(issuer: Any) -> None:
    if (
        not isinstance(issuer, dict)
        or set(issuer) != {"clients", "iss", "keys"}
        or not isinstance(issuer["iss"], str)
        or not issuer["iss"]
        or not isinstance(issuer["clients"], list)
        or not issuer["clients"]
        or any(not isinstance(c, str) or not c for c in issuer["clients"])
        or len(set(issuer["clients"])) != len(issuer["clients"])
        or not isinstance(issuer["keys"], list)
        or not issuer["keys"]
        or any(
            not isinstance(k, dict) or not isinstance(k.get("kid"), str)
            for k in issuer["keys"]
        )
    ):
        raise MeshError("roster.malformed", "issuer record")


def _check_epoch_body(body: Any, group: str | None = None) -> None:
    if (
        not isinstance(body, dict)
        or set(body) != {"group", "recovery_wrap", "wraps"}
        or body["group"] not in GROUPS
    ):
        raise MeshError("roster.malformed", "epoch body")
    if group is not None and body["group"] != group:
        raise MeshError("roster.malformed", "epoch group")
    wraps = body["wraps"]
    if not isinstance(wraps, list) or not wraps:
        raise MeshError("roster.malformed", "epoch wraps")
    seen = set()
    for item in wraps:
        if (
            not isinstance(item, dict)
            or set(item) != {"device", "wrap"}
            or not crypto.is_id(item["device"])
            or item["device"] in seen
        ):
            raise MeshError("roster.malformed", "epoch wrap item")
        seen.add(item["device"])
        check_wrap(item["wrap"])
    if (body["group"] == "personal") != (body["recovery_wrap"] is not None):
        raise MeshError(
            "roster.malformed", "only the personal group is wrapped to the recovery key"
        )
    if body["recovery_wrap"] is not None:
        check_wrap(body["recovery_wrap"])


def check_entry_shape(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise MeshError("roster.malformed", "not an object")
    allowed = ENTRY_FIELDS | {"presence_sig"}
    if (
        not ENTRY_FIELDS <= set(entry) <= allowed
        or entry["v"] != 1
        or entry["type"] not in TYPES
    ):
        raise MeshError("roster.malformed", "fields, version or type")
    kind = entry["type"]
    follows = entry["follows"]
    if (
        not isinstance(follows, list)
        or follows != sorted(set(follows))
        or any(not crypto.is_id(f) for f in follows)
    ):
        raise MeshError("roster.malformed", "follows")
    if kind == "genesis":
        if entry["mesh"] is not None or follows:
            raise MeshError(
                "roster.malformed", "genesis has no mesh and follows nothing"
            )
    elif not crypto.is_id(entry["mesh"]) or not follows:
        raise MeshError("roster.malformed", "mesh and follows")
    clock = entry["clock"]
    if (
        not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(type(v) is not int or v < 0 for v in clock.values())
    ):
        raise MeshError("roster.malformed", "clock")
    author = entry["author"]
    if author == "recovery":
        if kind not in RECOVERY_ALLOWED:
            raise MeshError("roster.recovery_not_allowed", kind)
        if "presence_sig" in entry:
            raise MeshError(
                "roster.malformed", "recovery entries carry no presence signature"
            )
    elif not crypto.is_id(author):
        raise MeshError("roster.malformed", "author")
    elif (kind in PRESENCE_REQUIRED) != ("presence_sig" in entry):
        raise MeshError("roster.malformed", "presence signature presence")
    body = entry["body"]
    if not isinstance(body, dict):
        raise MeshError("roster.malformed", "body")
    fields = set(body)
    if kind == "genesis":
        if fields != {"device", "epoch", "issuers", "recovery"}:
            raise MeshError("roster.malformed", "genesis body")
        check_device_record(body["device"], None)
        _check_epoch_body(body["epoch"], "personal")
        if not isinstance(body["issuers"], list) or not body["issuers"]:
            raise MeshError(
                "roster.malformed", "a mesh needs at least one accepted issuer"
            )
        for item in body["issuers"]:
            _check_issuer(item)
        recovery = body["recovery"]
        if not isinstance(recovery, dict) or set(recovery) != {"sign", "wrap"}:
            raise MeshError("roster.malformed", "recovery keys")
        _bytes(recovery["sign"], 32, "recovery signing key")
        _bytes(recovery["wrap"], 32, "recovery wrap key")
    elif kind == "add":
        if fields != {"attestation", "device"}:
            raise MeshError("roster.malformed", "add body")
        check_device_record(body["device"], entry["mesh"])
    elif kind == "rotate":
        if fields != {"device"}:
            raise MeshError("roster.malformed", "rotate body")
        check_device_record(body["device"], entry["mesh"])
    elif kind == "remove":
        if (
            fields != {"cutoff", "device"}
            or not crypto.is_id(body["device"])
            or not isinstance(body["cutoff"], dict)
        ):
            raise MeshError("roster.malformed", "remove body")
        for feed, seq in body["cutoff"].items():
            _bytes(feed, 32, "cutoff feed")
            if type(seq) is not int or seq < 0:
                raise MeshError("roster.malformed", "cutoff seq")
    elif kind == "attest":
        if fields != {"attestation"} or not isinstance(body["attestation"], dict):
            raise MeshError("roster.malformed", "attest body")
    elif kind == "issuer":
        if body.get("action") == "add" and fields == {"action", "issuer"}:
            _check_issuer(body["issuer"])
        elif (
            body.get("action") == "remove"
            and fields == {"action", "iss"}
            and isinstance(body["iss"], str)
        ):
            pass
        else:
            raise MeshError("roster.malformed", "issuer body")
    elif kind == "phi_authority":
        if body.get("action") == "add" and fields == {"action", "institution"}:
            inst = body["institution"]
            if (
                not isinstance(inst, dict)
                or set(inst) != {"key", "name"}
                or not isinstance(inst["name"], str)
            ):
                raise MeshError("roster.malformed", "institution")
            if _bytes(inst["key"], 65, "institution key")[0] != 4:
                raise MeshError("roster.malformed", "institution key")
        elif body.get("action") == "remove" and fields == {"action", "key"}:
            _bytes(body["key"], 65, "institution key")
        else:
            raise MeshError("roster.malformed", "phi_authority body")
    elif kind == "phi_approval":
        if (
            body.get("action") == "grant"
            and fields == {"action", "attestation"}
            and isinstance(body["attestation"], dict)
        ):
            pass
        elif (
            body.get("action") == "revoke"
            and fields == {"action", "subject"}
            and isinstance(body["subject"], str)
        ):
            pass
        else:
            raise MeshError("roster.malformed", "phi_approval body")
    elif kind == "epoch":
        _check_epoch_body(body)
    elif kind == "wrap":
        if (
            fields != {"device", "grants", "group"}
            or body["group"] not in GROUPS
            or not crypto.is_id(body["device"])
        ):
            raise MeshError("roster.malformed", "wrap body")
        grants = body["grants"]
        if not isinstance(grants, list) or not grants:
            raise MeshError("roster.malformed", "wrap grants")
        for grant in grants:
            if (
                not isinstance(grant, dict)
                or set(grant) != {"epoch", "wrap"}
                or not crypto.is_id(grant["epoch"])
            ):
                raise MeshError("roster.malformed", "wrap grant")
            check_wrap(grant["wrap"])
    elif kind == "stop":
        if (
            fields != {"counter"}
            or type(body["counter"]) is not int
            or body["counter"] < 1
        ):
            raise MeshError("roster.malformed", "stop body")


class _Projection:
    def __init__(self) -> None:
        self.admitted: dict[str, dict[str, Any]] = {}
        self.removed: dict[str, dict[str, Any]] = {}
        self.recovery: dict[str, str] | None = None
        self.issuers: dict[str, dict[str, Any]] = {}
        self.authorities: dict[str, str] = {}
        self.phi_subjects: dict[str, dict[str, Any]] = {}
        self.epochs: dict[str, dict[str, Any]] = {}
        self.stop = 0
        self.attested: dict[str, list[dict[str, Any]]] = {}

    @property
    def members(self) -> dict[str, dict[str, Any]]:
        return {d: r for d, r in self.admitted.items() if d not in self.removed}

    def phi_members(self, now_s: int | None) -> set[str]:
        out = set()
        for device in self.members:
            grant = self.phi_subjects.get(device)
            if grant is not None and (now_s is None or grant["not_after_s"] >= now_s):
                out.add(device)
        return out

    def current_epoch(self, group: str, anc: dict[str, frozenset[str]]) -> str | None:
        candidates = [e for e, info in self.epochs.items() if info["group"] == group]
        heads = [
            e for e in candidates if not any(e in anc[o] for o in candidates if o != e)
        ]
        if not heads:
            return None
        return max(
            heads,
            key=lambda e: (
                self.epochs[e]["clock"]["ms"],
                self.epochs[e]["clock"]["n"],
                e,
            ),
        )


class RosterState:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def feed_status(self, feed: str) -> dict[str, Any]:
        return self.data["feeds"].get(feed, {"state": "unknown"})


def fold(
    entries: list[dict[str, Any]], mesh: str | None = None, *, now_s: int | None = None
) -> RosterState:
    return _Fold(entries, mesh, now_s).run()


class _Fold:
    def __init__(
        self, entries: list[dict[str, Any]], mesh: str | None, now_s: int | None
    ) -> None:
        self.now_s = now_s
        self.entries: dict[str, dict[str, Any]] = {}
        self.rejected: dict[str, str] = {}
        for entry in entries:
            try:
                check_entry_shape(entry)
            except MeshError as exc:
                try:
                    self.rejected[entry_id(entry)] = exc.code
                except MeshError:
                    continue
                continue
            self.entries.setdefault(entry_id(entry), entry)
        geneses = sorted(i for i, e in self.entries.items() if e["type"] == "genesis")
        if mesh is None:
            if len(geneses) != 1:
                raise MeshError(
                    "roster.ambiguous_genesis" if geneses else "roster.no_genesis", ""
                )
            mesh = geneses[0]
        elif mesh not in self.entries or self.entries[mesh]["type"] != "genesis":
            raise MeshError("roster.no_genesis", mesh)
        self.mesh = mesh
        for gid in geneses:
            if gid != mesh:
                self.rejected[gid] = "roster.extra_genesis"
                del self.entries[gid]
        for eid in [
            i
            for i, e in self.entries.items()
            if e["type"] != "genesis" and e["mesh"] != mesh
        ]:
            self.rejected[eid] = "roster.wrong_mesh"
            del self.entries[eid]
        self.att_ok: dict[str, dict[str, Any]] = {}
        self._order()

    def _order(self) -> None:
        self.anc: dict[str, frozenset[str]] = {}
        self.topo: list[str] = []
        self.pending: list[str] = []
        remaining = set(self.entries)
        progress = True
        while progress:
            progress = False
            for eid in sorted(remaining):
                follows = self.entries[eid]["follows"]
                if all(f in self.anc for f in follows):
                    ancestors: set[str] = set()
                    for f in follows:
                        ancestors |= self.anc[f] | {f}
                    self.anc[eid] = frozenset(ancestors)
                    self.topo.append(eid)
                    remaining.discard(eid)
                    progress = True
        self.pending = sorted(remaining)
        self.index = {eid: n for n, eid in enumerate(self.topo)}

    def _project(self, ids: Any) -> _Projection:
        p = _Projection()
        ordered = sorted(ids, key=self.index.__getitem__)
        issuer_adds: dict[str, list[str]] = {}
        issuer_removes: dict[str, list[str]] = {}
        auth_adds: dict[str, list[str]] = {}
        auth_removes: dict[str, list[str]] = {}
        grants: dict[str, list[str]] = {}
        revokes: dict[str, list[str]] = {}
        for eid in ordered:
            e = self.entries[eid]
            body = e["body"]
            kind = e["type"]
            if kind in ("genesis", "add"):
                record = body["device"]
                device = crypto.device_id(b64url_decode(record["identity"]))
                p.admitted[device] = {
                    **record,
                    "feeds": [record["ops"]],
                    "admitted_by": eid,
                }
                if kind == "genesis":
                    p.recovery = body["recovery"]
                    for issuer in body["issuers"]:
                        issuer_adds.setdefault(issuer["iss"], []).append(eid)
                    p.epochs[eid] = {
                        "group": "personal",
                        "clock": e["clock"],
                        "entry": eid,
                    }
                elif eid in self.att_ok:
                    p.attested.setdefault(device, []).append(self.att_ok[eid])
            elif kind == "rotate":
                record = body["device"]
                device = crypto.device_id(b64url_decode(record["identity"]))
                if device in p.admitted:
                    feeds = p.admitted[device]["feeds"]
                    p.admitted[device] = {
                        **p.admitted[device],
                        **record,
                        "feeds": feeds + [record["ops"]],
                    }
            elif kind == "remove":
                p.removed[body["device"]] = {"cutoff": body["cutoff"], "entry": eid}
            elif kind == "attest" and eid in self.att_ok:
                p.attested.setdefault(e["author"], []).append(self.att_ok[eid])
            elif kind == "issuer":
                if body["action"] == "add":
                    issuer_adds.setdefault(body["issuer"]["iss"], []).append(eid)
                else:
                    issuer_removes.setdefault(body["iss"], []).append(eid)
            elif kind == "phi_authority":
                if body["action"] == "add":
                    auth_adds.setdefault(body["institution"]["key"], []).append(eid)
                else:
                    auth_removes.setdefault(body["key"], []).append(eid)
            elif kind == "phi_approval":
                if body["action"] == "grant":
                    grants.setdefault(body["attestation"]["subject"], []).append(eid)
                else:
                    revokes.setdefault(body["subject"], []).append(eid)
            elif kind == "epoch":
                p.epochs[eid] = {
                    "group": body["group"],
                    "clock": e["clock"],
                    "entry": eid,
                }
            elif kind == "stop":
                p.stop = max(p.stop, body["counter"])

        def effective(adds: list[str], removes: list[str]) -> list[str]:
            return [a for a in adds if all(r in self.anc[a] for r in removes)]

        def newest(ids_: list[str]) -> str:
            return max(
                ids_,
                key=lambda i: (
                    self.entries[i]["clock"]["ms"],
                    self.entries[i]["clock"]["n"],
                    i,
                ),
            )

        for iss, adds in issuer_adds.items():
            live = effective(adds, issuer_removes.get(iss, []))
            if live:
                chosen = self.entries[newest(live)]
                p.issuers[iss] = (
                    next(i for i in chosen["body"]["issuers"] if i["iss"] == iss)
                    if chosen["type"] == "genesis"
                    else chosen["body"]["issuer"]
                )
        for key, adds in auth_adds.items():
            live = effective(adds, auth_removes.get(key, []))
            if live:
                p.authorities[key] = self.entries[newest(live)]["body"]["institution"][
                    "name"
                ]
        for subject, adds in grants.items():
            live = effective(adds, revokes.get(subject, []))
            if live:
                att = self.entries[newest(live)]["body"]["attestation"]
                if att["institution"] in p.authorities:
                    p.phi_subjects[subject] = {
                        "not_after_s": att["not_after_s"],
                        "institution": att["institution"],
                        "entry": newest(live),
                    }
        return p

    def _check(self, eid: str, e: dict[str, Any], p: _Projection | None) -> str | None:
        kind = e["type"]
        body = e["body"]
        message = _signing(e)
        try:
            if kind == "genesis":
                record = body["device"]
                identity = b64url_decode(record["identity"])
                if crypto.device_id(identity) != e["author"]:
                    return "roster.unauthorized_author"
                if not crypto.p256_verify(identity, message, b64url_decode(e["sig"])):
                    return "roster.bad_signature"
                if not crypto.p256_verify(
                    b64url_decode(record["presence"]),
                    message,
                    b64url_decode(e["presence_sig"]),
                ):
                    return "roster.bad_presence_signature"
                return None
            assert p is not None
            if e["author"] == "recovery":
                if p.recovery is None or not crypto.ed25519_verify(
                    b64url_decode(p.recovery["sign"]), message, b64url_decode(e["sig"])
                ):
                    return "roster.bad_signature"
                if kind == "epoch" and body["group"] == "phi":
                    return "roster.recovery_not_allowed"
            else:
                author = p.members.get(e["author"])
                if author is None:
                    return (
                        "roster.author_removed"
                        if e["author"] in p.removed
                        else "roster.unauthorized_author"
                    )
                if not crypto.p256_verify(
                    b64url_decode(author["identity"]), message, b64url_decode(e["sig"])
                ):
                    return "roster.bad_signature"
                if "presence_sig" in e and not crypto.p256_verify(
                    b64url_decode(author["presence"]),
                    message,
                    b64url_decode(e["presence_sig"]),
                ):
                    return "roster.bad_presence_signature"
            return self._check_body(eid, e, p)
        except MeshError as exc:
            return exc.code

    def _check_body(self, eid: str, e: dict[str, Any], p: _Projection) -> str | None:
        kind = e["type"]
        body = e["body"]
        phi = p.phi_members(self.now_s)
        if kind == "add":
            device = crypto.device_id(b64url_decode(body["device"]["identity"]))
            if device in p.admitted:
                return (
                    "roster.already_member"
                    if device in p.members
                    else "roster.device_removed"
                )
            if self._key_in_use(p, body["device"]["ops"]):
                return "roster.duplicate_key"
            if body["attestation"] is not None:
                self.att_ok[eid] = attestation.verify_identity(
                    body["attestation"],
                    b64url_decode(body["device"]["identity"]),
                    self.mesh,
                    p.issuers,
                    entry_time_s=e["clock"]["ms"] // 1000,
                    now_s=self.now_s,
                )
        elif kind == "rotate":
            record = body["device"]
            device = crypto.device_id(b64url_decode(record["identity"]))
            if e["author"] != device or device not in p.members:
                return "roster.unauthorized_author"
            if record["identity"] != p.members[device]["identity"] or self._key_in_use(
                p, record["ops"]
            ):
                return "roster.duplicate_key"
        elif kind == "remove":
            if body["device"] not in p.members:
                return "roster.unknown_device"
            if set(body["cutoff"]) != set(p.members[body["device"]]["feeds"]):
                return "roster.cutoff_incomplete"
        elif kind == "attest":
            if e["author"] == "recovery" or e["author"] not in p.members:
                return "roster.unauthorized_author"
            att = body["attestation"]
            self.att_ok[eid] = attestation.verify_identity(
                att,
                b64url_decode(p.members[e["author"]]["identity"]),
                self.mesh,
                p.issuers,
                entry_time_s=e["clock"]["ms"] // 1000,
                now_s=self.now_s,
            )
        elif (
            kind == "issuer"
            and body["action"] == "remove"
            and body["iss"] not in p.issuers
        ):
            return "roster.unknown_issuer"
        elif (
            kind == "phi_authority"
            and body["action"] == "remove"
            and body["key"] not in p.authorities
        ):
            return "roster.unknown_authority"
        elif kind == "phi_approval" and body["action"] == "grant":
            att = body["attestation"]
            attestation.verify_phi(att, set(p.authorities), now_s=None)
            if att["subject_kind"] == "device" and att["subject"] not in p.members:
                return "roster.unknown_device"
        elif kind == "epoch":
            group = body["group"]
            allowed = set(p.members) if group == "personal" else phi
            if any(item["device"] not in allowed for item in body["wraps"]):
                return "roster.epoch_wrong_members"
            if group == "phi" and e["author"] not in phi:
                return "roster.not_phi_member"
        elif kind == "wrap":
            group = body["group"]
            allowed = set(p.members) if group == "personal" else phi
            if body["device"] not in allowed:
                return "roster.epoch_wrong_members"
            for grant in body["grants"]:
                info = p.epochs.get(grant["epoch"])
                if info is None or info["group"] != group:
                    return "roster.unknown_epoch"
            if group == "phi" and e["author"] not in phi:
                return "roster.not_phi_member"
        elif kind == "stop" and body["counter"] <= p.stop:
            return "roster.stop_not_rising"
        return None

    @staticmethod
    def _key_in_use(p: _Projection, ops: str) -> bool:
        return any(ops in record["feeds"] for record in p.admitted.values())

    def run(self) -> RosterState:
        void: set[str] = set()
        while True:
            active: list[str] = []
            active_set: set[str] = set()
            rejected = dict(self.rejected)
            for eid in self.topo:
                if eid in void:
                    continue
                e = self.entries[eid]
                projection = (
                    None
                    if e["type"] == "genesis"
                    else self._project([a for a in self.anc[eid] if a in active_set])
                )
                code = self._check(eid, e, projection)
                if code is not None:
                    rejected[eid] = code
                    continue
                active.append(eid)
                active_set.add(eid)
            removals = [
                (eid, self.entries[eid]["body"]["device"])
                for eid in active
                if self.entries[eid]["type"] == "remove"
            ]
            fresh = set()
            for eid in active:
                e = self.entries[eid]
                if e["type"] in ("remove", "genesis") or e["author"] == "recovery":
                    continue
                if any(
                    device == e["author"] and eid not in self.anc[r]
                    for r, device in removals
                ):
                    fresh.add(eid)
            if not fresh:
                break
            void |= fresh
        return RosterState(self._export(active, active_set, void, rejected))

    def _export(
        self,
        active: list[str],
        active_set: set[str],
        void: set[str],
        rejected: dict[str, str],
    ) -> dict[str, Any]:
        p = self._project(active_set)
        members = p.members
        feeds: dict[str, dict[str, Any]] = {}
        for device, record in p.admitted.items():
            removal = p.removed.get(device)
            for feed in record["feeds"]:
                feeds[feed] = (
                    {
                        "device": device,
                        "state": "removed",
                        "cutoff": removal["cutoff"][feed],
                    }
                    if removal
                    else {"device": device, "state": "member"}
                )
        phi = p.phi_members(self.now_s)
        attested = {
            device: [
                list(pair)
                for pair in sorted(
                    {(a["issuer"], a["subject"]) for a in p.attested.get(device, [])}
                )
            ]
            for device in members
        }
        stale = {
            device: all(a["stale"] for a in p.attested.get(device, []))
            if p.attested.get(device)
            else False
            for device in members
        }
        all_present = set(self.topo)
        leaves = sorted(
            i for i in all_present if not any(i in self.anc[o] for o in all_present)
        )
        return {
            "mesh": self.mesh,
            "heads": leaves,
            "count": len(self.topo),
            "members": {
                d: {"kind": r["kind"], "label": r["label"], "feeds": r["feeds"]}
                for d, r in sorted(members.items())
            },
            "removed": {d: info for d, info in sorted(p.removed.items())},
            "unattested": sorted(d for d in members if not attested[d]),
            "stale_attestation": sorted(d for d, flag in stale.items() if flag),
            "principal": {
                "mesh": self.mesh,
                "attested": [
                    list(t)
                    for t in sorted(
                        {tuple(pair) for pairs in attested.values() for pair in pairs}
                    )
                ],
            },
            "issuers": sorted(p.issuers),
            "phi_authorities": sorted(p.authorities),
            "phi_members": sorted(phi),
            "epochs": {e: info["group"] for e, info in sorted(p.epochs.items())},
            "current_epoch": {g: p.current_epoch(g, self.anc) for g in GROUPS},
            "stop": p.stop,
            "recovery": p.recovery,
            "feeds": feeds,
            "void": sorted(void),
            "rejected": dict(sorted(rejected.items())),
            "pending": self.pending,
            "identity_keys": {d: r["identity"] for d, r in members.items()},
            "presence_keys": {d: r["presence"] for d, r in members.items()},
            "wrap_keys": {d: r["wrap"] for d, r in members.items()},
        }
