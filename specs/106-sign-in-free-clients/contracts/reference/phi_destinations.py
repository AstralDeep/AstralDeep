"""Reference for astral.phi-destination/v1: canonical destinations and subjects, approver statements, a roster v2 fold of the PHI entries, upgrade markers, the view, group duties and plans, and the chat PHI key rule.
The fold mirrors the Kernel roster fold for every entry type, and the 105 reference supplies canonical JSON, hashes, signatures and the PHI statement check.
generate_phi_destination_vectors.py builds every vector with it and the tests run each vector back through evaluate.
"""

from __future__ import annotations

import re
from typing import Any

from astral_mesh_ref import attestation, crypto
from astral_mesh_ref import roster as roster_v1
from astral_mesh_ref.cj import b64url_decode, b64url_encode, canonical, strict_loads
from astral_mesh_ref.errors import MeshError

ROSTER_CONTEXT = "astral.roster/v2"
APPROVER_PREFIX = "astral-device:"
APPROVAL_LIFETIME_S = 631152000
MARKER_PREFIX = "phi.capable/"
MARKER_VALUE = {
    "v": 1,
    "detector": "astral.phi-detector/v1",
    "labels": "astral.labels/v2",
}
MARKER_LABELS = {"confidentiality": "private", "integrity": "owner"}
FACTS = ["not_owner_run", "leaves_devices", "unseen_handling"]
SOURCES = ("provider", "connection", "legacy_server", "contact")
ROTATION_REASONS = ("first_write", "epoch_changed", "revoked")
STATUSES = (
    "unsupported",
    "invalid",
    "revoked",
    "authority_removed",
    "expired",
    "legacy_authority",
    "approver_removed",
    "device_removed",
    "contact_missing",
    "approved",
)
GROUP_STATES = ("included", "excluded", "not_upgraded", "not_approved")
TYPES = (
    "genesis",
    "add",
    "remove",
    "rotate",
    "phi_authority",
    "phi_approval",
    "epoch",
    "wrap",
    "stop",
    "resume",
    "rename",
)
PRESENCE = {"genesis", "add", "remove", "phi_authority", "phi_approval", "resume"}
RECOVERY = {"add", "remove", "epoch", "wrap", "stop", "resume", "phi_authority"}
ENTRY_FIELDS = {"v", "type", "mesh", "follows", "clock", "author", "body", "sig"}
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
IPV4 = re.compile(r"(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}")
PCHAR = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-F]{2})+")
ESCAPE = re.compile(r"%([0-9A-F]{2})")
UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]")
DEVICE_ID = re.compile(r"sha256:[0-9a-f]{64}")


class PhiError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _wild() -> PhiError:
    return PhiError("phi_destination.wildcard")


def _bad() -> PhiError:
    return PhiError("phi_destination.malformed")


def _host(text: str) -> tuple[str, bool]:
    if text.startswith("["):
        close = text.find("]")
        inner = text[1:close] if close > 0 else ""
        if close < 0 or not inner or not re.fullmatch(r"[0-9A-Fa-f:.]+", inner):
            raise _wild()
        if close != len(text) - 1:
            raise _bad()
        if inner != "::1":
            raise _bad()
        return "[::1]", True
    if "[" in text or "]" in text:
        raise _wild()
    host = text.lower()
    if not host:
        raise _bad()
    labels = host.split(".")
    if any(label == "" for label in labels):
        raise _wild()
    if labels[-1].isdigit() and all(label.isdigit() for label in labels):
        if len(labels) == 4 and IPV4.fullmatch(host):
            octets = [int(label) for label in labels]
            if all(o <= 255 for o in octets) and octets[0] == 127:
                return host, True
        raise _bad()
    if len(host) > 253 or not all(LABEL.fullmatch(label) for label in labels):
        raise _bad()
    last = labels[-1]
    if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last):
        raise _bad()
    if host == "localhost" or host.endswith(".localhost"):
        return host, True
    if len(labels) < 2:
        raise _bad()
    return host, False


def _path(text: str) -> str:
    if "?" in text or "#" in text:
        raise _bad()
    if text.endswith("/"):
        text = text[:-1]
    if not text:
        return ""
    for segment in text[1:].split("/"):
        if not PCHAR.fullmatch(segment) or segment in (".", ".."):
            raise _bad()
        for escape in ESCAPE.finditer(segment):
            if UNRESERVED.fullmatch(chr(int(escape.group(1), 16))):
                raise _bad()
    return text


def canonical_endpoint(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(not 0x21 <= ord(c) <= 0x7E for c in value)
    ):
        raise _bad()
    scheme, sep, rest = value.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("https", "http"):
        raise _bad()
    slash = rest.find("/")
    hostport, path = (rest, "") if slash < 0 else (rest[:slash], rest[slash:])
    if "*" in value or "?" in hostport:
        raise _wild()
    if "@" in hostport or "#" in hostport:
        raise _bad()
    port_text = None
    if hostport.startswith("["):
        close = hostport.find("]")
        if close >= 0 and hostport[close + 1 :].startswith(":"):
            hostport, port_text = hostport[: close + 1], hostport[close + 2 :]
    elif ":" in hostport:
        hostport, port_text = hostport.rsplit(":", 1)
    host, loopback = _host(hostport)
    port = ""
    if port_text is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,4}", port_text) or int(port_text) > 65535:
            raise _bad()
        if int(port_text) != (443 if scheme == "https" else 80):
            port = ":" + port_text
    if scheme == "http" and not loopback:
        raise _bad()
    target = scheme + "://" + host + port + _path(path)
    return {"target": target, "loopback": loopback}


def is_device_id(value: Any) -> bool:
    return isinstance(value, str) and DEVICE_ID.fullmatch(value) is not None


def contact_subject(mesh_id: Any) -> str:
    if not is_device_id(mesh_id):
        raise _bad()
    return "contact:" + mesh_id


def local_subject(device: Any, endpoint: str) -> str:
    if not is_device_id(device):
        raise _bad()
    return "local:" + device + ":" + endpoint


def _subject(form: str, code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"form": form, "code": code, "target": None, "device": None, **extra}


def parse_subject(subject: Any) -> dict[str, Any]:
    if not isinstance(subject, str) or not subject:
        return _subject("unsupported")
    if is_device_id(subject):
        return _subject("device", device=subject)
    if subject.startswith("contact:"):
        target = subject[len("contact:") :]
        if not is_device_id(target):
            return _subject("invalid", "phi_destination.malformed")
        return _subject("contact", target=target)
    if subject.startswith("local:"):
        device, endpoint = subject[6:77], subject[78:]
        if not is_device_id(device) or subject[77:78] != ":":
            return _subject("invalid", "phi_destination.malformed")
        try:
            canon = canonical_endpoint(endpoint)
        except PhiError as exc:
            return _subject("invalid", exc.code)
        if not canon["loopback"]:
            return _subject("invalid", "phi_destination.malformed")
        if canon["target"] != endpoint:
            return _subject("invalid", "phi_destination.not_canonical")
        return _subject("local", target=endpoint, device=device)
    if subject.lower().startswith(("https://", "http://")):
        try:
            canon = canonical_endpoint(subject)
        except PhiError as exc:
            return _subject("invalid", exc.code)
        if canon["target"] != subject:
            return _subject("invalid", "phi_destination.not_canonical")
        if canon["loopback"]:
            return _subject("invalid", "phi_destination.loopback_unbound")
        return _subject("endpoint", target=subject)
    return _subject("unsupported")


def _endpoint_destination(
    endpoint: Any, kind: str, device: Any, bind: bool
) -> dict[str, Any]:
    canon = canonical_endpoint(endpoint)
    if not canon["loopback"]:
        return {"kind": kind, "target": canon["target"], "on_device": False}
    if not bind:
        return {"kind": kind, "target": canon["target"], "on_device": True}
    return {
        "kind": kind,
        "target": local_subject(device, canon["target"]),
        "on_device": False,
    }


def destination_of(source: Any, device: str) -> dict[str, Any]:
    if not isinstance(source, dict) or source.get("source") not in SOURCES:
        raise _bad()
    if not isinstance(source.get("name"), str):
        raise _bad()
    origin = source["source"]
    if origin == "provider":
        return _endpoint_destination(source.get("base_url"), "model", None, False)
    if origin == "legacy_server":
        return _endpoint_destination(source.get("endpoint"), "service", device, True)
    if origin == "contact":
        return {
            "kind": "contact",
            "target": contact_subject(source.get("mesh_id"))[len("contact:") :],
            "on_device": False,
        }
    use = source.get("use")
    if use not in ("model", "service") or type(source.get("confined")) is not bool:
        raise _bad()
    if use == "service" and source["confined"]:
        return {"kind": "service", "target": None, "on_device": True}
    if source.get("endpoint") is None:
        raise PhiError("phi_destination.no_target")
    return _endpoint_destination(
        source["endpoint"], use, source.get("device"), use == "service"
    )


def propose(source: Any, device: str) -> dict[str, Any]:
    found = destination_of(source, device)
    needs = not found["on_device"]
    subject = None
    if needs:
        subject = (
            contact_subject(found["target"])
            if found["kind"] == "contact"
            else found["target"]
        )
    return {
        "kind": found["kind"],
        "target": found["target"],
        "subject": subject,
        "name": source["name"],
        "needs_approval": needs,
        "facts": list(FACTS) if needs else [],
    }


def approve(source: Any, device: str, device_kind: str) -> dict[str, Any]:
    proposal = propose(source, device)
    if device_kind == "node":
        raise PhiError("phi_group.node_device")
    if not proposal["needs_approval"]:
        raise PhiError("phi_destination.on_device")
    if proposal["kind"] == "contact":
        raise PhiError("phi_destination.contact_unavailable")
    return {"subject_kind": "endpoint", "subject": proposal["subject"]}


def approval_statement(
    approver_scalar: int, subject_kind: str, subject: str, issued_s: int
) -> dict[str, Any]:
    return attestation.build_phi(
        approver_scalar,
        crypto.p256_public(approver_scalar),
        subject_kind,
        subject,
        issued_s,
        issued_s + APPROVAL_LIFETIME_S,
    )


def authority_body(approver_public: bytes, device: str) -> dict[str, Any]:
    return {
        "action": "add",
        "institution": {
            "key": b64url_encode(approver_public),
            "name": APPROVER_PREFIX + device,
        },
    }


def wire_text(value: Any) -> str:
    return canonical(value).decode("utf-8")


def read_wire(text: str) -> Any:
    return strict_loads(text)


def entry_id(entry: dict[str, Any]) -> str:
    return crypto.object_id(
        {k: v for k, v in entry.items() if k not in ("sig", "presence_sig")}
    )


def signing_message(entry: dict[str, Any]) -> bytes:
    return (
        ROSTER_CONTEXT.encode()
        + b"\n"
        + canonical({k: v for k, v in entry.items() if k not in ("sig", "presence_sig")})
    )


def build_entry(
    kind: str,
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
        "type": kind,
        "v": 2,
    }
    message = signing_message(entry)
    if recovery_seed is not None:
        entry["sig"] = b64url_encode(crypto.ed25519_sign(recovery_seed, message))
        return entry
    assert identity_scalar is not None
    entry["sig"] = b64url_encode(crypto.p256_sign(identity_scalar, message))
    if presence_scalar is not None:
        entry["presence_sig"] = b64url_encode(crypto.p256_sign(presence_scalar, message))
    return entry


def _shape(entry: Any) -> None:
    malformed = MeshError("roster.malformed", "entry")
    if not isinstance(entry, dict):
        raise malformed
    if not ENTRY_FIELDS <= set(entry) <= ENTRY_FIELDS | {"presence_sig"}:
        raise malformed
    if type(entry["v"]) is not int or entry["v"] != 2 or entry["type"] not in TYPES:
        raise malformed
    kind, follows = entry["type"], entry["follows"]
    if (
        not isinstance(follows, list)
        or follows != sorted(set(follows))
        or not all(crypto.is_id(f) for f in follows)
    ):
        raise malformed
    if kind == "genesis":
        if entry["mesh"] is not None or follows:
            raise malformed
    elif not crypto.is_id(entry["mesh"]) or not follows:
        raise malformed
    clock = entry["clock"]
    if (
        not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(type(v) is not int or v < 0 for v in clock.values())
    ):
        raise malformed
    author, body = entry["author"], entry["body"]
    if author == "recovery":
        if kind not in RECOVERY:
            raise MeshError("roster.recovery_not_allowed", kind)
        if "presence_sig" in entry:
            raise malformed
    elif not crypto.is_id(author):
        raise malformed
    if not isinstance(body, dict):
        raise malformed
    if author != "recovery":
        wanted = body.get("device") != author if kind == "rename" else kind in PRESENCE
        if wanted != ("presence_sig" in entry):
            raise malformed
    fields = set(body)
    if kind == "genesis":
        if fields != {"device", "epoch", "recovery"}:
            raise malformed
        roster_v1.check_device_record(body["device"], None)
        roster_v1._check_epoch_body(body["epoch"], "personal")
        recovery = body["recovery"]
        if not isinstance(recovery, dict) or set(recovery) != {"sign", "wrap"}:
            raise malformed
        roster_v1._bytes(recovery["sign"], 32, "recovery signing key")
        roster_v1._bytes(recovery["wrap"], 32, "recovery wrap key")
    elif kind in ("add", "rotate"):
        if fields != {"device"}:
            raise malformed
        roster_v1.check_device_record(body["device"], entry["mesh"])
    else:
        # Roster v2 keeps the version 1 validation of every other body.
        roster_v1.check_entry_shape({**entry, "v": 1})


class _Projection:
    def __init__(self) -> None:
        self.admitted: dict[str, dict[str, Any]] = {}
        self.removed: dict[str, dict[str, Any]] = {}
        self.recovery: dict[str, str] | None = None
        self.authorities: dict[str, dict[str, Any]] = {}
        self.chosen: dict[str, dict[str, Any]] = {}
        self.phi_subjects: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[str]] = {}
        self.epochs: dict[str, dict[str, Any]] = {}
        self.stop = 0
        self.resumed = 0

    @property
    def members(self) -> dict[str, dict[str, Any]]:
        return {d: r for d, r in self.admitted.items() if d not in self.removed}

    def phi_members(self, now_s: int | None) -> set[str]:
        return {
            device
            for device in self.members
            if device in self.phi_subjects
            and (now_s is None or self.phi_subjects[device]["not_after_s"] >= now_s)
        }


class _Fold:
    def __init__(self, entries: list[Any], mesh: str, now_s: int | None) -> None:
        self.now_s = now_s
        self.mesh = mesh
        self.entries: dict[str, dict[str, Any]] = {}
        self.rejected: dict[str, str] = {}
        for entry in entries:
            try:
                _shape(entry)
            except MeshError as exc:
                try:
                    self.rejected[entry_id(entry)] = exc.code
                except (MeshError, TypeError):
                    pass
                continue
            self.entries.setdefault(entry_id(entry), entry)
        if mesh not in self.entries or self.entries[mesh]["type"] != "genesis":
            raise MeshError("roster.no_genesis", mesh)
        for eid in list(self.entries):
            e = self.entries[eid]
            if e["type"] == "genesis" and eid != mesh:
                self.rejected[eid] = "roster.extra_genesis"
                del self.entries[eid]
            elif e["type"] != "genesis" and e["mesh"] != mesh:
                self.rejected[eid] = "roster.wrong_mesh"
                del self.entries[eid]
        self.anc: dict[str, frozenset[str]] = {}
        self.topo: list[str] = []
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

    def _newest(self, ids: list[str]) -> str:
        return max(
            ids,
            key=lambda i: (
                self.entries[i]["clock"]["ms"],
                self.entries[i]["clock"]["n"],
                i,
            ),
        )

    def _live(self, adds: list[str], removes: list[str]) -> list[str]:
        return [a for a in adds if all(r in self.anc[a] for r in removes)]

    def _project(self, ids: Any) -> _Projection:
        p = _Projection()
        auth_adds: dict[str, list[str]] = {}
        auth_removes: dict[str, list[str]] = {}
        grants: dict[str, list[str]] = {}
        revokes: dict[str, list[str]] = {}
        renames: dict[str, list[str]] = {}
        for eid in sorted(ids, key=self.index.__getitem__):
            e = self.entries[eid]
            kind, body = e["type"], e["body"]
            if kind in ("genesis", "add"):
                record = body["device"]
                device = crypto.device_id(b64url_decode(record["identity"]))
                p.admitted[device] = {**record, "feeds": [record["ops"]]}
                if kind == "genesis":
                    p.recovery = body["recovery"]
                    p.epochs[eid] = {"group": "personal", "clock": e["clock"]}
            elif kind == "rotate":
                record = body["device"]
                device = crypto.device_id(b64url_decode(record["identity"]))
                if device in p.admitted:
                    feeds = p.admitted[device]["feeds"]
                    p.admitted[device] = {**record, "feeds": feeds + [record["ops"]]}
            elif kind == "remove":
                p.removed[body["device"]] = {"cutoff": body["cutoff"], "entry": eid}
            elif kind == "phi_authority":
                if body["action"] == "add":
                    auth_adds.setdefault(body["institution"]["key"], []).append(eid)
                else:
                    auth_removes.setdefault(body["key"], []).append(eid)
            elif kind == "phi_approval":
                subject = (
                    body["attestation"]["subject"]
                    if body["action"] == "grant"
                    else body["subject"]
                )
                p.history.setdefault(subject, []).append(eid)
                target = grants if body["action"] == "grant" else revokes
                target.setdefault(subject, []).append(eid)
            elif kind == "epoch":
                p.epochs[eid] = {"group": body["group"], "clock": e["clock"]}
            elif kind == "rename":
                renames.setdefault(body["device"], []).append(eid)
            elif kind == "stop":
                p.stop = max(p.stop, body["counter"])
            elif kind == "resume":
                p.resumed = max(p.resumed, body["counter"])
        for device, named in renames.items():
            if device in p.admitted:
                label = self.entries[self._newest(named)]["body"]["label"]
                p.admitted[device] = {**p.admitted[device], "label": label}
        for key, adds in auth_adds.items():
            live = self._live(adds, auth_removes.get(key, []))
            if live:
                chosen = self.entries[self._newest(live)]
                p.authorities[key] = {
                    "name": chosen["body"]["institution"]["name"],
                    "author": chosen["author"],
                }
        for subject, adds in grants.items():
            live = self._live(adds, revokes.get(subject, []))
            if not live:
                continue
            chosen = self._newest(live)
            att = self.entries[chosen]["body"]["attestation"]
            p.chosen[subject] = {
                "entry": chosen,
                "institution": att["institution"],
                "not_after_s": att["not_after_s"],
            }
            if att["institution"] in p.authorities:
                p.phi_subjects[subject] = p.chosen[subject]
        return p

    def _check(self, e: dict[str, Any], p: _Projection | None) -> str | None:
        kind, body = e["type"], e["body"]
        message = signing_message(e)
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
            return self._check_body(e, p)
        except MeshError as exc:
            return exc.code

    def _check_body(self, e: dict[str, Any], p: _Projection) -> str | None:
        kind, body = e["type"], e["body"]
        phi = p.phi_members(self.now_s)
        if kind == "add":
            device = crypto.device_id(b64url_decode(body["device"]["identity"]))
            if device in p.admitted:
                return (
                    "roster.already_member"
                    if device in p.members
                    else "roster.device_removed"
                )
            if any(body["device"]["ops"] in r["feeds"] for r in p.admitted.values()):
                return "roster.duplicate_key"
        elif kind == "rotate":
            record = body["device"]
            device = crypto.device_id(b64url_decode(record["identity"]))
            if e["author"] != device or device not in p.members:
                return "roster.unauthorized_author"
            if record["identity"] != p.members[device]["identity"] or any(
                record["ops"] in r["feeds"] for r in p.admitted.values()
            ):
                return "roster.duplicate_key"
        elif kind == "remove":
            if body["device"] not in p.members:
                return "roster.unknown_device"
            if set(body["cutoff"]) != set(p.members[body["device"]]["feeds"]):
                return "roster.cutoff_incomplete"
        elif kind == "phi_authority":
            if body["action"] == "remove" and body["key"] not in p.authorities:
                return "roster.unknown_authority"
        elif kind == "phi_approval" and body["action"] == "grant":
            att = body["attestation"]
            attestation.verify_phi(att, set(p.authorities), now_s=None)
            if att["subject_kind"] == "device" and att["subject"] not in p.members:
                return "roster.unknown_device"
        elif kind in ("epoch", "wrap"):
            group = body["group"]
            allowed = set(p.members) if group == "personal" else phi
            if kind == "epoch":
                if any(item["device"] not in allowed for item in body["wraps"]):
                    return "roster.epoch_wrong_members"
            else:
                if body["device"] not in allowed:
                    return "roster.epoch_wrong_members"
                for grant in body["grants"]:
                    info = p.epochs.get(grant["epoch"])
                    if info is None or info["group"] != group:
                        return "roster.unknown_epoch"
            if group == "phi" and e["author"] not in phi:
                return "roster.not_phi_member"
        elif kind == "rename" and body["device"] not in p.members:
            return "roster.unknown_device"
        elif kind == "stop" and body["counter"] <= p.stop:
            return "roster.stop_not_rising"
        elif kind == "resume" and body["counter"] > p.stop:
            return "roster.resume_not_stopped"
        return None

    def run(self) -> dict[str, Any]:
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
                code = self._check(e, projection)
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
            fresh = {
                eid
                for eid in active
                if self.entries[eid]["type"] not in ("remove", "genesis")
                and self.entries[eid]["author"] != "recovery"
                and any(
                    device == self.entries[eid]["author"] and eid not in self.anc[r]
                    for r, device in removals
                )
            }
            if not fresh:
                return self._export(active, active_set, void, rejected)
            void |= fresh

    def _export(
        self,
        active: list[str],
        active_set: set[str],
        void: set[str],
        rejected: dict[str, str],
    ) -> dict[str, Any]:
        p = self._project(active_set)
        recipients: dict[str, set[str]] = {}
        for eid in active:
            e = self.entries[eid]
            if e["type"] == "genesis":
                recipients.setdefault(eid, set()).update(
                    w["device"] for w in e["body"]["epoch"]["wraps"]
                )
            elif e["type"] == "epoch":
                recipients.setdefault(eid, set()).update(
                    w["device"] for w in e["body"]["wraps"]
                )
            elif e["type"] == "wrap":
                for grant in e["body"]["grants"]:
                    recipients.setdefault(grant["epoch"], set()).add(e["body"]["device"])

        def current(group: str) -> str | None:
            candidates = [e for e, info in p.epochs.items() if info["group"] == group]
            heads = [
                e
                for e in candidates
                if not any(e in self.anc[o] for o in candidates if o != e)
            ]
            if not heads:
                return None
            return self._newest(heads)

        return {
            "mesh": self.mesh,
            "now_s": self.now_s,
            "members": {d: dict(r) for d, r in sorted(p.members.items())},
            "admitted": sorted(p.admitted),
            "removed": sorted(p.removed),
            "phi_members": sorted(p.phi_members(self.now_s)),
            "authorities": p.authorities,
            "chosen": p.chosen,
            "history": p.history,
            "epochs": {e: info["group"] for e, info in sorted(p.epochs.items())},
            "current_epoch": {g: current(g) for g in ("personal", "phi")},
            "recipients": {e: sorted(r) for e, r in sorted(recipients.items())},
            "rejected": dict(sorted(rejected.items())),
            "void": sorted(void),
            "pending": self.pending,
        }


def fold(entries: list[Any], mesh: str, now_s: int | None) -> dict[str, Any]:
    return _Fold(entries, mesh, now_s).run()


def roster_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "members": sorted(state["members"]),
        "removed": state["removed"],
        "phi_members": state["phi_members"],
        "phi_authorities": {
            key: info["name"] for key, info in sorted(state["authorities"].items())
        },
        "epochs": state["epochs"],
        "current_epoch": state["current_epoch"],
        "recipients": state["recipients"],
        "rejected": state["rejected"],
        "void": state["void"],
        "pending": state["pending"],
    }


def _marker_code(state: dict[str, Any], event: dict[str, Any]) -> str | None:
    body = event["body"]
    key = body["key"]
    named = key[len(MARKER_PREFIX) :]
    value = body.get("value")
    version = value.get("v") if isinstance(value, dict) else None
    if (
        event["scope"] != "mesh"
        or event["labels"] != MARKER_LABELS
        or set(body) != {"key", "value"}
        or not is_device_id(named)
        or type(version) is not int
        or version < 1
        or (version == 1 and value != MARKER_VALUE)
    ):
        return "phi_marker.malformed"
    member = state["members"].get(named)
    if member is None:
        return "phi_marker.not_member"
    if event["feed"] not in member["feeds"]:
        return "phi_marker.foreign_feed"
    return None


def markers(state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    devices: set[str] = set()
    ignored: dict[str, str] = {}
    for event in events:
        body = event.get("body")
        if (
            event.get("type") != "settings.set"
            or not isinstance(body, dict)
            or not isinstance(body.get("key"), str)
            or not body["key"].startswith(MARKER_PREFIX)
        ):
            continue
        code = _marker_code(state, event)
        if code is None:
            devices.add(body["key"][len(MARKER_PREFIX) :])
        else:
            ignored[event["id"]] = code
    return {"devices": sorted(devices), "ignored": dict(sorted(ignored.items()))}


def _approver_of(state: dict[str, Any], institution: str) -> str | None:
    info = state["authorities"].get(institution)
    if info is None or info["name"] != APPROVER_PREFIX + info["author"]:
        return None
    return info["author"]


def _status(
    state: dict[str, Any], subject: str, parsed: dict[str, Any], contacts: list[str]
) -> tuple[str, str | None, str | None]:
    if parsed["form"] in ("unsupported", "invalid"):
        return parsed["form"], parsed["code"], None
    chosen = state["chosen"].get(subject)
    if chosen is None:
        return "revoked", None, None
    institution = chosen["institution"]
    if institution not in state["authorities"]:
        return "authority_removed", None, None
    approver = _approver_of(state, institution)
    now_s = state["now_s"]
    if now_s is not None and chosen["not_after_s"] < now_s:
        return "expired", None, approver
    if approver is None:
        return "legacy_authority", None, None
    if approver not in state["members"]:
        return "approver_removed", None, approver
    if parsed["form"] == "local" and parsed["device"] not in state["members"]:
        return "device_removed", None, approver
    if parsed["form"] == "contact" and parsed["target"] not in contacts:
        return "contact_missing", None, approver
    return "approved", None, approver


def view(
    state: dict[str, Any], events: list[dict[str, Any]], contacts: list[str]
) -> dict[str, Any]:
    marked = markers(state, events)
    upgraded = set(marked["devices"])
    phi = set(state["phi_members"])
    history = {s for s, ids in state["history"].items() if ids}
    group = []
    for device, record in state["members"].items():
        if device in phi:
            row_state = "included"
        elif device in history:
            row_state = "excluded"
        elif device not in upgraded:
            row_state = "not_upgraded"
        else:
            row_state = "not_approved"
        chosen = state["chosen"].get(device)
        approved_by = (
            _approver_of(state, chosen["institution"])
            if device in phi and chosen
            else None
        )
        group.append(
            {
                "device": device,
                "kind": record["kind"],
                "state": row_state,
                "approved_by": approved_by,
            }
        )
    destinations = []
    for subject in sorted(history):
        if is_device_id(subject):
            continue
        parsed = parse_subject(subject)
        status, code, approved_by = _status(state, subject, parsed, contacts)
        destinations.append(
            {
                "subject": subject,
                "form": parsed["form"],
                "target": parsed["target"],
                "status": status,
                "code": code,
                "approved_by": approved_by,
            }
        )
    current = state["current_epoch"]["phi"]
    local = {}
    for device in state["members"]:
        if device in phi and current and device in state["recipients"].get(current, []):
            local[device] = None
        elif device not in phi and device in history:
            local[device] = "phi.device_excluded"
        else:
            local[device] = "phi.group_not_ready"
    return {
        "state": state,
        "markers": marked,
        "upgraded": upgraded,
        "history": history,
        "group": group,
        "destinations": destinations,
        "local": local,
        "contacts": list(contacts),
    }


def _phi_epochs(state: dict[str, Any]) -> list[str]:
    return [e for e, group in state["epochs"].items() if group == "phi"]


def _needs_epoch(state: dict[str, Any], members: set[str]) -> bool:
    current = state["current_epoch"]["phi"]
    if current is None:
        return True
    return bool(set(state["recipients"].get(current, [])) - members)


def _wraps_from(
    state: dict[str, Any], holder: str, targets: list[str]
) -> dict[str, list[str]]:
    out = {}
    for target in targets:
        if target == holder:
            continue
        lacking = [
            epoch
            for epoch in _phi_epochs(state)
            if holder in state["recipients"].get(epoch, [])
            and target not in state["recipients"].get(epoch, [])
        ]
        if lacking:
            out[target] = sorted(lacking)
    return out


def duties(v: dict[str, Any], device: str) -> dict[str, Any]:
    state = v["state"]
    none = {"grants": [], "epoch": False, "wraps": {}}
    if device not in v["upgraded"]:
        return none
    phi = set(state["phi_members"])
    grants = []
    if state["members"][device]["kind"] != "node":
        grants = sorted(
            d
            for d in state["members"]
            if d in v["upgraded"] and d not in v["history"] and d not in phi
        )
    keepers = sorted(phi & v["upgraded"])
    if not keepers or keepers[0] != device:
        return {**none, "grants": grants}
    return {
        "grants": grants,
        "epoch": _needs_epoch(state, phi),
        "wraps": _wraps_from(state, device, sorted(phi)),
    }


def registered(state: dict[str, Any], device: str) -> bool:
    return any(
        info["author"] == device and info["name"] == APPROVER_PREFIX + device
        for info in state["authorities"].values()
    )


def plan_include(v: dict[str, Any], device: str, targets: list[str]) -> dict[str, Any]:
    state = v["state"]
    phi = set(state["phi_members"])
    if state["members"][device]["kind"] == "node":
        raise PhiError("phi_group.node_device")
    for target in targets:
        if target not in state["members"]:
            raise PhiError("phi_group.unknown_device")
        if target in phi:
            raise PhiError("phi_group.already_included")
        if target not in v["upgraded"]:
            raise PhiError("phi_group.not_upgraded")
    after = phi | set(targets)
    inside = device in after
    return {
        "register_approver": not registered(state, device),
        "grants": sorted(targets),
        "epoch": inside and _needs_epoch(state, after),
        "wraps": _wraps_from(state, device, sorted(targets)) if inside else {},
    }


def plan_exclude(v: dict[str, Any], device: str, target: str) -> dict[str, Any]:
    state = v["state"]
    phi = set(state["phi_members"])
    if state["members"][device]["kind"] == "node":
        raise PhiError("phi_group.node_device")
    if target not in state["members"]:
        raise PhiError("phi_group.unknown_device")
    if target not in phi:
        raise PhiError("phi_group.not_included")
    after = phi - {target}
    return {
        "revoke": target,
        "rotate": bool(after) and device in after,
        "rekey_needed": bool(after) and device not in after,
    }


def phi_destination_allowed(v: dict[str, Any], device: str, kind: Any, target: Any) -> bool:
    state = v["state"]
    if device not in state["phi_members"]:
        return False
    rows = {row["subject"]: row["status"] for row in v["destinations"]}
    if kind == "device":
        return target in state["phi_members"]
    if kind == "contact":
        try:
            subject = contact_subject(target)
        except PhiError:
            return False
        return rows.get(subject) == "approved"
    if kind not in ("model", "service"):
        return False
    if isinstance(target, str) and target.startswith("local:"):
        if kind != "service" or parse_subject(target)["form"] != "local":
            return False
        return rows.get(target) == "approved"
    try:
        canon = canonical_endpoint(target)
    except PhiError:
        return False
    if canon["loopback"]:
        return kind == "model"
    return rows.get(canon["target"]) == "approved"


def _order(key: dict[str, Any]) -> tuple[int, int, str, int]:
    return (key["clock"]["ms"], key["clock"]["n"], key["feed"], key["seq"])


def chat_write_key(given: dict[str, Any]) -> dict[str, Any]:
    group = given["group"]
    if group == "phi" and given["local"] is not None:
        raise PhiError(given["local"])
    current = given["current"][group]
    if current is None:
        raise PhiError("phi.group_not_ready")
    found = [
        k for k in given["keys"] if k["scope"] == given["chat"] and k["group"] == group
    ]
    if found:
        latest = max(found, key=_order)
        key, epoch = latest["key"], latest["epoch"]
    elif given["chat_create"]["group"] == group:
        key, epoch = given["chat_create"]["key"], given["chat_create"]["epoch"]
    else:
        return {"rotate": "first_write", "epoch": current}
    if epoch != current:
        return {"rotate": "epoch_changed", "epoch": current}
    return {"use": key}


def _ok(fn: Any, *args: Any) -> dict[str, Any]:
    try:
        return {"ok": fn(*args)}
    except PhiError as exc:
        return {"error": exc.code}


def run_step(
    given_case: dict[str, Any],
    entries: list[Any],
    events: list[dict[str, Any]],
    given: dict[str, Any],
) -> dict[str, Any]:
    state = fold(entries, given_case["mesh"], given_case["now_s"])
    v = view(state, events, given["contacts"])
    out_plans = []
    for plan in given["plans"]:
        if plan["op"] == "include":
            out_plans.append(_ok(plan_include, v, plan["device"], plan["targets"]))
        else:
            out_plans.append(_ok(plan_exclude, v, plan["device"], plan["target"]))
    return {
        "roster": roster_summary(state),
        "markers": v["markers"],
        "group": v["group"],
        "destinations": v["destinations"],
        "local": v["local"],
        "duties": {d: duties(v, d) for d in state["members"]},
        "queries": [
            phi_destination_allowed(v, q["device"], q["kind"], q["target"])
            for q in given["queries"]
        ],
        "plans": out_plans,
    }


def evaluate(case: dict[str, Any]) -> dict[str, Any]:
    kind, given = case["kind"], case["input"]
    if kind == "phi_endpoint":
        return _ok(canonical_endpoint, given["value"])
    if kind == "phi_subject":
        return {"ok": parse_subject(given["subject"])}
    if kind == "phi_contact":
        return _ok(lambda m: {"subject": contact_subject(m)}, given["mesh_id"])
    if kind == "phi_propose":
        return _ok(propose, given["source"], given["device"])
    if kind == "phi_approve":
        return _ok(approve, given["source"], given["device"], given["device_kind"])
    if kind == "phi_statement":
        scalar = int(given["approver_scalar_hex"], 16)
        return {
            "ok": {
                "statement": approval_statement(
                    scalar, given["subject_kind"], given["subject"], given["issued_s"]
                ),
                "authority": wire_text(
                    authority_body(crypto.p256_public(scalar), given["device"])
                ),
            }
        }
    if kind == "phi_chat_key":
        return _ok(chat_write_key, given)
    if kind == "phi_sequence":
        steps = []
        entries: list[Any] = []
        events: list[dict[str, Any]] = []
        for step in given["steps"]:
            entries = entries + [read_wire(text) for text in step["entries"]]
            events = events + step["events"]
            steps.append(run_step(given, entries, events, step))
        return {"steps": steps}
    raise ValueError(kind)
