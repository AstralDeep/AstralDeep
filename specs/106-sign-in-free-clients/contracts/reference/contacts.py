"""Reference for contacts and grants carried on settings.set: the key grammar, the invitation, the roster digest and comparison code, the contact and grant folds, roster following and the reader checks.
It reuses the consent primitives of connection.py, the roster v2 fold and the preserved 105 helpers, and vectors/contacts-v1.json pins it for every other implementation.
"""

from __future__ import annotations

import hashlib
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
import caller_ticket
import connection
import phi_destinations as phi
from astral_mesh_ref import crypto
from astral_mesh_ref import fold as mesh_fold
from astral_mesh_ref.cj import b64url_decode, b64url_encode, canonical, strict_loads
from astral_mesh_ref.errors import MeshError

CARRIER = connection.CARRIER
LABELS = connection.LABELS
CONTACT_KIND = "contact"
SHARE_KIND = "share"
CONTACT_PREFIX = "contact/"
MARK_PREFIX = "contact.roster/"
GRANT_PREFIX = "share/"
REVOKED_PREFIX = "share.revoked/"
RESERVED_PREFIXES = ("contact/", "contact.", "share/", "share.")
INVITE_CONTEXT = "astral.contact/v1"
CODE_CONTEXT = b"astral.contact.code/v1\n"
INVITE_TEXT = "astral-contact:v1:"
INVITE_LINK = "https://astraldeep.github.io/contact/#"
INVITE_FIELDS = {"v", "mesh", "device", "identity", "addr", "nonce", "expires_s", "name", "sig"}
NONCE_BYTES = 16
MIN_LIFETIME_S = 60
DEFAULT_LIFETIME_S = 1800
MAX_LIFETIME_S = 86400
MAX_ADDR = 1024
MAX_NAME = 64
MAX_CONTACTS = 256
MAX_GRANTS = 1024
MAX_MARK_HEADS = 64
MAX_MARK_REMOVED = 256
CODE_MODULUS = 10**12
CONTACT_FIELDS = {"v", "mesh", "name", "state", "heads", "count", "ms"}
MARK_FIELDS = {"v", "heads", "count", "removed"}
GRANT_FIELDS = {"v", "id", "contact", "item", "kind", "phi", "supersedes", "ms"}
STATES = ("unverified", "verified")
RESERVED_ITEM_KINDS = ("surface", "artifact", "file")
MARK_WRITER_KINDS = ("desktop", "pocket")
READER_KINDS = ("desktop", "pocket")

UNVERIFIABLE_CODES = (
    "roster.malformed",
    "roster.unauthorized_author",
    "roster.bad_signature",
    "roster.bad_presence_signature",
    "roster.author_removed",
    "roster.recovery_not_allowed",
    "roster.extra_genesis",
    "roster.wrong_mesh",
)
STRUCTURAL_CODES = (
    "roster.malformed",
    "roster.extra_genesis",
    "roster.wrong_mesh",
)
HONEST_CODES = (
    "roster.unknown_device",
    "roster.cutoff_incomplete",
    "roster.unknown_authority",
    "roster.epoch_wrong_members",
    "roster.not_phi_member",
    "roster.unknown_epoch",
    "roster.resume_not_stopped",
    "attestation.unknown_authority",
    "attestation.expired",
)
TRIGGER_CODES = (
    "roster.already_member",
    "roster.device_removed",
    "roster.duplicate_key",
    "roster.stop_not_rising",
    "attestation.bad_signature",
    "attestation.malformed",
)

CONTACT_CHECKS = (
    "contact.malformed",
    "contact.self",
    "contact.writer_not_member",
    "contact.writer_kind",
    "contact.consent_missing",
    "contact.consent_wrong_action",
    "contact.consent_unknown_device",
    "contact.consent_node_device",
    "contact.consent_foreign_feed",
    "contact.consent_bad_signature",
    "contact.consent_stale",
    "contact.consent_reused",
    "contact.too_many",
)
SHARE_CHECKS = (
    "share.malformed",
    "share.writer_not_member",
    "share.item_kind_unsupported",
    "share.supersedes_unknown",
    "share.id_reused",
    "share.consent_missing",
    "share.consent_wrong_action",
    "share.consent_unknown_device",
    "share.consent_node_device",
    "share.consent_foreign_feed",
    "share.consent_bad_signature",
    "share.consent_stale",
    "share.consent_reused",
    "share.too_many",
)
INVITE_CODES = (
    "contact.invite_malformed",
    "contact.invite_bad_signature",
    "contact.invite_expired",
    "contact.invite_lifetime",
)
FOLLOW_CODES = ("contact.roster_rejected", "contact.heads_missing", "roster.no_genesis")
STATE_CODES = ("contact.unverified", "share.contact_unverified")
READER_CODES = ("share.reader_kind", "share.device_removed", "share.roster_stale")
CODES = tuple(
    dict.fromkeys(
        CONTACT_CHECKS
        + SHARE_CHECKS
        + INVITE_CODES
        + FOLLOW_CODES
        + STATE_CODES
        + READER_CODES
    )
)

HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
VISIBLE = re.compile(r"[\x21-\x7e]+")


def _fail(code: str, detail: str = "") -> MeshError:
    return MeshError(code, detail)


def _int(value: Any) -> bool:
    return type(value) is int


def reserved_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(RESERVED_PREFIXES)


def _family(key: str) -> str:
    return "contact" if key.startswith("contact") else "share"


def parse_key(key: Any) -> dict[str, Any]:
    if not reserved_key(key):
        return {"form": "other"}
    forms = (
        (CONTACT_PREFIX, HEX64, "contact"),
        (MARK_PREFIX, HEX64, "mark"),
        (GRANT_PREFIX, HEX32, "grant"),
        (REVOKED_PREFIX, HEX32, "revoked"),
    )
    for prefix, pattern, form in forms:
        if key.startswith(prefix) and pattern.fullmatch(key[len(prefix) :]):
            return {"form": form, "id": key[len(prefix) :]}
    raise _fail(_family(key) + ".malformed", "reserved key")


def is_event(event: dict[str, Any]) -> bool:
    body = event.get("body")
    return (
        event.get("type") == CARRIER
        and isinstance(body, dict)
        and reserved_key(body.get("key"))
    )


def mesh_of(hex_id: str) -> str:
    return "sha256:" + hex_id


def digest(value: Any) -> str:
    return connection.digest(value)


def classify(code: str) -> str:
    if code in UNVERIFIABLE_CODES:
        return "unverifiable"
    if code in TRIGGER_CODES:
        return "trigger"
    return "honest"


def _name_ok(name: Any) -> bool:
    return (
        isinstance(name, str)
        and 1 <= len(name) <= MAX_NAME
        and not any(caller_ticket.refused_in_label(c) for c in name)
    )


def _sorted_ids(items: Any, limit: int, check: Any) -> bool:
    return (
        isinstance(items, list)
        and len(items) <= limit
        and all(check(i) for i in items)
        and all(a < b for a, b in zip(items, items[1:]))
    )


def check_contact(key_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CONTACT_FIELDS:
        raise _fail("contact.malformed", "fields")
    if not _int(value["v"]) or value["v"] != 1:
        raise _fail("contact.malformed", "v")
    if value["mesh"] != mesh_of(key_id) or not crypto.is_id(value["mesh"]):
        raise _fail("contact.malformed", "mesh")
    if not _name_ok(value["name"]):
        raise _fail("contact.malformed", "name")
    if value["state"] not in STATES:
        raise _fail("contact.malformed", "state")
    if not _sorted_ids(value["heads"], MAX_MARK_HEADS, crypto.is_id):
        raise _fail("contact.malformed", "heads")
    if not _int(value["count"]) or value["count"] < 0 or not _int(value["ms"]) or value["ms"] < 0:
        raise _fail("contact.malformed", "count or ms")
    verified = value["state"] == "verified"
    if verified != (bool(value["heads"]) and value["count"] >= len(value["heads"])):
        raise _fail("contact.malformed", "roster state")
    if not verified and (value["heads"] or value["count"]):
        raise _fail("contact.malformed", "roster state")
    return value


def check_mark(key_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MARK_FIELDS:
        raise _fail("contact.malformed", "mark fields")
    if not _int(value["v"]) or value["v"] != 1:
        raise _fail("contact.malformed", "mark v")
    if not _sorted_ids(value["heads"], MAX_MARK_HEADS, crypto.is_id):
        raise _fail("contact.malformed", "mark heads")
    if not _int(value["count"]) or value["count"] < len(value["heads"]):
        raise _fail("contact.malformed", "mark count")
    if not _sorted_ids(value["removed"], MAX_MARK_REMOVED, crypto.is_id):
        raise _fail("contact.malformed", "mark removed")
    return value


def check_grant(key_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != GRANT_FIELDS:
        raise _fail("share.malformed", "fields")
    if not _int(value["v"]) or value["v"] != 1 or value["id"] != key_id:
        raise _fail("share.malformed", "v or id")
    if not crypto.is_id(value["contact"]) or value["kind"] != "view":
        raise _fail("share.malformed", "contact or kind")
    if type(value["phi"]) is not bool or not _int(value["ms"]) or value["ms"] < 0:
        raise _fail("share.malformed", "phi or ms")
    supersedes = value["supersedes"]
    if supersedes is not None and (
        not isinstance(supersedes, str)
        or not HEX32.fullmatch(supersedes)
        or supersedes == key_id
    ):
        raise _fail("share.malformed", "supersedes")
    item = value["item"]
    if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
        raise _fail("share.malformed", "item")
    if item["kind"] in RESERVED_ITEM_KINDS:
        raise _fail("share.item_kind_unsupported", item["kind"])
    if item["kind"] != "chat" or set(item) != {"kind", "chat"} or not crypto.is_id(item["chat"]):
        raise _fail("share.malformed", "item")
    return value


def check_revoked(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"v", "ms"}:
        raise _fail("share.malformed", "revoked fields")
    if not _int(value["v"]) or value["v"] != 1 or not _int(value["ms"]) or value["ms"] < 0:
        raise _fail("share.malformed", "revoked v or ms")
    return value


def _reprefix(error: MeshError, prefix: str) -> MeshError:
    return _fail(prefix + error.code.removeprefix("connection."), error.detail)


def check_consent(
    event: dict[str, Any],
    value: Any,
    kind: str,
    members: dict[str, Any],
    used: set[bytes],
) -> dict[str, Any]:
    prefix = kind + "."
    body = event["body"]
    if "consent" not in body:
        raise _fail(prefix + "consent_missing")
    try:
        consent = connection._check_consent_shape(body["consent"])
    except MeshError as error:
        raise _reprefix(error, prefix) from error
    if consent["kind"] != kind or consent["digest"] != digest(value):
        raise _fail(prefix + "consent_wrong_action")
    member = members.get(consent["device"])
    if member is None:
        raise _fail(prefix + "consent_unknown_device", consent["device"])
    if member["kind"] == "node":
        raise _fail(prefix + "consent_node_device")
    if event["feed"] not in member["feeds"]:
        raise _fail(prefix + "consent_foreign_feed")
    if not connection._signed_by(consent, member["presence"]):
        raise _fail(prefix + "consent_bad_signature")
    if not connection._in_window(consent, event):
        raise _fail(prefix + "consent_stale")
    if connection._consent_key(consent) in used:
        raise _fail(prefix + "consent_reused")
    return consent


class _Roster:
    def __init__(self, state: dict[str, Any]) -> None:
        self.members = state["members"]
        self.removed = state["removed"]
        self.feed_owner = {f: d for d, m in self.members.items() for f in m["feeds"]}
        self.any_owner = {
            f: d for d, m in {**self.removed, **self.members}.items() for f in m["feeds"]
        }

    def writer(self, event: dict[str, Any], prefix: str) -> str:
        owner = self.any_owner.get(event["feed"])
        if owner is None:
            raise _fail(prefix + ".writer_not_member", event["feed"])
        return owner

    def mark_writer(self, event: dict[str, Any]) -> str:
        owner = self.feed_owner.get(event["feed"])
        if owner is None:
            raise _fail("contact.writer_not_member", event["feed"])
        if self.members[owner]["kind"] not in MARK_WRITER_KINDS:
            raise _fail("contact.writer_kind", self.members[owner]["kind"])
        return owner


def _shape(event: dict[str, Any], fields: tuple[set[str], ...]) -> dict[str, Any]:
    body = event["body"]
    if event.get("scope") != "mesh" or event.get("labels") != LABELS:
        raise _fail(_family(body["key"]) + ".malformed", "scope or labels")
    if set(body) not in fields:
        raise _fail(_family(body["key"]) + ".malformed", "body")
    return body


def _check_contact_event(
    event: dict[str, Any], roster: _Roster, own: str | None, used: set[bytes]
) -> dict[str, Any]:
    key = parse_key(event["body"]["key"])
    plain, with_consent = {"key", "value"}, {"key", "value", "consent"}
    if key["form"] == "mark":
        body = _shape(event, (plain,))
        mark = check_mark(key["id"], body["value"])
        if mesh_of(key["id"]) == own:
            raise _fail("contact.self")
        roster.mark_writer(event)
        return {"form": "mark", "id": key["id"], "mark": mark}
    if event["body"].get("value") is None:
        _shape(event, (plain,))
        roster.writer(event, "contact")
        return {"form": "remove", "id": key["id"]}
    body = _shape(event, (plain, with_consent))
    record = check_contact(key["id"], body["value"])
    if record["mesh"] == own:
        raise _fail("contact.self")
    if record["state"] == "unverified":
        if "consent" in body:
            raise _fail("contact.malformed", "consent on unverified")
        roster.writer(event, "contact")
        return {"form": "put", "id": key["id"], "consent": None}
    consent = check_consent(event, record, CONTACT_KIND, roster.members, used)
    return {"form": "put", "id": key["id"], "consent": consent}


def fold_contacts(state: dict[str, Any]) -> dict[str, Any]:
    roster = _Roster(state)
    own = state.get("mesh")
    cap = state.get("limits", {}).get("contacts", MAX_CONTACTS)
    heads: dict[str, tuple[Any, dict[str, Any], dict[str, Any]]] = {}
    marks: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    ignored: dict[str, str] = {}
    used: set[bytes] = set()
    for event in sorted(
        (e for e in state["events"] if is_event(e) and _family(e["body"]["key"]) == "contact"),
        key=mesh_fold.order_key,
    ):
        rank = mesh_fold.order_key(event)
        try:
            checked = _check_contact_event(event, roster, own, used)
        except MeshError as error:
            ignored[event["id"]] = error.code
            continue
        if checked["form"] == "mark":
            marks.setdefault(checked["id"], []).append((rank, checked["mark"]))
            continue
        if checked.get("consent") is not None:
            used.add(connection._consent_key(checked["consent"]))
        heads[checked["id"]] = (rank, event, checked)
    live = sorted(
        ((rank, event, checked) for rank, event, checked in heads.values() if checked["form"] == "put"),
        key=lambda item: item[0],
    )
    for _, event, _ in live[cap:]:
        ignored[event["id"]] = "contact.too_many"
    contacts = [
        {
            "mesh": event["body"]["value"]["mesh"],
            "event": event["id"],
            "state": event["body"]["value"]["state"],
            "record": event["body"]["value"],
            "approved_by": checked["consent"]["device"] if checked["consent"] else None,
        }
        for _, event, checked in live[:cap]
    ]
    return {
        "contacts": sorted(contacts, key=lambda c: c["mesh"]),
        "marks": {hex_id: merge_marks(found) for hex_id, found in sorted(marks.items())},
        "ignored": dict(sorted(ignored.items())),
    }


def merge_marks(found: list[tuple[Any, dict[str, Any]]]) -> dict[str, Any]:
    _, best = max(found, key=lambda item: (item[1]["count"], item[0]))
    removed = sorted({d for _, mark in found for d in mark["removed"]})
    return {"heads": best["heads"], "count": best["count"], "removed": removed}


def _check_share_event(
    event: dict[str, Any], roster: _Roster, used: set[bytes], taken: set[str]
) -> dict[str, Any]:
    key = parse_key(event["body"]["key"])
    plain = {"key", "value"}
    if key["form"] == "revoked":
        body = _shape(event, (plain,))
        value = check_revoked(body["value"])
        roster.writer(event, "share")
        return {"form": "revoked", "id": key["id"], "value": value}
    if event["body"].get("value") is None:
        raise _fail("share.malformed", "grants are revoked, never removed")
    body = _shape(event, (plain, {"key", "value", "consent"}))
    value = check_grant(key["id"], body["value"])
    consent = check_consent(event, value, SHARE_KIND, roster.members, used)
    if key["id"] in taken:
        raise _fail("share.id_reused", key["id"])
    return {"form": "grant", "id": key["id"], "value": value, "consent": consent}


def fold_shares(state: dict[str, Any]) -> dict[str, Any]:
    roster = _Roster(state)
    cap = state.get("limits", {}).get("grants", MAX_GRANTS)
    contacts = {c["mesh"]: c for c in fold_contacts(state)["contacts"]}
    chats = set(state.get("chats", []))
    ignored: dict[str, str] = {}
    used: set[bytes] = set()
    grants: dict[str, tuple[Any, dict[str, Any]]] = {}
    revoked: set[str] = set()
    for event in sorted(
        (e for e in state["events"] if is_event(e) and _family(e["body"]["key"]) == "share"),
        key=mesh_fold.order_key,
    ):
        try:
            checked = _check_share_event(event, roster, used, set(grants))
        except MeshError as error:
            ignored[event["id"]] = error.code
            continue
        if checked["form"] == "revoked":
            revoked.add(checked["id"])
            continue
        used.add(connection._consent_key(checked["consent"]))
        grants[checked["id"]] = (
            mesh_fold.order_key(event),
            {
                "event": event["id"],
                "approved_by": checked["consent"]["device"],
                **checked["value"],
            },
        )
    accepted: dict[str, dict[str, Any]] = {}
    superseded: dict[str, str] = {}
    for _, grant in sorted(grants.values(), key=lambda item: item[0]):
        target = accepted.get(grant["supersedes"]) if grant["supersedes"] else None
        if grant["supersedes"] and (
            target is None
            or target["contact"] != grant["contact"]
            or target["item"] != grant["item"]
        ):
            ignored[grant["event"]] = "share.supersedes_unknown"
            continue
        if target is not None:
            superseded[target["id"]] = grant["id"]
        accepted[grant["id"]] = grant
    in_effect: list[dict[str, Any]] = []
    waiting: list[dict[str, str]] = []
    orphaned: list[str] = []
    chat_missing: list[str] = []
    for grant_id, grant in accepted.items():
        contact = contacts.get(grant["contact"])
        if grant_id in revoked or grant_id in superseded:
            continue
        if contact is None:
            orphaned.append(grant_id)
        elif contact["state"] != "verified":
            waiting.append({"id": grant_id, "code": "share.contact_unverified"})
        elif grant["item"]["chat"] not in chats:
            chat_missing.append(grant_id)
        else:
            in_effect.append(
                {
                    "id": grant_id,
                    "contact": grant["contact"],
                    "chat": grant["item"]["chat"],
                    "phi": grant["phi"],
                    "event": grant["event"],
                    "approved_by": grant["approved_by"],
                }
            )
    for extra in in_effect[cap:]:
        ignored[extra["event"]] = "share.too_many"
    return {
        "in_effect": in_effect[:cap],
        "waiting": sorted(waiting, key=lambda w: w["id"]),
        "orphaned": sorted(orphaned),
        "chat_missing": sorted(chat_missing),
        "revoked": sorted(revoked),
        "superseded": dict(sorted(superseded.items())),
        "ignored": dict(sorted(ignored.items())),
    }


def propose_share(contacts: dict[str, Any], mesh: str) -> dict[str, Any]:
    found = next((c for c in contacts["contacts"] if c["mesh"] == mesh), None)
    if found is None or found["state"] != "verified":
        raise _fail("contact.unverified", mesh)
    return {}


def _parsed(entries: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in entries:
        if isinstance(item, str):
            try:
                item = phi.read_wire(item)
            except MeshError:
                continue
        out.append(item)
    return out


def _followed(entries: list[Any], structural: set[str | None]) -> set[str]:
    return {
        f
        for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("follows"), list)
        and _safe_id(e) not in structural
        for f in e["follows"]
    }


def roster_state(entries: list[Any], mesh: str) -> dict[str, Any]:
    kept = _parsed(entries)
    dropped: dict[str, str] = {}
    while True:
        state = phi.fold(kept, mesh, None)
        structural = {
            eid for eid, code in state["rejected"].items() if code in STRUCTURAL_CODES
        }
        followed = _followed(kept, structural)
        bad = {
            eid: code
            for eid, code in state["rejected"].items()
            if classify(code) == "unverifiable"
            and (eid in structural or eid not in followed)
        }
        if not bad:
            break
        dropped.update(bad)
        kept = [e for e in kept if _safe_id(e) not in bad]
    by_id = {phi.entry_id(e): e for e in kept}
    usable = set(by_id) - set(state["pending"])
    followed = {f for eid in usable for f in by_id[eid]["follows"]}
    heads = sorted(usable - followed)
    return {
        "fold": state,
        "entries": by_id,
        "usable": usable,
        "dropped": dict(sorted(dropped.items())),
        "heads": heads,
        "count": len(usable),
    }


def _safe_id(entry: Any) -> str | None:
    try:
        return phi.entry_id(entry)
    except (MeshError, TypeError, KeyError, AttributeError):
        return None


def roster_digest(heads: list[str], count: int) -> str:
    return crypto.object_id({"count": count, "heads": sorted(heads)})


def _closure(heads: list[str], by_id: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    stack = list(heads)
    while stack:
        eid = stack.pop()
        if eid in seen or eid not in by_id:
            continue
        seen.add(eid)
        stack.extend(by_id[eid]["follows"])
    return seen


def _notices(
    roster: dict[str, Any], closure: set[str], mesh: str
) -> list[dict[str, str]]:
    base = phi.fold([roster["entries"][e] for e in sorted(closure)], mesh, None)
    now = roster["fold"]
    found = [{"notice": "device_added", "device": d} for d in sorted(set(now["members"]) - set(base["members"]))]
    found += [{"notice": "device_removed", "device": d} for d in sorted(set(base["members"]) - set(now["members"]))]
    skipped = set(now["rejected"]) | set(now["void"]) | set(now["pending"]) | closure
    if any(
        roster["entries"][e]["author"] == "recovery"
        for e in roster["usable"] - skipped
    ):
        found.append({"notice": "recovery_used"})
    return found


def follow(mesh: str, record: dict[str, Any], entries: list[Any]) -> dict[str, Any]:
    check_contact(record["mesh"].removeprefix("sha256:"), record)
    if record["mesh"] != mesh or record["state"] != "verified":
        raise _fail("contact.malformed", "record")
    result: dict[str, Any] = {
        "follows": False,
        "reason": None,
        "downgrade": None,
        "dropped": {},
        "triggers": {},
        "notices": [],
        "roster": None,
        "mark": None,
    }
    try:
        roster = roster_state(entries, mesh)
    except MeshError as error:
        result["reason"] = error.code
        return result
    closure = _closure(record["heads"], roster["entries"])
    triggers = {
        eid: code
        for eid, code in sorted(roster["fold"]["rejected"].items())
        if classify(code) == "trigger" and eid not in closure
    }
    result["dropped"] = roster["dropped"]
    result["triggers"] = triggers
    result["roster"] = {
        "heads": roster["heads"],
        "count": roster["count"],
        "digest": roster_digest(roster["heads"], roster["count"]),
    }
    result["mark"] = {
        "v": 1,
        "heads": roster["heads"],
        "count": roster["count"],
        "removed": roster["fold"]["removed"],
    }
    if triggers:
        result["reason"] = result["downgrade"] = "contact.roster_rejected"
        return result
    if not all(h in roster["usable"] for h in record["heads"]):
        result["reason"] = "contact.heads_missing"
        return result
    result["follows"] = True
    result["notices"] = _notices(roster, closure, mesh)
    return result


def reader_check(mark: dict[str, Any] | None, reader: dict[str, Any]) -> dict[str, Any]:
    if reader["kind"] not in READER_KINDS:
        raise _fail("share.reader_kind", reader["kind"])
    if mark is None:
        return {}
    if reader["device"] in mark["removed"]:
        raise _fail("share.device_removed", reader["device"])
    if not set(mark["heads"]) <= set(reader["entries"]):
        raise _fail("share.roster_stale")
    return {}


def _invite_message(invite: dict[str, Any]) -> bytes:
    return (
        INVITE_CONTEXT.encode()
        + b"\n"
        + canonical({k: v for k, v in invite.items() if k != "sig"})
    )


def make_invite(
    identity_scalar: int,
    mesh: str,
    name: str,
    addr: str,
    nonce: bytes,
    now_s: int,
    lifetime_s: int = DEFAULT_LIFETIME_S,
) -> dict[str, Any]:
    if not MIN_LIFETIME_S <= lifetime_s <= MAX_LIFETIME_S:
        raise _fail("contact.invite_lifetime", str(lifetime_s))
    public = crypto.p256_public(identity_scalar)
    invite: dict[str, Any] = {
        "v": 1,
        "mesh": mesh,
        "device": crypto.device_id(public),
        "identity": b64url_encode(public),
        "addr": addr,
        "nonce": b64url_encode(nonce),
        "expires_s": now_s + lifetime_s,
        "name": name,
    }
    invite["sig"] = b64url_encode(crypto.p256_sign(identity_scalar, _invite_message(invite)))
    return invite


def invite_text(invite: dict[str, Any]) -> str:
    return INVITE_TEXT + b64url_encode(canonical(invite))


def invite_link(invite: dict[str, Any]) -> str:
    return INVITE_LINK + b64url_encode(canonical(invite))


def decode_invite(text: Any) -> dict[str, Any]:
    malformed = _fail("contact.invite_malformed")
    if not isinstance(text, str):
        raise malformed
    for prefix in (INVITE_TEXT, INVITE_LINK):
        if text.startswith(prefix):
            payload = text[len(prefix) :]
            break
    else:
        raise malformed
    try:
        raw = b64url_decode(payload)
        invite = strict_loads(raw)
        if canonical(invite) != raw:
            raise malformed
    except (MeshError, UnicodeDecodeError) as error:
        raise malformed from error
    if not isinstance(invite, dict):
        raise malformed
    return invite


def _check_invite_shape(invite: dict[str, Any]) -> bytes:
    malformed = _fail("contact.invite_malformed")
    if set(invite) != INVITE_FIELDS or not _int(invite["v"]) or invite["v"] != 1:
        raise malformed
    if not crypto.is_id(invite["mesh"]) or not _name_ok(invite["name"]):
        raise malformed
    if not _int(invite["expires_s"]) or invite["expires_s"] <= 0:
        raise malformed
    addr = invite["addr"]
    if not isinstance(addr, str) or len(addr) > MAX_ADDR or not VISIBLE.fullmatch(addr):
        raise malformed
    try:
        public = b64url_decode(invite["identity"])
        nonce = b64url_decode(invite["nonce"])
        b64url_decode(invite["sig"])
    except MeshError as error:
        raise malformed from error
    if len(nonce) != NONCE_BYTES or invite["device"] != crypto.device_id(public):
        raise malformed
    return public


def check_invite(invite: dict[str, Any], now_s: int, own_mesh: str) -> dict[str, Any]:
    public = _check_invite_shape(invite)
    if not crypto.p256_verify(public, _invite_message(invite), b64url_decode(invite["sig"])):
        raise _fail("contact.invite_bad_signature")
    if invite["mesh"] == own_mesh:
        raise _fail("contact.self")
    if now_s >= invite["expires_s"]:
        raise _fail("contact.invite_expired")
    return invite


def _nonce_bytes(nonce: Any) -> bytes:
    try:
        raw = b64url_decode(nonce)
    except MeshError as error:
        raise _fail("contact.malformed", "nonce") from error
    if len(raw) != NONCE_BYTES:
        raise _fail("contact.malformed", "nonce")
    return raw


def comparison_code(nonces: list[str], sides: list[dict[str, Any]]) -> dict[str, Any]:
    if len(nonces) != 2 or len(sides) != 2:
        raise _fail("contact.malformed", "inputs")
    raw = sorted(_nonce_bytes(n) for n in nonces)
    if raw[0] == raw[1]:
        raise _fail("contact.malformed", "nonces")
    meshes = [s["mesh"] for s in sides]
    if meshes[0] == meshes[1]:
        raise _fail("contact.self")
    digests = [roster_digest(s["heads"], s["count"]) for s in sides]
    ordered = sorted(
        ({"mesh": s["mesh"], "roster": d} for s, d in zip(sides, digests)),
        key=lambda s: s["mesh"],
    )
    code = hashlib.sha256(
        CODE_CONTEXT
        + canonical({"nonces": [b64url_encode(n) for n in raw], "sides": ordered})
    ).digest()
    number = int.from_bytes(code[:8], "big") % CODE_MODULUS
    text = f"{number:012d}"
    return {
        "digests": digests,
        "code": code.hex(),
        "display": f"{text[:4]} {text[4:8]} {text[8:]}",
        "qr": b64url_encode(code),
    }


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as error:
        return {"error": error.code}


def _invite_result(case_input: dict[str, Any]) -> dict[str, Any]:
    invite = check_invite(
        decode_invite(case_input["text"]), case_input["now_s"], case_input["own_mesh"]
    )
    return {"invite": invite, "text": invite_text(invite), "link": invite_link(invite)}


def _make_invite_result(case_input: dict[str, Any]) -> dict[str, Any]:
    nonce = b64url_decode(case_input["nonce"])
    invite = make_invite(
        int(case_input["identity_scalar_hex"], 16),
        case_input["mesh"],
        case_input["name"],
        case_input["addr"],
        nonce,
        case_input["now_s"],
        case_input["lifetime_s"],
    )
    return {"invite": invite, "text": invite_text(invite), "link": invite_link(invite)}


def _record_result(case_input: dict[str, Any]) -> dict[str, Any]:
    check_contact(case_input["id"], case_input["value"])
    return {"digest": digest(case_input["value"])}


def _follow_result(case_input: dict[str, Any]) -> dict[str, Any]:
    return follow(case_input["mesh"], case_input["record"], case_input["entries"])


def _roster_result(case_input: dict[str, Any]) -> dict[str, Any]:
    roster = roster_state(case_input["entries"], case_input["mesh"])
    return {
        "heads": roster["heads"],
        "count": roster["count"],
        "digest": roster_digest(roster["heads"], roster["count"]),
        "dropped": roster["dropped"],
    }


def coexistence(case_input: dict[str, Any]) -> dict[str, Any]:
    events = case_input["events"]
    plain = [e for e in events if not is_event(e)]
    return {
        "authority": mesh_fold.authority(events, case_input["presence_keys"]),
        "settings_with": mesh_fold.settings(events),
        "settings_without": mesh_fold.settings(plain),
    }


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    calls = {
        "contact_key": lambda: parse_key(case_input["key"]),
        "contact_record": lambda: _record_result(case_input),
        "contact_invite_make": lambda: _make_invite_result(case_input),
        "contact_invite_check": lambda: _invite_result(case_input),
        "contact_roster": lambda: _roster_result(case_input),
        "contact_code": lambda: comparison_code(case_input["nonces"], case_input["sides"]),
        "contact_fold": lambda: fold_contacts(case_input),
        "contact_follow": lambda: _follow_result(case_input),
        "contact_reader": lambda: reader_check(case_input["mark"], case_input["reader"]),
        "share_fold": lambda: fold_shares(case_input),
        "share_propose": lambda: propose_share(fold_contacts(case_input), case_input["contact"]),
        "contact_classify": lambda: {"class": classify(case_input["code"])},
        "contact_coexistence": lambda: coexistence(case_input),
    }
    return _guard(calls[kind])
