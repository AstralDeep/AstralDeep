"""Generates the share session vectors from public fixed test keys, clocks and ids, running every case through the reference module.
The owner chat is built from real signed and sealed events, the reader mesh from signed roster v2 entries, and `--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
share = importlib.import_module("share")
contacts = importlib.import_module("contacts")
gpd = importlib.import_module("generate_phi_destination_vectors")
cj = importlib.import_module("astral_mesh_ref.cj")
crypto = importlib.import_module("astral_mesh_ref.crypto")
mesh_event = importlib.import_module("astral_mesh_ref.event")
testkit = importlib.import_module("astral_mesh_ref.testkit")
b64 = cj.b64url_encode
DetRng, TestDevice, hlc = testkit.DetRng, testkit.TestDevice, testkit.hlc

VECTORS = HERE.parent / "vectors"
SPEC = "106/contracts/share.md"
T0 = gpd.T0
NOW_S = T0 // 1000
OWN = crypto.sha256_id(b"share-v1/own-mesh")
OA = TestDevice("share-v1/owner-desktop")
ON = TestDevice("share-v1/owner-node", kind="node")
OD = TestDevice("share-v1/owner-removed")
ALICE, BOB, CAROL, NODE, DAVE, MALLORY = (
    gpd.ALICE,
    gpd.BOB,
    gpd.CAROL,
    gpd.NODE,
    gpd.DAVE,
    gpd.MALLORY,
)
PRIVATE = {"confidentiality": "private", "integrity": "owner"}
PHI = {"confidentiality": "phi", "integrity": "owner"}
RNG = DetRng("share-v1/ids")
GA, GB, GC = (RNG.bytes(16).hex() for _ in range(3))
EPOCH_P = crypto.sha256_id(b"share-v1/epoch/personal")
EPOCH_H = crypto.sha256_id(b"share-v1/epoch/phi")
EK_P = f"ek:personal:{EPOCH_P}"
EK_H = f"ek:phi:{EPOCH_H}"
EPOCH_KEY = testkit.seed("share-v1/epoch-key")


def ck(label: str) -> tuple[str, bytes]:
    return "ck:" + b64(testkit.seed("share-v1/ckid/" + label)[:16]), testkit.seed("share-v1/ck/" + label)


CK_P, K_P = ck("personal")
CK_R, K_R = ck("rotated")
CK_H, K_H = ck("phi")
CK_X, K_X = ck("unwrapped")
KEYS = {CK_P: K_P, CK_R: K_R, CK_H: K_H, CK_X: K_X}


class Chain:
    def __init__(self, device: Any) -> None:
        self.device, self.seq, self.prev = device, 0, None

    def event(
        self,
        name: str,
        ms: int,
        scope: str,
        type_: str,
        body: dict[str, Any],
        *,
        labels: dict[str, str] | None = None,
        key: str | None = None,
        key_bytes: bytes | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        self.seq = seq or self.seq + 1
        key = key or CK_P
        key_bytes = key_bytes or KEYS.get(key, EPOCH_KEY)
        nonce = DetRng("share-v1/event/" + name).bytes(crypto.NONCE_LEN)
        body_ct = mesh_event.seal_body(
            key_bytes,
            {"feed": self.device.feed, "key": key, "scope": scope, "seq": self.seq},
            cj.canonical(body),
            nonce,
        )
        header = mesh_event.build_header(
            self.device.ops_seed,
            seq=self.seq,
            prev=self.prev if self.seq > 1 else None,
            after=[],
            clock=hlc(ms),
            scope=scope,
            type=type_,
            labels=labels or PRIVATE,
            key=key,
            body_hash=crypto.sha256_id(body_ct),
        )
        self.prev = crypto.object_id(header)
        return {"header": header, "body": body, "body_ct": b64(body_ct)}


def eid(item: dict[str, Any]) -> str:
    return crypto.object_id(item["header"])


def chunk(label: str, length: int = 300) -> tuple[bytes, str]:
    data = DetRng("share-v1/chunk/" + label).bytes(length)
    return data, crypto.sha256_id(data)


CHUNKS = {n: chunk(n) for n in ("a", "b", "p")}


def build_fixture() -> dict[str, Any]:
    c = Chain(OA)
    t = T0
    keys = [
        c.event("k_new", t, "mesh", "key.content", {"id": CK_P, "key": b64(K_P), "scope": "new"}, key=EK_P),
    ]
    create = c.event("create", t + 1000, "new", "chat.create", {"title": "Lab results"})
    chat = eid(create)
    keys.append(
        c.event("k_rot", t + 2000, "mesh", "key.content", {"id": CK_R, "key": b64(K_R), "scope": chat}, key=EK_P)
    )
    keys.append(
        c.event("k_phi", t + 3000, "mesh", "key.content", {"id": CK_H, "key": b64(K_H), "scope": chat}, key=EK_H)
    )
    keys.append(
        c.event("k_other", t + 3500, "mesh", "key.content", {"id": CK_X, "key": b64(K_X), "scope": crypto.sha256_id(b"elsewhere")}, key=EK_P)
    )
    ev: dict[str, dict[str, Any]] = {}

    def add(name: str, type_: str, body: dict[str, Any], ms: int, **kw: Any) -> dict[str, Any]:
        ev[name] = c.event(name, ms, kw.pop("scope", chat), type_, body, **kw)
        return ev[name]

    turn = crypto.sha256_id(b"share-v1/turn")
    add("user", "chat.user_message", {"text": "What do the results say?", "base": None}, t + 4000)
    add("turn_start", "chat.turn_start", {"continues": None, "message": eid(ev["user"])}, t + 5000)
    tr_bound = add("tr_bound", "chat.tool_result", {"turn": turn, "call": "c1", "status": "ok", "content": "7.1", "code": None}, t + 6000)
    tr_patch = add("tr_patch", "chat.tool_result", {"turn": turn, "call": "c2", "status": "ok", "content": "normal", "code": None}, t + 6100)
    tr_source = add("tr_source", "chat.tool_result", {"turn": turn, "call": "c3", "status": "ok", "content": "{}", "code": None}, t + 6200)
    add("tr_unbound", "chat.tool_result", {"turn": turn, "call": "c4", "status": "ok", "content": "hidden", "code": None}, t + 6300)
    document = {"v": 1, "surface": "new", "root": "root", "components": {"root": {"id": "root", "type": "text", "props": {"text": {"$bind": {"event": eid(tr_bound), "path": "/content"}}}, "children": [], "prov": "tool"}}, "capabilities": {}}
    add("surface", "ui.surface_put", {"document": document, "turn": turn}, t + 7000)
    add("patch", "ui.patch", {"surface": eid(ev["surface"]), "ops": [{"op": "set", "id": "root", "prop": "text", "value": {"$bind": {"event": eid(tr_patch), "path": "/content"}}}]}, t + 7100)
    add("surface_source", "ui.surface_put", {"document": document, "turn": None, "source": {"origin": "tool", "name": "viewer", "format": "a2ui/1.0", "catalog": "urn:astralprims:a2ui:catalog:1", "surface": "s1", "result": eid(tr_source)}}, t + 7200)
    add("assistant", "chat.assistant_message", {"turn": turn, "text": "Within range."}, t + 8000, key=CK_R)
    add("spoken", "chat.spoken", {"kind": "answer", "text": "Within range.", "turn": turn}, t + 8100)
    add("attachment", "chat.attachment", {"name": "panel.csv", "mime": "text/csv", "size": 600, "sha256": crypto.sha256_id(b"panel"), "chunks": [CHUNKS["a"][1], CHUNKS["b"][1]]}, t + 8200)
    add("turn_end", "chat.turn_end", {"turn": turn, "reason": "completed", "code": None}, t + 9000)
    add("unknown_outcome", "chat.turn_outcome_unknown", {"turn": turn, "calls": ["c9"]}, t + 9100)
    add("phi_message", "chat.assistant_message", {"turn": turn, "text": "Result for the patient."}, t + 9200, labels=PHI, key=CK_H)
    add("phi_attachment", "chat.attachment", {"name": "scan.csv", "mime": "text/csv", "size": 300, "sha256": crypto.sha256_id(b"scan"), "chunks": [CHUNKS["p"][1]]}, t + 9300, labels=PHI, key=CK_H)
    add("secret_message", "chat.user_message", {"text": "token", "base": None}, t + 9400, labels={"confidentiality": "secret", "integrity": "owner"})
    add("unknown_label", "chat.user_message", {"text": "x", "base": None}, t + 9500, labels={"confidentiality": "classified", "integrity": "owner"})
    add("unknown_integrity", "chat.user_message", {"text": "x", "base": None}, t + 9550, labels={"confidentiality": "private", "integrity": "trusted"})
    add("request_field", "chat.turn_start", {"continues": None, "message": None, "request": crypto.sha256_id(b"req")}, t + 9600)
    add("action_field", "chat.turn_start", {"continues": None, "message": None, "action": {"event": crypto.sha256_id(b"act"), "consent": "policy"}}, t + 9650)
    add("extra_text_field", "chat.user_message", {"text": "x", "base": None, "agent_note": "x"}, t + 9700)
    add("no_wrap_key", "chat.assistant_message", {"turn": turn, "text": "x"}, t + 9800, key=CK_X)
    add("elsewhere", "chat.user_message", {"text": "x", "base": None}, t + 9900, scope=crypto.sha256_id(b"elsewhere"))
    other_create = Chain(OA)
    other_create.seq = 0
    ev["other_create"] = other_create.event("other_create", t + 9950, "new", "chat.create", {"title": "Other"})
    labels_for = {"auth.policy": "mesh", "auth.transfer": "mesh", "learn.proposal": "mesh", "learn.adoption": "mesh", "learn.reversal": "mesh", "mem.remember": "mesh", "mem.supersede": "mesh", "settings.set": "mesh", "key.content": "mesh", "key.credential": "mesh", "tools.catalog": "mesh", "device.wake_route": "mesh"}
    ev["never"] = {}
    for n, type_ in enumerate(sorted(mesh_event.EVENT_TYPES)):
        if type_ in share.SERVED_FIELDS or type_ == "chat.create":
            continue
        scope = chat if type_ not in labels_for else "mesh"
        ev["never"][type_] = c.event("never/" + type_, t + 10000 + n, scope, type_, {"x": 1})
    return {"chat": chat, "create": create, "key_events": keys, "events": ev}


FIX = build_fixture()
CHAT = FIX["chat"]
EV = FIX["events"]
SERVED_NAMES = (
    "user", "turn_start", "tr_bound", "tr_patch", "tr_source", "surface", "patch", "surface_source",
    "assistant", "spoken", "attachment", "turn_end", "unknown_outcome",
)


def all_events(with_create: bool = True) -> list[dict[str, Any]]:
    items = [v for k, v in EV.items() if k != "never"]
    items += list(EV["never"].values())
    return lean(([FIX["create"]] if with_create else []) + items)


def lean(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k != "body_ct"} for i in items]


def mesh_b() -> Any:
    return gpd.ready("share-b", [BOB, CAROL, NODE, DAVE], [ALICE, BOB], [ALICE, BOB])[0]


MB = mesh_b()


def wire(entries: list[Any]) -> list[str]:
    return [gpd.phi.wire_text(e) for e in entries]


def contact_record(m: Any, state: str = "verified") -> dict[str, Any]:
    verified = state == "verified"
    return {"v": 1, "mesh": m.mesh, "name": "Bea", "state": state, "heads": m.heads() if verified else [], "count": len(m.entries) if verified else 0, "ms": T0}


def hello(kind: str, device: Any, m: Any = None, **change: Any) -> dict[str, Any]:
    m = m or MB
    value: dict[str, Any] = {
        "t": "hello", "v": 1, "kind": kind, "mesh": m.mesh, "device": device.id,
        "roster": {"heads": m.heads(), "count": len(m.entries)},
        "nonce": b64(DetRng("share-v1/nonce/" + kind + device.id).bytes(16)),
    }
    if kind == "read":
        value["phi"] = False
    if kind == "contact":
        value["invite"] = INVITE
        value["name"] = "Bea"
    if kind == "verify":
        value["name"] = "Bea"
    value.update(change)
    return value


INVITE = b64(DetRng("share-v1/invite").bytes(16))
RID = {OA.id: "oa"}


def view(m: Any = None, state: str = "verified", mark: Any = None, entries: Any = None, **change: Any) -> dict[str, Any]:
    m = m or MB
    value = {
        "own_mesh": OWN, "now_s": NOW_S, "invites": [{"nonce": INVITE, "expires_s": NOW_S + 600}],
        "contact": contact_record(m, state), "mark": mark, "entries": wire(m.entries if entries is None else entries),
    }
    value.update(change)
    return value


def wraps_for(grant: str, keys: dict[str, bytes]) -> dict[str, Any]:
    rng = DetRng("share-v1/wrap/" + grant)
    made = share.make_wraps(grant, BOB.id, BOB.wrap_public, [{"ref": r, "secret": b64(k)} for r, k in keys.items()], rng)
    return {m["key"]: m["wrap"] for m in made}


CTX = {
    "reader": {"device": BOB.id, "wrap_private_hex": BOB.wrap_private.hex()},
    "grants": {
        GA: {"chat": CHAT, "wraps": wraps_for(GA, {CK_P: K_P, CK_R: K_R, CK_H: K_H})},
        GB: {"chat": CHAT, "wraps": wraps_for(GB, {CK_P: K_P, CK_R: K_R})},
    },
    "members": {OA.id: {"feeds": [OA.feed]}, ON.id: {"feeds": [ON.feed]}},
    "removed": {OD.id: {"feeds": [OD.feed], "cutoff": {OD.feed: 5}}},
}


def forge(flags: set[str], *, env_key: str | None = None, grant: str = GA, device: Any = None, seq: int = 50, type_: str = "chat.assistant_message", body: dict[str, Any] | None = None, key: str = CK_P, labels: dict[str, str] | None = None, raw: bytes | None = None) -> dict[str, Any]:
    device = MALLORY if "feed" in flags else (device or OA)
    scope = crypto.sha256_id(b"elsewhere") if "scope" in flags else CHAT
    type_ = "chat.model_request" if "type" in flags else type_
    labels = {"confidentiality": "classified", "integrity": "owner"} if "label" in flags else (labels or PRIVATE)
    body = {"turn": crypto.sha256_id(b"t"), "text": "hello"} if body is None else body
    plain = cj.canonical(body)
    if "fields" in flags:
        body = {**body, "agent_note": "x"}
        plain = cj.canonical(body)
    if "bodyparse" in flags:
        plain = raw if raw is not None else b'{"text":'
    body_key = K_X if "bodydec" in flags else KEYS[key]
    nonce = DetRng(f"share-v1/forge/{sorted(flags)}/body").bytes(24)
    body_ct = mesh_event.seal_body(body_key, {"feed": device.feed, "key": key, "scope": scope, "seq": seq}, plain, nonce)
    if "bodyshort" in flags:
        body_ct = bytes(10)
    hashed = crypto.sha256_id(b"other") if "bodyhash" in flags else crypto.sha256_id(body_ct)
    header_seq = seq + 1 if "mismatch" in flags else seq
    header = mesh_event.build_header(device.ops_seed, seq=header_seq, prev=crypto.sha256_id(b"prev"), after=[], clock=hlc(T0 + 99), scope=scope, type=type_, labels=labels, key=key, body_hash=hashed)
    if "badsig" in flags:
        header["sig"] = b64(bytes(64))
    if "parse" in flags:
        del header["clock"]
    salt_grant = (GA if grant == GB else GB) if "wrong_salt" in flags else grant
    envelope_grant = crypto.sha256_id(b"x").removeprefix("sha256:")[:32] if "grant" in flags else grant
    envelope_key = CK_X if "key" in flags else (env_key or key)
    nonce_h = DetRng(f"share-v1/forge/{sorted(flags)}/header").bytes(24)
    envelope = share.seal_envelope(salt_grant, KEYS[env_key or key], header, body_ct, nonce_h, key=envelope_key, feed=device.feed, seq=seq)
    envelope["grant"] = envelope_grant
    if "shape" in flags:
        envelope["extra"] = 1
    return {"envelope": envelope, "context": CTX}


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, given: dict[str, Any]) -> dict[str, Any]:
        expect = share.run(kind, given)
        cases.append({"name": name, "kind": kind, "input": given, "expect": expect})
        return expect

    message_cases(add)
    frame_cases(add)
    admit_cases(add)
    select_cases(add)
    key_cases(add)
    grants_cases(add)
    receive_cases(add)
    other_cases(add)
    return {
        "format": "astral.vectors/v1",
        "name": "share-v1",
        "spec": SPEC,
        "constants": {
            "alpn": share.ALPN,
            "frame_limit": share.FRAME_LIMIT,
            "header_info": share.HEADER_INFO.decode(),
            "max_keys": share.MAX_KEYS,
            "pre_admission": share.PRE_ADMISSION,
            "own_mesh": OWN,
            "now_s": NOW_S,
            "chat": CHAT,
            "grants": {"a": GA, "b": GB, "c": GC},
            "invite": INVITE,
            "served_types": sorted(share.SERVED_FIELDS),
            "served_fields": {k: (sorted(v) if v is not None else None) for k, v in sorted(share.SERVED_FIELDS.items())},
            "receiver_steps": list(share.RECEIVER_STEPS),
            "codes": list(share.CODES),
        },
        "test_devices": {
            n: {"id": d.id, "kind": d.kind, "feed": d.feed, "wrap_public": b64(d.wrap_public), "wrap_private_hex": d.wrap_private.hex()}
            for n, d in (("owner_desktop", OA), ("owner_node", ON), ("owner_removed", OD), ("reader_alice", ALICE), ("reader_bob", BOB), ("reader_carol", CAROL), ("reader_node", NODE), ("reader_dave", DAVE), ("stranger", MALLORY))
        },
        "cases": cases,
    }


def sample_envelope() -> dict[str, Any]:
    return forge(set())["envelope"]


def sample_entry(**change: Any) -> dict[str, Any]:
    wraps = [{"key": k, "wrap": w} for k, w in sorted(CTX["grants"][GB]["wraps"].items())]
    value = {"id": GA, "chat": CHAT, "title": "Lab results", "phi": False, "withheld_phi": 0, "withheld_fields": 0, "withheld_keys": 0, "supersedes": None, "heads": {OA.feed: 5}, "wraps": wraps}
    value.update(change)
    return value


def message_cases(add: Any) -> None:
    ranges = [{"feed": OA.feed, "from": 1, "to": 10}]
    valid: dict[str, Any] = {
        "hello_contact": hello("contact", BOB),
        "hello_read": hello("read", BOB),
        "hello_verify": hello("verify", BOB),
        "hello_nudge": hello("nudge", BOB),
        "roster_want": {"t": "roster_want", "ids": MB.heads()},
        "roster": {"t": "roster", "entries": wire(MB.entries)},
        "grants_want": {"t": "grants_want", "grants": sorted([GA, GB])},
        "grants": {"t": "grants", "grants": [sample_entry()]},
        "want": {"t": "want", "grant": GA, "ranges": ranges},
        "events": {"t": "events", "grant": GA, "events": [sample_envelope()]},
        "chunks_want": {"t": "chunks_want", "grant": GA, "hashes": sorted(h for _, h in CHUNKS.values())},
        "chunks": {"t": "chunks", "grant": GA, "chunks": [{"hash": CHUNKS["a"][1], "ct": b64(CHUNKS["a"][0])}], "withheld": [{"hash": CHUNKS["p"][1], "reason": "phi"}]},
        "revoked": {"t": "revoked", "grants": [GA]},
        "error": {"t": "error", "code": "share.malformed"},
        "bye": {"t": "bye"},
    }
    for name, message in valid.items():
        add("message_valid_" + name, "share_message", {"message": message})

    def bad(label: str, base: str, /, **change: Any) -> None:
        message = copy.deepcopy(valid[base])
        for k, v in change.items():
            if v is None and k.startswith("drop_"):
                del message[k.removeprefix("drop_")]
            else:
                message[k] = v
        add("message_" + label, "share_message", {"message": message})

    add("message_unknown_kind", "share_message", {"message": {"t": "mailbox"}})
    add("message_not_an_object", "share_message", {"message": ["bye"]})
    add("message_hello_without_kind", "share_message", {"message": {k: v for k, v in valid["hello_read"].items() if k != "kind"}})
    bad("bye_extra_field", "bye", x=1)
    bad("hello_read_with_invite", "hello_read", invite=INVITE)
    bad("hello_contact_without_invite", "hello_contact", drop_invite=None)
    bad("hello_nudge_with_phi", "hello_nudge", phi=True)
    bad("hello_read_phi_not_bool", "hello_read", phi=1)
    bad("hello_version_2", "hello_read", v=2)
    bad("hello_nonce_15_bytes", "hello_read", nonce=b64(bytes(15)))
    bad("hello_mesh_not_id", "hello_read", mesh="mesh")
    bad("hello_heads_unsorted", "hello_read", roster={"heads": sorted(MB.heads() + [crypto.sha256_id(b"z")], reverse=True), "count": 9})
    bad("hello_count_below_heads", "hello_read", roster={"heads": MB.heads(), "count": 0})
    bad("hello_roster_extra_field", "hello_read", roster={"heads": [], "count": 0, "x": 1})
    bad("hello_name_empty", "hello_verify", name="")
    bad("hello_name_bidi", "hello_verify", name="a‮b")
    bad("hello_invite_not_base64", "hello_contact", invite="!!")
    bad("roster_want_257_ids", "roster_want", ids=sorted(crypto.sha256_id(str(i).encode()) for i in range(257)))
    bad("roster_want_256_ids_valid", "roster_want", ids=sorted(crypto.sha256_id(str(i).encode()) for i in range(256)))
    bad("roster_want_unsorted", "roster_want", ids=sorted(MB.heads() + [crypto.sha256_id(b"z")], reverse=True))
    bad("roster_257_entries", "roster", entries=["x"] * 257)
    bad("roster_entry_not_text", "roster", entries=[1])
    bad("grants_want_bad_id", "grants_want", grants=["XYZ"])
    bad("grants_want_257", "grants_want", grants=sorted(f"{i:032x}" for i in range(257)))
    bad("grants_want_duplicate", "grants_want", grants=[GA, GA])
    bad("want_65_ranges", "want", ranges=[{"feed": OA.feed, "from": i * 2 + 1, "to": i * 2 + 1} for i in range(65)])
    bad("want_64_ranges_valid", "want", ranges=[{"feed": OA.feed, "from": i * 2 + 1, "to": i * 2 + 1} for i in range(64)])
    bad("want_span_513", "want", ranges=[{"feed": OA.feed, "from": 1, "to": 513}])
    bad("want_span_512_valid", "want", ranges=[{"feed": OA.feed, "from": 1, "to": 512}])
    bad("want_from_zero", "want", ranges=[{"feed": OA.feed, "from": 0, "to": 3}])
    bad("want_to_below_from", "want", ranges=[{"feed": OA.feed, "from": 5, "to": 4}])
    bad("want_overlapping", "want", ranges=[{"feed": OA.feed, "from": 1, "to": 5}, {"feed": OA.feed, "from": 5, "to": 6}])
    bad("want_feed_not_a_key", "want", ranges=[{"feed": "abc", "from": 1, "to": 5}])
    bad("want_missing_grant", "want", drop_grant=None)
    bad("events_extra_envelope_field", "events", events=[{**sample_envelope(), "x": 1}])
    bad("events_null_body", "events", events=[{**sample_envelope(), "body_ct": None}])
    bad("chunks_want_257", "chunks_want", hashes=sorted(crypto.sha256_id(str(i).encode()) for i in range(257)))
    bad("chunks_want_not_ids", "chunks_want", hashes=["a"])
    bad("chunks_unknown_reason", "chunks", withheld=[{"hash": CHUNKS["p"][1], "reason": "other"}])
    bad("error_unknown_code", "error", code="share.nothing")
    bad("revoked_bad_id", "revoked", grants=["g"])
    entry = sample_entry
    bad("grants_unsorted", "grants", grants=[entry(id=GB), entry(id=GA)] if GA < GB else [entry(id=GA), entry(id=GB)])
    bad("grants_entry_extra_field", "grants", grants=[{**entry(), "x": 1}])
    bad("grants_title_257", "grants", grants=[entry(title="x" * 257)])
    bad("grants_title_256_valid", "grants", grants=[entry(title="x" * 256)])
    bad("grants_title_empty_valid", "grants", grants=[entry(title="")])
    bad("grants_supersedes_itself", "grants", grants=[entry(supersedes=GA)])
    bad("grants_negative_withheld", "grants", grants=[entry(withheld_phi=-1)])
    bad("grants_heads_seq_zero", "grants", grants=[entry(heads={OA.feed: 0})])
    many = [{"key": f"ck:{i:022d}", "wrap": sample_entry()["wraps"][0]["wrap"]} for i in range(65)]
    bad("grants_65_wraps_too_many_keys", "grants", grants=[entry(wraps=many)])
    bad("grants_64_wraps_valid", "grants", grants=[entry(wraps=many[:64])])
    bad("grants_wrap_unsorted", "grants", grants=[entry(wraps=list(reversed(sample_entry()["wraps"])))])
    bad("grants_wrap_short_ct", "grants", grants=[entry(wraps=[{"key": CK_P, "wrap": {"epk": b64(bytes(32)), "nonce": b64(bytes(24)), "ct": b64(bytes(47))}}])])
    bad("grants_wrap_phi_key_shape", "grants", grants=[entry(wraps=[{"key": EK_P, "wrap": sample_entry()["wraps"][0]["wrap"]}])])


def frame_cases(add: Any) -> None:
    message = {"t": "revoked", "grants": [GA]}
    framed = share.frame(message).hex()
    add("frame_roundtrip", "share_frame", {"message": message})
    add("unframe_valid", "share_unframe", {"hex": framed})
    add("unframe_short_header", "share_unframe", {"hex": "0000"})
    add("unframe_length_too_long", "share_unframe", {"hex": framed[:-2]})
    add("unframe_length_too_short", "share_unframe", {"hex": framed + "00"})
    add("unframe_over_limit", "share_unframe", {"hex": (share.FRAME_LIMIT + 1).to_bytes(4, "big").hex()})
    add("unframe_not_json", "share_unframe", {"hex": (3).to_bytes(4, "big").hex() + b"abc".hex()})
    raw = b'{"t":"bye","t":"bye"}'
    add("unframe_duplicate_key", "share_unframe", {"hex": len(raw).to_bytes(4, "big").hex() + raw.hex()})
    spaced = b'{"grants": ["' + GA.encode() + b'"], "t": "revoked"}'
    add("unframe_not_canonical_spacing", "share_unframe", {"hex": len(spaced).to_bytes(4, "big").hex() + spaced.hex()})
    reordered = b'{"t":"revoked","grants":["' + GA.encode() + b'"]}'
    add("unframe_not_canonical_key_order", "share_unframe", {"hex": len(reordered).to_bytes(4, "big").hex() + reordered.hex()})
    raw = b'{"t":"mailbox"}'
    add("unframe_unknown_kind", "share_unframe", {"hex": len(raw).to_bytes(4, "big").hex() + raw.hex()})
    for kind in share.SESSION_KINDS:
        for admitted in (False, True):
            for t, sender in (("hello", "reader"), ("roster", "server"), ("grants_want", "reader"), ("grants", "server"), ("events", "server"), ("want", "reader")):
                add(f"allowed_{kind}_{'admitted' if admitted else 'open'}_{t}_from_{sender}", "share_allowed", {"kind": kind, "admitted": admitted, "sender": sender, "t": t})
    add("allowed_read_admitted_grants_from_reader", "share_allowed", {"kind": "read", "admitted": True, "sender": "reader", "t": "grants"})
    add("allowed_read_admitted_want_from_server", "share_allowed", {"kind": "read", "admitted": True, "sender": "server", "t": "want"})
    add("allowed_read_admitted_chunks_from_reader", "share_allowed", {"kind": "read", "admitted": True, "sender": "reader", "t": "chunks"})
    add("allowed_read_open_bye", "share_allowed", {"kind": "read", "admitted": False, "sender": "reader", "t": "bye"})
    add("allowed_unknown_session_kind", "share_allowed", {"kind": "mailbox", "admitted": True, "sender": "reader", "t": "bye"})
    for name, rounds in (
        ("four_rounds_at_the_limit", [{"frames": 16, "bytes": 1048576}] * 4),
        ("five_rounds", [{"frames": 1, "bytes": 1}] * 5),
        ("seventeen_frames", [{"frames": 17, "bytes": 1}]),
        ("one_byte_over", [{"frames": 1, "bytes": 1048577}]),
    ):
        add("pre_admission_" + name, "share_pre_admission", {"rounds": rounds})


def admit_cases(add: Any) -> None:
    def case(name: str, kind: str, device: Any, v: dict[str, Any] | None = None, h: dict[str, Any] | None = None, feed: str | None = None) -> None:
        add(name, "share_admit", {"kind": kind, "hello": h or hello(kind, device), "peer_feed": feed or device.feed, "view": v or view()})

    case("admit_contact_open_invitation", "contact", BOB)
    case("admit_contact_expired_at_its_second", "contact", BOB, view(invites=[{"nonce": INVITE, "expires_s": NOW_S}]))
    case("admit_contact_last_second", "contact", BOB, view(invites=[{"nonce": INVITE, "expires_s": NOW_S + 1}]))
    case("admit_contact_unknown_invitation", "contact", BOB, view(invites=[]))
    case("admit_contact_other_nonce", "contact", BOB, view(invites=[{"nonce": b64(bytes(16)), "expires_s": NOW_S + 600}]))
    case("admit_contact_own_mesh", "contact", BOB, h=hello("contact", BOB, mesh=OWN))
    case("admit_contact_hello_kind_differs", "contact", BOB, h=hello("read", BOB))
    case("admit_contact_needs_no_contact_record", "contact", BOB, view(state="unverified"))
    case("admit_read_pocket", "read", BOB)
    case("admit_read_desktop", "read", ALICE)
    case("admit_read_satellite", "read", CAROL)
    case("admit_read_node", "read", NODE)
    case("admit_read_unverified_contact", "read", BOB, view(state="unverified"))
    case("admit_read_no_contact_record", "read", BOB, view(contact=None))
    case("admit_read_own_mesh", "read", BOB, h=hello("read", BOB, mesh=OWN))
    case("admit_read_unlisted_feed", "read", BOB, feed=MALLORY.feed)
    case("admit_read_device_not_in_roster", "read", MALLORY, feed=MALLORY.feed)
    case("admit_read_heads_not_presented", "read", BOB, view(entries=MB.entries[:-1]))
    case("admit_read_no_genesis", "read", BOB, view(entries=[]))
    stale = {"heads": MB.heads(), "count": len(MB.entries), "removed": []}
    short = view()
    short["contact"] = {**short["contact"], "heads": sorted(MB.heads() + [crypto.sha256_id(b"uncached")]), "count": len(MB.entries) + 1}
    case("admit_read_contact_heads_not_cached", "read", BOB, short)
    case("admit_read_current_mark", "read", BOB, view(mark=stale))
    case("admit_read_removed_device", "read", DAVE, view(mark={**stale, "removed": [DAVE.id]}))
    case("admit_read_roster_stale", "read", BOB, view(mark={**stale, "heads": sorted(stale["heads"] + [crypto.sha256_id(b"newer")])}))
    case("admit_read_removed_checked_before_stale", "read", DAVE, view(mark={"heads": [crypto.sha256_id(b"newer")], "count": 99, "removed": [DAVE.id]}))
    case("admit_read_satellite_checked_before_removal", "read", CAROL, view(mark={**stale, "removed": [CAROL.id]}))
    cases_phi = (
        ("admit_read_phi_claimed_by_a_phi_member", ALICE, True),
        ("admit_read_phi_claimed_by_a_non_member", DAVE, True),
        ("admit_read_phi_not_claimed_by_a_member", BOB, False),
    )
    for name, device, claim in cases_phi:
        case(name, "read", device, h=hello("read", device, phi=claim))
    case("admit_read_phi_member_pocket", "read", BOB, h=hello("read", BOB, phi=True))
    mt = mesh_b()
    mt.add(BOB)
    case("admit_read_trigger_seen_at_verification_stays_verified", "read", BOB, view(mt, entries=mt.entries), None)
    old = view()
    old["contact"] = contact_record(mt)
    old["contact"] = {**old["contact"], "heads": MB.heads(), "count": len(MB.entries)}
    old["entries"] = wire(mt.entries)
    case("admit_read_roster_rejected_after_verification", "read", BOB, old, hello("read", BOB, mt))
    case("admit_verify_unverified_contact", "verify", BOB, view(state="unverified"))
    case("admit_verify_verified_contact", "verify", BOB)
    case("admit_verify_node", "verify", NODE)
    case("admit_verify_recovers_a_downgraded_roster", "verify", BOB, old, hello("verify", BOB, mt))
    case("admit_verify_no_contact_record", "verify", BOB, view(contact=None))
    case("admit_verify_unlisted_feed", "verify", BOB, feed=MALLORY.feed)
    case("admit_verify_roster_without_genesis", "verify", BOB, view(entries=[]))
    other = gpd.Mesh("share-other", ALICE)
    other.add(CAROL)
    case("admit_verify_genesis_not_the_mesh", "verify", BOB, view(entries=other.entries))
    case("admit_verify_ignores_the_mark", "verify", DAVE, view(mark={**stale, "removed": [DAVE.id]}))
    case("admit_nudge_pocket", "nudge", BOB)
    case("admit_nudge_node", "nudge", NODE)
    case("admit_nudge_satellite", "nudge", CAROL)
    case("admit_nudge_unverified_contact", "nudge", BOB, view(state="unverified"))
    case("admit_nudge_removed_device", "nudge", DAVE, view(mark={**stale, "removed": [DAVE.id]}))
    case("admit_nudge_roster_stale", "nudge", BOB, view(mark={**stale, "heads": [crypto.sha256_id(b"newer")]}))
    case("admit_nudge_roster_rejected", "nudge", BOB, old, hello("nudge", BOB, mt))


def select_cases(add: Any) -> None:
    wrapped = [CK_P, CK_R]
    both = [CK_P, CK_R, CK_H]
    events = all_events()

    def case(name: str, phi: bool, ev: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
        return add(name, "share_select", {"chat": CHAT, "events": ev, "phi": phi, "wrapped": keys})

    case("select_every_type_without_phi", False, events, wrapped)
    case("select_every_type_with_phi", True, events, both)
    case("select_phi_reader_without_the_phi_key", True, events, wrapped)
    case("select_phi_off_even_with_the_phi_key_wrapped", False, events, both)
    tr_only = lean([EV["tr_bound"], EV["tr_unbound"], EV["tr_patch"], EV["tr_source"]])
    case("select_tool_results_without_any_surface", False, tr_only, wrapped)
    case("select_tool_result_bound_by_a_put", False, tr_only + lean([EV["surface"]]), wrapped)
    case("select_tool_result_bound_by_a_patch", False, tr_only + lean([EV["patch"]]), wrapped)
    case("select_tool_result_named_by_source_result", False, tr_only + lean([EV["surface_source"]]), wrapped)
    case("select_binding_from_a_surface_that_is_not_served", False, tr_only + lean([EV["surface"]]), [CK_R])
    phi_surface = copy.deepcopy(lean([EV["surface"]])[0])
    phi_surface["header"]["labels"] = PHI
    case("select_binding_from_a_phi_surface_without_phi", False, tr_only + [phi_surface], wrapped)
    case("select_create_of_the_granted_chat", False, lean([FIX["create"]]), wrapped)
    case("select_create_of_another_chat", False, lean([EV["other_create"]]), wrapped)
    case("select_event_of_another_chat", False, lean([EV["elsewhere"]]), wrapped)
    case("select_event_under_an_unwrapped_key", False, lean([EV["no_wrap_key"]]), wrapped)
    case("select_secret_event", False, lean([EV["secret_message"]]), wrapped)
    case("select_unknown_confidentiality", False, lean([EV["unknown_label"]]), wrapped)
    case("select_unknown_integrity", False, lean([EV["unknown_integrity"]]), wrapped)
    case("select_turn_start_naming_a_request", False, lean([EV["request_field"]]), wrapped)
    case("select_turn_start_naming_an_action", False, lean([EV["action_field"]]), wrapped)
    case("select_message_with_an_extra_field", False, lean([EV["extra_text_field"]]), wrapped)
    case("select_empty_chat", False, [], wrapped)
    case("select_two_feeds_report_each_head", False, lean([EV["user"], EV["turn_start"]]), wrapped)
    for type_, item in sorted(EV["never"].items()):
        case("select_never_" + type_.replace(".", "_"), False, lean([item]), wrapped)


def key_cases(add: Any) -> None:
    key_events = lean(FIX["key_events"])
    create = lean([FIX["create"]])[0]

    def case(name: str, phi: bool, ev: list[dict[str, Any]]) -> None:
        add(name, "share_keys", {"chat": CHAT, "create": create, "key_events": ev, "phi": phi})

    case("keys_chat_key_and_chat_scoped_keys", False, key_events)
    case("keys_with_phi_group_key_wrapped", True, key_events)
    case("keys_exclude_a_key_made_for_another_chat", True, key_events[:1] + key_events[3:])
    many = []
    c = Chain(OA)
    for i in range(66):
        kid, kb = ck(f"many/{i}")
        many.append(lean([c.event(f"many/{i}", T0 + 20000 + i, "mesh", "key.content", {"id": kid, "key": b64(kb), "scope": CHAT}, key=EK_P)])[0])
    case("keys_cap_keeps_the_newest_64", False, many)
    broken = copy.deepcopy(key_events)
    del broken[1]["body"]["key"]
    case("keys_malformed_key_content_body", False, broken)
    case("keys_exactly_64_withhold_none", False, many[:64])
    case("keys_cap_counts_only_keys_of_a_served_group", False, many + [key_events[2]])
    case("keys_cap_with_phi_counts_the_phi_key", True, many[:64] + [key_events[2]])


def grants_cases(add: Any) -> None:
    reader = {"mesh": MB.mesh, "device": BOB.id, "wrap": b64(BOB.wrap_public), "phi": False}
    chat_data = {"title": "Lab results", "create": lean([FIX["create"]])[0], "key_events": lean(FIX["key_events"]), "events": all_events(False) + lean(list(EV["never"].values()))}
    grant = {"id": GA, "contact": MB.mesh, "chat": CHAT, "phi": False, "supersedes": None}

    def case(name: str, rd: dict[str, Any], grants: list[dict[str, Any]], destination: bool = True, chats: Any = None) -> None:
        add(name, "share_grants", {"reader": rd, "grants": grants, "chats": chats or {CHAT: chat_data}, "destination_allowed": destination, "rng": "share-v1/grants/" + name})

    case("grants_plain_grant_for_a_non_phi_reader", reader, [grant])
    case("grants_phi_grant_for_a_non_phi_reader", reader, [{**grant, "phi": True}])
    case("grants_non_phi_grant_for_a_phi_reader", {**reader, "phi": True}, [grant])
    case("grants_phi_grant_for_a_phi_reader", {**reader, "phi": True}, [{**grant, "phi": True}])
    case("grants_phi_grant_with_a_withdrawn_destination", {**reader, "phi": True}, [{**grant, "phi": True}], destination=False)
    case("grants_only_the_readers_mesh", reader, [grant, {**grant, "id": GB, "contact": crypto.sha256_id(b"someone")}])
    case("grants_a_superseding_grant_names_the_old_one", {**reader, "phi": True}, [{**grant, "id": GB, "phi": True, "supersedes": GA}])
    case("grants_none_in_effect", reader, [])
    case("grants_a_bad_title_is_cleaned_not_fatal", reader, [grant], chats={CHAT: {**chat_data, "title": "Lab\u202e res\x00ults " + "x" * 300}})
    many = lean(FIX["key_events"][:1]) + key_events_many()
    case("grants_key_cap_reports_withheld_keys", reader, [grant], chats={CHAT: {**chat_data, "key_events": many}})


def key_events_many() -> list[dict[str, Any]]:
    c = Chain(OA)
    out = []
    for i in range(66):
        kid, kb = ck(f"cap/{i}")
        KEYS[kid] = kb
        out.extend(lean([c.event(f"cap/{i}", T0 + 30000 + i, "mesh", "key.content", {"id": kid, "key": b64(kb), "scope": CHAT}, key=EK_P)]))
    return out


def receive_cases(add: Any) -> None:
    steps = (
        ("shape", "share.malformed"),
        ("grant", "share.grant_unknown"),
        ("key", "share.key_unknown"),
        ("wrong_salt", "share.decrypt_failed"),
        ("parse", "event.malformed"),
        ("mismatch", "share.envelope_mismatch"),
        ("badsig", "share.bad_signature"),
        ("feed", "share.feed_unknown"),
        ("scope", "share.scope_mismatch"),
        ("type", "share.type_not_allowed"),
        ("label", "share.label_unknown"),
        ("bodyhash", "event.body_mismatch"),
        ("bodydec", "event.decrypt_failed"),
        ("bodyparse", "event.malformed"),
        ("fields", "share.body_field_refused"),
    )
    add("receive_valid_assistant_message", "share_receive", forge(set()))
    add("receive_valid_under_the_other_grant", "share_receive", forge(set(), grant=GB))
    for flag, _ in steps:
        add("receive_defect_" + flag, "share_receive", forge({flag}))
    for (first, _), (second, _) in zip(steps, steps[1:]):
        add(f"receive_defects_{first}_and_{second}", "share_receive", forge({first, second}))
    add("receive_secret_label", "share_receive", forge(set(), labels={"confidentiality": "secret", "integrity": "owner"}))
    add("receive_unknown_integrity", "share_receive", forge(set(), labels={"confidentiality": "private", "integrity": "trusted"}))
    add("receive_unknown_label_before_secret", "share_receive", forge(set(), labels={"confidentiality": "secret", "integrity": "trusted"}))
    add("receive_header_key_differs", "share_receive", forge(set(), key=CK_R, env_key=CK_P))
    add("receive_phi_event_with_the_phi_key", "share_receive", forge(set(), labels=PHI, key=CK_H))
    add("receive_phi_event_when_the_grant_has_no_phi_wrap", "share_receive", forge(set(), grant=GB, labels=PHI, key=CK_H))
    add("receive_removed_device_at_its_cutoff", "share_receive", forge(set(), device=OD, seq=5))
    add("receive_removed_device_above_its_cutoff", "share_receive", forge(set(), device=OD, seq=6))
    add("receive_node_feed", "share_receive", forge(set(), device=ON))
    add("receive_chat_create_of_the_granted_chat", "share_receive", chat_create_case(FIX["create"], GA))
    add("receive_chat_create_of_another_chat", "share_receive", chat_create_case(EV["other_create"], GA))
    add("receive_tool_result_any_fields", "share_receive", forge(set(), type_="chat.tool_result", body={"turn": "x", "extra": [1, 2]}))
    add("receive_turn_start_naming_a_request", "share_receive", forge(set(), type_="chat.turn_start", body={"continues": None, "message": None, "request": "r"}))
    add("receive_header_resealed_under_another_grant", "share_receive", forge({"wrong_salt"}, grant=GB))
    add("receive_body_shorter_than_nonce_and_tag", "share_receive", forge({"bodyshort"}))
    add("receive_body_duplicate_key", "share_receive", forge({"bodyparse"}, raw=b'{"text":"a","text":"b"}'))
    add("receive_body_not_json", "share_receive", forge({"bodyparse"}, raw=b"not json"))
    add("receive_body_not_an_object", "share_receive", forge(set(), body=[1]))
    reseal = forge(set())
    reseal["envelope"] = {**reseal["envelope"], "grant": GB}
    add("receive_envelope_regranted_after_sealing", "share_receive", reseal)
    tampered = forge(set())
    tampered["envelope"] = {**tampered["envelope"], "seq": 51}
    add("receive_envelope_seq_edited", "share_receive", tampered)
    short = forge(set())
    short["envelope"] = {**short["envelope"], "header_ct": b64(bytes(10))}
    add("receive_header_ct_too_short", "share_receive", short)
    wrapless = forge(set())
    wrapless["context"] = {**CTX, "reader": {**CTX["reader"], "device": DAVE.id}}
    add("receive_wrap_for_another_device", "share_receive", wrapless)
    badwrap = copy.deepcopy(CTX)
    badwrap["grants"][GA]["wraps"][CK_P]["ct"] = b64(bytes(48))
    add("receive_wrap_edited", "share_receive", {**forge(set()), "context": badwrap})


def chat_create_case(item: dict[str, Any], grant: str) -> dict[str, Any]:
    header = item["header"]
    envelope = share.seal_envelope(grant, KEYS[header["key"]], header, cj.b64url_decode(item["body_ct"]), DetRng("share-v1/create/" + eid(item)).bytes(24), key=header["key"], feed=header["feed"], seq=header["seq"])
    return {"envelope": envelope, "context": CTX}


def other_cases(add: Any) -> None:
    add("header_key_derivation", "share_header_key", {"content_key_hex": K_P.hex(), "grant": GA})
    add("header_key_changes_with_the_grant", "share_header_key", {"content_key_hex": K_P.hex(), "grant": GB})
    wrap = CTX["grants"][GA]["wraps"][CK_P]
    base = {"grant": GA, "key": CK_P, "recipient": BOB.id, "wrap_private_hex": BOB.wrap_private.hex(), "wrap": wrap}
    add("wrap_opens_for_the_reader", "share_open_wrap", base)
    add("wrap_wrong_grant", "share_open_wrap", {**base, "grant": GB})
    add("wrap_wrong_key", "share_open_wrap", {**base, "key": CK_R})
    add("wrap_wrong_recipient", "share_open_wrap", {**base, "recipient": DAVE.id})
    add("wrap_wrong_device_key", "share_open_wrap", {**base, "wrap_private_hex": DAVE.wrap_private.hex()})
    add("wrap_purpose_epoch_not_share", "share_open_wrap", {**base, "wrap": gpd.crypto.wrap_key(BOB.wrap_public, K_P, {"group": "personal", "purpose": "epoch", "recipient": BOB.id}, bytes(32), bytes(24))})
    eff = {"in_effect": [{"id": GA, "contact": MB.mesh}], "contact": MB.mesh, "grant": GA}
    held = [{"feed": OA.feed, "seq": s, "id": crypto.sha256_id(f"held{s}".encode())} for s in (1, 2, 4, 7)] + [{"feed": ON.feed, "seq": 3, "id": crypto.sha256_id(b"node3")}]
    add("want_sparse_range_returns_only_held", "share_want", {"held": held, "ranges": [{"feed": OA.feed, "from": 1, "to": 5}], **eff})
    add("want_two_feeds", "share_want", {"held": held, "ranges": sorted([{"feed": OA.feed, "from": 4, "to": 9}, {"feed": ON.feed, "from": 1, "to": 3}], key=lambda r: r["feed"]), **eff})
    add("want_unknown_feed", "share_want", {"held": held, "ranges": [{"feed": MALLORY.feed, "from": 1, "to": 3}], **eff})
    big = [{"feed": OA.feed, "seq": s, "id": crypto.sha256_id(f"b{s}".encode())} for s in range(1, 301)]
    add("want_caps_at_256_and_says_more", "share_want", {"held": big, "ranges": [{"feed": OA.feed, "from": 1, "to": 400}][:0] + [{"feed": OA.feed, "from": 1, "to": 512}], **eff})
    add("want_range_too_long", "share_want", {"held": held, "ranges": [{"feed": OA.feed, "from": 1, "to": 513}], **eff})
    fold = {"revoked": [GA]}
    add("revoked_names_only_revoked", "share_revoked", {"named": sorted([GA, GB]), "fold": fold})
    add("revoked_none_named", "share_revoked", {"named": [], "fold": fold})
    add("revoked_bad_id", "share_revoked", {"named": ["x"], "fold": fold})
    served_att = {
        eid(EV["attachment"]): {"labels": PRIVATE, "chunks": [CHUNKS["a"][1], CHUNKS["b"][1]]},
    }
    phi_att = {eid(EV["phi_attachment"]): {"labels": PHI, "chunks": [CHUNKS["p"][1]]}}
    req = sorted([CHUNKS["a"][1], CHUNKS["b"][1], CHUNKS["p"][1], crypto.sha256_id(b"stray")])
    add("chunks_named_by_a_served_attachment", "share_chunks", {"attachments": served_att, "requested": req, "phi": False, **eff})
    add("chunks_phi_attachment_without_phi", "share_chunks", {"attachments": {**served_att, **phi_att}, "requested": req, "phi": False, **eff})
    add("chunks_phi_attachment_with_phi", "share_chunks", {"attachments": {**served_att, **phi_att}, "requested": req, "phi": True, **eff})
    add("chunks_unserved_attachment_names_nothing", "share_chunks", {"attachments": {}, "requested": req, "phi": True, **eff})
    add("chunks_257_hashes", "share_chunks", {"attachments": served_att, "requested": sorted(crypto.sha256_id(str(i).encode()) for i in range(257)), "phi": False, **eff})
    for name, listed in (
        ("unknown", []),
        ("revoked", [{"id": GB, "contact": MB.mesh}]),
        ("expired", [{"id": GC, "contact": MB.mesh}]),
        ("another_contacts", [{"id": GA, "contact": crypto.sha256_id(b"someone")}]),
    ):
        stranger = {**eff, "in_effect": listed}
        add(f"want_grant_{name}", "share_want", {"held": held, "ranges": [{"feed": OA.feed, "from": 1, "to": 5}], **stranger})
        add(f"chunks_grant_{name}", "share_chunks", {"attachments": served_att, "requested": [CHUNKS["a"][1]], "phi": False, **stranger})
    add("want_grant_checked_before_the_ranges", "share_want", {"held": held, "ranges": [{"feed": OA.feed, "from": 1, "to": 513}], **{**eff, "in_effect": []}})
    members = {OA.id: {"feeds": [OA.feed]}}
    entries = {
        "r1": {"type": "remove", "body": {"device": OD.id, "cutoff": {OD.feed: 9}}},
        "r2": {"type": "remove", "body": {"device": OD.id, "cutoff": {OD.feed: 5}}},
    }
    fold = {"members": members, "removed": {OD.id: {}}}
    add("granter_roster_smallest_cutoff_wins", "share_granter_roster", {"roster": {"entries": entries, "fold": fold}})
    add("granter_roster_smallest_cutoff_wins_in_either_order", "share_granter_roster", {"roster": {"entries": {"r1": entries["r2"], "r2": entries["r1"]}, "fold": fold}})
    add("granter_roster_single_removal", "share_granter_roster", {"roster": {"entries": {"r1": entries["r1"]}, "fold": fold}})
    long_title = "a" * 255 + "\U0001f600" + "b" * 10
    for name, title in (
        ("plain", "Lab results"),
        ("drops_refused_characters", "Lab\u202e res\x00ults\u200b"),
        ("keeps_a_whole_character_at_the_limit", "a" * 255 + "\U0001f600" + "b" * 10),
        ("cuts_before_a_character_past_the_limit", "a" * 256 + "\U0001f600" + "b" * 10),
        ("truncates_after_dropping", "\x00" * 5 + long_title),
        ("not_text", None),
    ):
        add("title_" + name, "share_title", {"title": title})
    named = [CHUNKS["a"][1], CHUNKS["b"][1]]
    message = {"t": "chunks", "grant": GA, "chunks": [{"hash": CHUNKS["a"][1], "ct": b64(CHUNKS["a"][0])}], "withheld": [{"hash": CHUNKS["b"][1], "reason": "unknown"}]}
    add("chunks_receive_named_chunk", "share_chunks_receive", {"message": message, "named": named, "requested": named})
    add("chunks_receive_unnamed_chunk", "share_chunks_receive", {"message": message, "named": [CHUNKS["b"][1]], "requested": named})
    withheld_only = {**message, "chunks": [], "withheld": [{"hash": CHUNKS["b"][1], "reason": "unknown"}]}
    add("chunks_receive_withheld_hash_not_requested", "share_chunks_receive", {"message": withheld_only, "named": named, "requested": [CHUNKS["a"][1]]})
    add("chunks_receive_unrequested_chunk", "share_chunks_receive", {"message": message, "named": named, "requested": [CHUNKS["b"][1]]})
    add("chunks_receive_edited_bytes", "share_chunks_receive", {"message": {**message, "chunks": [{"hash": CHUNKS["a"][1], "ct": b64(CHUNKS["b"][0])}]}, "named": named, "requested": named})


def render() -> dict[str, str]:
    return {"share-v1": json.dumps(generate(), indent=2, sort_keys=True) + "\n"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render()
    differs = [
        name
        for name, content in rendered.items()
        if not (VECTORS / f"{name}.json").exists()
        or (VECTORS / f"{name}.json").read_text(encoding="utf-8") != content
    ]
    if args.check:
        if differs:
            raise SystemExit(", ".join(differs) + " vectors differ")
        return
    for name, content in rendered.items():
        (VECTORS / f"{name}.json").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
