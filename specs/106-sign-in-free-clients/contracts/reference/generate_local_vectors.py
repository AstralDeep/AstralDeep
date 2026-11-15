"""Generates signed accountless roster vectors from public fixed test keys.
The preserved reference supplies cryptography helpers; expected v2 membership and refusals are explicit here."""

import argparse
import copy
import json
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(
    0, str(HERE.parents[2] / "105-decentralized-personal-mesh/contracts/reference")
)
crypto = importlib.import_module("astral_mesh_ref.crypto")
keys = importlib.import_module("astral_mesh_ref.keys")
cj = importlib.import_module("astral_mesh_ref.cj")
testkit = importlib.import_module("astral_mesh_ref.testkit")
b64url_encode, canonical = cj.b64url_encode, cj.canonical
DetRng, TestDevice, seed = testkit.DetRng, testkit.TestDevice, testkit.seed


def generate():
    alice, bob = TestDevice("local-v2/alice"), TestDevice("local-v2/bob")
    recovery = keys.derive_recovery(seed("local-v2/recovery"))
    cases = []

    def identifier(entry):
        return crypto.object_id(
            {k: v for k, v in entry.items() if k not in ("sig", "presence_sig")}
        )

    def sign(
        kind, mesh, follows, body, tick, presence=False, domain="astral.roster/v2"
    ):
        value = {
            "v": 2,
            "type": kind,
            "mesh": mesh,
            "follows": sorted(follows),
            "clock": {"ms": 1794441600000 + tick, "n": 0},
            "author": alice.id,
            "body": body,
        }
        message = domain.encode() + b"\n" + canonical(value)
        value["sig"] = b64url_encode(crypto.p256_sign(alice.identity_scalar, message))
        if presence:
            value["presence_sig"] = b64url_encode(
                crypto.p256_sign(alice.presence_scalar, message)
            )
        return value

    epoch = keys.make_epoch_body(
        "personal",
        seed("local-v2/epoch"),
        {alice.id: alice.wrap_public},
        recovery["wrap_public"],
        DetRng("local-v2/epoch"),
    )
    genesis = sign(
        "genesis",
        None,
        [],
        {
            "device": alice.record(None),
            "epoch": epoch,
            "recovery": {
                "sign": b64url_encode(recovery["sign_public"]),
                "wrap": b64url_encode(recovery["wrap_public"]),
            },
        },
        0,
        True,
    )
    mesh = identifier(genesis)
    add = sign("add", mesh, [mesh], {"device": bob.record(mesh)}, 1, True)
    add_id = identifier(add)
    stop = sign("stop", mesh, [add_id], {"counter": 1}, 2)
    resume = sign("resume", mesh, [identifier(stop)], {"counter": 1}, 3, True)
    remove = sign(
        "remove",
        mesh,
        [identifier(resume)],
        {"device": bob.id, "cutoff": {bob.feed: 0}},
        4,
        True,
    )

    def fold(name, entries, members, stop=0, rejected=None):
        cases.append(
            {
                "name": name,
                "kind": "roster_fold",
                "input": {"entries": entries, "mesh": mesh, "now_s": 1794441601},
                "expect": {
                    "ok": {
                        "mesh": mesh,
                        "members": sorted(members),
                        "stop": stop,
                        "rejected": rejected or {},
                    }
                },
            }
        )

    def shape(name, entry, error="roster.malformed"):
        cases.append(
            {
                "name": name,
                "kind": "roster_shape",
                "input": {"entry": entry},
                "expect": {"error": error},
            }
        )

    fold("issuer_free_genesis", [genesis], [alice.id])
    fold("signed_device_add", [genesis, add], [alice.id, bob.id])
    fold("arrival_order_independent", [add, genesis], [alice.id, bob.id])
    fold("duplicates_idempotent", [genesis, genesis, add, add], [alice.id, bob.id])
    fold(
        "stop_preserves_signed_membership", [genesis, add, stop], [alice.id, bob.id], 1
    )
    fold(
        "resume_requires_presence", [genesis, add, stop, resume], [alice.id, bob.id], 1
    )
    fold(
        "removal_preserves_cutoff", [genesis, add, stop, resume, remove], [alice.id], 1
    )
    old = copy.deepcopy(genesis)
    old["v"] = 1
    shape("version_1_is_unsupported", old)
    old = copy.deepcopy(genesis)
    old["body"]["issuers"] = []
    shape("genesis_has_no_issuers", old)
    old = copy.deepcopy(add)
    old["body"]["attestation"] = None
    shape("add_has_no_attestation", old)
    for kind in ("attest", "issuer"):
        old = copy.deepcopy(add)
        old["type"] = kind
        shape("removed_" + kind + "_family", old)
    old = copy.deepcopy(add)
    old["v"] = 3
    shape("future_version_is_unsupported", old)
    old = copy.deepcopy(add)
    old["unexpected"] = True
    shape("unknown_envelope_field", old)
    forged = copy.deepcopy(add)
    forged["sig"] = b64url_encode(bytes(64))
    fold(
        "invalid_identity_signature_denied",
        [genesis, forged],
        [alice.id],
        rejected={identifier(forged): "roster.bad_signature"},
    )
    forged = copy.deepcopy(add)
    forged["presence_sig"] = b64url_encode(bytes(64))
    fold(
        "invalid_presence_signature_denied",
        [genesis, forged],
        [alice.id],
        rejected={identifier(forged): "roster.bad_presence_signature"},
    )
    forged = sign(
        "add", mesh, [mesh], {"device": bob.record(mesh)}, 1, True, "astral.roster/v1"
    )
    fold(
        "v1_signing_domain_denied",
        [genesis, forged],
        [alice.id],
        rejected={identifier(forged): "roster.bad_signature"},
    )
    return {
        "format": "astral.vectors/v1",
        "name": "roster-v2",
        "spec": "106/contracts/roster-v2.md",
        "cases": cases,
    }


def generate_crypto():
    old = (
        HERE.parents[2]
        / "105-decentralized-personal-mesh/contracts/vectors/crypto.json"
    )
    doc = json.loads(old.read_text())
    doc["contexts"] = [
        c.replace("astral.roster/v1", "astral.roster/v2") for c in doc["contexts"]
    ]
    doc["name"] = "crypto-v2"
    doc["spec"] = "106/contracts/roster-v2.md"
    replacements = {}
    for case in doc["cases"]:
        value = case["input"]
        if value.get("context") == "astral.roster/v1":
            value["context"] = "astral.roster/v2"
        if case["kind"] == "sign" and value["context"] == "astral.roster/v2":
            message = b"astral.roster/v2\n" + canonical(
                {k: v for k, v in value["object"].items() if k != "sig"}
            )
            secret = bytes.fromhex(value["secret_hex"])
            sig = (
                crypto.p256_sign(int.from_bytes(secret, "big"), message)
                if value["alg"] == "p256"
                else crypto.ed25519_sign(secret, message)
            )
            replacements[case["expect"]["ok"]["sig"]] = b64url_encode(sig)
            case["expect"]["ok"]["sig"] = b64url_encode(sig)
    for case in doc["cases"]:
        if "sig" in case["input"] and case["input"]["sig"] in replacements:
            case["input"]["sig"] = replacements[case["input"]["sig"]]
    return doc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, doc in (("roster-v2", generate()), ("crypto-v2", generate_crypto())):
        output = HERE.parent / "vectors" / (name + ".json")
        content = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        if args.check:
            if output.read_text() != content:
                raise SystemExit(name + " vectors differ")
        else:
            output.write_text(content)


if __name__ == "__main__":
    main()
