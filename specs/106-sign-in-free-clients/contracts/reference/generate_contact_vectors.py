"""Generates the contacts and grants vectors from public fixed test keys, clocks and ids, running every case through the reference module.
Roster entries are signed v2 entries built with the PHI destination generator's mesh builder on the roster v2 test devices, and `--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
contacts = importlib.import_module("contacts")
gcv = importlib.import_module("generate_connection_vectors")
gpd = importlib.import_module("generate_phi_destination_vectors")
cj = importlib.import_module("astral_mesh_ref.cj")
crypto = importlib.import_module("astral_mesh_ref.crypto")
keys = importlib.import_module("astral_mesh_ref.keys")
testkit = importlib.import_module("astral_mesh_ref.testkit")
b64 = cj.b64url_encode
DetRng, hlc = testkit.DetRng, testkit.hlc

VECTORS = HERE.parent / "vectors"
SPEC = "106/contracts/contacts.md"
T0 = gcv.T0
MIN = 60000
NOW_S = T0 // 1000
ALICE, BOB, CAROL, NODE, DAVE, MALLORY = (
    gcv.ALICE,
    gcv.BOB,
    gcv.CAROL,
    gcv.NODE,
    gcv.DAVE,
    gcv.MALLORY,
)
MEMBERS = gcv.MEMBERS
REMOVED = {DAVE.id: {**gcv.REMOVED[DAVE.id], "cutoff": {DAVE.feed: 5}}}
OWN = crypto.sha256_id(b"contacts-v1/own-mesh")
CM = [crypto.sha256_id(f"contacts-v1/contact/{i}".encode()) for i in range(4)]
HEADS = sorted(crypto.sha256_id(f"contacts-v1/head/{i}".encode()) for i in range(3))
CHATS = [crypto.sha256_id(f"contacts-v1/chat/{i}".encode()) for i in range(3)]
_GID_RNG = DetRng("contacts-v1/grants")
GIDS = [_GID_RNG.bytes(16).hex() for _ in range(8)]
ADDR = "astral-node-addr-0123456789abcdefghijklmnopqrstuvwxyz"


class Log:
    def __init__(self, case: str) -> None:
        self.case = case
        self.events: list[dict[str, Any]] = []
        self.seqs: dict[str, int] = {}

    def add(
        self,
        name: str,
        writer: Any,
        ms: int,
        body: dict[str, Any],
        type_: str = "settings.set",
        n: int = 0,
        scope: str = "mesh",
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        seq = self.seqs.get(writer.feed, 0) + 1
        self.seqs[writer.feed] = seq
        event = {
            "after": [],
            "body": body,
            "clock": hlc(ms, n),
            "feed": writer.feed,
            "id": crypto.sha256_id(f"contacts-v1/{self.case}/{name}".encode()),
            "labels": labels or dict(contacts.LABELS),
            "scope": scope,
            "seq": seq,
            "type": type_,
        }
        if any(e["id"] == event["id"] for e in self.events):
            raise ValueError("duplicate event name " + name)
        self.events.append(event)
        return event


def cvalue(
    index: int,
    name: str = "Bea",
    state: str = "verified",
    heads: list[str] | None = None,
    count: int | None = None,
    ms: int = T0,
) -> dict[str, Any]:
    verified = state == "verified"
    return {
        "v": 1,
        "mesh": CM[index],
        "name": name,
        "state": state,
        "heads": (HEADS[:2] if heads is None else heads) if verified else [],
        "count": (5 if count is None else count) if verified else 0,
        "ms": ms,
    }


def consent_for(value: Any, signer: Any, ms: int, kind: str, **change: Any) -> dict[str, Any]:
    return gcv.make_consent(value, signer, ms, kind=kind, **change)


def cput(
    log: Log,
    name: str,
    writer: Any,
    ms: int,
    value: Any,
    consent: Any = None,
    key: str | None = None,
    **event: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": contacts.CONTACT_PREFIX + (key or value["mesh"].removeprefix("sha256:")),
        "value": value,
    }
    if consent is None and value["state"] == "verified":
        consent = consent_for(value, writer, ms - 1000, "contact")
    if consent:
        body["consent"] = consent
    return log.add(name, writer, ms, body, **event)


def cremove(log: Log, name: str, writer: Any, ms: int, index: int, **extra: Any) -> dict[str, Any]:
    key = contacts.CONTACT_PREFIX + CM[index].removeprefix("sha256:")
    return log.add(name, writer, ms, {"key": key, "value": None, **extra})


def mput(
    log: Log,
    name: str,
    writer: Any,
    ms: int,
    index: int,
    heads: list[str],
    count: int,
    removed: list[str],
    **event: Any,
) -> dict[str, Any]:
    key = contacts.MARK_PREFIX + CM[index].removeprefix("sha256:")
    value = {"v": 1, "heads": heads, "count": count, "removed": removed}
    return log.add(name, writer, ms, {"key": key, "value": value}, **event)


def gvalue(
    gid: str,
    contact: int = 0,
    chat: int = 0,
    phi: bool = False,
    supersedes: str | None = None,
    ms: int = T0,
    item: Any = None,
) -> dict[str, Any]:
    return {
        "v": 1,
        "id": gid,
        "contact": CM[contact],
        "item": item or {"kind": "chat", "chat": CHATS[chat]},
        "kind": "view",
        "phi": phi,
        "supersedes": supersedes,
        "ms": ms,
    }


def gput(
    log: Log,
    name: str,
    writer: Any,
    ms: int,
    value: Any,
    consent: Any = None,
    key: str | None = None,
    **event: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"key": contacts.GRANT_PREFIX + (key or value["id"]), "value": value}
    if consent is not False:
        body["consent"] = consent or consent_for(value, writer, ms - 1000, "share")
    return log.add(name, writer, ms, body, **event)


def grevoke(log: Log, name: str, writer: Any, ms: int, gid: str, value: Any = "ok", **event: Any) -> dict[str, Any]:
    value = {"v": 1, "ms": ms} if value == "ok" else value
    return log.add(name, writer, ms, {"key": contacts.REVOKED_PREFIX + gid, "value": value}, **event)


def state(log: Log, **extra: Any) -> dict[str, Any]:
    return {"mesh": OWN, "members": MEMBERS, "removed": REMOVED, "events": list(log.events), **extra}


def devices_block() -> dict[str, Any]:
    return {
        name: {
            "id": d.id,
            "label": d.name,
            "kind": d.kind,
            "feeds": MEMBERS.get(d.id, REMOVED.get(d.id, gcv._entry(d)))["feeds"],
            "presence_scalar_hex": f"{d.presence_scalar:064x}",
            "identity_scalar_hex": f"{d.identity_scalar:064x}",
        }
        for name, d in (
            ("alice", ALICE),
            ("bob", BOB),
            ("carol", CAROL),
            ("node", NODE),
            ("dave", DAVE),
            ("mallory", MALLORY),
        )
    }


def wire(entries: list[Any]) -> list[str]:
    return [gpd.phi.wire_text(e) for e in entries]


def tampered_text(invite: dict[str, Any], **change: Any) -> str:
    return contacts.INVITE_TEXT + b64(cj.canonical({**invite, **change}))


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
        expect = contacts.run(kind, case_input)
        cases.append({"name": name, "kind": kind, "input": case_input, "expect": expect})
        return expect

    keys_cases(add)
    invite_cases(add)
    code_cases(add)
    contact_fold_cases(add)
    follow_cases(add)
    reader_cases(add)
    share_cases(add)
    classify_cases(add)

    log = Log("coexistence")
    log.add("theme", ALICE, T0, {"key": "theme", "value": "dark"})
    log.add("home", BOB, T0, {"key": "home", "value": "Lexington"}, type_="mem.remember")
    grant = log.add(
        "grant",
        ALICE,
        T0 + 1,
        {"agent": "agent.mail", "scope": "mesh", "tool": "mail.send"},
        type_="auth.grant",
    )
    log.add(
        "consent",
        CAROL,
        T0 + 2,
        {
            "consent": gcv.make_consent(None, ALICE, T0 + 2, kind="grant", digest=grant["id"]),
            "grant": grant["id"],
        },
        type_="auth.consent",
    )
    value = cvalue(0)
    cput(log, "verified", ALICE, T0 + MIN, value)
    cput(log, "removed", CAROL, T0 + 2 * MIN, value | {"state": "unverified", "heads": [], "count": 0})
    mput(log, "mark", ALICE, T0 + 3 * MIN, 0, HEADS[:2], 5, [])
    gv = gvalue(GIDS[0])
    gput(log, "share_grant", ALICE, T0 + 4 * MIN, gv)
    grevoke(log, "revoked", ALICE, T0 + 5 * MIN, GIDS[0])
    log.add("theme2", ALICE, T0 + 6 * MIN, {"key": "theme", "value": "light"})
    add(
        "coexistence_deployed_folds",
        "contact_coexistence",
        {"events": log.events, "presence_keys": {ALICE.id: b64(ALICE.presence_public)}},
    )

    return {
        "format": "astral.vectors/v1",
        "name": "contacts-v1",
        "spec": SPEC,
        "constants": {
            "consent_window_ms": contacts.connection.CONSENT_WINDOW_MS,
            "invite_default_lifetime_s": contacts.DEFAULT_LIFETIME_S,
            "invite_lifetime_range_s": [contacts.MIN_LIFETIME_S, contacts.MAX_LIFETIME_S],
            "max_contacts": contacts.MAX_CONTACTS,
            "max_grants": contacts.MAX_GRANTS,
            "own_mesh": OWN,
            "now_s": NOW_S,
            "codes": list(contacts.CODES),
            "unverifiable_codes": list(contacts.UNVERIFIABLE_CODES),
            "honest_codes": list(contacts.HONEST_CODES),
            "trigger_codes": list(contacts.TRIGGER_CODES),
        },
        "test_devices": devices_block(),
        "cases": cases,
    }


def keys_cases(add: Any) -> None:
    c, g = CM[0].removeprefix("sha256:"), GIDS[0]
    for name, key in (
        ("key_contact", contacts.CONTACT_PREFIX + c),
        ("key_mark", contacts.MARK_PREFIX + c),
        ("key_grant", contacts.GRANT_PREFIX + g),
        ("key_revoked", contacts.REVOKED_PREFIX + g),
        ("key_other_setting", "theme"),
        ("key_bare_contact_word", "contact"),
        ("key_plural_prefix", "contacts/" + c),
        ("key_shared_prefix", "shared/" + g),
        ("key_contact_uppercase_hex", "contact/" + c.upper()),
        ("key_contact_63_hex", "contact/" + c[:63]),
        ("key_contact_with_32_hex", "contact/" + g),
        ("key_grant_with_64_hex", "share/" + c),
        ("key_grant_uppercase_hex", "share/" + g.upper()),
        ("key_revoked_trailing_slash", "share.revoked/" + g + "/"),
        ("key_unknown_contact_family", "contact.card/" + c),
        ("key_unknown_share_family", "share.note/" + g),
        ("key_mark_with_32_hex", "contact.roster/" + g),
    ):
        add(name, "contact_key", {"key": key})

    def rec(name: str, value: Any, key_id: str = c) -> None:
        add(name, "contact_record", {"id": key_id, "value": value})

    rec("record_verified_valid", cvalue(0))
    rec("record_unverified_valid", cvalue(0, state="unverified"))
    rec("record_name_64_valid", cvalue(0, name="n" * 64))
    rec("record_name_with_space_valid", cvalue(0, name="Bea Smith"))
    rec("record_name_empty", cvalue(0, name=""))
    rec("record_name_65", cvalue(0, name="n" * 65))
    rec("record_name_bidi_override", cvalue(0, name="Bea\u202eSmith"))
    rec("record_name_control", cvalue(0, name="Bea\u0007"))
    rec("record_name_not_text", cvalue(0, name=7))
    rec("record_state_unknown", cvalue(0) | {"state": "trusted"})
    rec("record_version_2", cvalue(0) | {"v": 2})
    rec("record_extra_field", cvalue(0) | {"extra": 1})
    rec("record_missing_field", {k: v for k, v in cvalue(0).items() if k != "ms"})
    rec("record_mesh_differs_from_key", cvalue(1), c)
    rec("record_mesh_not_an_id", cvalue(0) | {"mesh": "sha256:XYZ"})
    rec("record_heads_unsorted", cvalue(0, heads=list(reversed(HEADS))))
    rec("record_heads_duplicate", cvalue(0, heads=[HEADS[0], HEADS[0]]))
    rec("record_heads_not_ids", cvalue(0, heads=["abc"]))
    rec("record_verified_without_heads", cvalue(0, heads=[], count=0) | {"state": "verified"})
    rec("record_verified_count_below_heads", cvalue(0, heads=HEADS, count=2))
    rec("record_unverified_with_heads", cvalue(0, state="unverified") | {"heads": HEADS[:1]})
    rec("record_unverified_with_count", cvalue(0, state="unverified") | {"count": 3})
    rec("record_count_negative", cvalue(0, count=-1))
    rec("record_count_not_integer", cvalue(0) | {"count": True})
    rec("record_ms_negative", cvalue(0, ms=-1))
    rec("record_not_an_object", [cvalue(0)])
    many = sorted(crypto.sha256_id(f"contacts-v1/many/{i}".encode()) for i in range(65))
    rec("record_heads_64_valid", cvalue(0, heads=many[:64], count=64))
    rec("record_heads_65", cvalue(0, heads=many, count=65))


def invite_cases(add: Any) -> None:
    scalar = f"{ALICE.identity_scalar:064x}"
    rng = DetRng("contacts-v1/invite")
    nonce = b64(rng.bytes(16))

    def make(name: str, lifetime: int = contacts.DEFAULT_LIFETIME_S, **change: Any) -> dict[str, Any]:
        given = {
            "identity_scalar_hex": scalar,
            "mesh": OWN,
            "name": "Alex",
            "addr": ADDR,
            "nonce": nonce,
            "now_s": NOW_S,
            "lifetime_s": lifetime,
            **change,
        }
        return add(name, "contact_invite_make", given)

    ok = make("invite_make_default_lifetime")["ok"]
    make("invite_make_lifetime_60_valid", 60)
    make("invite_make_lifetime_86400_valid", 86400)
    make("invite_make_lifetime_59", 59)
    make("invite_make_lifetime_86401", 86401)
    invite, other = ok["invite"], crypto.sha256_id(b"contacts-v1/other-mesh")

    def check(name: str, text: Any, now_s: int = NOW_S + 60, own: str = other) -> None:
        add(name, "contact_invite_check", {"text": text, "now_s": now_s, "own_mesh": own})

    check("invite_valid_text", ok["text"])
    check("invite_valid_link", ok["link"])
    check("invite_valid_last_second", ok["text"], invite["expires_s"] - 1)
    check("invite_expired_at_expiry", ok["text"], invite["expires_s"])
    check("invite_expired_later", ok["text"], invite["expires_s"] + 3600)
    check("invite_own_mesh", ok["text"], own=OWN)
    long = add("invite_make_for_the_far_future", "contact_invite_make", {
        "identity_scalar_hex": scalar, "mesh": OWN, "name": "Alex", "addr": ADDR,
        "nonce": nonce, "now_s": NOW_S + 3600, "lifetime_s": 86400,
    })["ok"]
    check("invite_expiry_more_than_a_day_away", long["text"], NOW_S)
    check("invite_expiry_exactly_a_day_away", long["text"], NOW_S + 3600)
    check("invite_own_mesh_after_expiry_is_self", ok["text"], invite["expires_s"] + 1, OWN)
    check("invite_tampered_name_bad_signature", tampered_text(invite, name="Eve"))
    raw = bytearray(cj.b64url_decode(invite["sig"]))
    raw[7] ^= 1
    check("invite_tampered_signature", tampered_text(invite, sig=b64(bytes(raw))))
    s = int.from_bytes(cj.b64url_decode(invite["sig"])[32:], "big")
    high = cj.b64url_decode(invite["sig"])[:32] + (crypto.P256_ORDER - s).to_bytes(32, "big")
    check("invite_high_s_signature", tampered_text(invite, sig=b64(high)))
    forger = gcv.MALLORY
    forged = contacts.make_invite(forger.identity_scalar, OWN, "Alex", ADDR, cj.b64url_decode(nonce), NOW_S)
    check("invite_device_not_matching_identity", tampered_text(forged, device=ALICE.id))
    check("invite_signed_by_other_key_for_claimed_identity", tampered_text(invite, sig=forged["sig"]))
    check("invite_wrong_prefix", "astral-contact:v2:" + ok["text"].removeprefix(contacts.INVITE_TEXT))
    check("invite_link_wrong_host", "https://example.invalid/contact/#" + ok["text"].removeprefix(contacts.INVITE_TEXT))
    check("invite_not_text", 7)
    check("invite_empty_payload", contacts.INVITE_TEXT)
    check("invite_padded_base64", ok["text"] + "=")
    check("invite_not_base64", contacts.INVITE_TEXT + "***")
    payload = cj.canonical(invite)
    spaced = json.dumps(invite, sort_keys=True).encode()
    check("invite_spaced_json_not_canonical", contacts.INVITE_TEXT + b64(spaced))
    reordered = json.dumps(invite, separators=(",", ":")).encode()
    check("invite_unsorted_json_not_canonical", contacts.INVITE_TEXT + b64(reordered))
    check("invite_duplicate_key", contacts.INVITE_TEXT + b64(payload[:-1] + b',"v":1}'))
    check("invite_payload_not_an_object", contacts.INVITE_TEXT + b64(b"[1]"))
    check("invite_payload_not_utf8", contacts.INVITE_TEXT + b64(b"\xff\xfe"))
    for field in sorted(contacts.INVITE_FIELDS):
        check(f"invite_missing_{field}", tampered_text({k: v for k, v in invite.items() if k != field}))
    check("invite_extra_field", tampered_text(invite, extra=1))
    check("invite_version_2", tampered_text(invite, v=2))
    check("invite_nonce_15_bytes", tampered_text(invite, nonce=b64(bytes(15))))
    check("invite_nonce_17_bytes", tampered_text(invite, nonce=b64(bytes(17))))
    check("invite_name_empty", tampered_text(invite, name=""))
    check("invite_name_65", tampered_text(invite, name="n" * 65))
    check("invite_name_bidi_override", tampered_text(invite, name="Al\u202eex"))
    check("invite_addr_with_space", tampered_text(invite, addr="bad addr"))
    check("invite_addr_empty", tampered_text(invite, addr=""))
    check("invite_addr_1025", tampered_text(invite, addr="a" * 1025))
    check("invite_expires_zero", tampered_text(invite, expires_s=0))
    check("invite_expires_not_integer", tampered_text(invite, expires_s=True))
    check("invite_mesh_not_an_id", tampered_text(invite, mesh="nope"))


def _find_nonces(accept: Any) -> list[str]:
    counter = 0
    while True:
        pair = [
            hashlib.sha256(f"contacts-v1/code/{counter}/{side}".encode()).digest()[:16]
            for side in "ab"
        ]
        counter += 1
        sides = [{"mesh": CM[0], "heads": HEADS, "count": 7}, {"mesh": CM[1], "heads": HEADS[:2], "count": 5}]
        if accept(pair, contacts.comparison_code([b64(n) for n in pair], sides)):
            return [b64(n) for n in pair]


def code_cases(add: Any) -> None:
    a = {"mesh": CM[0], "heads": HEADS, "count": 7}
    b = {"mesh": CM[1], "heads": HEADS[:2], "count": 5}
    rng = DetRng("contacts-v1/code")
    n1, n2 = (b64(rng.bytes(16)) for _ in range(2))

    def code(name: str, nonces: list[str], sides: list[dict[str, Any]]) -> None:
        add(name, "contact_code", {"nonces": nonces, "sides": sides})

    code("code_base", [n1, n2], [a, b])
    code("code_nonces_swapped_equal", [n2, n1], [a, b])
    code("code_sides_swapped_equal", [n1, n2], [b, a])
    code("code_changes_with_first_nonce", [b64(rng.bytes(16)), n2], [a, b])
    code("code_changes_with_second_nonce", [n1, b64(rng.bytes(16))], [a, b])
    code("code_changes_with_first_mesh", [n1, n2], [a | {"mesh": CM[2]}, b])
    code("code_changes_with_second_mesh", [n1, n2], [a, b | {"mesh": CM[2]}])
    code("code_changes_with_one_head", [n1, n2], [a | {"heads": [HEADS[0], HEADS[1]]}, b])
    code("code_changes_with_count", [n1, n2], [a | {"count": 8}, b])
    code("code_head_order_irrelevant", [n1, n2], [a | {"heads": list(reversed(HEADS))}, b])
    code("code_same_nonce_twice", [n1, n1], [a, b])
    code("code_same_mesh_both_sides", [n1, n2], [a, a])
    code("code_nonce_15_bytes", [b64(bytes(15)), n2], [a, b])
    code("code_nonce_not_base64", ["***", n2], [a, b])
    code("code_three_nonces", [n1, n2, n1], [a, b])
    code("code_side_missing_heads", [n1, n2], [{"mesh": CM[0], "count": 7}, b])
    code("code_side_not_an_object", [n1, n2], ["x", b])
    code("code_side_bad_mesh", [n1, n2], [a | {"mesh": "x"}, b])
    code("code_side_count_negative", [n1, n2], [a | {"count": -1}, b])
    code("code_sides_not_a_list", [n1, n2], {"a": 1})
    code("code_nonces_not_a_list", "ab", [a, b])

    def zeros(pair: list[bytes], result: dict[str, Any]) -> bool:
        return result["ok"]["display"].startswith("0000 ") if "ok" in result else False

    def disordered(pair: list[bytes], result: dict[str, Any]) -> bool:
        texts = [b64(n) for n in pair]
        return sorted(texts) != [b64(n) for n in sorted(pair)]

    def guarded(check: Any) -> Any:
        def inner(pair: list[bytes], result: dict[str, Any]) -> bool:
            return check(pair, {"ok": result})

        return inner

    code("code_display_leading_zeros", _find_nonces(guarded(zeros)), [a, b])
    code("code_nonce_text_order_differs_from_byte_order", _find_nonces(guarded(disordered)), [a, b])

    rosters = gpd.Mesh("code-roster", ALICE)
    rosters.add(BOB)
    stranger = copy.deepcopy(rosters.entries[-1])
    m = rosters
    m.write("stranger", "add", MALLORY, {"device": CAROL.record(m.mesh)})
    add("roster_digest_plain", "contact_roster", {"mesh": rosters.mesh, "entries": wire(rosters.entries[:2])})
    add("roster_digest_drops_unverifiable_entry", "contact_roster", {"mesh": rosters.mesh, "entries": wire(rosters.entries)})
    add("roster_digest_arrival_order_irrelevant", "contact_roster", {"mesh": rosters.mesh, "entries": wire(list(reversed(rosters.entries)))})
    add("roster_digest_duplicates_idempotent", "contact_roster", {"mesh": rosters.mesh, "entries": wire(rosters.entries[:2] + [stranger])})
    m.write("child", "rename", ALICE, {"device": BOB.id, "label": "x"}, presence=True, follows=[gpd.phi.entry_id(m.entries[-1])])
    add("roster_digest_keeps_a_followed_unverifiable_entry", "contact_roster", {"mesh": rosters.mesh, "entries": wire(rosters.entries)})
    add("roster_digest_no_genesis", "contact_roster", {"mesh": rosters.mesh, "entries": wire(rosters.entries[1:2])})


def contact_fold_cases(add: Any) -> None:
    def fold(name: str, log: Log, **extra: Any) -> None:
        add(name, "contact_fold", state(log, **extra))

    t = T0 + 10 * MIN

    log = Log("verified_with_consent")
    cput(log, "put", ALICE, t, cvalue(0))
    fold("fold_verified_with_consent", log)

    log = Log("verified_by_pocket")
    cput(log, "put", BOB, t, cvalue(0))
    fold("fold_verified_by_pocket_device", log)

    log = Log("verified_without_consent")
    cput(log, "put", ALICE, t, cvalue(0), consent=False)
    fold("fold_verified_without_consent", log)

    log = Log("unverified")
    cput(log, "put", ALICE, t, cvalue(0, state="unverified"))
    fold("fold_unverified_needs_no_consent", log)

    log = Log("unverified_with_consent")
    value = cvalue(0, state="unverified")
    cput(log, "put", ALICE, t, value, consent=consent_for(value, ALICE, t - 1000, "contact"))
    fold("fold_unverified_with_consent_is_malformed", log)

    log = Log("unverified_by_node")
    cput(log, "put", NODE, t, cvalue(0, state="unverified"))
    fold("fold_unverified_by_node_allowed", log)

    log = Log("node_consent")
    value = cvalue(0)
    cput(log, "put", NODE, t, value, consent=consent_for(value, NODE, t - 1000, "contact"))
    fold("fold_node_consent", log)

    log = Log("satellite_consent")
    value = cvalue(0)
    cput(log, "put", CAROL, t, value)
    fold("fold_satellite_consent_allowed", log)

    for label, delta in (
        ("window_600000_before", 600000),
        ("window_600001_before", 600001),
        ("window_same_instant", 0),
        ("window_1_after", -1),
    ):
        log = Log(label)
        value = cvalue(0)
        cput(log, "put", ALICE, t, value, consent=consent_for(value, ALICE, t - delta, "contact"))
        fold("fold_stale_" + label, log)

    log = Log("wrong_kind")
    value = cvalue(0)
    cput(log, "put", ALICE, t, value, consent=consent_for(value, ALICE, t - 1000, "share"))
    fold("fold_consent_of_other_kind", log)

    log = Log("wrong_digest")
    cput(log, "put", ALICE, t, cvalue(0), consent=consent_for(cvalue(0), ALICE, t - 1000, "contact", digest=crypto.sha256_id(b"other")))
    fold("fold_consent_wrong_digest", log)

    log = Log("wrong_value")
    cput(log, "put", ALICE, t, cvalue(0), consent=consent_for(cvalue(0, name="Other"), ALICE, t - 1000, "contact"))
    fold("fold_consent_for_other_value", log)

    log = Log("bad_signature")
    value = cvalue(0)
    cput(log, "put", ALICE, t, value, consent=gcv.tampered(consent_for(value, ALICE, t - 1000, "contact")))
    fold("fold_consent_tampered_signature", log)

    log = Log("high_s")
    cput(log, "put", ALICE, t, value, consent=gcv.high_s(consent_for(value, ALICE, t - 1000, "contact")))
    fold("fold_consent_high_s_signature", log)

    log = Log("unknown_device")
    cput(log, "put", ALICE, t, value, consent=consent_for(value, MALLORY, t - 1000, "contact"))
    fold("fold_consent_unknown_device", log)

    log = Log("foreign_feed")
    cput(log, "put", CAROL, t, value, consent=consent_for(value, ALICE, t - 1000, "contact"))
    fold("fold_consent_on_another_members_feed", log)

    log = Log("malformed_consent")
    cput(log, "put", ALICE, t, value, consent={"v": 1, "kind": "contact"})
    fold("fold_consent_malformed_object", log)

    log = Log("recarried")
    first = cput(log, "put", ALICE, t, value)
    cput(log, "recarry", BOB, t + 5000, value, consent=first["body"]["consent"])
    cput(log, "downgrade", ALICE, t + 10000, cvalue(0, state="unverified"))
    cput(log, "recarry_late", BOB, t + 15000, value, consent=first["body"]["consent"])
    fold("fold_recarried_verified_put_on_another_feed", log)

    log = Log("recarried_same_feed")
    first = cput(log, "put", ALICE, t, value)
    cput(log, "downgrade", ALICE, t + 10000, cvalue(0, state="unverified"))
    cput(log, "recarry", ALICE, t + 20000, value, consent=first["body"]["consent"])
    fold("fold_recarried_on_same_feed_inside_window", log)

    log = Log("recarried_after_window")
    first = cput(log, "put", ALICE, t, value)
    cput(log, "downgrade", ALICE, t + 10000, cvalue(0, state="unverified"))
    cput(log, "recarry", ALICE, t + 11 * MIN, value, consent=first["body"]["consent"])
    fold("fold_recarried_after_window", log)

    log = Log("downgrade_then_verify")
    cput(log, "put", ALICE, t, value)
    cput(log, "downgrade", BOB, t + MIN, cvalue(0, state="unverified"))
    fold("fold_downgrade_by_another_device", log)
    cput(log, "again", ALICE, t + 20 * MIN, cvalue(0, heads=HEADS, count=9, ms=t + 20 * MIN))
    fold("fold_downgrade_then_new_verify", log)

    log = Log("older_put_after_newer")
    cput(log, "new", ALICE, t + MIN, cvalue(0, name="Newer"))
    cput(log, "old", BOB, t, cvalue(0, name="Older"))
    fold("fold_order_by_clock_not_arrival", log)

    log = Log("clock_tie")
    cput(log, "a", ALICE, t, cvalue(0, name="Alpha"))
    cput(log, "b", BOB, t, cvalue(0, name="Beta"))
    fold("fold_clock_tie_broken_by_feed", log)

    log = Log("removal")
    cput(log, "put", ALICE, t, value)
    cremove(log, "remove", BOB, t + MIN, 0)
    fold("fold_removal", log)

    log = Log("removal_then_put")
    cput(log, "put", ALICE, t, value)
    cremove(log, "remove", BOB, t + MIN, 0)
    cput(log, "again", ALICE, t + 2 * MIN, cvalue(0, state="unverified"))
    fold("fold_put_after_removal", log)

    log = Log("removal_by_removed_device")
    cput(log, "put", ALICE, t, value)
    cremove(log, "remove", DAVE, t + MIN, 0)
    fold("fold_removal_by_removed_device_stands", log)

    log = Log("removal_by_stranger")
    cput(log, "put", ALICE, t, value)
    cremove(log, "remove", MALLORY, t + MIN, 0)
    fold("fold_removal_by_stranger", log)

    log = Log("removal_with_consent")
    cput(log, "put", ALICE, t, value)
    cremove(log, "remove", BOB, t + MIN, 0, consent=consent_for(None, BOB, t, "contact"))
    fold("fold_removal_with_consent_field", log)

    log = Log("unverified_by_stranger")
    cput(log, "put", MALLORY, t, cvalue(0, state="unverified"))
    fold("fold_unverified_by_stranger", log)

    log = Log("approver_removed")
    old = cvalue(0, state="unverified")
    cput(log, "old", ALICE, t, old)
    gone = cvalue(0, name="ByDave")
    cput(log, "by_removed", DAVE, t + MIN, gone)
    fold("fold_verifying_device_removed_older_put_stands", log)

    log = Log("self")
    cput(log, "put", ALICE, t, cvalue(0) | {"mesh": OWN}, key=OWN.removeprefix("sha256:"))
    fold("fold_own_mesh_refused", log)

    log = Log("two_contacts")
    cput(log, "a", ALICE, t, cvalue(1, name="Cy"))
    cput(log, "b", ALICE, t + 1000, cvalue(0, name="Bea"))
    cput(log, "c", ALICE, t + 2000, cvalue(2, name="Di", state="unverified"))
    fold("fold_three_contacts_sorted_by_mesh", log)
    fold("fold_contact_cap_keeps_the_earliest", log, limits={"contacts": 2})
    cput(log, "reverify", ALICE, t + 3000, cvalue(1, name="Cy again", ms=t + 3000))
    fold("fold_contact_cap_ignores_later_updates", log, limits={"contacts": 2})

    log = Log("removed_cutoff")
    cput(log, "put", ALICE, t, cvalue(0, state="unverified"))
    for n in range(6):
        cput(log, f"dave{n}", DAVE, t + (n + 1) * 1000, cvalue(0, name=f"Dave{n}", state="unverified"))
    fold("fold_removed_device_events_past_its_cutoff_are_ignored", log)

    log = Log("no_mesh")
    cput(log, "put", ALICE, t, cvalue(0, state="unverified"))
    add("fold_without_a_mesh_id", "contact_fold", {k: v for k, v in state(log).items() if k != "mesh"})
    add("fold_with_a_bad_mesh_id", "contact_fold", state(log) | {"mesh": "x"})

    for name, change in (
        ("scope_not_mesh", {"scope": "personal"}),
        ("labels_not_private_owner", {"labels": {"confidentiality": "private", "integrity": "none"}}),
        ("labels_public", {"labels": {"confidentiality": "public", "integrity": "owner"}}),
    ):
        log = Log("shape_" + name)
        cput(log, "put", ALICE, t, cvalue(0, state="unverified"), **change)
        fold("fold_" + name, log)

    log = Log("body_extra")
    log.add("put", ALICE, t, {"key": contacts.CONTACT_PREFIX + CM[0][7:], "value": cvalue(0, state="unverified"), "extra": 1})
    fold("fold_body_with_extra_field", log)

    log = Log("bad_record")
    cput(log, "put", ALICE, t, cvalue(0, name=""))
    fold("fold_malformed_record_ignored", log)

    log = Log("bad_key")
    log.add("put", ALICE, t, {"key": contacts.CONTACT_PREFIX + "ABC", "value": None})
    fold("fold_malformed_key_ignored", log)

    log = Log("reused")
    first = cput(log, "a", ALICE, t, cvalue(0))
    cput(log, "b", ALICE, t + 5000, cvalue(0), consent=first["body"]["consent"])
    fold("fold_same_consent_twice", log)

    log = Log("marks")
    mput(log, "one", ALICE, t, 0, HEADS[:1], 3, [DAVE.id])
    mput(log, "two", BOB, t + MIN, 0, HEADS[:2], 5, [CAROL.id])
    mput(log, "three", ALICE, t + 2 * MIN, 0, HEADS[:1], 4, [])
    fold("fold_marks_merge_union_and_greatest_count", log)

    log = Log("marks_tie")
    mput(log, "one", ALICE, t, 0, HEADS[:1], 5, [DAVE.id])
    mput(log, "two", BOB, t + MIN, 0, HEADS[:2], 5, [])
    fold("fold_marks_equal_count_latest_event_wins", log)

    log = Log("marks_node")
    mput(log, "node", NODE, t, 0, HEADS[:1], 3, [BOB.id])
    fold("fold_mark_from_node_refused", log)

    log = Log("marks_satellite")
    mput(log, "satellite", CAROL, t, 0, HEADS[:1], 3, [BOB.id])
    fold("fold_mark_from_satellite_refused", log)

    log = Log("marks_stranger")
    mput(log, "stranger", MALLORY, t, 0, HEADS[:1], 3, [BOB.id])
    fold("fold_mark_from_stranger_refused", log)

    log = Log("marks_removed_writer")
    mput(log, "removed", DAVE, t, 0, HEADS[:1], 3, [BOB.id])
    fold("fold_mark_from_removed_device_refused", log)

    log = Log("marks_malformed")
    mput(log, "unsorted", ALICE, t, 0, HEADS[:1], 3, sorted([DAVE.id, BOB.id], reverse=True))
    mput(log, "count_below_heads", ALICE, t + 1000, 0, HEADS, 2, [])
    mput(log, "self", ALICE, t + 2000, 0, HEADS[:1], 3, [])
    log.events[-1]["body"]["key"] = contacts.MARK_PREFIX + OWN.removeprefix("sha256:")
    fold("fold_mark_malformed_and_own_mesh", log)

    log = Log("marks_without_record")
    mput(log, "one", ALICE, t, 3, HEADS[:1], 3, [BOB.id])
    cput(log, "put", ALICE, t + MIN, cvalue(0))
    fold("fold_mark_without_contact_record_kept", log)


def _base(label: str) -> Any:
    m = gpd.Mesh("follow-" + label, ALICE)
    m.add(BOB)
    return m


def _record(m: Any, name: str = "Bea") -> dict[str, Any]:
    return {
        "v": 1,
        "mesh": m.mesh,
        "name": name,
        "state": "verified",
        "heads": m.heads(),
        "count": len(m.entries),
        "ms": T0,
    }


def follow_cases(add: Any) -> None:
    def follow(name: str, m: Any, record: dict[str, Any], entries: list[Any] | None = None) -> dict[str, Any]:
        return add(
            name,
            "contact_follow",
            {"mesh": m.mesh, "record": record, "entries": wire(m.entries if entries is None else entries)},
        )["ok"]

    m = _base("unchanged")
    follow("follow_unchanged", m, _record(m))

    m = _base("arrival")
    rec = _record(m)
    follow("follow_entries_in_reverse_arrival_order", m, rec, list(reversed(m.entries)))

    m = _base("add")
    rec = _record(m)
    m.add(CAROL)
    follow("follow_valid_add_notice", m, rec)

    m = _base("remove")
    rec = _record(m)
    m.remove(ALICE, BOB)
    follow("follow_valid_removal_marks_removed", m, rec)

    m = _base("rotate")
    rec = _record(m)
    m.write("rename", "rename", ALICE, {"device": BOB.id, "label": "Phone"}, presence=True)
    follow("follow_rename_is_silent", m, rec)

    m = _base("recovery")
    rec = _record(m)
    m.write("recovery_add", "add", "recovery", {"device": CAROL.record(m.mesh)})
    follow("follow_recovery_key_add_notices", m, rec)

    m = _base("stop")
    m.write("stop", "stop", ALICE, {"counter": 1})
    rec = _record(m)
    m.write("resume", "resume", ALICE, {"counter": 1})
    follow("follow_stop_and_resume_are_silent", m, rec)

    m = _base("stranger")
    rec = _record(m)
    m.write("stranger", "add", MALLORY, {"device": CAROL.record(m.mesh)}, follows=m.heads())
    ok = follow("follow_stranger_entry_changes_nothing", m, rec)
    assert ok["follows"] and ok["roster"]["heads"] == rec["heads"] and not ok["notices"]

    m = _base("stranger_child")
    rec = _record(m)
    first = m.write("stranger", "add", MALLORY, {"device": CAROL.record(m.mesh)}, follows=m.heads())
    m.write("child", "rename", ALICE, {"device": BOB.id, "label": "x"}, presence=True, follows=[first])
    ok = follow("follow_stranger_entry_followed_by_a_valid_one_stays", m, rec)
    assert ok["follows"] and not ok["dropped"] and ok["roster"]["count"] == rec["count"] + 2

    m = _base("stranger_then_removal")
    rec = _record(m)
    first = m.write("stranger", "add", MALLORY, {"device": CAROL.record(m.mesh)}, follows=m.heads())
    m.write("removal", "remove", ALICE, {"device": BOB.id, "cutoff": {BOB.feed: 0}}, follows=[first])
    ok = follow("follow_removal_built_on_a_stranger_entry_still_counts", m, rec)
    assert ok["follows"] and ok["mark"]["removed"] == [BOB.id] and not ok["dropped"]

    m = _base("stranger_chain")
    rec = _record(m)
    first = m.write("stranger", "add", MALLORY, {"device": CAROL.record(m.mesh)}, follows=m.heads())
    m.write("second", "add", MALLORY, {"device": CAROL.record(m.mesh)}, follows=[first])
    ok = follow("follow_chain_of_unfollowed_stranger_entries_is_dropped", m, rec)
    assert len(ok["dropped"]) == 2 and ok["roster"]["heads"] == rec["heads"]

    def corrupt(label: str, tamper: Any, kind: str = "add") -> None:
        m = _base(label)
        rec = _record(m)
        if kind == "add":
            m.add(CAROL)
        tamper(m)
        follow(f"follow_unverifiable_{label}", m, rec)

    def bad_sig(m: Any) -> None:
        m.entries[-1]["sig"] = b64(bytes(64))

    def bad_presence(m: Any) -> None:
        m.entries[-1]["presence_sig"] = b64(bytes(64))

    corrupt("bad_signature", bad_sig)
    corrupt("bad_presence_signature", bad_presence)

    m = _base("author_removed")
    m.remove(ALICE, BOB)
    rec = _record(m)
    m.write("late", "add", BOB, {"device": CAROL.record(m.mesh)})
    follow("follow_unverifiable_author_removed", m, rec)

    other = gpd.Mesh("follow-other", ALICE)
    other.add(CAROL)
    m = _base("wrong_mesh")
    rec = _record(m)
    follow("follow_unverifiable_wrong_mesh", m, rec, m.entries + [other.entries[1]])
    follow("follow_unverifiable_extra_genesis", m, rec, m.entries + [other.entries[0]])

    m = _base("malformed")
    rec = _record(m)
    broken = copy.deepcopy(m.entries[-1])
    broken["follows"] = [m.mesh]
    broken["clock"] = {"ms": T0 + 99000, "n": 0}
    broken["extra"] = 1
    follow("follow_unverifiable_malformed_entry", m, rec, m.entries + [broken])

    m = _base("recovery_phi")
    rec = _record(m)
    body = keys.make_epoch_body("phi", gpd.seed("contacts-v1/recovery-phi"), {ALICE.id: ALICE.wrap_public}, None, DetRng("contacts-v1/recovery-phi"))
    m.write("recovery_phi", "epoch", "recovery", body)
    ok = follow("follow_unverifiable_recovery_phi_epoch", m, rec)
    assert ok["follows"] and not ok["triggers"]

    def trigger(label: str, build: Any, code: str) -> None:
        m = _base(label)
        rec = _record(m)
        build(m)
        ok = follow(f"follow_trigger_{label}", m, rec)
        assert ok["downgrade"] == "contact.roster_rejected" and code in ok["triggers"].values(), ok
        m2 = _base(label + "_seen")
        build(m2)
        rec2 = _record(m2)
        ok = follow(f"follow_trigger_{label}_seen_at_verification_stays_verified", m2, rec2)
        assert ok["follows"], ok

    def add_again(m: Any) -> None:
        m.add(BOB)

    def removed_returns(m: Any) -> None:
        m.remove(ALICE, BOB)
        m.add(BOB)

    def same_ops(m: Any) -> None:
        clone = gpd.TestDevice("contacts-v1/clone")
        clone.ops_seed = ALICE.ops_seed
        m.write("clone", "add", ALICE, {"device": clone.record(m.mesh)})

    def second_stop(m: Any) -> None:
        m.write("stop1", "stop", ALICE, {"counter": 1})
        m.write("stop2", "stop", ALICE, {"counter": 1})

    def forged_attestation(m: Any) -> None:
        m.authority(ALICE)
        statement = gpd.phi.approval_statement(gpd.approver_scalar(ALICE), "endpoint", "https://m.example/v1", NOW_S)
        sig = list(statement["sig"])
        sig[3] = "A" if sig[3] != "A" else "B"
        m.write("forged", "phi_approval", ALICE, {"action": "grant", "attestation": {**statement, "sig": "".join(sig)}})

    def malformed_attestation(m: Any) -> None:
        m.authority(ALICE)
        statement = gpd.phi.approval_statement(gpd.approver_scalar(ALICE), "endpoint", "https://m.example/v1", NOW_S)
        m.write("malformed", "phi_approval", ALICE, {"action": "grant", "attestation": {**statement, "not_after_s": statement["issued_s"]}})

    trigger("already_member", add_again, "roster.already_member")
    trigger("device_removed", removed_returns, "roster.device_removed")
    trigger("duplicate_key", same_ops, "roster.duplicate_key")
    trigger("stop_not_rising", second_stop, "roster.stop_not_rising")
    trigger("attestation_bad_signature", forged_attestation, "attestation.bad_signature")
    trigger("attestation_malformed", malformed_attestation, "attestation.malformed")

    m = _base("concurrent_same_key")
    rec = _record(m)
    heads = m.heads()
    first, second = gpd.TestDevice("contacts-v1/first"), gpd.TestDevice("contacts-v1/second")
    first.ops_seed = second.ops_seed = CAROL.ops_seed
    m.write("a", "add", ALICE, {"device": first.record(m.mesh)}, follows=heads)
    m.write("b", "add", BOB, {"device": second.record(m.mesh)}, follows=heads)
    ok = follow("follow_concurrent_adds_of_one_key_are_not_rejected", m, rec)
    assert ok["follows"] and not ok["triggers"]

    def honest(label: str, build: Any, code: str) -> None:
        m = _base(label)
        rec = _record(m)
        build(m)
        assert code in gpd.phi.fold(m.entries, m.mesh, None)["rejected"].values(), (label, code)
        ok = follow(f"follow_honest_{label}", m, rec)
        assert ok["follows"] and not ok["triggers"], ok

    def rename_unknown(m: Any) -> None:
        m.write("rename", "rename", ALICE, {"device": CAROL.id, "label": "x"}, presence=True)

    def short_cutoff(m: Any) -> None:
        m.write("remove", "remove", ALICE, {"device": BOB.id, "cutoff": {}})

    def unknown_authority(m: Any) -> None:
        key = b64(crypto.p256_public(gpd.other_scalar("contacts-v1/unknown")))
        m.write("authority", "phi_authority", ALICE, {"action": "remove", "key": key})

    def wrong_members(m: Any) -> None:
        body = keys.make_epoch_body("personal", gpd.seed("contacts-v1/wrong"), {MALLORY.id: MALLORY.wrap_public}, gpd.RECOVERY["wrap_public"], DetRng("contacts-v1/wrong"))
        m.write("epoch", "epoch", ALICE, body)

    def not_phi_member(m: Any) -> None:
        m.authority(ALICE)
        m.grant_device(ALICE, BOB)
        body = keys.make_epoch_body("phi", gpd.seed("contacts-v1/notphi"), {BOB.id: BOB.wrap_public}, None, DetRng("contacts-v1/notphi"))
        m.write("epoch", "epoch", ALICE, body)

    def unknown_epoch(m: Any) -> None:
        grants = keys.make_wrap_grants("personal", BOB.id, BOB.wrap_public, {crypto.sha256_id(b"contacts-v1/none"): gpd.seed("contacts-v1/none")}, DetRng("contacts-v1/none"))
        m.write("wrap", "wrap", ALICE, {"device": BOB.id, "grants": grants, "group": "personal"})

    def resume_alone(m: Any) -> None:
        m.write("resume", "resume", ALICE, {"counter": 1})

    def unknown_authority_attestation(m: Any) -> None:
        m.grant(ALICE, "https://m.example/v1")

    honest("unknown_device", rename_unknown, "roster.unknown_device")
    honest("cutoff_incomplete", short_cutoff, "roster.cutoff_incomplete")
    honest("unknown_authority", unknown_authority, "roster.unknown_authority")
    honest("epoch_wrong_members", wrong_members, "roster.epoch_wrong_members")
    honest("not_phi_member", not_phi_member, "roster.not_phi_member")
    honest("unknown_epoch", unknown_epoch, "roster.unknown_epoch")
    honest("resume_not_stopped", resume_alone, "roster.resume_not_stopped")
    honest("attestation_unknown_authority", unknown_authority_attestation, "attestation.unknown_authority")

    m = _base("void_cascade")
    heads = m.heads()
    rec = _record(m)
    m.write("bob_adds", "add", BOB, {"device": CAROL.record(m.mesh)}, follows=heads)
    added = gpd.phi.entry_id(m.entries[-1])
    m.write("alice_removes", "remove", ALICE, {"device": BOB.id, "cutoff": {BOB.feed: 0}}, follows=heads)
    m.write("alice_renames", "rename", ALICE, {"device": CAROL.id, "label": "x"}, presence=True, follows=[added])
    state = gpd.phi.fold(m.entries, m.mesh, None)
    assert "roster.unknown_device" in state["rejected"].values() and state["void"], state
    ok = follow("follow_honest_rejection_after_a_voided_ancestor", m, rec)
    assert ok["follows"] and not ok["triggers"]

    m = _base("trigger_in_a_later_branch")
    rec = _record(m)
    m.add(BOB)
    m.add(CAROL)
    follow("follow_trigger_with_later_valid_entries", m, rec)

    m = _base("too_many_heads")
    rec = _record(m)
    heads = m.heads()
    for n in range(65):
        m.write(f"r{n}", "rename", ALICE, {"device": BOB.id, "label": f"n{n}"}, presence=True, follows=heads)
    follow("follow_roster_with_more_heads_than_a_mark_holds", m, rec)

    m = _base("heads_missing")
    rec = _record(m)
    m.add(CAROL)
    rec_full = _record(m)
    follow("follow_heads_missing_from_cache", m, rec_full, m.entries[:-1])

    m = _base("no_genesis")
    rec = _record(m)
    follow("follow_no_genesis_in_cache", m, rec, m.entries[1:])

    m = _base("empty_cache")
    rec = _record(m)
    follow("follow_empty_cache", m, rec, [])

    m = _base("wrong_record_mesh")
    rec = _record(m)
    add("follow_record_for_another_mesh", "contact_follow", {"mesh": CM[0], "record": rec, "entries": wire(m.entries)})
    add("follow_unverified_record", "contact_follow", {"mesh": m.mesh, "record": rec | {"state": "unverified", "heads": [], "count": 0}, "entries": wire(m.entries)})


def reader_cases(add: Any) -> None:
    mark = {"heads": HEADS[:2], "count": 5, "removed": [DAVE.id]}

    def reader(name: str, mark_value: Any, device: Any, kind: str, entries: list[str]) -> None:
        add(name, "contact_reader", {"mark": mark_value, "reader": {"device": device, "kind": kind, "entries": entries}})

    reader("reader_current_desktop", mark, BOB.id, "desktop", HEADS)
    reader("reader_current_pocket", mark, BOB.id, "pocket", HEADS[:2])
    reader("reader_without_any_mark", None, BOB.id, "desktop", [])
    reader("reader_satellite", mark, CAROL.id, "satellite", HEADS)
    reader("reader_node", mark, NODE.id, "node", HEADS)
    reader("reader_removed_device", mark, DAVE.id, "desktop", HEADS)
    reader("reader_roster_lacks_a_named_head", mark, BOB.id, "desktop", HEADS[:1])
    reader("reader_kind_checked_before_removal", mark, DAVE.id, "node", HEADS)
    reader("reader_removed_checked_before_stale", mark, DAVE.id, "desktop", [])


def share_cases(add: Any) -> None:
    t = T0 + 10 * MIN
    chats = CHATS[:2]

    def fold(name: str, log: Log, **extra: Any) -> None:
        add(name, "share_fold", state(log, chats=chats, **extra))

    def base(label: str) -> Log:
        log = Log("share_" + label)
        cput(log, "verify0", ALICE, T0 + MIN, cvalue(0))
        cput(log, "unverified1", ALICE, T0 + 2 * MIN, cvalue(1, name="Cy", state="unverified"))
        return log

    g0, g1, g2, g3 = GIDS[:4]

    log = base("effect")
    gput(log, "grant", ALICE, t, gvalue(g0))
    fold("share_grant_in_effect", log)

    log = base("by_pocket")
    gput(log, "grant", BOB, t, gvalue(g0, phi=True))
    fold("share_grant_by_pocket_device_with_phi_flag", log)

    log = base("unverified")
    gput(log, "grant", ALICE, t, gvalue(g0, contact=1))
    fold("share_grant_to_unverified_contact_waits", log)
    add("share_propose_unverified_contact", "share_propose", state(log, contact=CM[1]))
    add("share_propose_unknown_contact", "share_propose", state(log, contact=CM[2]))
    add("share_propose_verified_contact", "share_propose", state(log, contact=CM[0]))

    log = base("unknown_contact")
    gput(log, "grant", ALICE, t, gvalue(g0, contact=2))
    fold("share_grant_to_unknown_contact_is_orphaned", log)

    log = base("downgrade_resume")
    gput(log, "grant", ALICE, t, gvalue(g0))
    cput(log, "downgrade", BOB, t + MIN, cvalue(0, state="unverified"))
    fold("share_contact_downgraded_grant_waits", log)
    cput(log, "reverify", ALICE, t + 20 * MIN, cvalue(0, heads=HEADS, count=9, ms=t + 20 * MIN))
    fold("share_contact_reverified_grant_resumes", log)

    log = base("contact_removed")
    gput(log, "grant", ALICE, t, gvalue(g0))
    cremove(log, "remove", ALICE, t + MIN, 0)
    fold("share_contact_removed_grant_orphaned", log)

    log = base("chat_missing")
    gput(log, "grant", ALICE, t, gvalue(g0, chat=2))
    fold("share_chat_missing", log)

    log = base("revoked")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", BOB, t + MIN, g0)
    fold("share_revoked", log)

    log = base("revoked_then_reput")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", ALICE, t + MIN, g0)
    gput(log, "reput", ALICE, t + 2 * MIN, gvalue(g0, ms=t + 2 * MIN))
    fold("share_revoked_then_put_again_stays_revoked", log)

    log = base("revoke_before_put")
    grevoke(log, "revoke", ALICE, t - MIN, g0)
    gput(log, "grant", ALICE, t, gvalue(g0))
    fold("share_revoke_ordered_before_the_put", log)

    log = base("revoke_by_removed")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", DAVE, t + MIN, g0)
    fold("share_revoke_by_removed_device_stands", log)

    log = base("revoke_by_node")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", NODE, t + MIN, g0)
    fold("share_revoke_by_node_needs_no_consent_in_the_fold", log)

    log = base("revoke_by_stranger")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", MALLORY, t + MIN, g0)
    fold("share_revoke_by_stranger", log)

    log = base("revoke_unknown_id")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", ALICE, t + MIN, g1)
    fold("share_revoke_of_an_id_never_granted", log)

    log = base("revoke_malformed")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "null", ALICE, t + MIN, g0, value=None)
    grevoke(log, "extra", ALICE, t + 2 * MIN, g0, value={"v": 1, "ms": 1, "x": 2})
    grevoke(log, "version", ALICE, t + 3 * MIN, g0, value={"v": 2, "ms": 1})
    fold("share_revoke_malformed_values", log)

    log = base("supersede")
    gput(log, "old", ALICE, t, gvalue(g0))
    gput(log, "new", ALICE, t + MIN, gvalue(g1, phi=True, supersedes=g0, ms=t + MIN))
    fold("share_supersede", log)

    log = base("supersede_revoke_new")
    gput(log, "old", ALICE, t, gvalue(g0))
    gput(log, "new", ALICE, t + MIN, gvalue(g1, phi=True, supersedes=g0, ms=t + MIN))
    grevoke(log, "revoke", ALICE, t + 2 * MIN, g1)
    fold("share_revoking_the_newer_grant_keeps_the_older_dead", log)

    log = base("supersede_chain")
    gput(log, "a", ALICE, t, gvalue(g0))
    gput(log, "b", ALICE, t + MIN, gvalue(g1, supersedes=g0, ms=t + MIN))
    gput(log, "c", ALICE, t + 2 * MIN, gvalue(g2, supersedes=g1, ms=t + 2 * MIN))
    grevoke(log, "revoke", ALICE, t + 3 * MIN, g2)
    fold("share_supersede_chain_revoke_last", log)

    log = base("supersede_bad_consent")
    gput(log, "old", ALICE, t, gvalue(g0))
    gput(log, "new", ALICE, t + MIN, gvalue(g1, supersedes=g0, ms=t + MIN), consent=False)
    fold("share_superseder_without_consent_leaves_the_older_standing", log)

    for label, change in (
        ("other_contact", {"contact": 1}),
        ("other_chat", {"chat": 1}),
    ):
        log = base("supersede_" + label)
        gput(log, "old", ALICE, t, gvalue(g0))
        gput(log, "new", ALICE, t + MIN, gvalue(g1, supersedes=g0, ms=t + MIN, **change))
        fold("share_supersedes_grant_for_" + label, log)

    log = base("supersede_fork")
    gput(log, "old", ALICE, t, gvalue(g0))
    gput(log, "a", ALICE, t + MIN, gvalue(g1, supersedes=g0, ms=t + MIN))
    gput(log, "b", ALICE, t + 2 * MIN, gvalue(g2, supersedes=g0, ms=t + 2 * MIN))
    fold("share_second_superseder_of_one_target_is_refused", log)

    log = base("supersede_unknown")
    gput(log, "new", ALICE, t, gvalue(g1, supersedes=g2))
    fold("share_supersedes_unknown_grant", log)

    log = base("supersede_later")
    gput(log, "new", ALICE, t, gvalue(g1, supersedes=g0))
    gput(log, "old", ALICE, t + MIN, gvalue(g0, ms=t + MIN))
    fold("share_supersedes_a_grant_ordered_after_it", log)

    log = base("supersede_self")
    gput(log, "self", ALICE, t, gvalue(g0, supersedes=g0))
    fold("share_supersedes_itself", log)

    log = base("supersede_revoked_old")
    gput(log, "old", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", ALICE, t + MIN, g0)
    gput(log, "new", ALICE, t + 2 * MIN, gvalue(g1, supersedes=g0, ms=t + 2 * MIN))
    fold("share_supersede_a_revoked_grant_is_a_new_grant", log)

    log = base("two_contacts")
    cput(log, "verify2", ALICE, T0 + 3 * MIN, cvalue(2, name="Di"))
    gput(log, "a", ALICE, t, gvalue(g0))
    gput(log, "b", ALICE, t + MIN, gvalue(g1, contact=2))
    gput(log, "c", ALICE, t + 2 * MIN, gvalue(g2, chat=1))
    fold("share_grants_in_event_order", log)
    fold("share_cap_keeps_the_earliest", log, limits={"grants": 2})

    for kind in ("surface", "artifact", "file"):
        log = base("kind_" + kind)
        item = {"kind": kind, kind: "x"}
        gput(log, "grant", ALICE, t, gvalue(g0, item=item))
        fold(f"share_item_kind_{kind}_reserved", log)

    for label, item in (
        ("unknown", {"kind": "folder", "chat": CHATS[0]}),
        ("chat_with_extra_field", {"kind": "chat", "chat": CHATS[0], "title": "x"}),
        ("chat_not_an_id", {"kind": "chat", "chat": "abc"}),
        ("chat_missing_chat", {"kind": "chat"}),
        ("not_an_object", "chat"),
    ):
        log = base("item_" + label)
        gput(log, "grant", ALICE, t, gvalue(g0, item=item))
        fold("share_item_" + label, log)

    value = gvalue(g0)
    for label, change in (
        ("kind_edit", {"kind": "edit"}),
        ("phi_not_boolean", {"phi": 1}),
        ("extra_field", {"extra": 1}),
        ("version_2", {"v": 2}),
        ("id_differs_from_key", {"id": g1}),
        ("contact_not_an_id", {"contact": "x"}),
        ("supersedes_not_hex", {"supersedes": "xyz"}),
        ("ms_negative", {"ms": -1}),
    ):
        log = base("value_" + label)
        gput(log, "grant", ALICE, t, value | change, key=g0)
        fold("share_value_" + label, log)

    log = base("value_missing_field")
    gput(log, "grant", ALICE, t, {k: v for k, v in value.items() if k != "phi"})
    fold("share_value_missing_field", log)

    log = base("null_value")
    log.add("null", ALICE, t, {"key": contacts.GRANT_PREFIX + g0, "value": None})
    fold("share_grant_removal_is_malformed", log)

    log = base("consent_missing")
    gput(log, "grant", ALICE, t, value, consent=False)
    fold("share_consent_missing", log)

    log = base("consent_node")
    gput(log, "grant", NODE, t, value, consent=consent_for(value, NODE, t - 1000, "share"))
    fold("share_consent_from_node", log)

    log = base("consent_contact_kind")
    gput(log, "grant", ALICE, t, value, consent=consent_for(value, ALICE, t - 1000, "contact"))
    fold("share_consent_of_contact_kind", log)

    log = base("consent_stale")
    gput(log, "grant", ALICE, t, value, consent=consent_for(value, ALICE, t - 600001, "share"))
    fold("share_consent_stale", log)

    log = base("consent_in_window")
    gput(log, "grant", ALICE, t, value, consent=consent_for(value, ALICE, t - 600000, "share"))
    fold("share_consent_at_window_edge", log)

    log = base("consent_foreign")
    gput(log, "grant", CAROL, t, value, consent=consent_for(value, ALICE, t - 1000, "share"))
    fold("share_consent_on_another_members_feed", log)

    log = base("consent_bad_signature")
    gput(log, "grant", ALICE, t, value, consent=gcv.tampered(consent_for(value, ALICE, t - 1000, "share")))
    fold("share_consent_tampered_signature", log)

    log = base("consent_unknown_device")
    gput(log, "grant", ALICE, t, value, consent=consent_for(value, MALLORY, t - 1000, "share"))
    fold("share_consent_unknown_device", log)

    log = base("consent_removed_device")
    gput(log, "grant", DAVE, t, value, consent=consent_for(value, DAVE, t - 1000, "share"))
    fold("share_consent_from_removed_device", log)

    log = base("consent_wrong_digest")
    gput(log, "grant", ALICE, t, value, consent=consent_for(gvalue(g0, phi=True), ALICE, t - 1000, "share"))
    fold("share_consent_for_other_value", log)

    log = base("consent_reused")
    first = gput(log, "a", ALICE, t, value)
    gput(log, "b", ALICE, t + 5000, gvalue(g1), consent=first["body"]["consent"])
    gput(log, "c", ALICE, t + 6000, value, consent=first["body"]["consent"])
    fold("share_consent_reused", log)

    log = base("recarried")
    first = gput(log, "a", ALICE, t, value)
    grevoke(log, "revoke", ALICE, t + MIN, g0)
    gput(log, "b", BOB, t + 2 * MIN, value, consent=first["body"]["consent"])
    fold("share_recarried_grant_on_another_feed", log)

    log = base("grant_rewritten")
    gput(log, "a", ALICE, t, gvalue(g0))
    gput(log, "b", ALICE, t + MIN, gvalue(g0, phi=True, ms=t + MIN))
    fold("share_same_id_first_valid_put_wins", log)

    log = base("grant_reput_after_supersede")
    gput(log, "old", ALICE, t, gvalue(g0))
    gput(log, "new", ALICE, t + MIN, gvalue(g1, phi=True, supersedes=g0, ms=t + MIN))
    gput(log, "reput", ALICE, t + 2 * MIN, gvalue(g0, ms=t + 2 * MIN))
    fold("share_reput_of_a_superseded_id_is_ignored", log)

    log = base("grant_reput_invalid_first")
    gput(log, "bad", ALICE, t, gvalue(g0), consent=False)
    gput(log, "good", ALICE, t + MIN, gvalue(g0, ms=t + MIN))
    fold("share_a_refused_put_does_not_take_the_id", log)

    log = base("contact_removed_revoked")
    gput(log, "grant", ALICE, t, gvalue(g0))
    grevoke(log, "revoke", ALICE, t + MIN, g0)
    cremove(log, "remove", ALICE, t + MIN + 1, 0)
    cput(log, "again", ALICE, t + 20 * MIN, cvalue(0, ms=t + 20 * MIN))
    fold("share_contact_removed_and_verified_again_after_revocation", log)

    log = base("contact_removed_unrevoked")
    gput(log, "grant", ALICE, t, gvalue(g0))
    cremove(log, "remove", ALICE, t + MIN, 0)
    cput(log, "again", ALICE, t + 20 * MIN, cvalue(0, ms=t + 20 * MIN))
    fold("share_contact_removed_without_revocation_grant_returns", log)

    log = base("grant_scope")
    gput(log, "a", ALICE, t, gvalue(g0), scope="personal")
    fold("share_scope_not_mesh", log)

    log = base("grant_labels")
    gput(log, "a", ALICE, t, gvalue(g0), labels={"confidentiality": "public", "integrity": "owner"})
    fold("share_labels_public", log)

    log = base("bad_key")
    log.add("a", ALICE, t, {"key": contacts.GRANT_PREFIX + "XYZ", "value": None})
    fold("share_malformed_key", log)


def classify_cases(add: Any) -> None:
    for code in (
        *contacts.UNVERIFIABLE_CODES,
        *contacts.HONEST_CODES,
        *contacts.TRIGGER_CODES,
        "roster.future_code",
        "attestation.future_code",
    ):
        add("class_" + code.replace(".", "_"), "contact_classify", {"code": code})


def render() -> dict[str, str]:
    return {"contacts-v1": json.dumps(generate(), indent=2, sort_keys=True) + "\n"}


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
