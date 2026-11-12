"""Generates the deterministic vector files under ../vectors from fixed seeds, fixed nonces and fixed clocks, and checks them with --check.
Every case is run through astral_mesh_ref.runner, and invalid cases state their expected error code by hand so the reference cannot mask its own bug.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import scenarios as sc
from astral_mesh_ref import (
    attestation,
    consent,
    crypto,
    event,
    keys,
    mailbox as mailbox_ref,
    pairing,
    policy,
    roster,
    runner,
    sync,
    ticket,
)
from astral_mesh_ref.cj import b64url_decode, b64url_encode, canonical
from astral_mesh_ref.testkit import DetRng, TestDevice, hlc, seed

mailbox_ref.register(runner.op)

HERE = Path(__file__).resolve().parent
VECTORS = HERE.parent / "vectors"
FORMAT = "astral.vectors/v1"


class Suite:
    def __init__(self, name: str, spec: str, **extra: Any) -> None:
        self.name = name
        self.doc: dict[str, Any] = {
            "cases": [],
            "format": FORMAT,
            "name": name,
            "spec": spec,
            **extra,
        }

    def add(
        self,
        name: str,
        kind: str,
        case_input: dict[str, Any],
        *,
        error: str | None = None,
        expect: Any = None,
    ) -> dict[str, Any]:
        result = runner.run(kind, case_input)
        if error is not None:
            assert result == {"error": error}, (
                f"{self.name}/{name}: wanted error {error}, got {result}"
            )
            stored: dict[str, Any] = {"error": error}
        else:
            assert "ok" in result, f"{self.name}/{name}: unexpected {result}"
            if expect is not None:
                assert result["ok"] == expect, (
                    f"{self.name}/{name}: wanted {expect}, got {result['ok']}"
                )
            stored = {"ok": result["ok"]}
        assert all(c["name"] != name for c in self.doc["cases"]), (
            f"duplicate case {name}"
        )
        self.doc["cases"].append(
            {"expect": stored, "input": case_input, "kind": kind, "name": name}
        )
        return stored.get("ok", stored)

    def text(self) -> str:
        return json.dumps(self.doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


ALICE = TestDevice("alice", "desktop")
BOB = TestDevice("bob", "pocket")
CAROL = TestDevice("carol", "desktop")
DAVE = TestDevice("dave", "satellite")
NODE = TestDevice("node1", "node")
MALLORY = TestDevice("mallory", "pocket")


def secrets_block() -> dict[str, Any]:
    return {
        d.name: {"kind": d.kind, **d.secrets()}
        for d in (ALICE, BOB, CAROL, DAVE, NODE, MALLORY)
    }


def build_canonical() -> Suite:
    s = Suite("canonical-json", "contracts/event.md#canonical-json")
    nested = '{"z":[3,2,{"b":null,"a":true}],"a":{"y":-9223372036854775808,"x":9223372036854775807}}'
    s.add(
        "sorted-keys-no-whitespace",
        "canonical",
        {"json": '{"b": 2, "a": 1}'},
        expect={
            "canonical": '{"a":1,"b":2}',
            "sha256": crypto.sha256_id(b'{"a":1,"b":2}')[7:],
        },
    )
    s.add("nested-and-int64-bounds", "canonical", {"json": nested})
    s.add(
        "unicode-scalar-order",
        "canonical",
        {
            "json": json.dumps(
                {"\U0001f600": "astral", "": "private", "é": "composed", "a": 1},
                ensure_ascii=True,
            )
        },
    )
    s.add("no-normalization", "canonical", {"json": '{"é": 1, "é": 2}'})
    s.add(
        "control-characters-escaped",
        "canonical",
        {"json": '{"c": "\\u0000\\n\\t\\u001f"}'},
    )
    s.add(
        "duplicate-key",
        "canonical",
        {"json": '{"a":1,"a":2}'},
        error="canonical.duplicate_key",
    )
    s.add(
        "nested-duplicate-key",
        "canonical",
        {"json": '{"n":{"k":1,"k":2}}'},
        error="canonical.duplicate_key",
    )
    s.add("float-refused", "canonical", {"json": '{"v":1.5}'}, error="canonical.float")
    s.add(
        "exponent-refused", "canonical", {"json": '{"v":1e3}'}, error="canonical.float"
    )
    s.add(
        "nan-refused", "canonical", {"json": '{"v":NaN}'}, error="canonical.non_finite"
    )
    s.add(
        "int-over-int64",
        "canonical",
        {"json": '{"v":9223372036854775808}'},
        error="canonical.range",
    )
    s.add(
        "int-under-int64",
        "canonical",
        {"json": '{"v":-9223372036854775809}'},
        error="canonical.range",
    )
    s.add(
        "lone-surrogate",
        "canonical",
        {"json": '{"v":"\\ud800"}'},
        error="canonical.surrogate",
    )
    s.add("not-json", "canonical", {"json": "{"}, error="canonical.parse")
    s.add(
        "base64url-valid",
        "b64url",
        {"text": "c2lnbmVkIGJ5dGVz"},
        expect={"hex": "7369676e6564206279746573"},
    )
    for name, text in (
        ("padding", "c2lnbmVkIGJ5dGVz="),
        ("bad-character", "c2lnbmVkIGJ5dGVz!"),
        ("non-ascii", "é"),
        ("trailing-bits", "A"),
        ("non-canonical-tail", "QR"),
    ):
        s.add("base64url-" + name, "b64url", {"text": text}, error="canonical.base64")
    return s


def build_crypto() -> Suite:
    s = Suite(
        "crypto",
        "contracts/event.md#signatures-and-ids",
        devices=secrets_block(),
        contexts=list(crypto.CONTEXTS),
    )
    for d in (ALICE, BOB, NODE):
        s.add("keypair-" + d.name, "keypair", d.secrets())
    obj = {"a": 1, "text": "café", "list": [1, 2, 3]}
    for ctx in crypto.CONTEXTS:
        s.add(
            "ed25519-sign-" + ctx.split(".")[1].split("/")[0],
            "sign",
            {
                "alg": "ed25519",
                "context": ctx,
                "object": obj,
                "secret_hex": ALICE.ops_seed.hex(),
            },
        )
    sig_ed = crypto.sign_detached("ed25519", ALICE.ops_seed, "astral.event/v1", obj)
    sig_p = crypto.sign_detached("p256", ALICE.identity_scalar, "astral.roster/v1", obj)
    pub_ed, pub_p = (
        b64url_encode(ALICE.ops_public),
        b64url_encode(ALICE.identity_public),
    )
    s.add(
        "p256-sign",
        "sign",
        {
            "alg": "p256",
            "context": "astral.roster/v1",
            "object": obj,
            "secret_hex": f"{ALICE.identity_scalar:064x}",
        },
        expect={"sig": sig_p},
    )
    s.add(
        "ed25519-verify",
        "verify",
        {
            "alg": "ed25519",
            "context": "astral.event/v1",
            "object": obj,
            "public": pub_ed,
            "sig": sig_ed,
        },
        expect={"valid": True},
    )
    s.add(
        "p256-verify",
        "verify",
        {
            "alg": "p256",
            "context": "astral.roster/v1",
            "object": obj,
            "public": pub_p,
            "sig": sig_p,
        },
        expect={"valid": True},
    )
    s.add(
        "wrong-context",
        "verify",
        {
            "alg": "ed25519",
            "context": "astral.roster/v1",
            "object": obj,
            "public": pub_ed,
            "sig": sig_ed,
        },
        expect={"valid": False},
    )
    s.add(
        "wrong-context-p256",
        "verify",
        {
            "alg": "p256",
            "context": "astral.checkpoint/v1",
            "object": obj,
            "public": pub_p,
            "sig": sig_p,
        },
        expect={"valid": False},
    )
    s.add(
        "edited-object",
        "verify",
        {
            "alg": "ed25519",
            "context": "astral.event/v1",
            "object": {**obj, "a": 2},
            "public": pub_ed,
            "sig": sig_ed,
        },
        expect={"valid": False},
    )
    s.add(
        "wrong-key",
        "verify",
        {
            "alg": "ed25519",
            "context": "astral.event/v1",
            "object": obj,
            "public": b64url_encode(BOB.ops_public),
            "sig": sig_ed,
        },
        expect={"valid": False},
    )
    raw = b64url_decode(sig_p)
    r, sv = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    high = b64url_encode(
        r.to_bytes(32, "big") + (crypto.P256_ORDER - sv).to_bytes(32, "big")
    )
    s.add(
        "p256-high-s-refused",
        "verify",
        {
            "alg": "p256",
            "context": "astral.roster/v1",
            "object": obj,
            "public": pub_p,
            "sig": high,
        },
        expect={"valid": False},
    )
    s.add(
        "p256-short-signature",
        "verify",
        {
            "alg": "p256",
            "context": "astral.roster/v1",
            "object": obj,
            "public": pub_p,
            "sig": b64url_encode(raw[:63]),
        },
        expect={"valid": False},
    )
    s.add(
        "signature-field-excluded",
        "verify",
        {
            "alg": "ed25519",
            "context": "astral.event/v1",
            "object": obj,
            "public": pub_ed,
            "sig": sig_ed,
        },
        expect={"valid": True},
    )
    key, nonce = seed("aead/key"), DetRng("aead").bytes(24)
    aead = {
        "aad_hex": b"aad".hex(),
        "key_hex": key.hex(),
        "nonce_hex": nonce.hex(),
        "plaintext_hex": b"astral".hex(),
    }
    sealed = s.add("xchacha20poly1305-seal", "aead_seal", aead)["ct_hex"]
    s.add(
        "xchacha20poly1305-open",
        "aead_open",
        {
            "aad_hex": aead["aad_hex"],
            "ct_hex": sealed,
            "key_hex": aead["key_hex"],
            "nonce_hex": aead["nonce_hex"],
        },
        expect={"plaintext_hex": aead["plaintext_hex"]},
    )
    flipped = sealed[:-2] + ("00" if sealed[-2:] != "00" else "01")
    s.add(
        "xchacha20poly1305-tampered",
        "aead_open",
        {
            "aad_hex": aead["aad_hex"],
            "ct_hex": flipped,
            "key_hex": aead["key_hex"],
            "nonce_hex": aead["nonce_hex"],
        },
        error="aead.decrypt_failed",
    )
    s.add(
        "xchacha20poly1305-wrong-aad",
        "aead_open",
        {
            "aad_hex": b"other".hex(),
            "ct_hex": sealed,
            "key_hex": aead["key_hex"],
            "nonce_hex": aead["nonce_hex"],
        },
        error="aead.decrypt_failed",
    )
    s.add(
        "hkdf-sha256",
        "hkdf",
        {
            "ikm_hex": seed("hkdf").hex(),
            "info": "astral.wrap/v1",
            "length": 32,
            "salt_hex": seed("salt").hex(),
        },
    )
    s.add(
        "hkdf-sha256-no-salt",
        "hkdf",
        {
            "ikm_hex": seed("hkdf").hex(),
            "info": "astral.recovery.sign/v1",
            "length": 32,
            "salt_hex": None,
        },
    )
    return s


def _keys_for(rng: DetRng) -> tuple[bytes, bytes]:
    return rng.bytes(32), rng.bytes(24)


def build_event() -> Suite:
    s = Suite("event", "contracts/event.md", devices=secrets_block())
    rng = DetRng("event")
    epoch_id = crypto.sha256_id(b"personal-epoch-0")
    epoch_key = seed("event/epoch")
    chat_key_raw = rng.bytes(16)
    ck = event.new_content_key_ref(chat_key_raw)
    content = seed("event/content")
    phi_epoch = crypto.sha256_id(b"phi-epoch-0")
    phi_key = seed("event/phi-epoch")
    phi_ref = event.epoch_key_ref("phi", phi_epoch)

    def seal(
        name: str,
        seq: int,
        prev: str | None,
        after: list[str],
        scope: str,
        type_: str,
        labels_: dict[str, str],
        key: str,
        content_key: bytes,
        body: Any,
        ms: int,
        device: TestDevice = ALICE,
    ) -> dict[str, Any]:
        out = s.add(
            name,
            "seal_event",
            {
                "after": after,
                "body": body,
                "body_nonce_hex": rng.bytes(24).hex(),
                "clock": hlc(ms),
                "content_key_hex": content_key.hex(),
                "epoch_id": epoch_id,
                "epoch_key_hex": epoch_key.hex(),
                "header_nonce_hex": rng.bytes(24).hex(),
                "key": key,
                "labels": labels_,
                "ops_seed_hex": device.ops_seed.hex(),
                "prev": prev,
                "scope": scope,
                "seq": seq,
                "type": type_,
            },
        )
        return out

    own = {"confidentiality": "private", "integrity": "owner"}
    key_event = seal(
        "key-content-under-personal-epoch",
        1,
        None,
        [],
        "mesh",
        "key.content",
        own,
        event.epoch_key_ref("personal", epoch_id),
        epoch_key,
        {"id": ck, "key": b64url_encode(content), "scope": "new"},
        sc.BASE_MS + 1000,
    )
    create = seal(
        "chat-create-scope-new",
        2,
        key_event["event_id"],
        [],
        "new",
        "chat.create",
        own,
        ck,
        content,
        {"title": "First chat"},
        sc.BASE_MS + 2000,
    )
    chat_id = create["event_id"]
    msg = seal(
        "user-message-in-chat",
        3,
        create["event_id"],
        [],
        chat_id,
        "chat.user_message",
        own,
        ck,
        content,
        {"base": None, "text": "hello"},
        sc.BASE_MS + 3000,
    )
    seal(
        "tool-result-untrusted-after-other-feed",
        1,
        None,
        [msg["event_id"]],
        chat_id,
        "chat.tool_result",
        {"confidentiality": "private", "integrity": "untrusted"},
        ck,
        content,
        {"ok": True},
        sc.BASE_MS + 4000,
        device=BOB,
    )
    phi = seal(
        "phi-event-under-phi-key",
        4,
        msg["event_id"],
        [],
        chat_id,
        "chat.assistant_message",
        {"confidentiality": "phi", "integrity": "tool-trusted"},
        phi_ref,
        phi_key,
        {"text": "result"},
        sc.BASE_MS + 5000,
    )
    spoken = seal(
        "spoken-event-in-chat",
        5,
        phi["event_id"],
        [],
        chat_id,
        "chat.spoken",
        own,
        ck,
        content,
        {"kind": "answer", "text": "Here is what you heard."},
        sc.BASE_MS + 6000,
    )
    keysets = {
        "content_keys": {
            ck: content.hex(),
            event.epoch_key_ref("personal", epoch_id): epoch_key.hex(),
            phi_ref: phi_key.hex(),
        },
        "epoch_keys": {epoch_id: epoch_key.hex()},
    }
    for name, item in (
        ("open-key-content", key_event),
        ("open-chat-create", create),
        ("open-user-message", msg),
        ("open-phi-event", phi),
        ("open-spoken-event", spoken),
    ):
        s.add(
            name,
            "open_event",
            {"envelope": item["envelope"], "now_ms": sc.BASE_MS + 10_000, **keysets},
        )
    s.add(
        "open-withheld-body",
        "open_event",
        {
            "envelope": {**phi["envelope"], "body_ct": None},
            "now_ms": sc.BASE_MS + 10_000,
            **keysets,
        },
        expect={"body": None, "event_id": phi["event_id"], "header": phi["header"]},
    )
    s.add(
        "open-without-content-key",
        "open_event",
        {
            "envelope": phi["envelope"],
            "now_ms": None,
            "epoch_keys": keysets["epoch_keys"],
            "content_keys": {},
        },
        error="event.key_unknown",
    )
    s.add(
        "unknown-epoch",
        "open_event",
        {
            "envelope": msg["envelope"],
            "now_ms": None,
            "epoch_keys": {},
            "content_keys": keysets["content_keys"],
        },
        error="event.epoch_unknown",
    )
    env = dict(msg["envelope"])
    raw = bytearray(b64url_decode(env["body_ct"]))
    raw[-1] ^= 1
    s.add(
        "edited-body",
        "open_event",
        {
            "envelope": {**env, "body_ct": b64url_encode(bytes(raw))},
            "now_ms": None,
            **keysets,
        },
        error="event.body_mismatch",
    )
    hraw = bytearray(b64url_decode(env["header_ct"]))
    hraw[-1] ^= 1
    s.add(
        "edited-header-ciphertext",
        "open_event",
        {
            "envelope": {**env, "header_ct": b64url_encode(bytes(hraw))},
            "now_ms": None,
            **keysets,
        },
        error="event.decrypt_failed",
    )
    s.add(
        "envelope-seq-differs",
        "open_event",
        {"envelope": {**env, "seq": 99}, "now_ms": None, **keysets},
        error="event.decrypt_failed",
    )
    s.add(
        "future-clock-over-five-minutes",
        "open_event",
        {"envelope": msg["envelope"], "now_ms": sc.BASE_MS + 3000 - 300_001, **keysets},
        error="event.future_clock",
    )
    s.add(
        "future-clock-at-five-minutes",
        "open_event",
        {"envelope": msg["envelope"], "now_ms": sc.BASE_MS + 3000 - 300_000, **keysets},
    )
    h = dict(msg["header"])
    bad_sig = {**h, "sig": ("A" if h["sig"][0] != "A" else "B") + h["sig"][1:]}
    s.add(
        "bad-header-signature",
        "open_event",
        {
            "envelope": sc.sealed_craft(
                bad_sig, epoch_id, epoch_key, nonce_tag="bad-sig"
            ),
            "now_ms": None,
            **keysets,
        },
        error="event.bad_signature",
    )
    wrong_ctx = {k: v for k, v in h.items() if k != "sig"}
    wrong_ctx["sig"] = crypto.sign_object(
        "ed25519", ALICE.ops_seed, "astral.roster/v1", wrong_ctx
    )
    s.add(
        "signature-with-wrong-context",
        "open_event",
        {
            "envelope": sc.sealed_craft(
                wrong_ctx, epoch_id, epoch_key, nonce_tag="wrong-ctx"
            ),
            "now_ms": None,
            **keysets,
        },
        error="event.bad_signature",
    )

    def resign(**ch: Any) -> dict[str, Any]:
        return {**{k: v for k, v in h.items() if k != "sig"}, **ch}

    def signed(obj: dict[str, Any]) -> dict[str, Any]:
        obj = dict(obj)
        obj["sig"] = crypto.sign_object(
            "ed25519", ALICE.ops_seed, "astral.event/v1", obj
        )
        return obj

    cases = {
        "unknown-type": resign(type="chat.shout"),
        "extra-field": {**resign(), "extra": 1},
        "seq-zero": resign(seq=0),
        "seq-one-with-prev": resign(seq=1),
        "after-not-sorted": resign(
            after=sorted([crypto.sha256_id(b"x"), crypto.sha256_id(b"y")], reverse=True)
        ),
        "chat-scope-mesh": resign(scope="mesh"),
        "bad-label-value": resign(
            labels={"confidentiality": "classified", "integrity": "owner"}
        ),
        "bad-key-ref": resign(key="ck:short"),
        "clock-negative": resign(clock={"ms": -1, "n": 0}),
        "float-clock": resign(clock={"ms": 1, "n": 0, "x": 1}),
    }
    for name, hdr in cases.items():
        s.add(
            "malformed-" + name,
            "open_event",
            {
                "envelope": sc.sealed_craft(
                    signed(hdr) if name != "extra-field" else hdr,
                    epoch_id,
                    epoch_key,
                    nonce_tag="m-" + name,
                ),
                "now_ms": None,
                **keysets,
            },
            error="event.unknown_type" if name == "unknown-type" else "event.malformed",
        )
    create_wrong_scope = signed(
        {**{k: v for k, v in create["header"].items() if k != "sig"}, "scope": chat_id}
    )
    s.add(
        "chat-create-needs-scope-new",
        "open_event",
        {
            "envelope": sc.sealed_craft(
                create_wrong_scope, epoch_id, epoch_key, nonce_tag="create-scope"
            ),
            "now_ms": None,
            **keysets,
        },
        error="event.malformed",
    )
    mismatch = sc.sealed_craft(
        msg["header"],
        epoch_id,
        epoch_key,
        envelope_seq=msg["header"]["seq"],
        nonce_tag="mismatch",
    )
    other_feed = {**mismatch, "feed": b64url_encode(BOB.ops_public)}
    s.add(
        "envelope-feed-differs",
        "open_event",
        {"envelope": other_feed, "now_ms": None, **keysets},
        error="event.decrypt_failed",
    )
    return s


def build_feed() -> Suite:
    s = Suite("feed", "contracts/event.md#feeds-and-chains", devices=secrets_block())
    a = sc.chain(ALICE, 3)
    b = sc.chain(BOB, 2, start_ms=sc.BASE_MS + 10_000)
    member = {"state": "member"}
    statuses = {ALICE.feed: member, BOB.feed: member}
    ids_a = [event.event_id(h) for h in a]
    now = sc.BASE_MS + 60_000

    def case(
        name: str,
        steps: list[dict[str, Any]],
        results: list[str],
        st: dict[str, Any] | None = None,
        now_ms: int | None = now,
        **extra: Any,
    ) -> dict[str, Any]:
        out = s.add(
            name,
            "feed_replay",
            {
                "now_ms": now_ms,
                "statuses": statuses if st is None else st,
                "steps": [{"header": h} for h in steps],
            },
        )
        assert out["results"] == results, (name, out["results"])
        return out

    b_after = sc.header(BOB, 1, None, sc.BASE_MS + 20_000, after=[ids_a[1]])
    case("valid-chain-two-feeds", [a[0], a[1], b_after, a[2]], ["accepted"] * 4)
    case(
        "duplicate-is-idempotent",
        [a[0], a[0], a[1]],
        ["accepted", "duplicate", "accepted"],
    )
    case(
        "gap-then-fill",
        [a[0], a[2], a[1], a[2]],
        ["accepted", "feed.gap", "accepted", "accepted"],
    )
    forked = sc.header(ALICE, 2, ids_a[0], sc.BASE_MS + 2000, body_tag="other")
    out = case(
        "fork-two-events-at-one-seq",
        [a[0], a[1], forked, a[2]],
        ["accepted", "accepted", "feed.fork", "feed.forked"],
    )
    assert out["forked"] == [ALICE.feed]
    case(
        "fork-then-feed-refused-until-reenrolled",
        [a[0], a[1], forked, b[0]],
        ["accepted", "accepted", "feed.fork", "accepted"],
    )
    edited = {**a[1], "clock": hlc(a[1]["clock"]["ms"] + 1)}
    case("edited-header", [a[0], edited], ["accepted", "event.bad_signature"])
    ctx = {k: v for k, v in a[1].items() if k != "sig"}
    ctx["sig"] = crypto.sign_object("ed25519", ALICE.ops_seed, "astral.roster/v1", ctx)
    case("wrong-signature-context", [a[0], ctx], ["accepted", "event.bad_signature"])
    case(
        "future-clock",
        [sc.header(ALICE, 1, None, now + 300_001)],
        ["event.future_clock"],
    )
    case(
        "clock-at-skew-limit", [sc.header(ALICE, 1, None, now + 300_000)], ["accepted"]
    )
    mismatch = sc.header(ALICE, 3, crypto.sha256_id(b"not the head"), sc.BASE_MS + 3000)
    case(
        "prev-does-not-match-head",
        [a[0], a[1], mismatch],
        ["accepted", "accepted", "feed.fork"],
    )
    regress = sc.header(ALICE, 2, ids_a[0], sc.BASE_MS + 1000)
    case(
        "clock-must-rise-within-a-feed",
        [a[0], regress],
        ["accepted", "feed.clock_regress"],
    )
    case(
        "removed-device-beyond-cutoff",
        [a[0], a[1], a[2]],
        ["accepted", "accepted", "feed.removed"],
        st={ALICE.feed: {"cutoff": 2, "state": "removed"}},
    )
    case("unknown-feed", [a[0]], ["feed.unknown"], st={})
    early = sc.header(BOB, 1, None, sc.BASE_MS + 1500, after=[ids_a[1]])
    case(
        "after-event-must-precede",
        [a[0], a[1], early],
        ["accepted", "accepted", "feed.after_clock"],
    )
    case("after-unknown-event-is-held", [b_after], ["accepted"])
    return s


INST_SCALAR = (
    int.from_bytes(seed("institution/uky"), "big") % (crypto.P256_ORDER - 1) + 1
)
INST_PUBLIC = crypto.p256_public(INST_SCALAR)
INST_KEY = b64url_encode(INST_PUBLIC)
NEW_ISSUER = sc.issuer_record("https://auth.new.example.test/realms/astral")
DAY = 86400


def build_roster() -> Suite:
    s = Suite(
        "roster",
        "contracts/roster.md",
        devices=secrets_block(),
        issuer_secrets={"es256_scalar_hex": f"{sc.scalar('issuer/es256'):064x}"},
    )
    names: dict[str, str] = {}

    def R(name: str, m: sc.Mesh, **want: Any) -> dict[str, Any]:
        entries = list(m.entries) + want.pop("extra", [])
        mesh = want.pop("mesh", m.mesh)
        out = s.add(
            name,
            "roster_fold",
            {"entries": entries, "mesh": mesh, "now_s": want.pop("now_s", sc.BASE_S)},
        )
        for key, wanted in want.items():
            actual = out[key]
            if key in (
                "members",
                "void",
                "unattested",
                "phi_members",
                "issuers",
                "stale_attestation",
            ):
                assert sorted(actual) == sorted(wanted), (
                    name,
                    key,
                    sorted(actual),
                    sorted(wanted),
                )
            elif key == "removed":
                assert sorted(actual) == sorted(wanted), (name, key)
            elif key == "rejected":
                assert actual == dict(sorted(wanted.items())), (
                    name,
                    key,
                    actual,
                    wanted,
                )
            else:
                assert actual == wanted, (name, key, actual, wanted)
        return out

    A, B, C, D, N = ALICE.id, BOB.id, CAROL.id, DAVE.id, NODE.id
    m = sc.Mesh("s1", ALICE)
    R(
        "genesis-only",
        m,
        members=[A],
        unattested=[A],
        stop=0,
        epochs={m.mesh: "personal"},
        current_epoch={"personal": m.mesh, "phi": None},
        issuers=[sc.ISSUER],
        rejected={},
        void=[],
    )
    at = m.attest(ALICE, [m.mesh])
    R(
        "attest-after-genesis",
        m,
        members=[A],
        unattested=[],
        principal={"attested": [[sc.ISSUER, "user-1001"]], "mesh": m.mesh},
    )
    addb = m.add(
        ALICE,
        BOB,
        [at],
        sc.identity_attestation(BOB, m.mesh, alg="RS256", sub="user-1001"),
    )
    wrapb = m.wrap_grant(ALICE, BOB, [addb])
    R(
        "add-device-with-rs256-attestation",
        m,
        members=[A, B],
        unattested=[],
        rejected={},
    )
    assert wrapb
    m = sc.Mesh("s1b", ALICE)
    gb = m.mesh
    add_un = m.add(ALICE, CAROL, [gb], None)
    R("add-device-unattested", m, members=[A, C], unattested=[A, C])
    assert add_un
    m = sc.Mesh("s1c", ALICE)
    m.attest(ALICE, [m.mesh])
    R(
        "attestation-older-than-thirty-days-is-stale",
        m,
        now_s=sc.BASE_S + 31 * DAY,
        stale_attestation=[A],
        unattested=[],
    )
    m = sc.Mesh("s1d", ALICE)
    m.put(
        m.entry(
            "attest",
            ALICE,
            [m.mesh],
            {"attestation": sc.identity_attestation(ALICE, m.mesh, nonce="0" * 64)},
        )
    )
    R(
        "attestation-with-wrong-nonce-rejected",
        m,
        unattested=[A],
        rejected={roster.entry_id(m.entries[-1]): "attestation.wrong_nonce"},
    )

    for label, case_name, token, code in [
        ("s1e", "attestation-with-expired-token-rejected", {"iat": sc.BASE_S - 1000, "exp": sc.BASE_S - 400}, "attestation.token_expired"),
        ("s1f", "attestation-with-token-issued-after-the-entry-rejected", {"iat": sc.BASE_S + 400}, "attestation.token_not_yet_valid"),
        ("s1g", "attestation-from-an-unlisted-client-rejected", {"azp": "evil-app"}, "attestation.wrong_audience"),
    ]:
        m = sc.Mesh(label, ALICE)
        m.put(
            m.entry(
                "attest",
                ALICE,
                [m.mesh],
                {"attestation": sc.identity_attestation(ALICE, m.mesh, **token)},
            )
        )
        R(
            case_name,
            m,
            unattested=[A],
            rejected={roster.entry_id(m.entries[-1]): code},
        )

    m = sc.Mesh("s2", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    rem = m.remove(ALICE, BOB, [ab], {BOB.feed: 3})
    later = m.put(
        m.entry(
            "add", BOB, [rem], {"attestation": None, "device": CAROL.record(m.mesh)}
        )
    )
    R(
        "removed-device-later-entry-rejected",
        m,
        members=[A],
        removed=[B],
        rejected={later: "roster.author_removed"},
        void=[],
    )

    m = sc.Mesh("s3", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    rem = m.remove(ALICE, BOB, [ab], {BOB.feed: 2})
    x = m.add(BOB, CAROL, [ab])
    R(
        "concurrent-add-by-removed-device-is-void",
        m,
        members=[A],
        removed=[B],
        void=[x],
        rejected={},
    )
    assert rem

    m = sc.Mesh("s4", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    dv = m.add(BOB, DAVE, [ab])
    m.remove(ALICE, BOB, [ab, dv], {BOB.feed: 2})
    R(
        "entry-written-before-removal-stays-valid",
        m,
        members=[A, D],
        removed=[B],
        void=[],
        rejected={},
    )

    m = sc.Mesh("s5", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    r1 = m.remove(ALICE, BOB, [ab], {BOB.feed: 0})
    r2 = m.remove(BOB, ALICE, [ab], {ALICE.feed: 0})
    R(
        "mutual-removal-leaves-only-the-recovery-key",
        m,
        members=[],
        removed=[A, B],
        void=[],
        rejected={},
    )
    m.add("recovery", CAROL, [r1, r2])
    R("recovery-key-restores-a-member", m, members=[C], removed=[A, B])

    m = sc.Mesh("s6", ALICE)
    m.add("recovery", BOB, [m.mesh])
    R("recovery-adds-device-without-presence", m, members=[A, B], rejected={})

    m = sc.Mesh("s7", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    s1 = m.stop(ALICE, [ab], 1)
    s2 = m.stop(BOB, [ab], 2)
    R("stop-counter-takes-the-maximum", m, stop=2, rejected={})
    s3 = m.stop(ALICE, [s1, s2], 2)
    R("stop-counter-must-rise", m, stop=2, rejected={s3: "roster.stop_not_rising"})

    m = sc.Mesh("s8", ALICE)
    g = m.mesh
    e1 = m.put(m.entry("issuer", ALICE, [g], {"action": "add", "issuer": NEW_ISSUER}))
    R("accepted-issuer-added", m, issuers=sorted([sc.ISSUER, NEW_ISSUER["iss"]]))
    e2 = m.put(m.entry("issuer", ALICE, [e1], {"action": "remove", "iss": sc.ISSUER}))
    e3 = m.put(
        m.entry("issuer", ALICE, [e1], {"action": "add", "issuer": sc.issuer_record()})
    )
    R("issuer-removal-beats-concurrent-add", m, issuers=[NEW_ISSUER["iss"]])
    m.put(
        m.entry("issuer", ALICE, [e2], {"action": "add", "issuer": sc.issuer_record()})
    )
    R(
        "issuer-added-again-after-removal",
        m,
        issuers=sorted([sc.ISSUER, NEW_ISSUER["iss"]]),
    )
    assert e3

    m = sc.Mesh("s9", ALICE)
    g = m.mesh
    an = m.add(ALICE, NODE, [g])
    au = m.put(
        m.entry(
            "phi_authority",
            ALICE,
            [an],
            {
                "action": "add",
                "institution": {"key": INST_KEY, "name": "UK HealthCare"},
            },
        )
    )
    approval = attestation.build_phi(
        INST_SCALAR, INST_PUBLIC, "device", N, sc.BASE_S, sc.BASE_S + 30 * DAY
    )
    gr = m.put(
        m.entry(
            "phi_approval", ALICE, [au], {"action": "grant", "attestation": approval}
        )
    )
    R("phi-approval-makes-a-phi-member", m, phi_members=[N], members=[A, N])
    phi_key = seed("epoch/phi/s9")
    ep = m.put(
        m.entry(
            "epoch",
            NODE,
            [gr],
            keys.make_epoch_body("phi", phi_key, {N: NODE.wrap_public}, None, m.rng),
        )
    )
    R(
        "phi-epoch-written-by-phi-member",
        m,
        current_epoch={"personal": m.mesh, "phi": ep},
        rejected={},
    )
    R(
        "expired-phi-approval-drops-membership",
        m,
        now_s=sc.BASE_S + 40 * DAY,
        phi_members=[],
        rejected={ep: "roster.epoch_wrong_members"},
    )
    rv = m.put(m.entry("phi_approval", ALICE, [gr], {"action": "revoke", "subject": N}))
    g2 = m.put(
        m.entry(
            "phi_approval",
            ALICE,
            [gr],
            {
                "action": "grant",
                "attestation": attestation.build_phi(
                    INST_SCALAR,
                    INST_PUBLIC,
                    "device",
                    N,
                    sc.BASE_S + 1,
                    sc.BASE_S + 31 * DAY,
                ),
            },
        )
    )
    R("phi-revoke-beats-concurrent-grant", m, phi_members=[])
    m.put(
        m.entry(
            "phi_approval",
            ALICE,
            [rv],
            {
                "action": "grant",
                "attestation": attestation.build_phi(
                    INST_SCALAR,
                    INST_PUBLIC,
                    "device",
                    N,
                    sc.BASE_S + 2,
                    sc.BASE_S + 32 * DAY,
                ),
            },
        )
    )
    R("phi-grant-after-revoke-restores-membership", m, phi_members=[N])
    assert g2
    m2 = sc.Mesh("s9b", ALICE)
    an2 = m2.add(ALICE, NODE, [m2.mesh])
    bad = m2.put(
        m2.entry(
            "phi_approval", ALICE, [an2], {"action": "grant", "attestation": approval}
        )
    )
    R(
        "phi-approval-from-unknown-institution-rejected",
        m2,
        phi_members=[],
        rejected={bad: "attestation.unknown_authority"},
    )
    m3 = sc.Mesh("s9c", ALICE)
    m3.add(ALICE, NODE, [m3.mesh])
    rp = m3.put(
        m3.entry(
            "epoch",
            "recovery",
            [roster.entry_id(m3.entries[-1])],
            keys.make_epoch_body("phi", phi_key, {N: NODE.wrap_public}, None, m3.rng),
        )
    )
    R(
        "recovery-cannot-write-a-phi-epoch",
        m3,
        rejected={rp: "roster.recovery_not_allowed"},
    )

    m = sc.Mesh("s10", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    ep1 = m.epoch(ALICE, [ab], [ALICE, BOB], "a")
    ep2 = m.epoch(BOB, [ab], [ALICE, BOB], "b")
    R(
        "concurrent-epochs-both-valid-newest-clock-writes",
        m,
        epochs={g: "personal", ep1: "personal", ep2: "personal"},
        current_epoch={"personal": ep2, "phi": None},
    )
    ep3 = m.epoch(ALICE, [ep1, ep2], [ALICE, BOB], "c")
    R(
        "later-epoch-following-both-supersedes",
        m,
        current_epoch={"personal": ep3, "phi": None},
    )
    m = sc.Mesh("s10b", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    rem = m.remove(ALICE, BOB, [ab], {BOB.feed: 0})
    bad = m.epoch(ALICE, [rem], [BOB], "after-removal")
    R(
        "epoch-wrapped-to-a-removed-device-rejected",
        m,
        rejected={bad: "roster.epoch_wrong_members"},
    )
    m = sc.Mesh("s10c", ALICE)
    wrong = m.put(
        m.entry(
            "wrap",
            ALICE,
            [m.mesh],
            {
                "device": A,
                "grants": keys.make_wrap_grants(
                    "personal",
                    A,
                    ALICE.wrap_public,
                    {crypto.sha256_id(b"nope"): seed("x")},
                    m.rng,
                ),
                "group": "personal",
            },
        )
    )
    R("wrap-for-an-unknown-epoch-rejected", m, rejected={wrong: "roster.unknown_epoch"})

    m = sc.Mesh("s11", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    bob2 = TestDevice("bob", "pocket")
    bob2.ops_seed, bob2.wrap_private = seed("bob/ops2"), seed("bob/wrap2")
    rot = m.put(m.entry("rotate", BOB, [ab], {"device": bob2.record(m.mesh)}))
    out = R(
        "rotate-adds-a-second-feed-to-the-same-device", m, members=[A, B], rejected={}
    )
    assert (
        set(out["feeds"]) == {ALICE.feed, BOB.feed, bob2.feed}
        and out["feeds"][bob2.feed]["state"] == "member"
    )
    m.remove(ALICE, BOB, [rot], {BOB.feed: 2, bob2.feed: 1})
    out = R("removal-covers-every-feed-of-the-device", m, members=[A], removed=[B])
    assert out["feeds"][bob2.feed] == {"cutoff": 1, "device": B, "state": "removed"}
    m2 = sc.Mesh("s11b", ALICE)
    ab2 = m2.add(ALICE, BOB, [m2.mesh])
    m2.put(m2.entry("rotate", BOB, [ab2], {"device": bob2.record(m2.mesh)}))
    r2 = m2.remove(ALICE, BOB, [roster.entry_id(m2.entries[-1])], {BOB.feed: 2})
    R(
        "removal-with-missing-feed-cutoff-rejected",
        m2,
        rejected={r2: "roster.cutoff_incomplete"},
        members=[A, B],
    )

    m = sc.Mesh("n1", ALICE)
    g = m.mesh
    ab = m.add(ALICE, BOB, [g])
    tampered = dict(
        m.entries[-1], clock={"ms": m.entries[-1]["clock"]["ms"] + 1, "n": 0}
    )
    m.entries.pop()
    R(
        "tampered-entry-fails-signature",
        m,
        extra=[tampered],
        members=[A],
        rejected={roster.entry_id(tampered): "roster.bad_signature"},
    )
    m = sc.Mesh("n2", ALICE)
    other_presence = roster.build_entry(
        "add",
        m.mesh,
        [m.mesh],
        hlc(sc.BASE_MS + 5000),
        A,
        {"attestation": None, "device": BOB.record(m.mesh)},
        identity_scalar=ALICE.identity_scalar,
        presence_scalar=BOB.presence_scalar,
    )
    R(
        "presence-signature-from-wrong-key-rejected",
        m,
        extra=[other_presence],
        members=[A],
        rejected={roster.entry_id(other_presence): "roster.bad_presence_signature"},
    )
    foreign = roster.build_entry(
        "stop",
        crypto.sha256_id(b"another mesh"),
        [m.mesh],
        hlc(sc.BASE_MS + 6000),
        A,
        {"counter": 1},
        identity_scalar=ALICE.identity_scalar,
    )
    R(
        "entry-for-another-mesh-rejected",
        m,
        extra=[foreign],
        rejected={roster.entry_id(foreign): "roster.wrong_mesh"},
    )
    dangling = roster.build_entry(
        "stop",
        m.mesh,
        [crypto.sha256_id(b"unseen")],
        hlc(sc.BASE_MS + 7000),
        A,
        {"counter": 1},
        identity_scalar=ALICE.identity_scalar,
    )
    out = R(
        "entry-with-unseen-parent-is-pending",
        m,
        extra=[dangling],
        pending=[roster.entry_id(dangling)],
        stop=0,
        rejected={},
    )
    assert out["heads"] == [m.mesh]
    m = sc.Mesh("n3", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    rem = m.remove(ALICE, BOB, [ab], {BOB.feed: 0})
    readd = m.put(
        m.entry(
            "add", ALICE, [rem], {"attestation": None, "device": BOB.record(m.mesh)}
        )
    )
    R(
        "removed-device-cannot-be-added-again",
        m,
        members=[A],
        rejected={readd: "roster.device_removed"},
    )
    m = sc.Mesh("n4", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    dup = m.put(
        m.entry("add", ALICE, [ab], {"attestation": None, "device": BOB.record(m.mesh)})
    )
    R("member-cannot-be-added-twice", m, rejected={dup: "roster.already_member"})
    clone = TestDevice("mallory", "pocket")
    clone.ops_seed = BOB.ops_seed
    m = sc.Mesh("n5", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    reuse = m.put(
        m.entry(
            "add", ALICE, [ab], {"attestation": None, "device": clone.record(m.mesh)}
        )
    )
    R(
        "operations-key-cannot-belong-to-two-devices",
        m,
        rejected={reuse: "roster.duplicate_key"},
    )
    m = sc.Mesh("n6", ALICE)
    stranger = m.put(
        m.entry(
            "add", CAROL, [m.mesh], {"attestation": None, "device": DAVE.record(m.mesh)}
        )
    )
    R(
        "entry-by-a-non-member-rejected",
        m,
        members=[A],
        rejected={stranger: "roster.unauthorized_author"},
    )
    ghost = m.put(
        m.entry(
            "remove",
            ALICE,
            [m.mesh],
            {"cutoff": {MALLORY.feed: 0}, "device": MALLORY.id},
        )
    )
    R(
        "removing-an-unknown-device-rejected",
        m,
        rejected={
            stranger: "roster.unauthorized_author",
            ghost: "roster.unknown_device",
        },
    )
    m = sc.Mesh("n7", ALICE)
    forged = roster.build_entry(
        "add",
        m.mesh,
        [m.mesh],
        hlc(sc.BASE_MS + 9000),
        "recovery",
        {"attestation": None, "device": BOB.record(m.mesh)},
        recovery_seed=seed("not the recovery key"),
    )
    R(
        "recovery-entry-with-wrong-key-rejected",
        m,
        extra=[forged],
        members=[A],
        rejected={roster.entry_id(forged): "roster.bad_signature"},
    )
    second = sc.Mesh("n8x", BOB)
    R(
        "second-genesis-rejected",
        m,
        extra=[second.genesis],
        mesh=m.mesh,
        members=[A],
        rejected={second.mesh: "roster.extra_genesis"},
    )
    s.add(
        "two-geneses-without-a-mesh-id",
        "roster_fold",
        {"entries": [m.genesis, second.genesis], "mesh": None, "now_s": sc.BASE_S},
        error="roster.ambiguous_genesis",
    )
    s.add(
        "no-genesis",
        "roster_fold",
        {"entries": [], "mesh": None, "now_s": sc.BASE_S},
        error="roster.no_genesis",
    )

    full = sc.Mesh("shape", ALICE)
    add_entry = full.entry(
        "add",
        ALICE,
        [full.mesh],
        {"attestation": None, "device": BOB.record(full.mesh)},
    )
    s.add("shape-valid-add", "roster_shape", {"entry": add_entry})
    no_presence = {k: v for k, v in add_entry.items() if k != "presence_sig"}
    s.add(
        "shape-add-needs-presence-signature",
        "roster_shape",
        {"entry": no_presence},
        error="roster.malformed",
    )
    s.add(
        "shape-genesis-cannot-follow",
        "roster_shape",
        {"entry": {**full.genesis, "follows": [crypto.sha256_id(b"x")]}},
        error="roster.malformed",
    )
    s.add(
        "shape-unknown-type",
        "roster_shape",
        {"entry": {**add_entry, "type": "promote"}},
        error="roster.malformed",
    )
    s.add(
        "shape-follows-unsorted",
        "roster_shape",
        {
            "entry": {
                **add_entry,
                "follows": sorted(
                    [crypto.sha256_id(b"a"), crypto.sha256_id(b"b")], reverse=True
                ),
            }
        },
        error="roster.malformed",
    )
    s.add(
        "shape-body-extra-field",
        "roster_shape",
        {"entry": {**add_entry, "body": {**add_entry["body"], "extra": 1}}},
        error="roster.malformed",
    )
    s.add(
        "shape-stop-counter-zero",
        "roster_shape",
        {"entry": full.entry("stop", ALICE, [full.mesh], {"counter": 0})},
        error="roster.malformed",
    )
    s.add(
        "shape-recovery-with-presence-signature",
        "roster_shape",
        {"entry": {**forged, "presence_sig": add_entry["presence_sig"]}},
        error="roster.malformed",
    )
    s.add(
        "shape-recovery-cannot-approve-phi",
        "roster_shape",
        {
            "entry": roster.build_entry(
                "phi_approval",
                full.mesh,
                [full.mesh],
                hlc(1),
                "recovery",
                {"action": "revoke", "subject": "x"},
                recovery_seed=seed("r"),
            )
        },
        error="roster.recovery_not_allowed",
    )
    bad_cert = dict(add_entry["body"]["device"], label="renamed")
    s.add(
        "shape-device-cert-covers-every-field",
        "roster_shape",
        {"entry": {**add_entry, "body": {"attestation": None, "device": bad_cert}}},
        error="roster.bad_device_cert",
    )
    other_mesh_cert = BOB.record(crypto.sha256_id(b"elsewhere"))
    s.add(
        "shape-device-cert-bound-to-mesh",
        "roster_shape",
        {
            "entry": {
                **add_entry,
                "body": {"attestation": None, "device": other_mesh_cert},
            }
        },
        error="roster.bad_device_cert",
    )
    names.update({A: "alice", B: "bob", C: "carol", D: "dave", N: "node1"})
    s.doc["names"] = dict(sorted(names.items()))
    return s


def build_attestation() -> Suite:
    s = Suite(
        "attestation",
        "contracts/roster.md#identity-attestation",
        devices=secrets_block(),
        issuer_secrets={"es256_scalar_hex": f"{sc.scalar('issuer/es256'):064x}"},
    )
    mesh = crypto.sha256_id(b"attestation-mesh")
    issuers = {sc.ISSUER: sc.issuer_record()}
    ident = b64url_encode(ALICE.identity_public)

    def case(
        name: str,
        att: dict[str, Any],
        *,
        error: str | None = None,
        now_s: int | None = sc.BASE_S,
        entry_s: int = sc.BASE_S,
        **kw: Any,
    ) -> None:
        s.add(
            name,
            "attestation_verify",
            {
                "att": att,
                "entry_time_s": entry_s,
                "identity": ident,
                "issuers": kw.get("issuers", issuers),
                "mesh": mesh,
                "now_s": now_s,
            },
            error=error,
        )

    def make(**kw: Any) -> dict[str, Any]:
        return sc.identity_attestation(ALICE, mesh, **kw)

    s.add(
        "nonce-binds-key-and-mesh",
        "attestation_verify",
        {
            "att": make(),
            "entry_time_s": sc.BASE_S,
            "identity": ident,
            "issuers": issuers,
            "mesh": mesh,
            "now_s": sc.BASE_S,
        },
        expect={
            "iat": sc.BASE_S,
            "issuer": sc.ISSUER,
            "stale": False,
            "subject": "user-1001",
        },
    )
    s.doc["nonce_hex"] = attestation.nonce_for(ALICE.identity_public, mesh)
    case("es256-token", make())
    case("rs256-token", make(alg="RS256"))
    case(
        "stale-after-thirty-days",
        make(iat=sc.BASE_S - 31 * DAY),
        entry_s=sc.BASE_S - 31 * DAY + 10,
    )
    case(
        "fresh-at-thirty-days",
        make(iat=sc.BASE_S - 30 * DAY),
        entry_s=sc.BASE_S - 30 * DAY + 10,
    )
    case("wrong-nonce", make(nonce="f" * 64), error="attestation.wrong_nonce")
    case(
        "nonce-for-another-mesh",
        make(
            nonce=attestation.nonce_for(
                ALICE.identity_public, crypto.sha256_id(b"other")
            )
        ),
        error="attestation.wrong_nonce",
    )
    case("wrong-audience", make(aud="another-app"), error="attestation.wrong_audience")
    case(
        "unlisted-client-in-azp",
        make(azp="evil-app"),
        error="attestation.wrong_audience",
    )
    case(
        "audience-list-without-azp",
        make(aud=["astral-desktop", "astral-mobile"]),
        error="attestation.wrong_audience",
    )
    case("azp-selects-the-client", make(aud="other-api", azp="astral-mobile"))
    case(
        "token-expired-before-the-entry",
        make(iat=sc.BASE_S - 1000, exp=sc.BASE_S - 301),
        error="attestation.token_expired",
    )
    case(
        "token-expired-within-skew",
        make(iat=sc.BASE_S - 1000, exp=sc.BASE_S - 300),
    )
    case(
        "token-issued-after-the-entry",
        make(iat=sc.BASE_S + 301),
        error="attestation.token_not_yet_valid",
    )
    case("token-issued-within-skew-of-the-entry", make(iat=sc.BASE_S + 300))
    case(
        "token-with-negative-exp",
        sc.identity_attestation(ALICE, mesh, exp=-1),
        error="attestation.malformed",
    )
    case("unknown-key-id", make(kid="missing"), error="attestation.unknown_key")
    case("issuer-not-accepted", make(), error="attestation.unknown_issuer", issuers={})
    base = make()
    head, body, sig = base["id_token"].split(".")
    bad = {**base, "id_token": head + "." + body + "." + b64url_encode(b"\x00" * 64)}
    bad["sig"] = crypto.sign_object(
        "p256",
        ALICE.identity_scalar,
        "astral.attestation/v1",
        {k: v for k, v in bad.items() if k != "sig"},
    )
    case("token-signature-invalid", bad, error="attestation.bad_token_signature")
    none_tok = sc.make_jwt(
        "none",
        "es-1",
        {
            "aud": sc.AUDIENCE,
            "exp": sc.BASE_S + 300,
            "iat": sc.BASE_S,
            "iss": sc.ISSUER,
            "nonce": attestation.nonce_for(ALICE.identity_public, mesh),
            "sub": "user-1001",
        },
    )
    none_att = attestation.build_identity(
        ALICE.identity_scalar,
        ALICE.identity_public,
        mesh,
        sc.ISSUER,
        "user-1001",
        none_tok,
    )
    case("alg-none-refused", none_att, error="attestation.bad_alg")
    other_sub = attestation.build_identity(
        ALICE.identity_scalar,
        ALICE.identity_public,
        mesh,
        sc.ISSUER,
        "someone-else",
        sc.id_token(ALICE, mesh),
    )
    case("subject-differs-from-token", other_sub, error="attestation.wrong_subject")
    other_iss = sc.id_token(ALICE, mesh, iss="https://auth.other.test")
    case(
        "token-issuer-differs",
        attestation.build_identity(
            ALICE.identity_scalar,
            ALICE.identity_public,
            mesh,
            sc.ISSUER,
            "user-1001",
            other_iss,
        ),
        error="attestation.wrong_issuer",
    )
    case(
        "device-signature-edited",
        {**base, "subject": "user-1001 "},
        error="attestation.bad_signature",
    )
    case(
        "attestation-for-another-device",
        sc.identity_attestation(BOB, mesh),
        error="attestation.wrong_device",
    )
    case(
        "not-a-jws",
        attestation.build_identity(
            ALICE.identity_scalar,
            ALICE.identity_public,
            mesh,
            sc.ISSUER,
            "user-1001",
            "abc",
        ),
        error="attestation.malformed",
    )
    authorities = [INST_KEY]
    phi = attestation.build_phi(
        INST_SCALAR, INST_PUBLIC, "node", "node-7", sc.BASE_S, sc.BASE_S + 30 * DAY
    )
    s.add(
        "phi-approval",
        "attestation_verify",
        {"att": phi, "authorities": authorities, "now_s": sc.BASE_S + 10},
    )
    s.add(
        "phi-approval-endpoint",
        "attestation_verify",
        {
            "att": attestation.build_phi(
                INST_SCALAR,
                INST_PUBLIC,
                "endpoint",
                "https://llm.example.test/v1",
                sc.BASE_S,
                sc.BASE_S + DAY,
            ),
            "authorities": authorities,
            "now_s": sc.BASE_S,
        },
    )
    s.add(
        "phi-approval-expired",
        "attestation_verify",
        {"att": phi, "authorities": authorities, "now_s": sc.BASE_S + 31 * DAY},
        error="attestation.expired",
    )
    s.add(
        "phi-approval-not-yet-valid",
        "attestation_verify",
        {"att": phi, "authorities": authorities, "now_s": sc.BASE_S - 1},
        error="attestation.expired",
    )
    s.add(
        "phi-approval-unknown-institution",
        "attestation_verify",
        {"att": phi, "authorities": [], "now_s": sc.BASE_S},
        error="attestation.unknown_authority",
    )
    s.add(
        "phi-approval-edited",
        "attestation_verify",
        {
            "att": {**phi, "subject": "node-8"},
            "authorities": authorities,
            "now_s": sc.BASE_S,
        },
        error="attestation.bad_signature",
    )
    s.add(
        "phi-approval-bad-subject-kind",
        "attestation_verify",
        {
            "att": {**phi, "subject_kind": "user"},
            "authorities": authorities,
            "now_s": sc.BASE_S,
        },
        error="attestation.malformed",
    )
    return s


def build_pairing() -> Suite:
    s = Suite("pairing", "contracts/roster.md#pairing", devices=secrets_block())
    mesh = crypto.sha256_id(b"pairing-mesh")
    nonce = b64url_encode(DetRng("pairing").bytes(16))
    inviter = b64url_encode(ALICE.identity_public)
    invite = pairing.build_invite(
        ALICE.identity_scalar,
        ALICE.id,
        mesh,
        "iroh:endpoint-alice",
        nonce,
        sc.BASE_S + 600,
    )
    s.add(
        "invite-valid",
        "pairing_invite",
        {
            "invite": invite,
            "inviter_identity": inviter,
            "mesh": mesh,
            "now_s": sc.BASE_S,
        },
    )
    s.add(
        "invite-expired",
        "pairing_invite",
        {
            "invite": invite,
            "inviter_identity": inviter,
            "mesh": mesh,
            "now_s": sc.BASE_S + 601,
        },
        error="pairing.expired",
    )
    s.add(
        "invite-wrong-mesh",
        "pairing_invite",
        {
            "invite": invite,
            "inviter_identity": inviter,
            "mesh": crypto.sha256_id(b"x"),
            "now_s": sc.BASE_S,
        },
        error="pairing.wrong_mesh",
    )
    s.add(
        "invite-wrong-signer",
        "pairing_invite",
        {
            "invite": invite,
            "inviter_identity": b64url_encode(BOB.identity_public),
            "mesh": mesh,
            "now_s": sc.BASE_S,
        },
        error="pairing.wrong_signer",
    )
    s.add(
        "invite-address-edited",
        "pairing_invite",
        {
            "invite": {**invite, "addr": "iroh:endpoint-mallory"},
            "inviter_identity": inviter,
            "mesh": mesh,
            "now_s": sc.BASE_S,
        },
        error="pairing.bad_signature",
    )
    request = pairing.build_request(
        BOB.identity_scalar, BOB.record(mesh), mesh, "iroh:endpoint-bob", nonce
    )
    s.add(
        "request-valid",
        "pairing_request",
        {"mesh": mesh, "nonce": nonce, "request": request},
        expect={"device": BOB.id},
    )
    s.add(
        "request-wrong-nonce",
        "pairing_request",
        {"mesh": mesh, "nonce": b64url_encode(b"\x01" * 16), "request": request},
        error="pairing.wrong_nonce",
    )
    s.add(
        "request-wrong-mesh",
        "pairing_request",
        {"mesh": crypto.sha256_id(b"x"), "nonce": nonce, "request": request},
        error="pairing.wrong_mesh",
    )
    s.add(
        "request-address-edited",
        "pairing_request",
        {"mesh": mesh, "nonce": nonce, "request": {**request, "addr": "iroh:other"}},
        error="pairing.bad_signature",
    )
    s.add(
        "request-device-cert-for-another-mesh",
        "pairing_request",
        {
            "mesh": mesh,
            "nonce": nonce,
            "request": pairing.build_request(
                BOB.identity_scalar,
                BOB.record(crypto.sha256_id(b"x")),
                mesh,
                "a",
                nonce,
            ),
        },
        error="roster.bad_device_cert",
    )
    return s


def build_keys() -> Suite:
    s = Suite("keys", "contracts/keys.md", devices=secrets_block())
    rng = DetRng("keys")
    epoch_key = seed("keys/epoch")
    aad = keys.wrap_aad("personal", BOB.id)
    wrap_in = {
        "aad": aad,
        "ephemeral_hex": rng.bytes(32).hex(),
        "nonce_hex": rng.bytes(24).hex(),
        "recipient_public_hex": BOB.wrap_public.hex(),
        "secret_hex": epoch_key.hex(),
    }
    wrapped = s.add("wrap-epoch-key-to-device", "wrap", wrap_in)["wrapped"]
    s.add(
        "unwrap-epoch-key",
        "unwrap",
        {
            "aad": aad,
            "recipient_private_hex": BOB.wrap_private.hex(),
            "wrapped": wrapped,
        },
        expect={"secret_hex": epoch_key.hex()},
    )
    s.add(
        "unwrap-with-another-devices-key",
        "unwrap",
        {
            "aad": aad,
            "recipient_private_hex": ALICE.wrap_private.hex(),
            "wrapped": wrapped,
        },
        error="wrap.decrypt_failed",
    )
    s.add(
        "unwrap-with-wrong-group",
        "unwrap",
        {
            "aad": keys.wrap_aad("phi", BOB.id),
            "recipient_private_hex": BOB.wrap_private.hex(),
            "wrapped": wrapped,
        },
        error="wrap.decrypt_failed",
    )
    s.add(
        "unwrap-with-wrong-recipient-id",
        "unwrap",
        {
            "aad": keys.wrap_aad("personal", ALICE.id),
            "recipient_private_hex": BOB.wrap_private.hex(),
            "wrapped": wrapped,
        },
        error="wrap.decrypt_failed",
    )
    raw = bytearray(b64url_decode(wrapped["ct"]))
    raw[0] ^= 1
    s.add(
        "unwrap-edited-ciphertext",
        "unwrap",
        {
            "aad": aad,
            "recipient_private_hex": BOB.wrap_private.hex(),
            "wrapped": {**wrapped, "ct": b64url_encode(bytes(raw))},
        },
        error="wrap.decrypt_failed",
    )
    s.add(
        "unwrap-missing-field",
        "unwrap",
        {
            "aad": aad,
            "recipient_private_hex": BOB.wrap_private.hex(),
            "wrapped": {"epk": wrapped["epk"]},
        },
        error="wrap.malformed",
    )
    m = sc.Mesh("keys", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    body = m.genesis["body"]["epoch"]
    s.add(
        "open-genesis-epoch-as-member",
        "epoch_open",
        {
            "as": "device",
            "body": body,
            "device": ALICE.id,
            "private_hex": ALICE.wrap_private.hex(),
        },
        expect={"epoch_key_hex": m.epoch_key.hex()},
    )
    s.add(
        "open-genesis-epoch-as-recovery",
        "epoch_open",
        {
            "as": "recovery",
            "body": body,
            "private_hex": m.recovery["wrap_private"].hex(),
        },
        expect={"epoch_key_hex": m.epoch_key.hex()},
    )
    s.add(
        "open-epoch-as-non-recipient",
        "epoch_open",
        {
            "as": "device",
            "body": body,
            "device": BOB.id,
            "private_hex": BOB.wrap_private.hex(),
        },
        error="wrap.not_a_recipient",
    )
    phi_body = keys.make_epoch_body(
        "phi", seed("keys/phi"), {NODE.id: NODE.wrap_public}, None, rng
    )
    s.add(
        "phi-epoch-has-no-recovery-wrap",
        "epoch_open",
        {
            "as": "recovery",
            "body": phi_body,
            "private_hex": m.recovery["wrap_private"].hex(),
        },
        error="wrap.not_a_recipient",
    )
    assert ab
    for name, secret in (
        ("sequence", bytes(range(32))),
        ("zeros", bytes(32)),
        ("ones", b"\xff" * 32),
        ("seeded", seed("recovery/demo")),
    ):
        out = s.add("recovery-" + name, "recovery_encode", {"secret_hex": secret.hex()})
        s.add(
            "recovery-words-decode-" + name,
            "recovery_decode",
            {"words": out["words"]},
            expect={"secret_hex": secret.hex()},
        )
    words = keys.encode_words(bytes(range(32))).split()
    s.add(
        "recovery-case-and-spacing-normalised",
        "recovery_decode",
        {"words": "  " + " ".join(words).upper().replace(" ", "   ") + "\n"},
        expect={"secret_hex": bytes(range(32)).hex()},
    )
    swapped = words[:-1] + ["zoo" if words[-1] != "zoo" else "abandon"]
    s.add(
        "recovery-bad-checksum",
        "recovery_decode",
        {"words": " ".join(swapped)},
        error="recovery.bad_checksum",
    )
    s.add(
        "recovery-unknown-word",
        "recovery_decode",
        {"words": " ".join(words[:-1] + ["astral"])},
        error="recovery.unknown_word",
    )
    s.add(
        "recovery-twelve-words",
        "recovery_decode",
        {"words": " ".join(words[:12])},
        error="recovery.bad_length",
    )
    s.add(
        "recovery-twenty-five-words",
        "recovery_decode",
        {"words": " ".join(words + ["abandon"])},
        error="recovery.bad_length",
    )
    payload = {"events": [], "roster": [m.genesis]}
    rr = {
        "ephemeral_hex": rng.bytes(32).hex(),
        "mesh": m.mesh,
        "nonce_hex": rng.bytes(24).hex(),
        "payload": payload,
        "recovery_wrap_public_hex": m.recovery["wrap_public"].hex(),
    }
    backup = s.add("backup-seal", "backup_seal", rr)["backup"]
    s.add(
        "backup-open",
        "backup_open",
        {
            "backup": backup,
            "recovery_wrap_private_hex": m.recovery["wrap_private"].hex(),
        },
        expect=payload,
    )
    s.add(
        "backup-wrong-recovery-key",
        "backup_open",
        {"backup": backup, "recovery_wrap_private_hex": seed("other").hex()},
        error="backup.decrypt_failed",
    )
    craw = bytearray(b64url_decode(backup["ct"]))
    craw[3] ^= 1
    s.add(
        "backup-edited-ciphertext",
        "backup_open",
        {
            "backup": {**backup, "ct": b64url_encode(bytes(craw))},
            "recovery_wrap_private_hex": m.recovery["wrap_private"].hex(),
        },
        error="backup.decrypt_failed",
    )
    s.add(
        "backup-mesh-swapped",
        "backup_open",
        {
            "backup": {**backup, "mesh": crypto.sha256_id(b"other")},
            "recovery_wrap_private_hex": m.recovery["wrap_private"].hex(),
        },
        error="backup.decrypt_failed",
    )
    s.add(
        "backup-unknown-format",
        "backup_open",
        {
            "backup": {**backup, "format": "astral.backup/v2"},
            "recovery_wrap_private_hex": m.recovery["wrap_private"].hex(),
        },
        error="backup.malformed",
    )
    s.add(
        "backup-extra-field",
        "backup_open",
        {
            "backup": {**backup, "extra": 1},
            "recovery_wrap_private_hex": m.recovery["wrap_private"].hex(),
        },
        error="backup.malformed",
    )
    return s


def caps(fresh: tuple[int, int, int], solo: tuple[int, int, int]) -> dict[str, Any]:
    names = ("notes_write", "outside_effect", "model_tokens")
    return {
        "fresh": dict(zip(names, fresh, strict=True)),
        "solo": dict(zip(names, solo, strict=True)),
    }


POCKET = caps((200, 20, 200_000), (50, 0, 50_000))
DESKTOP = caps((1000, 100, 1_000_000), (200, 10, 200_000))
SATELLITE = caps((0, 0, 0), (0, 0, 0))
NODE_CAPS = caps((5000, 200, 5_000_000), (500, 20, 500_000))
PERIOD = {"anchor_s": 1_794_355_200, "length_s": 86400, "unit": "day"}


def policy_doc(
    m: sc.Mesh,
    author: TestDevice,
    follows: list[str],
    ms: int,
    *,
    classes: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
    freshness_s: int = 86400,
    period: dict[str, Any] | None = None,
    mesh: str | None = None,
) -> dict[str, Any]:
    base = {
        "desktop": DESKTOP,
        "node": NODE_CAPS,
        "pocket": POCKET,
        "satellite": SATELLITE,
    }
    return policy.build(
        author.identity_scalar,
        mesh=mesh or m.mesh,
        author=author.id,
        follows=follows,
        clock=hlc(ms),
        period=period or PERIOD,
        freshness_s=freshness_s,
        classes=classes or base,
        devices=devices or {},
    )


def build_policy() -> Suite:
    s = Suite("policy", "contracts/policy.md", devices=secrets_block())
    m = sc.Mesh("policy", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    state = roster.fold(m.entries, m.mesh)
    members = {
        d: {
            "identity": state["identity_keys"][d],
            "kind": r["kind"],
            "presence": state["presence_keys"][d],
        }
        for d, r in state["members"].items()
    }
    ident = {d: v["identity"] for d, v in members.items()}
    mesh = m.mesh
    assert ab

    def consent_for(
        doc: dict[str, Any],
        who: TestDevice = ALICE,
        digest: str | None = None,
        ms: int = sc.BASE_MS,
    ) -> dict[str, Any]:
        return consent.build(
            "policy",
            digest or policy.policy_id(doc),
            who.id,
            who.presence_scalar,
            hlc(ms),
        )

    p0 = policy_doc(m, ALICE, [], sc.BASE_MS + 1000)
    pid0 = policy.policy_id(p0)
    s.add(
        "policy-verifies",
        "policy_verify",
        {"identity_keys": ident, "mesh": mesh, "policy": p0},
        expect={"id": pid0},
    )
    s.add(
        "policy-bad-signature",
        "policy_verify",
        {"identity_keys": ident, "mesh": mesh, "policy": {**p0, "freshness_s": 1}},
        error="policy.bad_signature",
    )
    s.add(
        "policy-wrong-mesh",
        "policy_verify",
        {"identity_keys": ident, "mesh": crypto.sha256_id(b"x"), "policy": p0},
        error="policy.wrong_mesh",
    )
    s.add(
        "policy-unknown-author",
        "policy_verify",
        {"identity_keys": {}, "mesh": mesh, "policy": p0},
        error="policy.unknown_author",
    )
    bad_solo = dict(
        DESKTOP, solo={"model_tokens": 2_000_000, "notes_write": 0, "outside_effect": 0}
    )
    s.add(
        "policy-solo-cannot-exceed-fresh",
        "policy_verify",
        {
            "identity_keys": ident,
            "mesh": mesh,
            "policy": policy_doc(
                m,
                ALICE,
                [],
                sc.BASE_MS,
                classes={
                    "desktop": bad_solo,
                    "node": NODE_CAPS,
                    "pocket": POCKET,
                    "satellite": SATELLITE,
                },
            ),
        },
        error="policy.solo_exceeds_fresh",
    )
    incomplete = {"desktop": DESKTOP, "pocket": POCKET, "satellite": SATELLITE}
    s.add(
        "policy-needs-every-device-kind",
        "policy_verify",
        {
            "identity_keys": ident,
            "mesh": mesh,
            "policy": policy_doc(m, ALICE, [], sc.BASE_MS, classes=incomplete),
        },
        error="policy.malformed",
    )
    s.add(
        "policy-period-length-must-be-positive",
        "policy_verify",
        {
            "identity_keys": ident,
            "mesh": mesh,
            "policy": policy_doc(
                m, ALICE, [], sc.BASE_MS, period={**PERIOD, "length_s": 0}
            ),
        },
        error="policy.malformed",
    )

    def eff(name: str, wrappers: list[dict[str, Any]], **want: Any) -> dict[str, Any]:
        out = s.add(
            name,
            "policy_effective",
            {"members": members, "mesh": mesh, "wrappers": wrappers},
        )
        for key, wanted in want.items():
            assert out[key] == wanted, (name, key, out[key], wanted)
        return out

    w0 = {"consent": consent_for(p0), "policy": p0}
    out = eff(
        "first-policy-with-consent-is-accepted",
        [w0],
        accepted=[pid0],
        heads=[pid0],
        ignored={},
    )
    assert out["effective"]["classes"]["pocket"] == POCKET
    eff(
        "first-policy-without-consent-is-ignored",
        [{"consent": None, "policy": p0}],
        accepted=[],
        effective=None,
        ignored={pid0: "policy.widening_needs_consent"},
    )
    eff(
        "consent-for-another-policy-is-ignored",
        [{"consent": consent_for(p0, digest=crypto.sha256_id(b"other")), "policy": p0}],
        accepted=[],
        ignored={pid0: "policy.consent.wrong_action"},
    )
    eff(
        "consent-from-unknown-device-is-ignored",
        [
            {
                "consent": consent.build(
                    "policy", pid0, MALLORY.id, MALLORY.presence_scalar, hlc(1)
                ),
                "policy": p0,
            }
        ],
        accepted=[],
        ignored={pid0: "policy.consent.unknown_device"},
    )
    narrower_pocket = caps((100, 10, 100_000), (20, 0, 20_000))
    p1 = policy_doc(
        m,
        ALICE,
        [pid0],
        sc.BASE_MS + 2000,
        classes={
            "desktop": DESKTOP,
            "node": NODE_CAPS,
            "pocket": narrower_pocket,
            "satellite": SATELLITE,
        },
    )
    pid1 = policy.policy_id(p1)
    out = eff(
        "narrowing-needs-no-consent",
        [w0, {"consent": None, "policy": p1}],
        accepted=sorted([pid0, pid1]),
        heads=[pid1],
        ignored={},
    )
    assert out["effective"]["classes"]["pocket"] == narrower_pocket
    narrower_desktop = caps((500, 50, 500_000), (100, 5, 100_000))
    p2a = policy_doc(
        m,
        ALICE,
        [pid1],
        sc.BASE_MS + 3000,
        classes={
            "desktop": DESKTOP,
            "node": NODE_CAPS,
            "pocket": caps((50, 5, 50_000), (10, 0, 10_000)),
            "satellite": SATELLITE,
        },
    )
    p2b = policy_doc(
        m,
        BOB,
        [pid1],
        sc.BASE_MS + 3500,
        classes={
            "desktop": narrower_desktop,
            "node": NODE_CAPS,
            "pocket": narrower_pocket,
            "satellite": SATELLITE,
        },
    )
    id2a, id2b = policy.policy_id(p2a), policy.policy_id(p2b)
    out = eff(
        "concurrent-narrowings-merge-to-the-minimum",
        [
            w0,
            {"consent": None, "policy": p1},
            {"consent": None, "policy": p2a},
            {"consent": None, "policy": p2b},
        ],
        heads=sorted([id2a, id2b]),
        ignored={},
    )
    assert out["effective"]["classes"]["pocket"] == caps(
        (50, 5, 50_000), (10, 0, 10_000)
    )
    assert out["effective"]["classes"]["desktop"] == narrower_desktop
    widen = policy_doc(m, ALICE, sorted([id2a, id2b]), sc.BASE_MS + 4000)
    idw = policy.policy_id(widen)
    base_wrappers = [
        w0,
        {"consent": None, "policy": p1},
        {"consent": None, "policy": p2a},
        {"consent": None, "policy": p2b},
    ]
    eff(
        "widening-without-consent-is-ignored",
        base_wrappers + [{"consent": None, "policy": widen}],
        heads=sorted([id2a, id2b]),
        ignored={idw: "policy.widening_needs_consent"},
    )
    out = eff(
        "widening-with-consent-takes-effect",
        base_wrappers + [{"consent": consent_for(widen), "policy": widen}],
        heads=[idw],
        ignored={},
    )
    assert out["effective"]["classes"]["pocket"] == POCKET
    eff(
        "widening-with-someone-elses-signature-ignored",
        base_wrappers
        + [
            {
                "consent": consent_for(widen, digest=crypto.sha256_id(b"x")),
                "policy": widen,
            }
        ],
        ignored={idw: "policy.consent.wrong_action"},
    )
    override = policy_doc(
        m,
        ALICE,
        [pid1],
        sc.BASE_MS + 5000,
        classes={
            "desktop": DESKTOP,
            "node": NODE_CAPS,
            "pocket": narrower_pocket,
            "satellite": SATELLITE,
        },
        devices={BOB.id: caps((10, 1, 10_000), (0, 0, 0))},
    )
    ido = policy.policy_id(override)
    out = eff(
        "per-device-override-can-narrow",
        [w0, {"consent": None, "policy": p1}, {"consent": None, "policy": override}],
        heads=[ido],
        ignored={},
    )
    assert out["effective"]["devices"][BOB.id]["fresh"]["notes_write"] == 10
    raise_override = policy_doc(
        m,
        ALICE,
        [pid1],
        sc.BASE_MS + 5000,
        classes={
            "desktop": DESKTOP,
            "node": NODE_CAPS,
            "pocket": narrower_pocket,
            "satellite": SATELLITE,
        },
        devices={BOB.id: caps((150, 10, 100_000), (20, 0, 20_000))},
    )
    idr = policy.policy_id(raise_override)
    eff(
        "per-device-override-cannot-widen-without-consent",
        [
            w0,
            {"consent": None, "policy": p1},
            {"consent": None, "policy": raise_override},
        ],
        heads=[pid1],
        ignored={idr: "policy.widening_needs_consent"},
    )
    longer = policy_doc(
        m, ALICE, [pid0], sc.BASE_MS + 6000, period={**PERIOD, "length_s": 2 * 86400}
    )
    idl = policy.policy_id(longer)
    eff(
        "changing-the-period-needs-consent",
        [w0, {"consent": None, "policy": longer}],
        heads=[pid0],
        ignored={idl: "policy.widening_needs_consent"},
    )
    eff(
        "child-of-a-rejected-policy-is-ignored",
        [{"consent": None, "policy": p0}, {"consent": None, "policy": p1}],
        accepted=[],
        ignored={pid0: "policy.widening_needs_consent", pid1: "policy.parent_rejected"},
    )
    eff(
        "policy-with-unseen-parent-is-pending",
        [{"consent": None, "policy": p1}],
        accepted=[],
        pending=[pid1],
    )
    forged = {**p1, "author": MALLORY.id}
    out = eff(
        "policy-from-a-non-member-is-ignored",
        [w0, {"consent": None, "policy": forged}],
        accepted=[pid0],
        ignored={policy.policy_id(forged): "policy.unknown_author"},
    )
    members_kinds = {d: v["kind"] for d, v in members.items()}
    final = {
        k: out["effective"][k] for k in ("classes", "devices", "freshness_s", "period")
    }
    s.add(
        "budget-input-first-period",
        "budget_input",
        {
            "effective": final,
            "mesh": mesh,
            "members": members_kinds,
            "now_s": PERIOD["anchor_s"] + 100,
            "stop": 0,
        },
    )
    s.add(
        "budget-input-third-period",
        "budget_input",
        {
            "effective": final,
            "mesh": mesh,
            "members": members_kinds,
            "now_s": PERIOD["anchor_s"] + 2 * 86400 + 5,
            "stop": 1,
        },
    )
    s.add(
        "budget-input-before-anchor",
        "budget_input",
        {
            "effective": final,
            "mesh": mesh,
            "members": members_kinds,
            "now_s": PERIOD["anchor_s"] - 1,
            "stop": 0,
        },
        error="policy.before_anchor",
    )
    s.doc["budget_input_note"] = (
        "expected shares are produced by LETS and checked in its own vectors; this file fixes only the input and its digest"
    )
    return s


def load_pinned(name: str) -> dict[str, str]:
    path = VECTORS / f"{name}.json"
    if path.exists():
        return dict(json.loads(path.read_text(encoding="utf-8")).get("tokens", {}))
    return {}


def build_ticket(repin: bool = False) -> Suite:
    pinned = {} if repin else load_pinned("ticket")
    tokens: dict[str, str] = {}
    s = Suite("ticket", "contracts/ticket.md", devices=secrets_block())

    def tok(label: str, make: Any) -> str:
        if label not in pinned:
            pinned[label] = b64url_encode(make())
        tokens[label] = pinned[label]
        return pinned[label]

    m = sc.Mesh("ticket", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    state = roster.fold(m.entries, m.mesh)
    feeds = {f: v for f, v in state["feeds"].items()}
    presence = dict(state["presence_keys"])
    mesh = m.mesh
    assert ab
    now_s = sc.BASE_S + 100
    args = {"subject": "Hello", "to": "bob@example.test"}
    influence = [crypto.sha256_id(b"web-page-1")]
    turn = crypto.sha256_id(b"turn-1")
    action = {
        "agent": "agent.mail",
        "args_digest": crypto.object_id(args),
        "device": ALICE.id,
        "influence": influence,
        "mesh": mesh,
        "tool": "mail.send",
        "tool_def": crypto.sha256_id(b"tool-definition-v1"),
        "turn": turn,
        "v": 1,
    }
    digest = ticket.action_digest(action)
    s.add(
        "action-digest", "action_digest", {"action": action}, expect={"digest": digest}
    )
    s.add(
        "action-digest-extra-field",
        "action_digest",
        {"action": {**action, "note": "x"}},
        error="ticket.malformed_action",
    )
    s.add(
        "action-digest-influence-unsorted",
        "action_digest",
        {
            "action": {
                **action,
                "influence": sorted(
                    [crypto.sha256_id(b"a"), crypto.sha256_id(b"b")], reverse=True
                ),
            }
        },
        error="ticket.malformed_action",
    )

    def mint(seed_bytes: bytes = ALICE.ops_seed, **over: Any) -> bytes:
        kw: dict[str, Any] = {
            "mesh": mesh,
            "device": ALICE.id,
            "stop_counter": 0,
            "expires_s": now_s + 3600,
            "turn": turn,
            "tools": ["mail.send", "notes.read"],
            "turn_expires_s": now_s + 1800,
            "agents": ["agent.mail"],
            "tool": "mail.send",
            "args_digest": action["args_digest"],
            "action": digest,
            "integrity": "untrusted",
            "confidentiality": "private",
            "sources": influence,
        }
        kw.update(over)
        return ticket.mint(seed_bytes, **kw)

    good = tok("valid", mint)
    receipt = {
        "evidence_digest": digest,
        "receipt_id": "r-0001",
        "type": "lets.receipt/v1",
    }
    consent_obj = consent.build(
        "action", digest, ALICE.id, ALICE.presence_scalar, hlc(now_s * 1000)
    )

    def base(**over: Any) -> dict[str, Any]:
        v: dict[str, Any] = {
            "action": action,
            "consent_required": False,
            "effect_kind": "outside_effect",
            "mesh": mesh,
            "now_s": now_s,
            "presence_keys": presence,
            "receipt_valid": True,
            "roster_feeds": feeds,
            "stop": 0,
            "ticket": {"capability": good, "consent": None, "receipt": receipt, "v": 1},
        }
        v.update(over)
        return v

    def with_ticket(**over: Any) -> dict[str, Any]:
        return {**base()["ticket"], **over}

    claims = {
        "action": digest,
        "agents": ["agent.mail"],
        "device": ALICE.id,
        "feed": ALICE.feed,
        "labels": {
            "confidentiality": "private",
            "integrity": "untrusted",
            "sources": influence,
        },
        "mesh": mesh,
        "stop_counter": 0,
        "turn": turn,
    }
    s.add("valid-ticket", "ticket_verify", base(), expect=claims)
    s.add(
        "valid-with-consent",
        "ticket_verify",
        base(consent_required=True, ticket=with_ticket(consent=consent_obj)),
        expect=claims,
    )
    s.add(
        "consent-required-but-missing",
        "ticket_verify",
        base(consent_required=True),
        error="ticket.consent_required",
    )
    other = consent.build(
        "action",
        crypto.sha256_id(b"another action"),
        ALICE.id,
        ALICE.presence_scalar,
        hlc(now_s * 1000),
    )
    s.add(
        "consent-for-another-action",
        "ticket_verify",
        base(consent_required=True, ticket=with_ticket(consent=other)),
        error="ticket.consent_wrong_action",
    )
    old = consent.build(
        "action", digest, ALICE.id, ALICE.presence_scalar, hlc(now_s * 1000 - 600_001)
    )
    s.add(
        "consent-too-old",
        "ticket_verify",
        base(consent_required=True, ticket=with_ticket(consent=old)),
        error="ticket.consent_stale",
    )
    s.add(
        "consent-from-the-future",
        "ticket_verify",
        base(
            consent_required=True,
            ticket=with_ticket(
                consent=consent.build(
                    "action",
                    digest,
                    ALICE.id,
                    ALICE.presence_scalar,
                    hlc(now_s * 1000 + 600_001),
                )
            ),
        ),
        error="ticket.consent_stale",
    )
    s.add(
        "consent-at-the-age-limit",
        "ticket_verify",
        base(
            consent_required=True,
            ticket=with_ticket(
                consent=consent.build(
                    "action",
                    digest,
                    ALICE.id,
                    ALICE.presence_scalar,
                    hlc(now_s * 1000 - 600_000),
                )
            ),
        ),
        expect=claims,
    )
    s.add(
        "consent-from-unknown-device",
        "ticket_verify",
        base(
            consent_required=True,
            ticket=with_ticket(
                consent=consent.build(
                    "action",
                    digest,
                    MALLORY.id,
                    MALLORY.presence_scalar,
                    hlc(now_s * 1000),
                )
            ),
        ),
        error="ticket.consent_invalid",
    )
    s.add(
        "consent-edited",
        "ticket_verify",
        base(
            consent_required=True,
            ticket=with_ticket(consent={**consent_obj, "clock": hlc(now_s * 1000 + 1)}),
        ),
        error="ticket.consent_invalid",
    )
    s.add(
        "consent-signed-with-identity-key",
        "ticket_verify",
        base(
            consent_required=True,
            ticket=with_ticket(
                consent={
                    **consent_obj,
                    "sig": crypto.sign_object(
                        "p256",
                        ALICE.identity_scalar,
                        "astral.consent/v1",
                        {k: v for k, v in consent_obj.items() if k != "sig"},
                    ),
                }
            ),
        ),
        error="ticket.consent_invalid",
    )
    s.add(
        "optional-consent-still-checked",
        "ticket_verify",
        base(ticket=with_ticket(consent=other)),
        error="ticket.consent_wrong_action",
    )

    def changed(**over: Any) -> dict[str, Any]:
        return {**action, **over}

    s.add(
        "tool-differs-from-pinned-call",
        "ticket_verify",
        base(
            action=changed(tool="mail.delete"),
            ticket=with_ticket(
                receipt={
                    **receipt,
                    "evidence_digest": ticket.action_digest(
                        changed(tool="mail.delete")
                    ),
                }
            ),
        ),
        error="ticket.unauthorized",
    )
    s.add(
        "arguments-differ-from-pinned-call",
        "ticket_verify",
        base(action=changed(args_digest=crypto.object_id({"to": "eve@example.test"}))),
        error="ticket.unauthorized",
    )
    s.add(
        "expired-capability",
        "ticket_verify",
        base(now_s=now_s + 3601),
        error="ticket.unauthorized",
    )
    s.add(
        "expired-turn-block",
        "ticket_verify",
        base(now_s=now_s + 1801),
        error="ticket.unauthorized",
    )
    s.add("issued-before-a-stop", "ticket_verify", base(stop=1), error="ticket.stopped")
    removed = {
        **feeds,
        ALICE.feed: {"cutoff": 4, "device": ALICE.id, "state": "removed"},
    }
    s.add(
        "device-removed-from-roster",
        "ticket_verify",
        base(roster_feeds=removed),
        error="ticket.device_removed",
    )
    s.add(
        "root-key-not-on-roster",
        "ticket_verify",
        base(roster_feeds={k: v for k, v in feeds.items() if k != ALICE.feed}),
        error="ticket.bad_root",
    )
    s.add(
        "action-for-another-mesh",
        "ticket_verify",
        base(action=changed(mesh=crypto.sha256_id(b"other"))),
        error="ticket.mesh_mismatch",
    )
    s.add(
        "action-device-differs-from-ticket",
        "ticket_verify",
        base(
            action=changed(device=BOB.id),
            ticket=with_ticket(
                receipt={
                    **receipt,
                    "evidence_digest": ticket.action_digest(changed(device=BOB.id)),
                }
            ),
        ),
        error="ticket.unauthorized",
    )
    wrong_root = tok("minted-by-bob-claiming-alice", lambda: mint(BOB.ops_seed))
    s.add(
        "device-fact-does-not-own-the-root-key",
        "ticket_verify",
        base(ticket=with_ticket(capability=wrong_root)),
        error="ticket.bad_root",
    )
    mal_feeds = {**feeds, MALLORY.feed: {"device": MALLORY.id, "state": "member"}}
    forged = tok(
        "minted-by-unlisted-key", lambda: mint(MALLORY.ops_seed, device=MALLORY.id)
    )
    s.add(
        "root-key-unrelated-to-this-mesh",
        "ticket_verify",
        base(ticket=with_ticket(capability=forged)),
        error="ticket.bad_root",
    )
    s.add(
        "signature-chain-under-another-device-key",
        "ticket_verify",
        base(
            roster_feeds={
                **mal_feeds,
                ALICE.feed: {"device": ALICE.id, "state": "member"},
            },
            ticket=with_ticket(capability=wrong_root),
        ),
        error="ticket.bad_root",
    )
    widened = tok("call-outside-the-turn-tool-set", lambda: mint(tools=["notes.read"]))
    s.add(
        "holder-cannot-widen-beyond-the-turn",
        "ticket_verify",
        base(ticket=with_ticket(capability=widened)),
        error="ticket.unauthorized",
    )
    after_call = tok(
        "block-appended-after-the-call",
        lambda: ticket.attenuate(
            b64url_decode(good),
            ALICE.ops_public,
            'check if tool($t), {"mail.send", "mail.delete"}.contains($t);',
        ),
    )
    s.add(
        "block-appended-after-the-call-block",
        "ticket_verify",
        base(ticket=with_ticket(capability=after_call)),
        error="ticket.wrong_block_shape",
    )
    two_action = changed(agent="agent.compose")
    two_digest = ticket.action_digest(two_action)
    sub2 = tok(
        "two-agent-hops-pinned-to-sub-agent",
        lambda: mint(agents=["agent.mail", "agent.compose"], action=two_digest),
    )
    two = {**claims, "action": two_digest, "agents": ["agent.mail", "agent.compose"]}
    s.add(
        "two-agent-hops-are-allowed",
        "ticket_verify",
        base(
            action=two_action,
            ticket=with_ticket(
                capability=sub2, receipt={**receipt, "evidence_digest": two_digest}
            ),
        ),
        expect=two,
    )
    s.add(
        "action-names-the-wrong-last-agent",
        "ticket_verify",
        base(
            action=changed(agent="agent.other"),
            ticket=with_ticket(
                capability=sub2,
                receipt={
                    **receipt,
                    "evidence_digest": ticket.action_digest(
                        changed(agent="agent.other")
                    ),
                },
            ),
        ),
        error="ticket.unauthorized",
    )

    def custom(label: str, blocks: list[str]) -> str:
        from biscuit_auth import Algorithm, BiscuitBuilder, BlockBuilder, PrivateKey

        def make() -> bytes:
            token = BiscuitBuilder(blocks[0]).build(
                PrivateKey.from_bytes(ALICE.ops_seed, Algorithm.Ed25519)
            )
            for source in blocks[1:]:
                token = token.append(BlockBuilder(source))
            return token.to_bytes()

        return tok(label, make)

    head = (
        f'mesh("{mesh}"); device("{ALICE.id}"); feed("{ALICE.feed}"); actor("device:{ALICE.id}"); stop_counter(0); '
        f"check if mesh_ctx($m), mesh($m); check if stop_current($n), stop_counter($c), $n <= $c;"
    )
    turn_block = f'actor("turn:{turn}"); turn("{turn}"); check if tool($t), {{"mail.send"}}.contains($t);'
    agent_block = 'actor("agent:agent.mail"); check if agent($a), $a == "agent.mail";'
    call_block = (
        f'actor("call:{digest}"); label("integrity", "owner"); label("confidentiality", "private"); '
        f'check if action_digest($d), $d == "{digest}";'
    )
    no_labels = f'actor("call:{digest}"); check if action_digest($d), $d == "{digest}";'
    rule_block = 'actor("agent:agent.mail"); agent("agent.evil") <- turn($t);'
    plain_action = {**action, "influence": []}
    plain_digest = ticket.action_digest(plain_action)
    plain_call = call_block.replace(digest, plain_digest)
    plain_receipt = {**receipt, "evidence_digest": plain_digest}
    ok_custom = custom("hand-built-valid", [head, turn_block, agent_block, plain_call])
    owner = {
        **claims,
        "action": plain_digest,
        "labels": {"confidentiality": "private", "integrity": "owner", "sources": []},
    }
    s.add(
        "hand-built-token-with-the-same-layout",
        "ticket_verify",
        base(
            action=plain_action,
            ticket=with_ticket(capability=ok_custom, receipt=plain_receipt),
        ),
        expect=owner,
    )
    s.add(
        "block-with-a-rule-refused",
        "ticket_verify",
        base(
            action=plain_action,
            ticket=with_ticket(
                capability=custom(
                    "rule-in-agent-block", [head, turn_block, rule_block, plain_call]
                ),
                receipt=plain_receipt,
            ),
        ),
        error="ticket.wrong_block_shape",
    )
    s.add(
        "call-block-without-labels",
        "ticket_verify",
        base(
            action=plain_action,
            ticket=with_ticket(
                capability=custom(
                    "no-labels",
                    [
                        head,
                        turn_block,
                        agent_block,
                        no_labels.replace(digest, plain_digest),
                    ],
                ),
                receipt=plain_receipt,
            ),
        ),
        error="ticket.label_missing",
    )
    s.add(
        "missing-agent-block",
        "ticket_verify",
        base(
            action=plain_action,
            ticket=with_ticket(
                capability=custom("no-agent-block", [head, turn_block, plain_call]),
                receipt=plain_receipt,
            ),
        ),
        error="ticket.wrong_block_shape",
    )
    s.add(
        "labels-disagree-with-action-influence",
        "ticket_verify",
        base(
            ticket=with_ticket(
                capability=tok("labels-without-sources", lambda: mint(sources=[]))
            )
        ),
        error="ticket.unauthorized",
    )
    s.add(
        "receipt-missing",
        "ticket_verify",
        base(ticket=with_ticket(receipt=None)),
        error="ticket.receipt_missing",
    )
    s.add(
        "receipt-bound-to-another-action",
        "ticket_verify",
        base(
            ticket=with_ticket(
                receipt={**receipt, "evidence_digest": crypto.sha256_id(b"x")}
            )
        ),
        error="ticket.receipt_mismatch",
    )
    s.add(
        "receipt-of-another-type",
        "ticket_verify",
        base(ticket=with_ticket(receipt={**receipt, "type": "lets.lease/v1"})),
        error="ticket.receipt_mismatch",
    )
    s.add(
        "receipt-refused-by-lets",
        "ticket_verify",
        base(receipt_valid=False),
        error="ticket.receipt_invalid",
    )
    for kind in ("mesh_read", "outward_read"):
        s.add(
            f"{kind.replace('_', '-')}-carries-no-receipt",
            "ticket_verify",
            base(effect_kind=kind, ticket=with_ticket(receipt=None)),
            expect=claims,
        )
        s.add(
            f"{kind.replace('_', '-')}-never-consults-lets",
            "ticket_verify",
            base(
                effect_kind=kind, receipt_valid=False, ticket=with_ticket(receipt=None)
            ),
            expect=claims,
        )
        s.add(
            f"{kind.replace('_', '-')}-refuses-an-attached-receipt",
            "ticket_verify",
            base(effect_kind=kind),
            error="ticket.receipt_unexpected",
        )
    s.add(
        "mesh-write-needs-its-receipt",
        "ticket_verify",
        base(effect_kind="mesh_write", ticket=with_ticket(receipt=None)),
        error="ticket.receipt_missing",
    )
    s.add(
        "mesh-write-with-bound-receipt",
        "ticket_verify",
        base(effect_kind="mesh_write"),
        expect=claims,
    )
    s.add(
        "unknown-effect-kind",
        "ticket_verify",
        base(effect_kind="send"),
        error="labels.invalid",
    )
    s.add(
        "capability-not-base64url",
        "ticket_verify",
        base(ticket=with_ticket(capability="not base64!")),
        error="ticket.malformed",
    )
    s.add(
        "capability-not-a-biscuit",
        "ticket_verify",
        base(ticket=with_ticket(capability=b64url_encode(b"hello world"))),
        error="ticket.malformed",
    )
    s.add(
        "ticket-extra-field",
        "ticket_verify",
        base(ticket={**with_ticket(), "extra": 1}),
        error="ticket.malformed",
    )
    s.add(
        "consent-verify-action",
        "consent_verify",
        {
            "consent": consent_obj,
            "digest": digest,
            "kind": "action",
            "presence_keys": presence,
        },
        expect={"valid": True},
    )
    s.add(
        "consent-verify-wrong-kind",
        "consent_verify",
        {
            "consent": consent_obj,
            "digest": digest,
            "kind": "policy",
            "presence_keys": presence,
        },
        error="consent.wrong_action",
    )
    s.doc["tokens"] = dict(sorted(tokens.items()))
    s.doc["tokens_note"] = (
        "Biscuit blocks are signed with fresh ephemeral keys, so token bytes are pinned and not regenerated. Outcomes are checked on every run."
    )
    return s


def lab(integrity: str, confidentiality: str) -> dict[str, str]:
    return {"confidentiality": confidentiality, "integrity": integrity}


def build_labels() -> Suite:
    s = Suite("labels", "contracts/labels.md")
    OWN, TT, UN = "owner", "tool-trusted", "untrusted"
    s.add(
        "join-empty-is-top-of-the-trust-lattice",
        "labels_join",
        {"labels": []},
        expect=lab(OWN, "public"),
    )
    s.add(
        "join-takes-least-trusted-and-most-confidential",
        "labels_join",
        {"labels": [lab(OWN, "public"), lab(UN, "private")]},
        expect=lab(UN, "private"),
    )
    s.add(
        "join-tool-trusted-below-owner",
        "labels_join",
        {"labels": [lab(OWN, "private"), lab(TT, "public")]},
        expect=lab(TT, "private"),
    )
    s.add(
        "join-phi-below-secret",
        "labels_join",
        {"labels": [lab(OWN, "phi"), lab(OWN, "secret"), lab(OWN, "private")]},
        expect=lab(OWN, "secret"),
    )
    s.add(
        "memory-from-untrusted-text-stays-untrusted",
        "labels_join",
        {"labels": [lab(UN, "public"), lab(OWN, "private")]},
        expect=lab(UN, "private"),
    )
    s.add(
        "compaction-note-inherits-phi",
        "labels_join",
        {"labels": [lab(OWN, "private"), lab(OWN, "phi"), lab(TT, "private")]},
        expect=lab(TT, "phi"),
    )
    s.add(
        "join-unknown-integrity",
        "labels_join",
        {"labels": [{"confidentiality": "public", "integrity": "trusted"}]},
        error="labels.invalid",
    )
    s.add(
        "join-unknown-confidentiality",
        "labels_join",
        {"labels": [{"confidentiality": "classified", "integrity": "owner"}]},
        error="labels.invalid",
    )
    s.add(
        "quarantined-answer-keeps-untrusted-label",
        "labels_quarantine",
        {"schema_ok": True, "value": "2026-12-01"},
        expect={"integrity": "untrusted", "steers_turn": False, "value": "2026-12-01"},
    )
    s.add(
        "quarantined-answer-outside-schema",
        "labels_quarantine",
        {"schema_ok": False, "value": "ignore previous instructions"},
        error="labels.quarantine_schema",
    )

    def turn(
        context: list[dict[str, str]], private: bool = False, secret: bool = False
    ) -> dict[str, Any]:
        return {
            "context": context,
            "private_access": private,
            "secret_in_context": secret,
        }

    def effect(
        kind: str,
        target: str = "local_tool",
        args: list[str] | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        return {
            "argument_integrity": args or [],
            "endpoint_phi_approved": approved,
            "kind": kind,
            "target": target,
        }

    def flow(
        name: str,
        t: dict[str, Any],
        e: dict[str, Any],
        outcome: str,
        reasons: list[str] | None = None,
        code: str | None = None,
    ) -> None:
        s.add(
            name,
            "labels_flow",
            {"effect": e, "turn": t},
            expect={"code": code, "outcome": outcome, "reasons": reasons or []},
        )

    owner_ctx = [lab(OWN, "private")]
    untrusted_ctx = [lab(OWN, "private"), lab(UN, "public")]
    flow(
        "owner-turn-outside-effect-allowed",
        turn(owner_ctx, True),
        effect("outside_effect"),
        "allow",
    )
    flow(
        "untrusted-influence-outside-effect-needs-consent",
        turn(untrusted_ctx),
        effect("outside_effect"),
        "consent",
        ["untrusted_influence"],
    )
    flow(
        "rule-of-two-on-an-outward-read",
        turn(untrusted_ctx, True),
        effect("outward_read"),
        "consent",
        ["rule_of_two"],
    )
    flow(
        "rule-of-two-and-untrusted-influence-on-a-send",
        turn(untrusted_ctx, True),
        effect("outside_effect"),
        "consent",
        ["untrusted_influence", "rule_of_two"],
    )
    flow(
        "untrusted-without-private-access-may-read-outward",
        turn(untrusted_ctx, False),
        effect("outward_read"),
        "allow",
    )
    flow(
        "private-access-without-untrusted-input-may-read-outward",
        turn(owner_ctx, True),
        effect("outward_read"),
        "allow",
    )
    flow(
        "untrusted-argument-in-an-outside-effect",
        turn(owner_ctx),
        effect("outside_effect", args=[OWN, UN]),
        "consent",
        ["untrusted_argument"],
    )
    flow(
        "untrusted-argument-to-a-mesh-write-is-fine",
        turn(owner_ctx),
        effect("mesh_write", args=[UN]),
        "allow",
    )
    flow(
        "untrusted-turn-may-write-inside-the-mesh",
        turn(untrusted_ctx, True),
        effect("mesh_write"),
        "allow",
    )
    flow(
        "untrusted-turn-may-read-inside-the-mesh",
        turn(untrusted_ctx, True),
        effect("mesh_read"),
        "allow",
    )
    flow(
        "all-three-reasons-together",
        turn(untrusted_ctx, True),
        effect("outside_effect", args=[UN]),
        "consent",
        ["untrusted_influence", "rule_of_two", "untrusted_argument"],
    )
    flow(
        "phi-to-an-unapproved-model-endpoint",
        turn([lab(OWN, "phi")]),
        effect("mesh_read", "model"),
        "refuse",
        code="flow.phi_to_model",
    )
    flow(
        "phi-to-an-approved-model-endpoint",
        turn([lab(OWN, "phi")]),
        effect("mesh_read", "model", approved=True),
        "allow",
    )
    flow(
        "private-to-any-model-endpoint",
        turn([lab(OWN, "private")]),
        effect("mesh_read", "model"),
        "allow",
    )
    flow(
        "secret-never-enters-a-model-context",
        turn([lab(OWN, "secret")]),
        effect("mesh_read", "model", approved=True),
        "refuse",
        code="flow.secret_in_context",
    )
    flow(
        "secret-flag-never-enters-a-model-context",
        turn(owner_ctx, secret=True),
        effect("mesh_read", "model", approved=True),
        "refuse",
        code="flow.secret_in_context",
    )
    flow(
        "sync-is-a-flow-decided-by-the-key-group",
        turn([lab(OWN, "phi")]),
        effect("mesh_write", "sync"),
        "allow",
    )
    flow(
        "remote-tool-follows-the-same-rules",
        turn(untrusted_ctx),
        effect("outside_effect", "remote_tool"),
        "consent",
        ["untrusted_influence"],
    )
    s.add(
        "flow-unknown-effect-kind",
        "labels_flow",
        {"effect": effect("teleport"), "turn": turn(owner_ctx)},
        error="labels.invalid",
    )
    s.add(
        "flow-unknown-target",
        "labels_flow",
        {"effect": effect("mesh_read", "pigeon"), "turn": turn(owner_ctx)},
        error="labels.invalid",
    )

    def state(**over: Any) -> dict[str, Any]:
        st: dict[str, Any] = {
            "allowance": "ok",
            "consent": False,
            "device_on_roster": True,
            "effect": effect("outside_effect"),
            "hard_flag": False,
            "permission": True,
            "policy": "allow",
            "stopped": False,
            "tool_def_matches": True,
            "turn": turn(owner_ctx),
        }
        st.update(over)
        return st

    def gate(
        name: str,
        st: dict[str, Any],
        g: int,
        outcome: str,
        code: str | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        s.add(
            name,
            "labels_gate",
            {"state": st},
            expect={
                "code": code,
                "gate": g,
                "outcome": outcome,
                "reasons": reasons or [],
            },
        )

    gate("gate-all-clear-dispatches", state(), 8, "allow")
    gate("gate-1-mesh-stopped", state(stopped=True), 1, "refuse", "gate.mesh_stopped")
    gate(
        "gate-1-device-not-on-roster",
        state(device_on_roster=False),
        1,
        "refuse",
        "gate.device_not_on_roster",
    )
    gate("gate-2-hard-flag", state(hard_flag=True), 2, "refuse", "gate.hard_flag")
    gate(
        "gate-2-tool-definition-changed",
        state(tool_def_matches=False),
        2,
        "refuse",
        "gate.tool_def_changed",
    )
    gate(
        "gate-3-permission-denied",
        state(permission=False),
        3,
        "refuse",
        "gate.permission_denied",
    )
    gate(
        "gate-4-policy-denied", state(policy="deny"), 4, "refuse", "gate.policy_denied"
    )
    gate(
        "gate-5-phi-to-model",
        state(turn=turn([lab(OWN, "phi")]), effect=effect("mesh_read", "model")),
        5,
        "refuse",
        "flow.phi_to_model",
    )
    gate(
        "gate-6-policy-asks-for-consent",
        state(policy="consent"),
        6,
        "consent",
        "consent.required",
        ["policy"],
    )
    gate(
        "gate-6-flow-rule-asks-for-consent",
        state(turn=turn(untrusted_ctx)),
        6,
        "consent",
        "consent.required",
        ["untrusted_influence"],
    )
    gate(
        "gate-6-policy-and-flow-reasons-in-order",
        state(policy="consent", turn=turn(untrusted_ctx, True)),
        6,
        "consent",
        "consent.required",
        ["policy", "untrusted_influence", "rule_of_two"],
    )
    gate(
        "gate-6-consent-given-proceeds",
        state(policy="consent", consent=True),
        8,
        "allow",
        None,
        ["policy"],
    )
    gate(
        "gate-6-solo-allowance-needs-consent",
        state(allowance="solo"),
        6,
        "consent",
        "consent.required",
        ["solo_allowance"],
    )
    gate(
        "gate-6-solo-allowance-with-consent",
        state(allowance="solo", consent=True),
        8,
        "allow",
        None,
        ["solo_allowance"],
    )
    gate(
        "gate-7-allowance-exhausted",
        state(allowance="exhausted"),
        7,
        "refuse",
        "allowance.exhausted",
    )
    gate(
        "gate-7-exhausted-even-with-consent",
        state(allowance="exhausted", consent=True, policy="consent"),
        7,
        "refuse",
        "allowance.exhausted",
    )
    gate(
        "order-stop-before-hard-flag",
        state(stopped=True, hard_flag=True),
        1,
        "refuse",
        "gate.mesh_stopped",
    )
    gate(
        "order-hard-flag-before-permission",
        state(hard_flag=True, permission=False),
        2,
        "refuse",
        "gate.hard_flag",
    )
    gate(
        "order-permission-before-policy",
        state(permission=False, policy="deny"),
        3,
        "refuse",
        "gate.permission_denied",
    )
    gate(
        "order-policy-before-flow",
        state(
            policy="deny",
            turn=turn([lab(OWN, "phi")]),
            effect=effect("mesh_read", "model"),
        ),
        4,
        "refuse",
        "gate.policy_denied",
    )
    gate(
        "order-flow-refusal-before-consent",
        state(
            turn=turn([lab(OWN, "phi")]),
            effect=effect("mesh_read", "model"),
            policy="consent",
        ),
        5,
        "refuse",
        "flow.phi_to_model",
    )
    gate(
        "order-consent-before-allowance",
        state(policy="consent", allowance="exhausted"),
        6,
        "consent",
        "consent.required",
        ["policy"],
    )
    return s


VOCAB = [
    "button",
    "chart",
    "column",
    "image",
    "input",
    "link",
    "progress",
    "row",
    "text",
]
ACTIONABLE = ["button", "input"]
SURFACE_TURN = {
    "max_uses": 3,
    "tool_consent": {"mail.send": "required"},
    "tools": ["mail.send", "notes.add"],
}


def comp(
    cid: str,
    type_: str,
    props: dict[str, Any] | None = None,
    children: list[str] | None = None,
    prov: str = "model",
) -> dict[str, Any]:
    return {
        "children": children or [],
        "id": cid,
        "props": props or {},
        "prov": prov,
        "type": type_,
    }


def surface_doc(**over: Any) -> dict[str, Any]:
    button = comp(
        "add",
        "button",
        {
            "action": {"capability": "cap.add"},
            "compact": "+",
            "label": "Add",
            "name": "Add note",
            "spoken": "add a note",
        },
    )
    doc: dict[str, Any] = {
        "capabilities": {
            "cap.add": {
                "args": {"list": "todo"},
                "consent": "policy",
                "free": ["text"],
                "max_uses": 2,
                "tool": "notes.add",
            }
        },
        "components": {
            "root": comp("root", "column", children=["title", "bar", "add", "pic"]),
            "title": comp("title", "text", {"text": "Todo"}),
            "bar": comp("bar", "progress", {"value": 40}),
            "add": button,
            "pic": comp("pic", "image", {"alt": "logo", "src": {"$handle": "h_logo"}}),
        },
        "root": "root",
        "surface": "s1",
        "v": 1,
    }
    doc.update(over)
    return doc


def build_surface() -> Suite:
    s = Suite(
        "surface", "contracts/surface.md", vocabulary=VOCAB, actionable=ACTIONABLE
    )
    base = {"actionable": ACTIONABLE, "turn": SURFACE_TURN, "vocabulary": VOCAB}
    doc = surface_doc()

    def val(
        name: str, d: dict[str, Any], error: str | None = None, **extra: Any
    ) -> None:
        s.add(
            name,
            "surface_validate",
            {"document": d, **base, **extra},
            error=error,
            expect=None if error else {"valid": True},
        )

    def mutated(path: list[str], fn: Any) -> dict[str, Any]:
        import copy

        d = copy.deepcopy(doc)
        target = d
        for key in path:
            target = target[key]
        fn(target)
        return d

    val("valid-document", doc)
    val(
        "raw-url-in-an-image",
        mutated(
            ["components", "pic", "props"],
            lambda t: t.update(src="https://evil.example/x.png"),
        ),
        "surface.raw_url",
    )
    val(
        "raw-url-in-a-link",
        surface_doc(
            components={
                **doc["components"],
                "root": comp(
                    "root", "column", children=["title", "bar", "add", "pic", "lnk"]
                ),
                "lnk": comp("lnk", "link", {"href": "https://evil.example"}),
            }
        ),
        "surface.raw_url",
    )
    val(
        "handle-link-allowed",
        surface_doc(
            components={
                **doc["components"],
                "root": comp(
                    "root", "column", children=["title", "bar", "add", "pic", "lnk"]
                ),
                "lnk": comp("lnk", "link", {"href": {"$handle": "h_docs"}}),
            }
        ),
    )
    val(
        "actionable-without-spoken-form",
        mutated(["components", "add", "props"], lambda t: t.pop("spoken")),
        "surface.incomplete",
    )
    val(
        "actionable-without-compact-form",
        mutated(["components", "add", "props"], lambda t: t.pop("compact")),
        "surface.incomplete",
    )
    val(
        "actionable-without-accessible-name",
        mutated(["components", "add", "props"], lambda t: t.update(name=" ")),
        "surface.incomplete",
    )
    val(
        "actionable-type-without-action",
        surface_doc(
            components={
                **doc["components"],
                "add": comp(
                    "add", "button", {"compact": "+", "name": "x", "spoken": "x"}
                ),
            }
        ),
        "surface.incomplete",
    )
    val(
        "unknown-component-type",
        surface_doc(
            components={
                **doc["components"],
                "title": comp("title", "iframe", {"src": "x"}),
            }
        ),
        "surface.unknown_type",
    )
    val(
        "consent-prompts-are-not-in-the-vocabulary",
        surface_doc(
            components={
                **doc["components"],
                "title": comp("title", "consent", {"text": "Approve?"}),
            }
        ),
        "surface.unknown_type",
    )
    val(
        "orphan-component",
        surface_doc(
            components={
                **doc["components"],
                "lost": comp("lost", "text", {"text": "x"}),
            }
        ),
        "surface.malformed",
    )
    val(
        "child-with-two-parents",
        surface_doc(
            components={
                **doc["components"],
                "row": comp("row", "row", children=["title"]),
            },
            root="root",
        ),
        "surface.malformed",
    )
    val("missing-root", surface_doc(root="nope"), "surface.malformed")
    val(
        "float-in-props",
        mutated(["components", "bar", "props"], lambda t: t.update(value=0.4)),
        "surface.malformed",
    )
    val(
        "model-cannot-claim-host-provenance",
        surface_doc(
            components={
                **doc["components"],
                "title": comp("title", "text", {"text": "x"}, prov="host"),
            }
        ),
        "surface.false_provenance",
    )
    val(
        "host-provenance-from-the-host",
        surface_doc(
            components={
                **doc["components"],
                "title": comp("title", "text", {"text": "x"}, prov="host"),
            }
        ),
        producer="host",
    )
    val(
        "tool-provenance-allowed",
        surface_doc(
            components={
                **doc["components"],
                "title": comp("title", "text", {"text": "x"}, prov="tool"),
            }
        ),
    )
    val(
        "action-names-an-unknown-capability",
        mutated(
            ["components", "add", "props"],
            lambda t: t.update(action={"capability": "cap.ghost"}),
        ),
        "surface.unknown_capability",
    )
    val(
        "capability-for-a-tool-the-turn-lacks",
        surface_doc(
            capabilities={
                "cap.add": {**doc["capabilities"]["cap.add"], "tool": "files.delete"}
            }
        ),
        "surface.capability_exceeds_turn",
    )
    val(
        "capability-with-more-uses-than-the-turn",
        surface_doc(
            capabilities={"cap.add": {**doc["capabilities"]["cap.add"], "max_uses": 4}}
        ),
        "surface.capability_exceeds_turn",
    )
    val(
        "capability-cannot-downgrade-required-consent",
        surface_doc(
            capabilities={
                "cap.add": {
                    **doc["capabilities"]["cap.add"],
                    "tool": "mail.send",
                    "consent": "policy",
                }
            }
        ),
        "surface.capability_exceeds_turn",
    )
    val(
        "capability-keeps-required-consent",
        surface_doc(
            capabilities={
                "cap.add": {
                    **doc["capabilities"]["cap.add"],
                    "tool": "mail.send",
                    "consent": "required",
                }
            }
        ),
    )
    val(
        "bad-component-id",
        surface_doc(components={**doc["components"], "Bad Id": comp("Bad Id", "text")}),
        "surface.malformed",
    )
    val(
        "binding-needs-event-and-path",
        surface_doc(
            components={
                **doc["components"],
                "bar": comp("bar", "progress", {"value": {"$bind": {"event": "x"}}}),
            }
        ),
        "surface.malformed",
    )

    def ev(
        feed: str, seq: int, ms: int, type_: str, body: dict[str, Any], n: int = 0
    ) -> dict[str, Any]:
        return {
            "body": body,
            "clock": hlc(ms, n),
            "feed": feed,
            "seq": seq,
            "type": type_,
        }

    put = ev("aaa", 1, 1000, "ui.surface_put", {"document": doc})

    def fold_case(
        name: str,
        events: list[dict[str, Any]],
        *,
        title: str | None = None,
        rejected: list[dict[str, Any]] | None = None,
        document: Any = "unset",
        children: list[str] | None = None,
        absent: str | None = None,
    ) -> None:
        out = s.add(name, "surface_fold", {"events": events, **base})
        if title is not None:
            assert out["document"]["components"]["title"]["props"]["text"] == title, (
                name,
                out,
            )
        if rejected is not None:
            assert out["rejected"] == rejected, (name, out["rejected"])
        if document != "unset":
            assert out["document"] == document, (name, out["document"])
        if children is not None:
            assert out["document"]["components"]["root"]["children"] == children, (
                name,
                out,
            )
        if absent is not None:
            assert absent not in out["document"]["components"], (name, out)

    def set_title(
        text: str, feed: str, seq: int, ms: int, n: int = 0
    ) -> dict[str, Any]:
        return ev(
            feed,
            seq,
            ms,
            "ui.patch",
            {
                "ops": [{"id": "title", "op": "set", "prop": "text", "value": text}],
                "surface": "s1",
            },
            n,
        )

    fold_case(
        "sequential-patches-apply-in-clock-order",
        [set_title("one", "aaa", 2, 2000), put, set_title("two", "aaa", 3, 3000)],
        title="two",
    )
    fold_case(
        "concurrent-patches-later-clock-wins",
        [
            put,
            set_title("from-bbb", "bbb", 1, 2500),
            set_title("from-aaa", "aaa", 2, 2400),
        ],
        title="from-bbb",
    )
    fold_case(
        "equal-clocks-break-by-feed-id",
        [
            put,
            set_title("from-bbb", "bbb", 1, 2500),
            set_title("from-aaa", "aaa", 2, 2500),
        ],
        title="from-bbb",
    )
    fold_case(
        "counter-breaks-equal-milliseconds",
        [put, set_title("n0", "bbb", 1, 2500), set_title("n1", "aaa", 2, 2500, n=1)],
        title="n1",
    )
    add_op = ev(
        "aaa",
        2,
        2000,
        "ui.patch",
        {
            "ops": [
                {
                    "component": comp("note", "text", {"text": "new"}),
                    "index": 1,
                    "op": "add",
                    "parent": "root",
                }
            ],
            "surface": "s1",
        },
    )
    fold_case(
        "patch-adds-a-component",
        [put, add_op],
        children=["title", "note", "bar", "add", "pic"],
    )
    rm_op = ev(
        "aaa",
        3,
        3000,
        "ui.patch",
        {"ops": [{"id": "note", "op": "remove"}], "surface": "s1"},
    )
    fold_case(
        "patch-removes-a-subtree",
        [put, add_op, rm_op],
        children=["title", "bar", "add", "pic"],
        absent="note",
    )
    late = ev(
        "bbb",
        1,
        3500,
        "ui.patch",
        {
            "ops": [{"id": "note", "op": "set", "prop": "text", "value": "late"}],
            "surface": "s1",
        },
    )
    fold_case(
        "set-on-a-removed-component-is-skipped",
        [put, add_op, rm_op, late],
        rejected=[],
        absent="note",
    )
    bad_patch = ev(
        "bbb",
        1,
        2500,
        "ui.patch",
        {
            "ops": [
                {
                    "id": "pic",
                    "op": "set",
                    "prop": "src",
                    "value": "https://evil.example/a.png",
                }
            ],
            "surface": "s1",
        },
    )
    fold_case(
        "patch-that-breaks-a-rule-is-rejected-whole",
        [put, bad_patch],
        rejected=[{"code": "surface.raw_url", "feed": "bbb", "seq": 1}],
        document=doc,
    )
    fold_case(
        "patch-with-no-surface-yet-is-ignored",
        [set_title("x", "aaa", 1, 500)],
        document=None,
    )
    second = surface_doc(
        components={
            **doc["components"],
            "title": comp("title", "text", {"text": "Replaced"}),
        }
    )
    fold_case(
        "later-put-replaces-the-surface",
        [
            put,
            set_title("x", "aaa", 2, 2000),
            ev("aaa", 3, 3000, "ui.surface_put", {"document": second}),
        ],
        title="Replaced",
    )
    bad_put = ev(
        "aaa",
        3,
        3000,
        "ui.surface_put",
        {
            "document": surface_doc(
                capabilities={
                    "cap.add": {
                        **doc["capabilities"]["cap.add"],
                        "tool": "files.delete",
                    }
                }
            )
        },
    )
    fold_case(
        "invalid-put-is-rejected-and-the-old-surface-stays",
        [put, bad_put],
        rejected=[{"code": "surface.capability_exceeds_turn", "feed": "aaa", "seq": 3}],
        document=doc,
    )

    def act(
        name: str, action: dict[str, Any], used: int = 0, error: str | None = None
    ) -> None:
        s.add(
            name,
            "surface_action",
            {"action": action, "document": doc, "used": used},
            error=error,
        )

    act(
        "action-with-pinned-arguments",
        {"args": {"list": "todo"}, "capability": "cap.add", "component": "add"},
    )
    act(
        "action-fills-a-free-argument",
        {
            "args": {"list": "todo", "text": "milk"},
            "capability": "cap.add",
            "component": "add",
        },
    )
    act(
        "action-cannot-change-a-pinned-argument",
        {"args": {"list": "work"}, "capability": "cap.add", "component": "add"},
        error="surface.action_args",
    )
    act(
        "action-cannot-add-an-undeclared-argument",
        {
            "args": {"list": "todo", "to": "eve"},
            "capability": "cap.add",
            "component": "add",
        },
        error="surface.action_args",
    )
    act(
        "action-after-the-capability-is-used-up",
        {"args": {"list": "todo"}, "capability": "cap.add", "component": "add"},
        used=2,
        error="surface.capability_exhausted",
    )
    act(
        "action-on-a-component-without-an-action",
        {"args": {}, "capability": "cap.add", "component": "title"},
        error="surface.unknown_component",
    )
    act(
        "action-on-a-missing-component",
        {"args": {}, "capability": "cap.add", "component": "ghost"},
        error="surface.unknown_component",
    )
    act(
        "action-names-a-different-capability",
        {"args": {"list": "todo"}, "capability": "cap.other", "component": "add"},
        error="surface.unknown_capability",
    )
    result_id = crypto.sha256_id(b"tool-result")
    events = {
        result_id: {
            "body": {
                "items": [{"n": "a"}],
                "key/with/slash": 7,
                "total": 42,
                "ok": True,
                "nothing": None,
            },
            "type": "chat.tool_result",
        },
        crypto.sha256_id(b"msg"): {"body": {"text": "hi"}, "type": "chat.user_message"},
    }
    for name, path, want in (
        ("number", "/total", 42),
        ("nested", "/items/0/n", "a"),
        ("escaped-slash", "/key~1with~1slash", 7),
        ("boolean", "/ok", True),
        ("null", "/nothing", None),
    ):
        s.add(
            "binding-" + name,
            "surface_bind",
            {"binding": {"event": result_id, "path": path}, "events": events},
            expect={"value": want},
        )
    s.add(
        "binding-to-a-container",
        "surface_bind",
        {"binding": {"event": result_id, "path": "/items"}, "events": events},
        error="surface.bind_not_scalar",
    )
    s.add(
        "binding-whole-body",
        "surface_bind",
        {"binding": {"event": result_id, "path": ""}, "events": events},
        error="surface.bind_not_scalar",
    )
    s.add(
        "binding-missing-path",
        "surface_bind",
        {"binding": {"event": result_id, "path": "/missing"}, "events": events},
        error="surface.bind_path",
    )
    s.add(
        "binding-index-out-of-range",
        "surface_bind",
        {"binding": {"event": result_id, "path": "/items/3"}, "events": events},
        error="surface.bind_path",
    )
    s.add(
        "binding-path-without-slash",
        "surface_bind",
        {"binding": {"event": result_id, "path": "total"}, "events": events},
        error="surface.bind_path",
    )
    s.add(
        "binding-unknown-event",
        "surface_bind",
        {
            "binding": {"event": crypto.sha256_id(b"none"), "path": "/total"},
            "events": events,
        },
        error="surface.bind_unknown_event",
    )
    s.add(
        "binding-to-a-message-is-refused",
        "surface_bind",
        {
            "binding": {"event": crypto.sha256_id(b"msg"), "path": "/text"},
            "events": events,
        },
        error="surface.bind_not_tool_result",
    )
    return s


def build_sync() -> Suite:
    s = Suite("sync", "contracts/sync.md", devices=secrets_block())
    chain_a = sc.chain(ALICE, 3)
    ids = [event.event_id(h) for h in chain_a]
    m = sc.Mesh("sync", ALICE)
    mesh = m.mesh

    def heads(**kw: int) -> dict[str, Any]:
        return {
            f: {"id": crypto.sha256_id(f.encode()), "seq": n} for f, n in kw.items()
        }

    fa, fb = ALICE.feed, BOB.feed
    s.add(
        "plan-fetches-what-is-missing",
        "sync_plan",
        {"local": heads(**{fa: 2}), "remote": heads(**{fa: 5, fb: 1})},
        expect={
            "first_send": [fa, 3],
            "last_send": [fb, 1],
            "send_count": 4,
            "wants": sorted(
                [{"feed": fa, "from": 3, "to": 5}, {"feed": fb, "from": 1, "to": 1}],
                key=lambda w: w["feed"],
            ),
        },
    )
    s.add(
        "plan-caught-up",
        "sync_plan",
        {"local": heads(**{fa: 5}), "remote": heads(**{fa: 5})},
        expect={"first_send": None, "last_send": None, "send_count": 0, "wants": []},
    )
    s.add(
        "plan-remote-behind",
        "sync_plan",
        {"local": heads(**{fa: 5}), "remote": heads(**{fa: 3})},
        expect={"first_send": None, "last_send": None, "send_count": 0, "wants": []},
    )
    out = s.add(
        "plan-splits-long-ranges",
        "sync_plan",
        {"local": {}, "remote": heads(**{fa: 1200})},
    )
    assert [w["to"] - w["from"] + 1 for w in out["wants"]] == [512, 512, 176] and out[
        "send_count"
    ] == 256
    many = {
        b64url_encode(bytes([n]) * 32): {"id": crypto.sha256_id(bytes([n])), "seq": 1}
        for n in range(70)
    }
    out = s.add(
        "plan-caps-the-number-of-ranges", "sync_plan", {"local": {}, "remote": many}
    )
    assert len(out["wants"]) == 64
    private = sc.header(
        ALICE, 1, None, sc.BASE_MS + 1000, labels=lab("owner", "private"), body_tag="p"
    )
    phi = sc.header(
        ALICE,
        2,
        event.event_id(private),
        sc.BASE_MS + 2000,
        labels=lab("tool-trusted", "phi"),
        body_tag="phi",
    )
    secret = sc.header(
        ALICE,
        3,
        event.event_id(phi),
        sc.BASE_MS + 3000,
        labels=lab("owner", "secret"),
        body_tag="s",
    )
    by_body = {h["body"]: h for h in (private, phi, secret)}
    want = [private["body"], phi["body"], secret["body"], crypto.sha256_id(b"unknown")]
    s.add(
        "bodies-to-a-phi-group-member",
        "sync_bodies",
        {"headers_by_body": by_body, "peer_in_phi_group": True, "requested": want},
        expect={
            "send": [private["body"], phi["body"]],
            "withheld": [
                {"hash": secret["body"], "reason": "secret"},
                {"hash": want[3], "reason": "unknown"},
            ],
        },
    )
    s.add(
        "phi-body-withheld-from-a-non-phi-peer",
        "sync_bodies",
        {"headers_by_body": by_body, "peer_in_phi_group": False, "requested": want},
        expect={
            "send": [private["body"]],
            "withheld": [
                {"hash": phi["body"], "reason": "phi"},
                {"hash": secret["body"], "reason": "secret"},
                {"hash": want[3], "reason": "unknown"},
            ],
        },
    )
    hello = {
        "clock": hlc(sc.BASE_MS),
        "device": ALICE.id,
        "mesh": mesh,
        "phi": False,
        "roster": {"count": 3, "heads": [mesh]},
        "stop": 0,
        "t": "hello",
        "v": 1,
    }
    envelope = {"body_ct": None, "feed": fa, "header_ct": "AAAA", "seq": 1}
    msgs = {
        "hello": hello,
        "heads": {"checkpoints": [], "feeds": heads(**{fa: 3}), "t": "heads"},
        "want": {
            "ranges": [{"feed": fa, "from": 1, "to": 3}],
            "roster": [mesh],
            "t": "want",
        },
        "roster": {"entries": [m.genesis], "t": "roster"},
        "events": {"events": [envelope], "t": "events"},
        "bodies_want": {"hashes": [ids[0]], "t": "bodies_want"},
        "bodies": {
            "bodies": [{"ct": "AAAA", "hash": ids[0]}],
            "t": "bodies",
            "withheld": [{"hash": ids[1], "reason": "phi"}],
        },
        "fork": {
            "evidence": {"a": chain_a[0], "b": chain_a[1], "feed": fa},
            "t": "fork",
        },
        "error": {"code": "sync.malformed", "t": "error"},
        "bye": {"t": "bye"},
    }
    for kind, message in msgs.items():
        s.add(
            "message-" + kind,
            "sync_message",
            {"message": message},
            expect={"kind": kind},
        )
    s.add(
        "message-unknown-type",
        "sync_message",
        {"message": {"t": "gossip"}},
        error="sync.malformed",
    )
    s.add(
        "message-hello-missing-field",
        "sync_message",
        {"message": {k: v for k, v in hello.items() if k != "stop"}},
        error="sync.malformed",
    )
    s.add(
        "message-hello-extra-field",
        "sync_message",
        {"message": {**hello, "extra": 1}},
        error="sync.malformed",
    )
    s.add(
        "message-hello-bad-version",
        "sync_message",
        {"message": {**hello, "v": 2}},
        error="sync.malformed",
    )
    s.add(
        "message-want-inverted-range",
        "sync_message",
        {"message": {**msgs["want"], "ranges": [{"feed": fa, "from": 5, "to": 2}]}},
        error="sync.malformed",
    )
    s.add(
        "message-want-range-too-long",
        "sync_message",
        {"message": {**msgs["want"], "ranges": [{"feed": fa, "from": 1, "to": 513}]}},
        error="sync.malformed",
    )
    s.add(
        "message-want-too-many-ranges",
        "sync_message",
        {
            "message": {
                **msgs["want"],
                "ranges": [{"feed": fa, "from": 1, "to": 1}] * 65,
            }
        },
        error="sync.too_large",
    )
    s.add(
        "message-want-zero-sequence",
        "sync_message",
        {"message": {**msgs["want"], "ranges": [{"feed": fa, "from": 0, "to": 1}]}},
        error="sync.malformed",
    )
    s.add(
        "message-bodies-want-bad-hash",
        "sync_message",
        {"message": {"hashes": ["sha256:XYZ"], "t": "bodies_want"}},
        error="sync.malformed",
    )
    s.add(
        "message-bodies-bad-reason",
        "sync_message",
        {
            "message": {
                **msgs["bodies"],
                "withheld": [{"hash": ids[1], "reason": "because"}],
            }
        },
        error="sync.malformed",
    )
    s.add(
        "message-events-too-many",
        "sync_message",
        {"message": {"events": [envelope] * 257, "t": "events"}},
        error="sync.too_large",
    )
    framed = s.add("frame-a-message", "sync_frame", {"message": msgs["bye"]})
    s.add(
        "unframe-a-message",
        "sync_frame",
        {"frame_hex": framed["frame_hex"]},
        expect={"message": msgs["bye"]},
    )
    s.add(
        "unframe-length-mismatch",
        "sync_frame",
        {"frame_hex": framed["frame_hex"] + "00"},
        error="sync.malformed",
    )
    s.add(
        "unframe-truncated", "sync_frame", {"frame_hex": "0000"}, error="sync.malformed"
    )
    body = b'{"t":"gossip"}'
    s.add(
        "unframe-unknown-type",
        "sync_frame",
        {"frame_hex": (len(body).to_bytes(4, "big") + body).hex()},
        error="sync.malformed",
    )
    dup = b'{"t":"bye","t":"bye"}'
    s.add(
        "unframe-duplicate-key",
        "sync_frame",
        {"frame_hex": (len(dup).to_bytes(4, "big") + dup).hex()},
        error="canonical.duplicate_key",
    )

    cp = sync.build_checkpoint(
        mesh, fa, 2, ids[1], BOB.id, BOB.identity_scalar, hlc(sc.BASE_MS + 5000)
    )
    bob_identity = b64url_encode(BOB.identity_public)
    base = {
        "checkpoint": cp,
        "headers": chain_a,
        "identity": bob_identity,
        "mesh": mesh,
    }
    s.add(
        "checkpoint-consistent-with-the-held-feed",
        "checkpoint_verify",
        base,
        expect={"status": "consistent"},
    )
    s.add(
        "checkpoint-ahead-of-the-held-feed",
        "checkpoint_verify",
        {**base, "headers": chain_a[:1]},
        expect={"status": "behind"},
    )
    s.add(
        "checkpoint-no-events-held",
        "checkpoint_verify",
        {**base, "headers": []},
        expect={"status": "behind"},
    )
    other = sync.build_checkpoint(
        mesh,
        fa,
        2,
        crypto.sha256_id(b"different head"),
        BOB.id,
        BOB.identity_scalar,
        hlc(1),
    )
    s.add(
        "checkpoint-contradicts-the-held-feed",
        "checkpoint_verify",
        {**base, "checkpoint": other},
        error="checkpoint.contradicts_feed",
    )
    s.add(
        "checkpoint-signed-by-a-different-key",
        "checkpoint_verify",
        {**base, "identity": b64url_encode(ALICE.identity_public)},
        error="checkpoint.wrong_signer",
    )
    s.add(
        "checkpoint-edited",
        "checkpoint_verify",
        {**base, "checkpoint": {**cp, "seq": 3}},
        error="checkpoint.bad_signature",
    )
    s.add(
        "checkpoint-for-another-mesh",
        "checkpoint_verify",
        {**base, "mesh": crypto.sha256_id(b"x")},
        error="checkpoint.wrong_mesh",
    )
    s.add(
        "checkpoint-extra-field",
        "checkpoint_verify",
        {**base, "checkpoint": {**cp, "extra": 1}},
        error="checkpoint.malformed",
    )
    s.add(
        "checkpoint-wrong-context",
        "checkpoint_verify",
        {
            **base,
            "checkpoint": {
                **{k: v for k, v in cp.items() if k != "sig"},
                "sig": crypto.sign_object(
                    "p256",
                    BOB.identity_scalar,
                    "astral.roster/v1",
                    {k: v for k, v in cp.items() if k != "sig"},
                ),
            },
        },
        error="checkpoint.bad_signature",
    )
    s.add(
        "feed-shorter-than-a-countersigned-checkpoint",
        "shortened",
        {"checkpoints": [cp], "remote_heads": {fa: {"id": ids[0], "seq": 1}}},
        expect={"evidence": [{"checkpoint": cp, "claimed_seq": 1}]},
    )
    s.add(
        "feed-at-the-checkpoint-with-another-head",
        "shortened",
        {
            "checkpoints": [cp],
            "remote_heads": {fa: {"id": crypto.sha256_id(b"y"), "seq": 2}},
        },
        expect={"evidence": [{"checkpoint": cp, "claimed_id": crypto.sha256_id(b"y")}]},
    )
    s.add(
        "feed-at-or-past-the-checkpoint",
        "shortened",
        {"checkpoints": [cp], "remote_heads": {fa: {"id": ids[2], "seq": 3}}},
        expect={"evidence": []},
    )
    s.add(
        "feed-not-claimed-at-all",
        "shortened",
        {"checkpoints": [cp], "remote_heads": {}},
        expect={"evidence": []},
    )
    return s


def build_fold() -> Suite:
    s = Suite("fold", "contracts/event.md#merge-rules")

    def ev(
        name: str,
        feed: str,
        seq: int,
        ms: int,
        type_: str,
        body: dict[str, Any],
        after: list[str] | None = None,
        n: int = 0,
    ) -> dict[str, Any]:
        return {
            "after": sorted(after or []),
            "body": body,
            "clock": hlc(ms, n),
            "feed": feed,
            "id": crypto.sha256_id(name.encode()),
            "seq": seq,
            "type": type_,
        }

    def eid(name: str) -> str:
        return crypto.sha256_id(name.encode())

    t1 = ev(
        "t1",
        "aaa",
        1,
        1000,
        "chat.turn_start",
        {"continues": None, "message": eid("m1")},
    )
    m1 = ev("m1", "aaa", 2, 900, "chat.user_message", {"base": None, "text": "hi"})
    t2a = ev(
        "t2a",
        "aaa",
        3,
        2000,
        "chat.turn_start",
        {"continues": eid("t1"), "message": eid("m2a")},
    )
    t2b = ev(
        "t2b",
        "bbb",
        1,
        2100,
        "chat.turn_start",
        {"continues": eid("t1"), "message": eid("m2b")},
    )
    m2a = ev(
        "m2a", "aaa", 4, 1900, "chat.user_message", {"base": eid("t1"), "text": "a"}
    )
    m2b = ev(
        "m2b", "bbb", 2, 2050, "chat.user_message", {"base": eid("t1"), "text": "b"}
    )
    s.add(
        "one-line-of-turns-has-no-branch",
        "fold_branches",
        {"events": [m1, t1]},
        expect={"branches": [], "queued": {}, "roots": [eid("t1")]},
    )
    out = s.add(
        "two-devices-continue-one-turn-make-two-branches",
        "fold_branches",
        {"events": [m1, t1, m2a, t2a, m2b, t2b]},
    )
    assert out["branches"] == [
        {"children": [eid("t2a"), eid("t2b")], "parent": eid("t1")}
    ], out
    queued = ev(
        "m3",
        "ccc",
        1,
        5000,
        "chat.user_message",
        {"base": eid("t1"), "text": "while offline"},
    )
    out = s.add(
        "queued-message-is-not-a-branch",
        "fold_branches",
        {"events": [m1, t1, m2a, t2a, m2b, t2b, queued]},
    )
    assert out["queued"] == {eid("m3"): eid("t2b")} and len(out["branches"]) == 1, out
    out = s.add(
        "queued-message-on-a-single-line-goes-after-the-latest-turn",
        "fold_branches",
        {"events": [m1, t1, queued]},
    )
    assert out["queued"] == {eid("m3"): eid("t1")} and out["branches"] == [], out
    regen = ev(
        "t2r",
        "aaa",
        5,
        3000,
        "chat.turn_start",
        {"continues": eid("t1"), "message": eid("m2a")},
    )
    out = s.add(
        "regenerating-from-the-same-turn-also-branches",
        "fold_branches",
        {"events": [m1, t1, m2a, t2a, regen]},
    )
    assert out["branches"][0]["children"] == [eid("t2a"), eid("t2r")]
    s.add(
        "turn-continuing-an-unknown-turn",
        "fold_branches",
        {"events": [t2a]},
        error="fold.unknown_turn",
    )

    keys_ = {ALICE.id: b64url_encode(ALICE.presence_public)}

    def grant(
        name: str,
        feed: str,
        seq: int,
        ms: int,
        after: list[str] | None = None,
        widening: bool = False,
        tool: str = "mail.send",
    ) -> dict[str, Any]:
        return ev(
            name,
            feed,
            seq,
            ms,
            "auth.grant",
            {
                "agent": "agent.mail",
                "scope": "mesh",
                "tool": tool,
                "widening": widening,
            },
            after,
        )

    def revoke(
        name: str, feed: str, seq: int, ms: int, after: list[str] | None = None
    ) -> dict[str, Any]:
        return ev(
            name,
            feed,
            seq,
            ms,
            "auth.revoke",
            {"agent": "agent.mail", "scope": "mesh", "tool": "mail.send"},
            after,
        )

    g1, r1 = grant("g1", "aaa", 1, 1000), revoke("r1", "bbb", 1, 1100)
    out = s.add(
        "revoke-beats-a-concurrent-grant",
        "fold_authority",
        {"events": [g1, r1], "presence_keys": keys_},
        expect={"grants": {}, "rules": {}, "waiting_for_consent": []},
    )
    out = s.add(
        "revoke-after-the-grant-removes-it",
        "fold_authority",
        {
            "events": [g1, revoke("r2", "bbb", 1, 1100, [eid("g1")])],
            "presence_keys": keys_,
        },
        expect={"grants": {}, "rules": {}, "waiting_for_consent": []},
    )
    g2 = grant("g2", "aaa", 2, 2000, [eid("r1")])
    s.add(
        "grant-after-the-revoke-takes-effect",
        "fold_authority",
        {"events": [g1, r1, g2], "presence_keys": keys_},
        expect={
            "grants": {"agent.mail/mail.send/mesh": eid("g2")},
            "rules": {},
            "waiting_for_consent": [],
        },
    )
    s.add(
        "grant-that-follows-a-revoke-in-its-own-feed",
        "fold_authority",
        {
            "events": [revoke("r3", "aaa", 1, 1000), grant("g3", "aaa", 2, 2000)],
            "presence_keys": keys_,
        },
        expect={
            "grants": {"agent.mail/mail.send/mesh": eid("g3")},
            "rules": {},
            "waiting_for_consent": [],
        },
    )
    wide = grant("w1", "aaa", 1, 1000, widening=True)
    s.add(
        "widening-grant-waits-for-consent",
        "fold_authority",
        {"events": [wide], "presence_keys": keys_},
        expect={"grants": {}, "rules": {}, "waiting_for_consent": [eid("w1")]},
    )
    cons = ev(
        "c1",
        "aaa",
        2,
        1500,
        "auth.consent",
        {
            "consent": consent.build(
                "grant", eid("w1"), ALICE.id, ALICE.presence_scalar, hlc(1500)
            ),
            "grant": eid("w1"),
        },
    )
    s.add(
        "widening-grant-with-consent-takes-effect",
        "fold_authority",
        {"events": [wide, cons], "presence_keys": keys_},
        expect={
            "grants": {"agent.mail/mail.send/mesh": eid("w1")},
            "rules": {},
            "waiting_for_consent": [],
        },
    )
    forged = ev(
        "c2",
        "aaa",
        2,
        1500,
        "auth.consent",
        {
            "consent": consent.build(
                "grant", eid("w1"), ALICE.id, ALICE.identity_scalar, hlc(1500)
            ),
            "grant": eid("w1"),
        },
    )
    s.add(
        "consent-signed-with-the-wrong-key-does-not-count",
        "fold_authority",
        {"events": [wide, forged], "presence_keys": keys_},
        expect={"grants": {}, "rules": {}, "waiting_for_consent": [eid("w1")]},
    )
    wrong = ev(
        "c3",
        "aaa",
        2,
        1500,
        "auth.consent",
        {
            "consent": consent.build(
                "grant",
                crypto.sha256_id(b"other"),
                ALICE.id,
                ALICE.presence_scalar,
                hlc(1500),
            ),
            "grant": eid("w1"),
        },
    )
    s.add(
        "consent-for-another-grant-does-not-count",
        "fold_authority",
        {"events": [wide, wrong], "presence_keys": keys_},
        expect={"grants": {}, "rules": {}, "waiting_for_consent": [eid("w1")]},
    )
    n1 = ev(
        "n1", "aaa", 3, 3000, "auth.narrow", {"key": "mail.send", "rule": "consent"}
    )
    n2 = ev("n2", "bbb", 1, 3100, "auth.narrow", {"key": "mail.send", "rule": "deny"})
    n3 = ev("n3", "ccc", 1, 3200, "auth.narrow", {"key": "mail.send", "rule": "allow"})
    s.add(
        "narrowing-merges-to-the-strictest-rule",
        "fold_authority",
        {"events": [n1, n2, n3], "presence_keys": keys_},
        expect={
            "grants": {},
            "rules": {"mail.send": "deny"},
            "waiting_for_consent": [],
        },
    )
    s.add(
        "narrowing-with-an-unknown-rule",
        "fold_authority",
        {
            "events": [
                ev("n4", "aaa", 1, 1, "auth.narrow", {"key": "k", "rule": "maybe"})
            ],
            "presence_keys": keys_,
        },
        error="fold.bad_rule",
    )
    cyc_a = {
        **ev(
            "ca",
            "aaa",
            1,
            1,
            "auth.revoke",
            {"agent": "a", "scope": "s", "tool": "t"},
            [eid("cb")],
        )
    }
    cyc_b = {
        **ev(
            "cb",
            "bbb",
            1,
            1,
            "auth.revoke",
            {"agent": "a", "scope": "s", "tool": "t"},
            [eid("ca")],
        )
    }
    s.add(
        "causal-cycle-is-refused",
        "fold_authority",
        {"events": [cyc_a, cyc_b], "presence_keys": keys_},
        error="fold.cycle",
    )

    def setting(
        name: str,
        feed: str,
        seq: int,
        ms: int,
        value: Any,
        n: int = 0,
        key: str = "theme",
        type_: str = "settings.set",
    ) -> dict[str, Any]:
        return ev(name, feed, seq, ms, type_, {"key": key, "value": value}, n=n)

    s.add(
        "later-clock-wins",
        "fold_settings",
        {
            "events": [
                setting("s1", "aaa", 1, 2000, "dark"),
                setting("s2", "bbb", 1, 1000, "light"),
            ]
        },
        expect={"theme": "dark"},
    )
    s.add(
        "equal-clock-breaks-by-feed-id",
        "fold_settings",
        {
            "events": [
                setting("s1", "aaa", 1, 2000, "dark"),
                setting("s2", "bbb", 1, 2000, "light"),
            ]
        },
        expect={"theme": "light"},
    )
    s.add(
        "counter-breaks-equal-milliseconds",
        "fold_settings",
        {
            "events": [
                setting("s1", "bbb", 1, 2000, "dark"),
                setting("s2", "aaa", 1, 2000, "light", n=1),
            ]
        },
        expect={"theme": "light"},
    )
    s.add(
        "memory-and-settings-share-the-rule",
        "fold_settings",
        {
            "events": [
                setting("s1", "aaa", 1, 1000, "x", key="home", type_="mem.remember"),
                setting("s2", "bbb", 1, 1001, "y", key="home", type_="mem.remember"),
                setting("s3", "aaa", 2, 5, 1, key="n"),
            ]
        },
        expect={"home": "y", "n": 1},
    )
    s.add(
        "order-of-arrival-does-not-matter",
        "fold_settings",
        {
            "events": [
                setting("s2", "bbb", 1, 1000, "light"),
                setting("s1", "aaa", 1, 1000, "dark"),
            ]
        },
        expect={"theme": "light"},
    )
    return s


MBOX_SEED = seed("mailbox/ops")
MBOX = b64url_encode(crypto.ed25519_public(MBOX_SEED))
MBOX_MESH = crypto.sha256_id(b"mailbox-test-mesh")
MBOX_OTHER_MESH = crypto.sha256_id(b"mailbox-other-mesh")
MBOX_RECOVERY_SEED = seed("mailbox/recovery")
MBOX_RECOVERY = b64url_encode(crypto.ed25519_public(MBOX_RECOVERY_SEED))
MBOX_T = 1794441600


def mb_request(
    dev: TestDevice,
    op: str,
    body: dict[str, Any],
    *,
    mesh: str = MBOX_MESH,
    ts: int = MBOX_T,
    audience: str = MBOX,
    signer: TestDevice | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "body": body,
        "device": dev.feed,
        "mailbox": audience,
        "mesh": mesh,
        "op": op,
        "ts": ts,
        "v": 1,
    }
    request["sig"] = mailbox_ref.sign("ed25519", (signer or dev).ops_seed, request)
    return request


def mb_members(*devs: TestDevice) -> list[dict[str, str]]:
    return sorted(
        ({"ops": d.feed, "presence": b64url_encode(d.presence_public)} for d in devs),
        key=lambda m: m["ops"],
    )


def mb_statement(
    signer: TestDevice | None,
    members: list[dict[str, str]],
    seq: int,
    *,
    mesh: str = MBOX_MESH,
    recovery: str = MBOX_RECOVERY,
) -> dict[str, Any]:
    statement: dict[str, Any] = {
        "kind": "members",
        "members": members,
        "mesh": mesh,
        "recovery": recovery,
        "seq": seq,
        "v": 1,
    }
    if signer is None:
        statement["by"] = MBOX_RECOVERY
        statement["via"] = "recovery"
        statement["sig"] = mailbox_ref.sign(
            "ed25519",
            MBOX_RECOVERY_SEED,
            statement | {"by": MBOX_RECOVERY, "via": "recovery"},
        )
    else:
        statement["by"] = b64url_encode(signer.presence_public)
        statement["via"] = "presence"
        statement["sig"] = mailbox_ref.sign("p256", signer.presence_scalar, statement)
    return statement


def mb_envelope(
    dev: TestDevice, seq: int, tag: str, body: bool = True
) -> dict[str, Any]:
    rng = DetRng(f"mailbox/{dev.name}/{seq}/{tag}")
    return {
        "body_ct": b64url_encode(rng.bytes(40)) if body else None,
        "feed": dev.feed,
        "header_ct": b64url_encode(rng.bytes(96)),
        "seq": seq,
    }


def mb_notice(
    dev: TestDevice, kind: str = "remove", mesh: str = MBOX_MESH, tag: str = "a"
) -> dict[str, Any]:
    return {
        "author": dev.id,
        "body": {"device": crypto.sha256_id(tag.encode())},
        "mesh": mesh,
        "sig": b64url_encode(DetRng("mailbox/notice/" + tag).bytes(64)),
        "type": kind,
    }


def build_mailbox() -> Suite:
    s = Suite(
        "mailbox",
        "contracts/mailbox.md",
        devices=secrets_block(),
        mailbox={
            "ops_seed_hex": MBOX_SEED.hex(),
            "key": MBOX,
            "recovery_seed_hex": MBOX_RECOVERY_SEED.hex(),
            "recovery": MBOX_RECOVERY,
            "mesh": MBOX_MESH,
        },
    )
    a, b, c, mal = ALICE, BOB, CAROL, MALLORY

    def check(
        name: str,
        request: dict[str, Any],
        now_s: int = MBOX_T,
        peer: str | None = None,
        error: str | None = None,
    ) -> None:
        case_input: dict[str, Any] = {
            "mailbox": MBOX,
            "now_s": now_s,
            "request": request,
        }
        if peer is not None:
            case_input["peer"] = peer
        s.add(name, "mailbox_request", case_input, error=error)

    fetch_body = {"after": 0, "limit": 10}
    good = mb_request(a, "fetch", fetch_body)
    check("request/valid-fetch", good, peer=a.feed)
    check(
        "request/wrong-audience",
        mb_request(
            a,
            "fetch",
            fetch_body,
            audience=b64url_encode(crypto.ed25519_public(seed("mailbox/other"))),
        ),
        error="mailbox.wrong_audience",
    )
    check(
        "request/stale-timestamp",
        mb_request(a, "fetch", fetch_body, ts=MBOX_T - 301),
        error="mailbox.stale",
    )
    check(
        "request/future-timestamp",
        mb_request(a, "fetch", fetch_body, ts=MBOX_T + 301),
        error="mailbox.stale",
    )
    check(
        "request/skew-edge-is-accepted",
        mb_request(a, "fetch", fetch_body, ts=MBOX_T - 300),
    )
    check(
        "request/peer-differs-from-signer",
        good,
        peer=b.feed,
        error="mailbox.wrong_peer",
    )
    check(
        "request/signed-by-another-key",
        mb_request(a, "fetch", fetch_body, signer=b),
        error="mailbox.bad_signature",
    )
    tampered = {**good, "body": {"after": 1, "limit": 10}}
    check("request/body-edited-after-signing", tampered, error="mailbox.bad_signature")
    check("request/extra-field", {**good, "extra": 1}, error="mailbox.malformed")
    check("request/unknown-op", {**good, "op": "delete"}, error="mailbox.malformed")
    check("request/not-an-object", [], error="mailbox.malformed")
    check("request/bad-mesh-id", {**good, "mesh": "mesh"}, error="mailbox.malformed")
    check("request/short-signature", {**good, "sig": "AAAA"}, error="mailbox.malformed")
    check(
        "request/fetch-limit-too-large",
        mb_request(a, "fetch", {"after": 0, "limit": 257}),
        error="mailbox.too_large",
    )
    check(
        "request/fetch-limit-zero",
        mb_request(a, "fetch", {"after": 0, "limit": 0}),
        error="mailbox.malformed",
    )
    check(
        "request/status-with-a-body",
        mb_request(a, "status", {"x": 1}),
        error="mailbox.malformed",
    )
    check(
        "request/empty-deposit",
        mb_request(a, "deposit", {"envelopes": []}),
        error="mailbox.malformed",
    )
    check(
        "request/too-many-envelopes",
        mb_request(
            a,
            "deposit",
            {"envelopes": [mb_envelope(a, n, "many") for n in range(1, 66)]},
        ),
        error="mailbox.too_large",
    )
    check(
        "request/notice-of-another-type",
        mb_request(a, "notice_put", {"notice": mb_notice(a, "add")}),
        error="mailbox.notice_type",
    )
    check(
        "request/notice-of-another-mesh",
        mb_request(a, "notice_put", {"notice": mb_notice(a, mesh=MBOX_OTHER_MESH)}),
        error="mailbox.wrong_mesh",
    )

    ctx_request = {k: v for k, v in good.items() if k != "sig"}
    s.add(
        "sign/request-ed25519",
        "mailbox_sign",
        {"alg": "ed25519", "object": ctx_request, "secret_hex": a.ops_seed.hex()},
        expect={"sig": good["sig"]},
    )
    s.add(
        "sign/ignores-an-existing-sig-field",
        "mailbox_sign",
        {
            "alg": "ed25519",
            "object": {**ctx_request, "sig": "x"},
            "secret_hex": a.ops_seed.hex(),
        },
        expect={"sig": good["sig"]},
    )

    st = mb_statement(a, mb_members(a, b), 1)
    s.add(
        "statement/presence-signed",
        "mailbox_statement",
        {"statement": st},
        expect={"by": st["by"], "seq": 1, "via": "presence"},
    )
    rs = mb_statement(None, mb_members(a), 2)
    s.add(
        "statement/recovery-signed",
        "mailbox_statement",
        {"statement": rs},
        expect={"by": MBOX_RECOVERY, "seq": 2, "via": "recovery"},
    )
    s.add(
        "statement/edited-after-signing",
        "mailbox_statement",
        {"statement": {**st, "seq": 2}},
        error="mailbox.bad_signature",
    )
    s.add(
        "statement/members-edited-after-signing",
        "mailbox_statement",
        {"statement": {**st, "members": mb_members(a)}},
        error="mailbox.bad_signature",
    )
    s.add(
        "statement/other-key-claims-to-sign",
        "mailbox_statement",
        {"statement": {**st, "by": b64url_encode(b.presence_public)}},
        error="mailbox.bad_signature",
    )
    s.add(
        "statement/members-out-of-order",
        "mailbox_statement",
        {"statement": {**st, "members": list(reversed(st["members"]))}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/duplicate-member",
        "mailbox_statement",
        {"statement": {**st, "members": [st["members"][0], st["members"][0]]}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/no-members",
        "mailbox_statement",
        {"statement": {**st, "members": []}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/seq-zero",
        "mailbox_statement",
        {"statement": {**st, "seq": 0}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/unknown-via",
        "mailbox_statement",
        {"statement": {**st, "via": "device"}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/presence-key-with-recovery-length",
        "mailbox_statement",
        {"statement": {**st, "via": "recovery"}},
        error="mailbox.malformed",
    )
    s.add(
        "statement/extra-field",
        "mailbox_statement",
        {"statement": {**st, "label": "x"}},
        error="mailbox.malformed",
    )

    notice = mb_notice(a)
    out = s.add(
        "notice/remove-entry-is-held",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": notice},
    )
    assert out["id"] == crypto.sha256_id(canonical(notice))
    s.add(
        "notice/stop-entry-is-held",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": mb_notice(a, "stop", tag="s")},
    )
    s.add(
        "notice/epoch-entry-is-held",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": mb_notice(a, "epoch", tag="e")},
    )
    s.add(
        "notice/add-entry-is-refused",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": mb_notice(a, "add")},
        error="mailbox.notice_type",
    )
    s.add(
        "notice/genesis-entry-is-refused",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": mb_notice(a, "genesis")},
        error="mailbox.notice_type",
    )
    s.add(
        "notice/missing-signature",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": {k: v for k, v in notice.items() if k != "sig"}},
        error="mailbox.malformed",
    )
    s.add(
        "notice/too-large",
        "mailbox_notice",
        {"mesh": MBOX_MESH, "notice": {**notice, "body": {"pad": "x" * 17000}}},
        error="mailbox.too_large",
    )

    env = mb_envelope(a, 1, "x")
    s.add("envelope/valid", "mailbox_envelope", {"envelope": env})
    s.add(
        "envelope/body-withheld",
        "mailbox_envelope",
        {"envelope": mb_envelope(a, 1, "w", body=False)},
    )
    s.add(
        "envelope/extra-field",
        "mailbox_envelope",
        {"envelope": {**env, "labels": []}},
        error="mailbox.malformed",
    )
    s.add(
        "envelope/seq-zero",
        "mailbox_envelope",
        {"envelope": {**env, "seq": 0}},
        error="mailbox.malformed",
    )
    s.add(
        "envelope/short-header",
        "mailbox_envelope",
        {"envelope": {**env, "header_ct": b64url_encode(b"x" * 71)}},
        error="mailbox.malformed",
    )
    s.add(
        "envelope/padded-base64",
        "mailbox_envelope",
        {"envelope": {**env, "body_ct": env["body_ct"] + "="}},
        error="mailbox.malformed",
    )
    s.add(
        "envelope/feed-not-a-key",
        "mailbox_envelope",
        {"envelope": {**env, "feed": "abc"}},
        error="mailbox.malformed",
    )
    s.add(
        "envelope/over-4-mib",
        "mailbox_envelope",
        {"envelope": {**env, "body_ct": b64url_encode(b"x" * 3200000)}},
        error="mailbox.too_large",
    )

    def run(
        name: str,
        steps: list[dict[str, Any]],
        limits: dict[str, int] | None = None,
        admission: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        case_input: dict[str, Any] = {"mailbox": MBOX, "steps": steps}
        if limits:
            case_input["limits"] = limits
        if admission is not None:
            case_input["admission"] = admission
        return s.add(name, "mailbox_replay", case_input)["results"]

    def step(
        request: dict[str, Any], at_s: int = MBOX_T, peer: str | None = None
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"at_s": at_s, "request": request}
        if peer is not None:
            out["peer"] = peer
        return out

    def register(
        dev: TestDevice, *others: TestDevice, mesh: str = MBOX_MESH
    ) -> dict[str, Any]:
        return step(
            mb_request(
                dev,
                "members",
                {
                    "statement": mb_statement(
                        dev, mb_members(dev, *others), 1, mesh=mesh
                    )
                },
                mesh=mesh,
            )
        )

    def dep(
        dev: TestDevice, *envelopes: dict[str, Any], at_s: int = MBOX_T
    ) -> dict[str, Any]:
        return step(
            mb_request(dev, "deposit", {"envelopes": list(envelopes)}, ts=at_s), at_s
        )

    def fet(
        dev: TestDevice, after: int, limit: int = 10, at_s: int = MBOX_T
    ) -> dict[str, Any]:
        return step(
            mb_request(dev, "fetch", {"after": after, "limit": limit}, ts=at_s), at_s
        )

    def stat(dev: TestDevice, at_s: int = MBOX_T) -> dict[str, Any]:
        return step(mb_request(dev, "status", {}, ts=at_s), at_s)

    e1, e2, e3 = (mb_envelope(a, n, "r") for n in (1, 2, 3))
    out = run(
        "replay/two-devices-never-online-together-converge",
        [
            register(a, b),
            dep(a, e1, e2),
            stat(a),
            fet(b, 0, at_s=MBOX_T + 3600),
            stat(a, at_s=MBOX_T + 3600),
            fet(b, 2, at_s=MBOX_T + 3601),
            stat(a, at_s=MBOX_T + 3602),
        ],
    )
    assert out[3]["ok"]["items"] == [{"envelope": e1, "n": 1}, {"envelope": e2, "n": 2}]
    assert out[3]["ok"]["next"] == 2 and out[4]["ok"]["items"] == 2
    assert out[5]["ok"] == {"items": [], "more": False, "next": 2, "truncated": False}
    assert out[6]["ok"]["items"] == 0 and out[6]["ok"]["head"] == 2

    out = run(
        "replay/depositor-does-not-fetch-its-own-envelopes",
        [register(a, b), dep(a, e1), fet(a, 0)],
    )
    assert out[2]["ok"]["items"] == [] and out[2]["ok"]["next"] == 1

    out = run(
        "replay/fetch-pages-with-more-and-next",
        [register(a, b), dep(a, e1, e2, e3), fet(b, 0, 2), fet(b, 2, 2), fet(b, 3, 2)],
    )
    assert out[2]["ok"]["more"] is True and out[2]["ok"]["next"] == 2
    assert out[3]["ok"]["more"] is False and out[3]["ok"]["next"] == 3
    assert [i["n"] for i in out[3]["ok"]["items"]] == [3]

    out = run(
        "replay/duplicate-deposits-are-counted-once",
        [register(a, b), dep(a, e1, e2), dep(a, e2, e3)],
    )
    assert out[2]["ok"] == {"accepted": 1, "duplicates": 1, "head": 3}

    fork = mb_envelope(a, 1, "fork")
    out = run(
        "replay/two-envelopes-at-one-position-are-both-held",
        [register(a, b, c), dep(b, e1), dep(c, fork), fet(a, 0)],
    )
    assert [i["n"] for i in out[3]["ok"]["items"]] == [1, 2]

    out = run(
        "replay/a-device-that-is-not-listed-is-refused",
        [
            register(a, b),
            dep(mal, e1),
            fet(mal, 0),
            stat(mal),
            step(mb_request(a, "notice_get", {}, mesh=MBOX_OTHER_MESH)),
        ],
    )
    assert [r.get("error") for r in out[1:]] == [
        "mailbox.not_a_member",
        "mailbox.not_a_member",
        "mailbox.not_a_member",
        "mailbox.unknown_mesh",
    ]

    out = run(
        "replay/a-removed-device-is-refused-and-told-so",
        [
            register(a, b),
            dep(a, e1),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a), 2)}
                )
            ),
            fet(b, 0),
            dep(b, e2),
            stat(a),
        ],
    )
    assert out[2]["ok"] == {"seq": 2}
    assert out[3]["error"] == "mailbox.removed" and out[4]["error"] == "mailbox.removed"
    assert out[5]["ok"]["items"] == 0

    out = run(
        "replay/membership-needs-a-listed-presence-key",
        [
            register(a, b),
            step(
                mb_request(
                    mal,
                    "members",
                    {"statement": mb_statement(mal, mb_members(a, b, mal), 2)},
                )
            ),
            step(
                mb_request(
                    a,
                    "members",
                    {"statement": mb_statement(mal, mb_members(a, b, mal), 2)},
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a, b, c), 2)}
                )
            ),
            fet(c, 0),
        ],
    )
    assert [r.get("error") for r in out[1:3]] == ["mailbox.members_unauthorized"] * 2
    assert out[3]["ok"] == {"seq": 2} and out[4]["ok"]["items"] == []

    out = run(
        "replay/membership-seq-must-rise",
        [
            register(a, b),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a), 1)}
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a, b), 1)}
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a, b, c), 3)}
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(b, mb_members(a, b), 2)}
                )
            ),
        ],
    )
    assert out[1]["error"] == "mailbox.members_seq"
    assert out[2]["ok"] == {"seq": 1}
    assert out[3]["ok"] == {"seq": 3}
    assert out[4]["error"] == "mailbox.members_seq"

    out = run(
        "replay/registration-rules",
        [
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a), 2)}
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(None, mb_members(a), 1)}
                )
            ),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(b, mb_members(a), 1)}
                )
            ),
            step(
                mb_request(
                    b, "members", {"statement": mb_statement(a, mb_members(a), 1)}
                )
            ),
            step(
                mb_request(
                    a,
                    "members",
                    {
                        "statement": mb_statement(
                            a, mb_members(a), 1, mesh=MBOX_OTHER_MESH
                        )
                    },
                )
            ),
            register(a),
            register(mal),
        ],
    )
    assert [r.get("error") for r in out] == [
        "mailbox.members_seq",
        "mailbox.members_unauthorized",
        "mailbox.members_unauthorized",
        "mailbox.members_unauthorized",
        "mailbox.wrong_mesh",
        None,
        "mailbox.members_unauthorized",
    ]

    out = run(
        "replay/recovery-key-restores-access",
        [
            register(a, b),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a), 2)}
                )
            ),
            step(
                mb_request(
                    c, "members", {"statement": mb_statement(None, mb_members(c), 3)}
                )
            ),
            dep(c, e1),
            fet(a, 0),
            step(
                mb_request(
                    c,
                    "members",
                    {
                        "statement": mb_statement(
                            None,
                            mb_members(c),
                            4,
                            recovery=b64url_encode(
                                crypto.ed25519_public(seed("mailbox/rec2"))
                            ),
                        )
                    },
                )
            ),
        ],
    )
    assert out[2]["ok"] == {"seq": 3} and out[4]["error"] == "mailbox.removed"
    assert out[5]["error"] == "mailbox.recovery_mismatch"

    out = run(
        "replay/admission-list-limits-who-may-register",
        [register(b), register(a)],
        admission=[a.feed],
    )
    assert out[0]["error"] == "mailbox.admission_denied" and "ok" in out[1]

    out = run(
        "replay/mesh-count-is-limited",
        [register(a), register(a, mesh=MBOX_OTHER_MESH)],
        limits={"max_meshes": 1},
    )
    assert out[1]["error"] == "mailbox.quota_exceeded"

    out = run(
        "replay/uncollected-envelopes-expire-and-the-reader-is-told",
        [
            register(a, b),
            dep(a, e1, e2),
            fet(b, 0, at_s=MBOX_T + 101),
            dep(a, e3, at_s=MBOX_T + 102),
            stat(a, at_s=MBOX_T + 102),
        ],
        limits={"max_age_s": 100},
    )
    assert out[2]["ok"]["truncated"] is True and out[2]["ok"]["items"] == []
    assert out[3]["ok"]["head"] == 3 and out[4]["ok"]["expired_through"] == 2

    out = run(
        "replay/collected-envelopes-leave-without-a-truncation-flag",
        [
            register(a, b),
            dep(a, e1),
            fet(b, 0),
            fet(b, 1, at_s=MBOX_T + 1000),
            fet(b, 1, at_s=MBOX_T + 2000),
        ],
        limits={"max_age_s": 100},
    )
    assert out[4]["ok"]["truncated"] is False

    out = run(
        "replay/deposits-over-the-item-quota-are-refused-whole",
        [register(a, b), dep(a, e1, e2), dep(a, e3), dep(a, e2), fet(b, 0, 1)],
        limits={"max_mesh_items": 2},
    )
    assert (
        out[2]["error"] == "mailbox.quota_exceeded" and out[3]["ok"]["duplicates"] == 1
    )
    out = run(
        "replay/deposits-over-the-byte-quota-are-refused",
        [register(a, b), dep(a, e1, e2), dep(a, e3)],
        limits={"max_mesh_bytes": 2 * len(canonical(e1)) + 1},
    )
    assert out[2]["error"] == "mailbox.quota_exceeded"

    out = run(
        "replay/a-cursor-ahead-of-the-head-is-refused",
        [register(a, b), dep(a, e1), fet(b, 5)],
    )
    assert out[2]["error"] == "mailbox.cursor_ahead"

    n_remove, n_stop = mb_notice(a, tag="r"), mb_notice(a, "stop", tag="s")
    out = run(
        "replay/a-removal-notice-waits-for-a-device-that-comes-online-later",
        [
            register(a, b, c),
            step(mb_request(a, "notice_put", {"notice": n_remove})),
            step(mb_request(a, "notice_put", {"notice": n_remove})),
            step(mb_request(a, "notice_put", {"notice": n_stop})),
            stat(b, at_s=MBOX_T + 86400 * 20),
            step(
                mb_request(b, "notice_get", {}, ts=MBOX_T + 86400 * 20),
                MBOX_T + 86400 * 20,
            ),
            step(
                mb_request(c, "notice_get", {}, ts=MBOX_T + 86400 * 40),
                MBOX_T + 86400 * 40,
            ),
            step(
                mb_request(c, "notice_get", {}, ts=MBOX_T + 86400 * 41),
                MBOX_T + 86400 * 41,
            ),
        ],
    )
    assert out[1]["ok"]["new"] is True and out[2]["ok"]["new"] is False
    assert out[4]["ok"]["notices"] == 2
    assert [n["notice"] for n in out[5]["ok"]["notices"]] == [n_remove, n_stop]
    assert len(out[6]["ok"]["notices"]) == 2 and out[7]["ok"]["notices"] == []

    out = run(
        "replay/notices-expire-and-are-limited",
        [
            register(a, b),
            step(mb_request(a, "notice_put", {"notice": n_remove})),
            step(mb_request(a, "notice_put", {"notice": n_stop})),
            step(mb_request(a, "notice_put", {"notice": mb_notice(a, tag="third")})),
            step(mb_request(b, "notice_get", {}, ts=MBOX_T + 200), MBOX_T + 200),
        ],
        limits={"max_notices": 2, "notice_max_age_s": 100},
    )
    assert out[3]["error"] == "mailbox.quota_exceeded" and out[4]["ok"]["notices"] == []

    out = run(
        "replay/a-removed-device-no-longer-holds-back-collection",
        [
            register(a, b, c),
            dep(a, e1),
            fet(b, 0),
            fet(b, 1),
            stat(a),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a, b), 2)}
                )
            ),
            stat(a),
        ],
    )
    assert out[4]["ok"]["items"] == 1 and out[6]["ok"]["items"] == 0

    out = run(
        "replay/an-added-device-collects-what-is-still-held",
        [
            register(a, b),
            dep(a, e1),
            step(
                mb_request(
                    a, "members", {"statement": mb_statement(a, mb_members(a, b, c), 2)}
                )
            ),
            fet(b, 0),
            fet(c, 0),
            stat(a),
        ],
    )
    assert [i["n"] for i in out[4]["ok"]["items"]] == [1] and out[5]["ok"]["items"] == 1

    out = run(
        "replay/request-checks-come-before-state",
        [
            register(a, b),
            step(mb_request(a, "status", {}, ts=MBOX_T - 400), MBOX_T),
            step(mb_request(a, "status", {}), MBOX_T, peer=b.feed),
            step(
                mb_request(
                    a,
                    "status",
                    {},
                    audience=b64url_encode(
                        crypto.ed25519_public(seed("mailbox/other"))
                    ),
                )
            ),
            stat(a),
        ],
    )
    assert [r.get("error") for r in out[1:4]] == [
        "mailbox.stale",
        "mailbox.wrong_peer",
        "mailbox.wrong_audience",
    ]
    assert out[4]["ok"]["seq"] == 1
    return s


def suites(repin: bool) -> list[Suite]:
    return [
        build_canonical(),
        build_crypto(),
        build_event(),
        build_feed(),
        build_roster(),
        build_attestation(),
        build_pairing(),
        build_keys(),
        build_policy(),
        build_ticket(repin),
        build_labels(),
        build_surface(),
        build_sync(),
        build_fold(),
        build_mailbox(),
    ]


def index_text(built: list[Suite]) -> str:
    entries = {}
    for suite in built:
        text = suite.text()
        entries[suite.name + ".json"] = {
            "cases": len(suite.doc["cases"]),
            "sha256": crypto.sha256_id(text.encode("utf-8"))[7:],
            "spec": suite.doc["spec"],
        }
    return (
        json.dumps(
            {"files": dict(sorted(entries.items())), "format": FORMAT},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and fail if a committed vector differs",
    )
    parser.add_argument(
        "--repin-tickets",
        action="store_true",
        help="mint fresh Biscuit tokens instead of reusing the committed ones",
    )
    args = parser.parse_args()
    built = suites(args.repin_tickets)
    files = {suite.name + ".json": suite.text() for suite in built}
    files["index.json"] = index_text(built)
    if args.check:
        bad = []
        for name, text in files.items():
            path = VECTORS / name
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                bad.append(name)
        extra = sorted(p.name for p in VECTORS.glob("*.json") if p.name not in files)
        if bad or extra:
            print("vectors differ: " + ", ".join(bad), file=sys.stderr)
            if extra:
                print("unexpected files: " + ", ".join(extra), file=sys.stderr)
            return 1
        print(f"{len(files)} vector files match")
        return 0
    VECTORS.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (VECTORS / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} vector files to {VECTORS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
