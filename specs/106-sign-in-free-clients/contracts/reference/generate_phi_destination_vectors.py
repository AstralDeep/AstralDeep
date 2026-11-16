"""Generates the PHI destination and labels v2 vectors from public fixed test keys, clocks and ids, running every case through the references.
Roster entries are signed v2 entries built with the preserved 105 helpers on the roster v2 test devices, so a kernel can fold them as they are.
`--check` fails when a committed file differs and writes nothing.
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
MESH_REF = HERE.parents[2] / "105-decentralized-personal-mesh/contracts/reference"
for path in (HERE, MESH_REF):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
phi = importlib.import_module("phi_destinations")
attestation = importlib.import_module("astral_mesh_ref.attestation")
crypto = importlib.import_module("astral_mesh_ref.crypto")
keys = importlib.import_module("astral_mesh_ref.keys")
testkit = importlib.import_module("astral_mesh_ref.testkit")
b64 = importlib.import_module("astral_mesh_ref.cj").b64url_encode
DetRng, TestDevice, seed = testkit.DetRng, testkit.TestDevice, testkit.seed

VECTORS = HERE.parent / "vectors"
V1_LABELS = MESH_REF.parent / "vectors/labels.json"
T0 = 1794441600000
NOW_S = T0 // 1000 + 86400
M = "https://api.model.example/v1"
S = "https://mcp.research.example/mcp"
LOOP = "http://127.0.0.1:8765/mcp"
CONTACT = crypto.sha256_id(b"phi-destination-v1/contact-mesh")

ALICE = TestDevice("local-v2/alice")
BOB = TestDevice("local-v2/bob", kind="pocket")
CAROL = TestDevice("local-v2/carol", kind="satellite")
NODE = TestDevice("local-v2/node", kind="node")
DAVE = TestDevice("local-v2/dave")
BOB_RESTORED = TestDevice("phi-destination-v1/bob-restored", kind="pocket")
MALLORY = TestDevice("phi-destination-v1/mallory")
DEVICES = {
    "alice": ALICE,
    "bob": BOB,
    "carol": CAROL,
    "node": NODE,
    "dave": DAVE,
    "bob_restored": BOB_RESTORED,
    "mallory": MALLORY,
}
RECOVERY = keys.derive_recovery(seed("local-v2/recovery"))
BOB_OPS_2 = seed("local-v2/bob/ops-2")
BOB_WRAP_2 = seed("phi-destination-v1/bob/wrap-2")
BOB_FEED_2 = b64(crypto.ed25519_public(BOB_OPS_2))


def approver_scalar(device: Any) -> int:
    return testkit.scalar("phi-destination-v1/approver/" + device.name)


def approver_public(device: Any) -> bytes:
    return crypto.p256_public(approver_scalar(device))


def other_scalar(label: str) -> int:
    return testkit.scalar("phi-destination-v1/authority/" + label)


class Mesh:
    def __init__(self, label: str, creator: Any) -> None:
        self.label = label
        self.tick = 0
        self.entries: list[dict[str, Any]] = []
        self.names: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.event_labels: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.mark = (0, 0)
        self.seq: dict[str, int] = {}
        self.secrets: dict[str, bytes] = {}
        self.wraps = {d.id: d.wrap_public for d in DEVICES.values()}
        self.feeds = {d.id: [d.feed] for d in DEVICES.values()}
        self.mesh: str | None = None
        secret = seed(f"phi-destination-v1/{label}/genesis")
        clock = self.clock()
        body = {
            "device": creator.record(None),
            "epoch": keys.make_epoch_body(
                "personal",
                secret,
                {creator.id: creator.wrap_public},
                RECOVERY["wrap_public"],
                DetRng(f"phi-destination-v1/{label}/genesis"),
            ),
            "recovery": {
                "sign": b64(RECOVERY["sign_public"]),
                "wrap": b64(RECOVERY["wrap_public"]),
            },
        }
        genesis = phi.build_entry(
            "genesis",
            None,
            [],
            clock,
            creator.id,
            body,
            identity_scalar=creator.identity_scalar,
            presence_scalar=creator.presence_scalar,
        )
        self._keep(genesis, "genesis")
        self.mesh = phi.entry_id(genesis)
        self.genesis = self.mesh
        self.secrets[self.mesh] = secret

    def clock(self) -> dict[str, int]:
        self.tick += 1
        return {"ms": T0 + self.tick * 1000, "n": 0}

    def _keep(self, entry: dict[str, Any], name: str) -> dict[str, Any]:
        self.entries.append(entry)
        self.names.append(name)
        return entry

    def heads(self) -> list[str]:
        ids = [phi.entry_id(e) for e in self.entries]
        followed = {f for e in self.entries for f in e["follows"]}
        return sorted(i for i in ids if i not in followed)

    def write(
        self,
        name: str,
        kind: str,
        author: Any,
        body: Any,
        *,
        follows: list[str] | None = None,
        presence: bool | None = None,
        clock: dict[str, int] | None = None,
    ) -> str:
        clock = clock or self.clock()
        if callable(body):
            body = body(clock)
        if author == "recovery":
            entry = phi.build_entry(
                kind,
                self.mesh,
                self.heads() if follows is None else follows,
                clock,
                "recovery",
                body,
                recovery_seed=RECOVERY["sign_seed"],
            )
        else:
            needs = kind in phi.PRESENCE if presence is None else presence
            entry = phi.build_entry(
                kind,
                self.mesh,
                self.heads() if follows is None else follows,
                clock,
                author.id,
                body,
                identity_scalar=author.identity_scalar,
                presence_scalar=author.presence_scalar if needs else None,
            )
        self._keep(entry, name)
        return phi.entry_id(entry)

    def add(self, device: Any, by: Any = ALICE) -> str:
        return self.write("add_" + _short(device), "add", by, {"device": device.record(self.mesh)})

    def authority(self, device: Any, **kw: Any) -> str:
        body = phi.authority_body(approver_public(device), device.id)
        return self.write("approver_" + _short(device), "phi_authority", device, body, **kw)

    def grant(
        self,
        approver: Any,
        subject: str,
        kind: str = "endpoint",
        *,
        name: str | None = None,
        by: Any = None,
        scalar: int | None = None,
        **kw: Any,
    ) -> str:
        scalar = approver_scalar(approver) if scalar is None else scalar

        def body(clock: dict[str, int]) -> dict[str, Any]:
            statement = phi.approval_statement(scalar, kind, subject, clock["ms"] // 1000)
            return {"action": "grant", "attestation": statement}

        return self.write(name or "grant", "phi_approval", by or approver, body, **kw)

    def grant_device(self, approver: Any, device: Any, **kw: Any) -> str:
        return self.grant(approver, device.id, "device", name="grant_" + _short(device), **kw)

    def revoke(self, author: Any, subject: str, name: str = "revoke", **kw: Any) -> str:
        return self.write(name, "phi_approval", author, {"action": "revoke", "subject": subject}, **kw)

    def epoch(self, name: str, group: str, author: Any, members: list[Any], **kw: Any) -> str:
        secret = seed(f"phi-destination-v1/{self.label}/{name}")
        body = keys.make_epoch_body(
            group,
            secret,
            {d.id: self.wraps[d.id] for d in members},
            RECOVERY["wrap_public"] if group == "personal" else None,
            DetRng(f"phi-destination-v1/{self.label}/{name}"),
        )
        eid = self.write(name, "epoch", author, body, **kw)
        self.secrets[eid] = secret
        return eid

    def wrap(self, name: str, group: str, author: Any, target: Any, epochs: list[str], **kw: Any) -> str:
        grants = keys.make_wrap_grants(
            group,
            target.id,
            self.wraps[target.id],
            {e: self.secrets.get(e, seed("phi-destination-v1/unknown/" + e)) for e in epochs},
            DetRng(f"phi-destination-v1/{self.label}/{name}"),
        )
        body = {"device": target.id, "grants": grants, "group": group}
        return self.write(name, "wrap", author, body, **kw)

    def remove(self, by: Any, device: Any, cutoff: int = 0, **kw: Any) -> str:
        body = {"device": device.id, "cutoff": {f: cutoff for f in self.feeds[device.id]}}
        return self.write("remove_" + _short(device), "remove", by, body, **kw)

    def event(
        self,
        name: str,
        feed: str,
        body: Any,
        *,
        kind: str = "settings.set",
        scope: str = "mesh",
        labels: dict[str, str] | None = None,
    ) -> None:
        self.seq[feed] = self.seq.get(feed, 0) + 1
        self.tick += 1
        event = {
            "type": kind,
            "feed": feed,
            "seq": self.seq[feed],
            "clock": {"ms": T0 + self.tick * 1000, "n": 0},
            "scope": scope,
            "labels": labels or dict(phi.MARKER_LABELS),
            "body": body,
        }
        event = {"id": crypto.object_id(event), **event}
        self.events.append(event)
        self.event_labels.append(name)

    def marker(self, device: Any, name: str | None = None, feed: str | None = None) -> None:
        body = {"key": phi.MARKER_PREFIX + device.id, "value": dict(phi.MARKER_VALUE)}
        self.event(name or "marker_" + _short(device), feed or device.feed, body)

    def step(
        self,
        name: str,
        *,
        contacts: list[str] | None = None,
        queries: list[tuple[Any, str, Any]] | None = None,
        plans: list[dict[str, Any]] | None = None,
    ) -> None:
        start_e, start_v = self.mark
        self.steps.append(
            {
                "name": name,
                "entries": [phi.wire_text(e) for e in self.entries[start_e:]],
                "entry_names": self.names[start_e:],
                "events": self.events[start_v:],
                "event_names": self.event_labels[start_v:],
                "contacts": contacts or [],
                "queries": [
                    {"device": d.id, "kind": k, "target": t} for d, k, t in (queries or [])
                ],
                "plans": plans or [],
            }
        )
        self.mark = (len(self.entries), len(self.events))

    def case(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "kind": "phi_sequence",
            "input": {"mesh": self.mesh, "now_s": NOW_S, "steps": self.steps},
        }


def _short(device: Any) -> str:
    return device.name.split("/")[-1].replace("-", "_")


def include(device: Any, targets: list[Any]) -> dict[str, Any]:
    return {"op": "include", "device": device.id, "targets": [t.id for t in targets]}


def exclude(device: Any, target: Any) -> dict[str, Any]:
    return {"op": "exclude", "device": device.id, "target": target.id}


def local(device: Any, endpoint: str = LOOP) -> str:
    return phi.local_subject(device.id, endpoint)


def ready(label: str, members: list[Any], granted: list[Any], marked: list[Any]) -> tuple[Mesh, str]:
    m = Mesh(label, ALICE)
    for device in members:
        m.add(device)
    for device in marked:
        m.marker(device)
    m.authority(ALICE)
    for device in granted:
        m.grant_device(ALICE, device)
    first = m.epoch("phi_epoch_1", "phi", ALICE, granted)
    return m, first


def seq_create_and_pair() -> dict[str, Any]:
    m = Mesh("create", ALICE)
    m.authority(ALICE)
    m.grant_device(ALICE, ALICE)
    first = m.epoch("phi_epoch_1", "phi", ALICE, [ALICE])
    m.step("create")
    m.marker(ALICE)
    m.step("alice_marker")
    m.add(BOB)
    m.wrap("wrap_personal_bob", "personal", ALICE, BOB, [m.genesis])
    m.step("pair_bob")
    m.marker(BOB)
    m.step("bob_marker", plans=[include(ALICE, [BOB]), include(BOB, [BOB])])
    m.grant_device(ALICE, BOB)
    m.wrap("wrap_phi_bob", "phi", ALICE, BOB, [first])
    m.step("grant_bob")
    return m.case("seq_create_and_pair")


def seq_adopt_group() -> dict[str, Any]:
    m = Mesh("adopt", ALICE)
    for device in (BOB, NODE, DAVE):
        m.add(device)
    m.step("existing_mesh")
    for device in (ALICE, BOB, NODE):
        m.marker(device)
    m.step(
        "markers",
        plans=[include(ALICE, [ALICE, BOB, NODE]), include(NODE, [NODE]), include(ALICE, [DAVE])],
    )
    m.authority(ALICE)
    for device in (ALICE, BOB, NODE):
        m.grant_device(ALICE, device)
    m.step("adopt_grants")
    first = m.epoch("phi_epoch_1", "phi", ALICE, [ALICE, BOB, NODE])
    m.step(
        "adopt_epoch",
        plans=[
            include(ALICE, [MALLORY]),
            include(ALICE, [BOB]),
            include(ALICE, [MALLORY, BOB]),
            exclude(ALICE, DAVE),
            exclude(ALICE, MALLORY),
            exclude(NODE, BOB),
        ],
    )
    m.add(CAROL)
    m.step("pair_carol")
    m.marker(CAROL)
    m.step("carol_marker", plans=[include(ALICE, [CAROL])])
    m.grant_device(ALICE, CAROL)
    m.wrap("wrap_phi_carol", "phi", ALICE, CAROL, [first])
    m.step("grant_carol", plans=[exclude(ALICE, BOB)])
    m.revoke(ALICE, BOB.id, "revoke_bob")
    second = m.epoch("phi_epoch_2", "phi", ALICE, [ALICE, CAROL, NODE])
    m.step("exclude_bob", plans=[exclude(ALICE, ALICE)])
    m.marker(BOB, "marker_bob_again")
    m.step("bob_not_granted_again")
    m.revoke(ALICE, ALICE.id, "revoke_alice")
    m.step("self_exclude")
    third = m.epoch("phi_epoch_3", "phi", NODE, [CAROL, NODE])
    m.step("rekey", plans=[include(CAROL, [BOB])])
    m.authority(CAROL)
    m.grant_device(CAROL, BOB)
    m.wrap("wrap_phi_bob", "phi", CAROL, BOB, [second, third])
    m.step("include_bob")
    return m.case("seq_adopt_group")


def seq_removal_by_deployed_device() -> dict[str, Any]:
    m, _ = ready("deployed", [BOB, NODE, DAVE], [ALICE, BOB, NODE], [ALICE, BOB, NODE])
    m.step("group_ready")
    m.remove(DAVE, BOB)
    m.epoch("personal_epoch_2", "personal", DAVE, [ALICE, NODE, DAVE])
    m.step("deployed_removes_bob")
    m.epoch("phi_epoch_2", "phi", ALICE, [ALICE, NODE])
    m.step("rekey")
    return m.case("seq_removal_by_deployed_device")


def seq_removal_by_phi_member() -> dict[str, Any]:
    m, _ = ready("member", [BOB, NODE, DAVE], [ALICE, BOB, NODE], [ALICE, BOB, NODE])
    m.step("group_ready")
    m.remove(ALICE, BOB)
    m.epoch("personal_epoch_2", "personal", ALICE, [ALICE, NODE, DAVE])
    m.epoch("phi_epoch_2", "phi", ALICE, [ALICE, NODE])
    m.step("member_removes_bob")
    return m.case("seq_removal_by_phi_member")


def seq_markers() -> dict[str, Any]:
    m = Mesh("markers", ALICE)
    m.add(BOB)
    record = {
        "identity": b64(BOB.identity_public),
        "kind": BOB.kind,
        "label": BOB.name,
        "ops": BOB_FEED_2,
        "presence": b64(BOB.presence_public),
        "wrap": b64(crypto.x25519_public(BOB_WRAP_2)),
    }
    record["cert"] = crypto.sign_detached(
        "p256", BOB.identity_scalar, "astral.pairing/v1", {"device": record, "mesh": m.mesh}
    )
    m.write("rotate_bob", "rotate", BOB, {"device": record})
    m.feeds[BOB.id] = [BOB.feed, BOB_FEED_2]
    m.add(NODE)
    m.add(CAROL)
    m.add(DAVE)
    m.marker(CAROL, "removed_device")
    m.remove(ALICE, CAROL, cutoff=1)
    value = dict(phi.MARKER_VALUE)
    m.marker(ALICE, "alice")
    m.marker(BOB, "bob_second_feed", BOB_FEED_2)
    m.marker(NODE, "foreign_feed", BOB.feed)
    later = {"v": 2, "detector": "astral.phi-detector/v2", "labels": "astral.labels/v2", "screens": ["model"]}
    m.event("node_later_version", NODE.feed, {"key": phi.MARKER_PREFIX + NODE.id, "value": later})
    m.marker(MALLORY, "stranger")
    dave_key = phi.MARKER_PREFIX + DAVE.id
    bad_values = {
        "wrong_value": {"v": 1, "detector": "astral.phi-detector/v1"},
        "null_value": None,
        "version_zero": dict(value, v=0),
        "version_text": dict(value, v="1"),
        "version_true": dict(value, v=True),
    }
    for name, bad in bad_values.items():
        m.event(name, DAVE.feed, {"key": dave_key, "value": bad})
    chat = crypto.sha256_id(b"phi-destination-v1/markers/chat")
    m.event("chat_scope", DAVE.feed, {"key": dave_key, "value": value}, scope=chat)
    m.event(
        "wrong_labels",
        DAVE.feed,
        {"key": dave_key, "value": value},
        labels={"confidentiality": "public", "integrity": "owner"},
    )
    m.event("extra_body_field", DAVE.feed, {"key": dave_key, "value": value, "consent": {}})
    upper = phi.MARKER_PREFIX + "sha256:" + DAVE.id[7:].upper()
    m.event("uppercase_device", DAVE.feed, {"key": upper, "value": value})
    m.event("trailing_segment", DAVE.feed, {"key": dave_key + "/x", "value": value})
    m.event(
        "order_malformed_before_member",
        MALLORY.feed,
        {"key": phi.MARKER_PREFIX + MALLORY.id, "value": dict(value, v=0)},
    )
    m.event(
        "order_member_before_feed",
        ALICE.feed,
        {"key": phi.MARKER_PREFIX + MALLORY.id, "value": value},
    )
    m.event("not_read_remember", DAVE.feed, {"key": dave_key, "value": value}, kind="mem.remember")
    m.event("not_read_bare_prefix", DAVE.feed, {"key": "phi.capable", "value": value})
    m.event("not_read_other_key", DAVE.feed, {"key": "theme", "value": "dark"})
    m.step("markers")
    return m.case("seq_markers")


def seq_concurrent_revoke_and_grant() -> dict[str, Any]:
    m, _ = ready("concurrent", [BOB, CAROL], [ALICE, BOB, CAROL], [ALICE, BOB, CAROL])
    m.authority(CAROL)
    m.grant(ALICE, M, name="grant_model")
    m.step("group_ready")
    heads = m.heads()
    m.revoke(ALICE, BOB.id, "revoke_bob", follows=heads)
    m.revoke(ALICE, M, "revoke_model", follows=heads)
    m.grant(CAROL, BOB.id, "device", name="grant_bob_concurrent", follows=heads)
    m.grant(CAROL, M, name="grant_model_concurrent", follows=heads)
    m.step("concurrent")
    m.grant_device(ALICE, BOB)
    m.grant(ALICE, M, name="grant_model_again")
    m.step("grant_after_revoke")
    return m.case("seq_concurrent_revoke_and_grant")


def seq_destinations() -> dict[str, Any]:
    m, _ = ready("destinations", [BOB, DAVE], [ALICE, BOB], [ALICE, BOB])
    m.step("group_ready")
    for name, subject in (
        ("grant_model", M),
        ("grant_service", S),
        ("grant_local_service", local(ALICE)),
        ("grant_contact", "contact:" + CONTACT),
        ("ffi_loopback_unbound", "http://127.0.0.1:9000/v1"),
        ("ffi_wildcard", "https://*.model.example/v1"),
        ("ffi_not_canonical", "https://API.model.example/v2"),
        ("ffi_unknown_form", "urn:example:destination"),
    ):
        m.grant(ALICE, subject, name=name)
    m.grant(ALICE, "node-7", "node", name="legacy_node_subject")
    m.step(
        "approvals",
        queries=[
            (ALICE, "model", M),
            (ALICE, "model", "HTTPS://API.model.example/v1/"),
            (ALICE, "service", M),
            (ALICE, "service", S),
            (ALICE, "model", "https://other.model.example/v1"),
            (ALICE, "model", "http://localhost:11434/v1"),
            (DAVE, "model", "http://localhost:11434/v1"),
            (DAVE, "model", M),
            (ALICE, "service", LOOP),
            (ALICE, "service", local(ALICE)),
            (ALICE, "service", local(BOB)),
            (ALICE, "model", local(ALICE)),
            (ALICE, "contact", CONTACT),
            (ALICE, "model", "https://*.model.example/v1"),
            (ALICE, "device", BOB.id),
            (ALICE, "device", DAVE.id),
            (ALICE, "printer", M),
            (ALICE, "contact", "sha256:ab"),
            (ALICE, "service", "local:" + ALICE.id + ":http://LOCALHOST:8765/mcp"),
        ],
    )
    m.step("contact_verified", contacts=[CONTACT], queries=[(ALICE, "contact", CONTACT)])
    m.revoke(ALICE, S, "revoke_service")
    m.step("revoke_service", contacts=[CONTACT], queries=[(ALICE, "service", S)])
    m.grant(ALICE, S, name="grant_service_again")
    m.step("grant_again", contacts=[CONTACT], queries=[(ALICE, "service", S)])
    return m.case("seq_destinations")


def seq_approver_removed() -> dict[str, Any]:
    m, _ = ready("approver", [BOB], [ALICE, BOB], [ALICE, BOB])
    m.authority(BOB)
    m.grant(BOB, "https://api.bob.example/v1", name="bob_grant_endpoint")
    m.grant(BOB, local(BOB), name="bob_grant_local")
    m.grant(ALICE, M, name="alice_grant_model")
    m.grant(ALICE, local(BOB, "http://127.0.0.1:8766/mcp"), name="alice_grant_local_on_bob")
    m.step("group_ready")
    m.remove(ALICE, BOB)
    m.epoch("personal_epoch_2", "personal", ALICE, [ALICE])
    m.epoch("phi_epoch_2", "phi", ALICE, [ALICE])
    m.step(
        "remove_bob",
        queries=[(ALICE, "model", "https://api.bob.example/v1"), (ALICE, "model", M)],
    )
    m.add(BOB_RESTORED)
    m.marker(BOB_RESTORED)
    m.authority(BOB_RESTORED)
    m.grant(BOB_RESTORED, "https://api.bob.example/v1", name="restored_grant_endpoint")
    m.step(
        "restored_device_approves",
        queries=[(ALICE, "model", "https://api.bob.example/v1")],
    )
    return m.case("seq_approver_removed")


def seq_legacy_authority() -> dict[str, Any]:
    m, _ = ready("legacy", [BOB, CAROL], [ALICE, BOB], [ALICE, BOB, CAROL])
    m.grant(ALICE, "https://api.shared.example/v1", name="alice_grant_shared")
    m.step("group_ready")
    clinic = other_scalar("clinic")
    mismatch = other_scalar("mismatch")
    for name, scalar, label in (
        ("legacy_clinic", clinic, "Example Clinic"),
        ("legacy_mismatch", mismatch, phi.APPROVER_PREFIX + BOB.id),
    ):
        body = {
            "action": "add",
            "institution": {"key": b64(crypto.p256_public(scalar)), "name": label},
        }
        m.write(name, "phi_authority", ALICE, body)
    m.grant(ALICE, "https://api.clinic.example/v1", name="legacy_clinic_endpoint", scalar=clinic)
    m.grant(ALICE, CAROL.id, "device", name="legacy_clinic_device", scalar=clinic)
    m.grant(ALICE, "https://api.shared.example/v1", name="legacy_clinic_shared", scalar=clinic)
    m.grant(ALICE, "https://api.other.example/v1", name="legacy_mismatch_endpoint", scalar=mismatch)
    m.step("legacy_authorities", queries=[(ALICE, "model", "https://api.clinic.example/v1")])
    body = {"action": "remove", "key": b64(crypto.p256_public(clinic))}
    m.write("legacy_clinic_removed", "phi_authority", ALICE, body)
    m.step("authority_removed")
    return m.case("seq_legacy_authority")


def fold_cases() -> list[dict[str, Any]]:
    cases = []

    def single(name: str, m: Mesh) -> None:
        m.step("fold")
        cases.append(m.case(name))

    def base(label: str, members: list[Any], granted: list[Any]) -> Mesh:
        m = Mesh(label, ALICE)
        for device in members:
            m.add(device)
        m.authority(ALICE)
        for device in granted:
            m.grant_device(ALICE, device)
        return m

    m = Mesh("epoch-concurrent", ALICE)
    m.add(BOB)
    early = m.heads()
    m.authority(ALICE)
    m.grant_device(ALICE, ALICE)
    m.grant_device(ALICE, BOB)
    m.epoch("bad_epoch_concurrent_with_grants", "phi", ALICE, [ALICE, BOB], follows=early)
    single("fold_epoch_concurrent_with_grants", m)

    m = base("epoch-non-member", [BOB], [ALICE])
    m.epoch("bad_phi_epoch_by_non_member", "phi", BOB, [ALICE])
    single("fold_phi_epoch_by_non_member", m)

    m = base("wrap-non-member", [BOB], [ALICE])
    first = m.epoch("phi_epoch_1", "phi", ALICE, [ALICE])
    m.wrap("bad_wrap_to_non_member", "phi", ALICE, BOB, [first])
    single("fold_wrap_to_non_member", m)

    m = base("wrap-by-non-member", [BOB, CAROL, DAVE], [ALICE, BOB, CAROL])
    first = m.epoch("phi_epoch_1", "phi", ALICE, [ALICE, BOB])
    m.wrap("bad_wrap_by_non_member", "phi", DAVE, CAROL, [first])
    single("fold_wrap_by_non_member", m)

    m = base("wrap-unknown", [BOB], [ALICE, BOB])
    m.epoch("phi_epoch_1", "phi", ALICE, [ALICE, BOB])
    unknown = crypto.sha256_id(b"phi-destination-v1/no-such-epoch")
    m.wrap("bad_wrap_unknown_epoch", "phi", ALICE, BOB, [unknown])
    single("fold_wrap_unknown_epoch", m)

    m = base("wrap-personal", [BOB], [ALICE, BOB])
    m.epoch("phi_epoch_1", "phi", ALICE, [ALICE, BOB])
    m.wrap("bad_wrap_personal_epoch_as_phi", "phi", ALICE, BOB, [m.genesis])
    single("fold_wrap_personal_epoch_as_phi", m)

    m = base("recovery-epoch", [], [ALICE])
    m.epoch("bad_recovery_phi_epoch", "phi", "recovery", [ALICE])
    single("fold_recovery_phi_epoch", m)

    m = base("no-presence", [BOB], [ALICE])
    m.grant(ALICE, BOB.id, "device", name="bad_grant_without_presence", presence=False)
    single("fold_grant_without_presence", m)

    m = base("bad-presence", [BOB], [ALICE])
    clock = m.clock()
    statement = phi.approval_statement(approver_scalar(ALICE), "device", BOB.id, clock["ms"] // 1000)
    entry = phi.build_entry(
        "phi_approval",
        m.mesh,
        m.heads(),
        clock,
        ALICE.id,
        {"action": "grant", "attestation": statement},
        identity_scalar=ALICE.identity_scalar,
        presence_scalar=ALICE.identity_scalar,
    )
    m._keep(entry, "bad_grant_bad_presence")
    single("fold_grant_bad_presence", m)

    m = base("unknown-authority", [BOB], [ALICE])
    m.grant(MALLORY, BOB.id, "device", name="bad_grant_unknown_authority", by=ALICE)
    single("fold_grant_unknown_authority", m)

    m = base("statement-signature", [BOB], [ALICE])

    def edited(clock: dict[str, int]) -> dict[str, Any]:
        statement = phi.approval_statement(approver_scalar(ALICE), "endpoint", M, clock["ms"] // 1000)
        statement["subject"] = S
        return {"action": "grant", "attestation": statement}

    m.write("bad_grant_bad_statement_signature", "phi_approval", ALICE, edited)
    single("fold_grant_bad_statement_signature", m)

    m = base("statement-malformed", [BOB], [ALICE])

    def short(clock: dict[str, int]) -> dict[str, Any]:
        statement = phi.approval_statement(approver_scalar(ALICE), "endpoint", M, clock["ms"] // 1000)
        del statement["issued_s"]
        return {"action": "grant", "attestation": statement}

    m.write("bad_grant_malformed_statement", "phi_approval", ALICE, short)
    single("fold_grant_malformed_statement", m)

    m = base("removed-device", [DAVE], [ALICE])
    m.remove(ALICE, DAVE)
    m.grant(ALICE, DAVE.id, "device", name="bad_grant_for_removed_device")
    single("fold_grant_for_removed_device", m)

    m = base("authority-unknown", [], [ALICE])
    m.write(
        "bad_authority_remove_unknown",
        "phi_authority",
        ALICE,
        {"action": "remove", "key": b64(approver_public(MALLORY))},
    )
    single("fold_authority_remove_unknown", m)

    m = base("recovery-grant", [BOB], [ALICE])

    def recovered(clock: dict[str, int]) -> dict[str, Any]:
        statement = phi.approval_statement(approver_scalar(ALICE), "device", BOB.id, clock["ms"] // 1000)
        return {"action": "grant", "attestation": statement}

    m.write("bad_grant_by_recovery", "phi_approval", "recovery", recovered)
    single("fold_grant_by_recovery", m)

    m = base("authority-presence", [BOB], [ALICE])
    m.write(
        "bad_authority_without_presence",
        "phi_authority",
        BOB,
        phi.authority_body(approver_public(BOB), BOB.id),
        presence=False,
    )
    single("fold_authority_without_presence", m)

    m = base("stranger", [], [ALICE])
    m.grant(ALICE, M, name="bad_grant_by_stranger", by=MALLORY)
    single("fold_grant_by_stranger", m)

    m = base("void", [BOB], [ALICE, BOB])
    m.authority(BOB)
    heads = m.heads()
    m.remove(ALICE, BOB, follows=heads)
    m.grant(BOB, "https://api.void.example/v1", name="bob_grant_concurrent", follows=heads)
    single("fold_void_after_removal", m)

    m = base("pending", [BOB], [ALICE])
    missing = crypto.sha256_id(b"phi-destination-v1/missing-entry")
    m.grant(ALICE, BOB.id, "device", name="grant_after_missing", follows=sorted(m.heads() + [missing]))
    single("fold_pending_entry", m)

    m = base("expired", [BOB], [ALICE])
    short_scalar = other_scalar("short")
    body = {
        "action": "add",
        "institution": {"key": b64(crypto.p256_public(short_scalar)), "name": "Example Short"},
    }
    m.write("legacy_short_authority", "phi_authority", ALICE, body)
    for name, kind, subject in (
        ("legacy_short_device", "device", BOB.id),
        ("legacy_short_endpoint", "endpoint", "https://api.short.example/v1"),
    ):
        statement = attestation.build_phi(
            short_scalar, crypto.p256_public(short_scalar), kind, subject, 1000, 2000
        )
        m.write(name, "phi_approval", ALICE, {"action": "grant", "attestation": statement})
    single("fold_expired_approval", m)

    m = base("endpoint-device", [DAVE], [ALICE])
    m.grant(ALICE, DAVE.id, "endpoint", name="ffi_endpoint_naming_dave")
    single("fold_endpoint_subject_naming_a_device", m)
    return cases


def endpoint_cases() -> list[tuple[str, Any]]:
    host_253 = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 53, "example"])
    long_path = "https://api.model.example/"
    return [
        ("endpoint_plain", M),
        ("endpoint_uppercase_scheme_and_host", "HTTPS://API.Model.Example/v1"),
        ("endpoint_path_case_kept", "https://api.model.example/V1"),
        ("endpoint_default_port_dropped", "https://api.model.example:443/v1"),
        ("endpoint_other_port_kept", "https://api.model.example:8443/v1"),
        ("endpoint_https_port_80_kept", "https://api.model.example:80/v1"),
        ("endpoint_port_65535", "https://api.model.example:65535/v1"),
        ("endpoint_trailing_slash_removed", M + "/"),
        ("endpoint_root_slash_removed", "https://api.model.example/"),
        ("endpoint_no_path", "https://api.model.example"),
        ("endpoint_reserved_escape_kept", "https://api.model.example/a%2Fb"),
        ("endpoint_at_sign_in_path", "https://api.model.example/v1@x"),
        ("endpoint_punycode", "https://xn--bcher-kva.example/v1"),
        ("endpoint_punycode_uppercase", "https://XN--BCHER-KVA.example/v1"),
        ("endpoint_label_63", "https://" + "a" * 63 + ".example"),
        ("endpoint_host_253", "https://" + host_253),
        ("endpoint_2048_characters", long_path + "a" * (2048 - len(long_path))),
        ("endpoint_loopback_localhost_http", "http://localhost:11434/v1"),
        ("endpoint_loopback_localhost_https", "https://localhost:8443/v1"),
        ("endpoint_loopback_localhost_uppercase", "http://LOCALHOST:11434/v1"),
        ("endpoint_loopback_subdomain", "http://llm.localhost:8080/v1"),
        ("endpoint_loopback_ipv4", "http://127.0.0.1:8080/v1"),
        ("endpoint_loopback_ipv4_other_address", "http://127.2.3.4:8080/v1"),
        ("endpoint_loopback_ipv6", "http://[::1]:8080/v1"),
        ("endpoint_http_default_port_dropped", "http://localhost:80/v1"),
        ("endpoint_http_port_443_kept", "http://localhost:443/v1"),
        ("endpoint_loopback_mcp", LOOP),
        ("endpoint_wildcard_label", "https://*.model.example/v1"),
        ("endpoint_wildcard_inside_label", "https://api-*.model.example"),
        ("endpoint_wildcard_question_mark", "https://api?.model.example/v1"),
        ("endpoint_wildcard_bracket_class", "https://api[0-9].model.example/v1"),
        ("endpoint_wildcard_leading_dot", "https://.model.example/v1"),
        ("endpoint_wildcard_trailing_dot", "https://api.model.example./v1"),
        ("endpoint_wildcard_double_dot", "https://api..model.example/v1"),
        ("endpoint_wildcard_in_path", "https://api.model.example/v1/*"),
        ("endpoint_loopback_trailing_dot", "http://localhost.:11434/v1"),
        ("endpoint_ipv6_zone", "http://[::1%25lo0]:8080/v1"),
        ("endpoint_query_without_path", "https://api.model.example?x=1"),
        ("endpoint_order_wildcard_before_query", "https://*.model.example/v1?x=1"),
        ("endpoint_order_wildcard_before_userinfo", "https://owner@*.model.example/v1"),
        ("endpoint_order_text_before_wildcard", "https://*.model.example/v1 "),
        ("endpoint_order_userinfo_before_bracket", "https://owner@[0-9].model.example/v1"),
        ("endpoint_userinfo", "https://owner@api.model.example/v1"),
        ("endpoint_query", M + "?api-version=1"),
        ("endpoint_fragment", M + "#top"),
        ("endpoint_fragment_without_path", "https://api.model.example#top"),
        ("endpoint_list", [M, S]),
        ("endpoint_empty", ""),
        ("endpoint_number", 7),
        ("endpoint_null", None),
        ("endpoint_two_urls", M + " " + S),
        ("endpoint_non_ascii_host", "https://bücher.example/v1"),
        ("endpoint_2049_characters", long_path + "a" * (2049 - len(long_path))),
        ("endpoint_http_remote", "http://api.model.example/v1"),
        ("endpoint_other_scheme", "ftp://api.model.example/v1"),
        ("endpoint_websocket_scheme", "wss://api.model.example/v1"),
        ("endpoint_no_scheme", "api.model.example/v1"),
        ("endpoint_single_label", "https://intranet/v1"),
        ("endpoint_underscore", "https://api_model.example/v1"),
        ("endpoint_numeric_last_label", "https://api.123/v1"),
        ("endpoint_hex_last_label", "https://api.0x1f/v1"),
        ("endpoint_label_64", "https://" + "a" * 64 + ".example"),
        ("endpoint_host_254", "https://a" + host_253),
        ("endpoint_empty_host", "https:///v1"),
        ("endpoint_port_leading_zero", "https://api.model.example:0443/v1"),
        ("endpoint_port_zero", "https://api.model.example:0/v1"),
        ("endpoint_port_65536", "https://api.model.example:65536/v1"),
        ("endpoint_port_empty", "https://api.model.example:/v1"),
        ("endpoint_port_not_digits", "https://api.model.example:https/v1"),
        ("endpoint_public_ipv4", "https://8.8.8.8/v1"),
        ("endpoint_private_ipv4", "https://10.0.0.5/v1"),
        ("endpoint_loopback_ipv4_short_form", "http://127.1:8080/v1"),
        ("endpoint_loopback_ipv4_leading_zero", "http://127.0.0.01:8080/v1"),
        ("endpoint_loopback_ipv4_octet_256", "http://127.0.0.256:8080/v1"),
        ("endpoint_loopback_ipv6_long_form", "http://[0:0:0:0:0:0:0:1]:8080/v1"),
        ("endpoint_public_ipv6", "https://[2001:db8::1]/v1"),
        ("endpoint_bracket_trailing_text", "http://[::1]x/v1"),
        ("endpoint_double_trailing_slash", M + "//"),
        ("endpoint_empty_segment", "https://api.model.example//v1"),
        ("endpoint_dot_segment", "https://api.model.example/v1/../v2"),
        ("endpoint_single_dot_segment", "https://api.model.example/./v1"),
        ("endpoint_escaped_dot_segment", "https://api.model.example/%2E%2E/v1"),
        ("endpoint_escape_lowercase_hex", "https://api.model.example/a%2fb"),
        ("endpoint_escape_unreserved", "https://api.model.example/%41"),
        ("endpoint_escape_incomplete", "https://api.model.example/a%2"),
        ("endpoint_backslash_in_path", "https://api.model.example/v1\\x"),
        ("endpoint_percent_in_host", "https://api%2Emodel.example/v1"),
    ]


def subject_cases() -> list[tuple[str, Any]]:
    return [
        ("subject_endpoint", M),
        ("subject_endpoint_not_canonical", "https://API.model.example/v1"),
        ("subject_endpoint_trailing_slash", M + "/"),
        ("subject_endpoint_scheme_uppercase", "HTTPS://api.model.example/v1"),
        ("subject_endpoint_wildcard", "https://*.model.example/v1"),
        ("subject_endpoint_query", M + "?x=1"),
        ("subject_loopback_unbound", LOOP),
        ("subject_local_bound", local(ALICE)),
        ("subject_local_remote_endpoint", "local:" + ALICE.id + ":" + M),
        ("subject_local_bad_device", "local:sha256:" + ALICE.id[7:].upper() + ":" + LOOP),
        ("subject_local_missing_endpoint", "local:" + ALICE.id + ":"),
        ("subject_local_not_canonical", "local:" + ALICE.id + ":http://LOCALHOST:8765/mcp"),
        ("subject_contact", "contact:" + CONTACT),
        ("subject_contact_uppercase", "contact:sha256:" + CONTACT[7:].upper()),
        ("subject_contact_short", "contact:" + CONTACT[:-1]),
        ("subject_contact_without_prefix", "contact:" + CONTACT[7:]),
        ("subject_device", ALICE.id),
        ("subject_unknown_form", "urn:example:destination"),
        ("subject_node_name", "node-7"),
        ("subject_mail_address", "mailto:owner@example.org"),
    ]


def provider(base_url: Any = M, **extra: Any) -> dict[str, Any]:
    source = {"source": "provider", "name": "Research model", "base_url": base_url}
    return {**source, **extra}


def connection(endpoint: Any, use: str = "service", device: Any = None, confined: bool = False) -> dict[str, Any]:
    return {
        "source": "connection",
        "name": "Research tools",
        "use": use,
        "endpoint": endpoint,
        "device": device.id if device else None,
        "confined": confined,
    }


def proposal_cases() -> list[tuple[str, Any]]:
    missing = provider()
    del missing["base_url"]
    return [
        ("propose_provider_remote", provider()),
        ("propose_provider_canonicalized", provider("HTTPS://API.model.example:443/v1/")),
        ("propose_provider_loopback", provider("http://localhost:11434/v1")),
        ("propose_provider_wildcard", provider("https://*.model.example/v1")),
        ("propose_provider_list", provider([M, S])),
        ("propose_provider_missing_url", missing),
        ("propose_connection_remote", connection(S)),
        ("propose_connection_local", connection(LOOP, device=BOB)),
        ("propose_connection_local_scheme_none", connection("http://127.0.0.1:8770/mcp", device=NODE)),
        ("propose_connection_local_without_device", connection(LOOP)),
        ("propose_connection_confined", connection(None, confined=True)),
        ("propose_connection_without_endpoint", connection(None)),
        ("propose_connection_model", connection(M, use="model")),
        ("propose_connection_model_loopback", connection("http://127.0.0.1:1234/v1", use="model")),
        ("propose_connection_unknown_use", connection(S, use="tools")),
        ("propose_legacy_server_remote", {"source": "legacy_server", "name": "lab", "endpoint": S}),
        ("propose_legacy_server_loopback", {"source": "legacy_server", "name": "lab", "endpoint": LOOP}),
        ("propose_contact", {"source": "contact", "name": "Avery Quill", "mesh_id": CONTACT}),
        ("propose_contact_malformed", {"source": "contact", "name": "Avery Quill", "mesh_id": "sha256:ab"}),
        ("propose_unknown_source", {"source": "plugin", "name": "Plug"}),
        ("propose_not_an_object", "provider"),
        ("propose_name_not_text", provider(name=7)),
    ]


def approval_cases() -> list[tuple[str, Any, Any]]:
    contact = {"source": "contact", "name": "Avery Quill", "mesh_id": CONTACT}
    return [
        ("approve_endpoint", provider(), ALICE),
        ("approve_service", connection(S), ALICE),
        ("approve_local_service", connection(LOOP, device=BOB), ALICE),
        ("approve_legacy_loopback_binds_this_device", {"source": "legacy_server", "name": "lab", "endpoint": LOOP}, ALICE),
        ("approve_on_device_model_refused", provider("http://localhost:11434/v1"), ALICE),
        ("approve_confined_service_refused", connection(None, confined=True), ALICE),
        ("approve_contact_unavailable", contact, ALICE),
        ("approve_on_node_refused", provider(), NODE),
        ("approve_wildcard_refused", provider("https://*.model.example/v1"), ALICE),
        ("approve_list_refused", provider([M, S]), ALICE),
        ("approve_order_destination_before_node", provider("https://*.model.example/v1"), NODE),
        ("approve_order_node_before_on_device", provider("http://localhost:11434/v1"), NODE),
        ("approve_order_node_before_contact", contact, NODE),
        ("approve_satellite_allowed", provider(), CAROL),
    ]


def chat_cases() -> list[tuple[str, dict[str, Any]]]:
    def eid(label: str) -> str:
        return crypto.sha256_id(("phi-destination-v1/chat/" + label).encode())

    chat, other = eid("chat"), eid("other-chat")
    p0, p1, h0, h1 = eid("personal-0"), eid("personal-1"), eid("phi-0"), eid("phi-1")
    fa, fb = b64(crypto.ed25519_public(seed("chat/feed-a"))), b64(crypto.ed25519_public(seed("chat/feed-b")))
    fa, fb = sorted((fa, fb))

    def key(name: str, group: str, epoch: str, ms: int = 10, n: int = 0, feed: str = fa, seq: int = 1, scope: str | None = None) -> dict[str, Any]:
        return {
            "id": eid("event/" + name),
            "feed": feed,
            "seq": seq,
            "clock": {"ms": T0 + ms, "n": n},
            "scope": chat if scope is None else scope,
            "key": "ck:" + name,
            "group": group,
            "epoch": epoch,
        }

    def given(group: str = "phi", keys_: list[Any] | None = None, **change: Any) -> dict[str, Any]:
        base = {
            "chat": chat,
            "group": group,
            "chat_create": {"key": "ck:create", "group": "personal", "epoch": p0},
            "keys": keys_ or [],
            "current": {"personal": p0, "phi": h0},
            "local": None,
        }
        return {**base, **change}

    return [
        ("chat_first_phi_write", given()),
        ("chat_phi_key_current", given(keys_=[key("phi-current", "phi", h0)])),
        ("chat_phi_epoch_changed", given(keys_=[key("phi-old", "phi", h0)], current={"personal": p0, "phi": h1})),
        ("chat_personal_create_key", given("personal")),
        ("chat_personal_epoch_changed", given("personal", current={"personal": p1, "phi": h0})),
        (
            "chat_personal_rotated_key_used",
            given("personal", keys_=[key("personal-rotated", "personal", p1)], current={"personal": p1, "phi": h0}),
        ),
        (
            "chat_personal_without_key",
            given("personal", chat_create={"key": "ck:create", "group": "phi", "epoch": h0}),
        ),
        ("chat_concurrent_by_clock", given(keys_=[key("later-ms", "phi", h0, ms=20, feed=fa), key("earlier-ms", "phi", h0, ms=10, feed=fb)])),
        ("chat_concurrent_same_ms_by_counter", given(keys_=[key("later-n", "phi", h0, n=1, feed=fa), key("earlier-n", "phi", h0, n=0, feed=fb)])),
        ("chat_concurrent_same_clock_by_feed", given(keys_=[key("feed-b", "phi", h0, feed=fb), key("feed-a", "phi", h0, feed=fa)])),
        ("chat_same_feed_by_seq", given(keys_=[key("seq-2", "phi", h0, seq=2), key("seq-1", "phi", h0, seq=1)])),
        (
            "chat_latest_key_under_old_epoch_rotates",
            given(
                keys_=[key("current-epoch", "phi", h1, ms=10), key("old-epoch-later", "phi", h0, ms=20, feed=fb)],
                current={"personal": p0, "phi": h1},
            ),
        ),
        (
            "chat_other_scope_ignored",
            given(keys_=[key("other-chat", "phi", h0, scope=other), key("mesh", "phi", h0, scope="mesh"), key("new", "phi", h0, scope="new")]),
        ),
        ("chat_other_group_ignored", given(keys_=[key("personal-only", "personal", p0)])),
        ("chat_create_key_of_other_group_ignored", given(chat_create={"key": "ck:create", "group": "personal", "epoch": p0})),
        ("chat_phi_without_epoch", given(current={"personal": p0, "phi": None})),
        ("chat_phi_device_excluded", given(local="phi.device_excluded")),
        ("chat_phi_device_not_ready", given(local="phi.group_not_ready")),
        ("chat_personal_ignores_phi_state", given("personal", local="phi.device_excluded")),
    ]


def generate_destinations() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, given: dict[str, Any]) -> None:
        case = {"name": name, "kind": kind, "input": given}
        case["expect"] = phi.evaluate(case)
        cases.append(case)

    for name, value in endpoint_cases():
        add(name, "phi_endpoint", {"value": value})
    for name, subject in subject_cases():
        add(name, "phi_subject", {"subject": subject})
    for name, mesh_id in (
        ("contact_round_trip", CONTACT),
        ("contact_uppercase_hex", "sha256:" + CONTACT[7:].upper()),
        ("contact_short", CONTACT[:-2]),
        ("contact_without_prefix", CONTACT[7:]),
        ("contact_not_text", None),
        ("contact_own_form", "contact:" + CONTACT),
    ):
        add(name, "phi_contact", {"mesh_id": mesh_id})
    for name, source in proposal_cases():
        add(name, "phi_propose", {"source": source, "device": ALICE.id})
    for name, source, device in approval_cases():
        add(name, "phi_approve", {"source": source, "device": device.id, "device_kind": device.kind})
    for name, kind, subject in (
        ("statement_device", "device", BOB.id),
        ("statement_endpoint", "endpoint", M),
        ("statement_contact", "endpoint", "contact:" + CONTACT),
    ):
        add(
            name,
            "phi_statement",
            {
                "approver": "alice",
                "device": ALICE.id,
                "approver_scalar_hex": f"{approver_scalar(ALICE):064x}",
                "subject_kind": kind,
                "subject": subject,
                "issued_s": T0 // 1000,
            },
        )
    for build in (
        seq_create_and_pair,
        seq_adopt_group,
        seq_removal_by_deployed_device,
        seq_removal_by_phi_member,
        seq_markers,
        seq_concurrent_revoke_and_grant,
        seq_destinations,
        seq_approver_removed,
        seq_legacy_authority,
    ):
        case = build()
        case["expect"] = phi.evaluate(case)
        cases.append(case)
    for case in fold_cases():
        case["expect"] = phi.evaluate(case)
        cases.append(case)
    for name, given in chat_cases():
        add(name, "phi_chat_key", given)
    return {
        "format": "astral.vectors/v1",
        "name": "phi-destination-v1",
        "spec": "106/contracts/phi-destinations.md",
        "constants": {
            "approval_lifetime_s": phi.APPROVAL_LIFETIME_S,
            "approver_prefix": phi.APPROVER_PREFIX,
            "contact_mesh": CONTACT,
            "facts": phi.FACTS,
            "group_states": list(phi.GROUP_STATES),
            "marker_labels": phi.MARKER_LABELS,
            "marker_prefix": phi.MARKER_PREFIX,
            "marker_value": phi.MARKER_VALUE,
            "now_s": NOW_S,
            "rotation_reasons": list(phi.ROTATION_REASONS),
            "statuses": list(phi.STATUSES),
        },
        "test_devices": {
            name: {
                "id": d.id,
                "label": d.name,
                "kind": d.kind,
                "feeds": [d.feed, BOB_FEED_2] if d is BOB else [d.feed],
                "presence_scalar_hex": f"{d.presence_scalar:064x}",
                "approver_scalar_hex": f"{approver_scalar(d):064x}",
                "approver_public": b64(approver_public(d)),
            }
            for name, d in DEVICES.items()
        },
        "cases": cases,
    }


def _turn(*context: tuple[str, str], private_access: bool = False) -> dict[str, Any]:
    return {
        "context": [{"confidentiality": c, "integrity": i} for c, i in context],
        "private_access": private_access,
        "secret_in_context": False,
    }


def _effect(**change: Any) -> dict[str, Any]:
    effect = {
        "argument_integrity": [],
        "endpoint_phi_approved": False,
        "kind": "outside_effect",
        "outside_service": True,
        "target": "remote_tool",
    }
    effect.update(change)
    return effect


def _gate(turn: dict[str, Any], effect: dict[str, Any], **change: Any) -> dict[str, Any]:
    state = {
        "allowance": "ok",
        "consent": False,
        "device_on_roster": True,
        "effect": effect,
        "hard_flag": False,
        "permission": True,
        "policy": "allow",
        "stopped": False,
        "tool_def_matches": True,
        "turn": turn,
    }
    state.update(change)
    return state


def generate_labels() -> dict[str, Any]:
    labels_v2 = importlib.import_module("labels_v2")
    mesh_error = importlib.import_module("astral_mesh_ref.errors").MeshError
    old = json.loads(V1_LABELS.read_text(encoding="utf-8"))
    cases = []
    for case in old["cases"]:
        case = copy.deepcopy(case)
        if case["kind"] == "labels_flow":
            case["input"]["effect"]["outside_service"] = False
        if case["kind"] == "labels_gate":
            case["input"]["state"]["effect"]["outside_service"] = False
        case["name"] = "v1-" + case["name"]
        cases.append(case)
    phi_owner = ("phi", "owner")
    untrusted = ("public", "untrusted")
    missing = _effect()
    del missing["outside_service"]
    flows = [
        ("flow-phi-to-an-unapproved-outside-service", _turn(phi_owner), _effect()),
        ("flow-phi-to-an-approved-outside-service", _turn(phi_owner), _effect(endpoint_phi_approved=True)),
        ("flow-phi-and-secret-to-an-unapproved-outside-service", _turn(phi_owner, ("secret", "owner")), _effect()),
        ("flow-secret-only-to-an-outside-service-keeps-its-v1-outcome", _turn(("secret", "owner")), _effect()),
        ("flow-private-to-an-unapproved-outside-service", _turn(("private", "owner")), _effect()),
        ("flow-phi-to-a-peer-tool", _turn(phi_owner), _effect(outside_service=False)),
        ("flow-phi-to-a-local-tool", _turn(phi_owner), _effect(outside_service=False, target="local_tool")),
        ("flow-phi-read-from-an-unapproved-outside-service", _turn(phi_owner), _effect(kind="outward_read")),
        ("flow-service-refusal-comes-before-consent", _turn(phi_owner, untrusted), _effect(argument_integrity=["untrusted"])),
        ("flow-approved-service-still-asks-for-consent", _turn(phi_owner, untrusted), _effect(endpoint_phi_approved=True)),
        ("flow-outside-service-on-a-model-target", _turn(phi_owner), _effect(target="model", kind="mesh_read")),
        ("flow-outside-service-on-a-local-tool", _turn(phi_owner), _effect(target="local_tool")),
        ("flow-outside-service-on-sync", _turn(phi_owner), _effect(target="sync", kind="mesh_write")),
        ("flow-outside-service-missing", _turn(phi_owner), missing),
        ("flow-outside-service-not-a-boolean", _turn(phi_owner), _effect(outside_service=1)),
        (
            "flow-phi-to-model-is-unchanged-for-services",
            _turn(phi_owner),
            _effect(target="model", kind="mesh_read", outside_service=False),
        ),
    ]
    for name, turn, effect in flows:
        case = {"name": name, "kind": "labels_flow", "input": {"turn": turn, "effect": effect}}
        try:
            case["expect"] = {"ok": labels_v2.flow_rules(turn, effect)}
        except mesh_error as exc:
            case["expect"] = {"error": exc.code}
        cases.append(case)
    gates = [
        ("gate-5-phi-to-service", _gate(_turn(phi_owner), _effect())),
        ("gate-5-approved-service-dispatches", _gate(_turn(phi_owner), _effect(endpoint_phi_approved=True))),
        ("order-policy-before-service-flow", _gate(_turn(phi_owner), _effect(), policy="deny")),
    ]
    for name, state in gates:
        case = {"name": name, "kind": "labels_gate", "input": {"state": state}}
        case["expect"] = {"ok": labels_v2.gate_effect(state)}
        cases.append(case)
    return {
        "format": "astral.vectors/v1",
        "name": "labels-v2",
        "spec": "106/contracts/labels-v2.md",
        "cases": cases,
    }


def render() -> dict[str, str]:
    return {
        name: json.dumps(doc, indent=2, sort_keys=True) + "\n"
        for name, doc in (
            ("phi-destination-v1", generate_destinations()),
            ("labels-v2", generate_labels()),
        )
    }


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
