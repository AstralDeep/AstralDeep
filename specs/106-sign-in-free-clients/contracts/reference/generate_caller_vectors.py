"""Generates the caller ticket vectors from public fixed test keys and checks them against the reference verifier.
It reuses the roster v2 test devices and the preserved 105 helpers, and `--check` fails when the committed file differs."""

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
caller_ticket = importlib.import_module("caller_ticket")
crypto = importlib.import_module("astral_mesh_ref.crypto")
tools = importlib.import_module("astral_mesh_ref.tools")
testkit = importlib.import_module("astral_mesh_ref.testkit")
DetRng, TestDevice, seed = testkit.DetRng, testkit.TestDevice, testkit.seed
b64 = caller_ticket.b64url_encode

NOW = 1794445200000
DAY_MS = 86400000
OUTPUT = HERE.parent / "vectors" / "caller-v1.json"


def _definition(name: str) -> str:
    return tools.definition_hash(
        name, "Public test tool " + name, {"type": "object"}, {"readOnlyHint": True}
    )


def generate() -> dict[str, Any]:
    alice = TestDevice("local-v2/alice")
    bob = TestDevice("local-v2/bob", kind="phone")
    node = TestDevice("local-v2/node", kind="node")
    mesh = crypto.sha256_id(b"astral-test/caller-v1/mesh")
    other_mesh = crypto.sha256_id(b"astral-test/caller-v1/other-mesh")
    holder = seed("caller-v1/holder")
    stranger = seed("caller-v1/stranger")
    rng = DetRng("caller-v1")
    cases: list[dict[str, Any]] = []
    echo, search = _definition("echo"), _definition("search")

    state = {
        "mesh": mesh,
        "node": node.id,
        "members": {
            alice.id: {"kind": alice.kind, "presence": b64(alice.presence_public)},
            bob.id: {"kind": bob.kind, "presence": b64(bob.presence_public)},
            node.id: {"kind": node.kind, "presence": b64(node.presence_public)},
        },
        "removed": [],
        "stop": 1,
        "revoked": [],
        "catalog": {"echo": echo, "search": search},
        "now_ms": NOW,
        "started_ms": NOW - 3600000,
    }
    request = {
        "method": "POST",
        "target": "/mcp?session=1",
        "body": '{"jsonrpc":"2.0","method":"tools/call"}',
        "tool": "echo",
    }

    def make_ticket(signer: Any = alice, **change: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "id": b64(rng.bytes(16)),
            "mesh": mesh,
            "device": alice.id,
            "node": node.id,
            "holder": b64(crypto.ed25519_public(holder)),
            "label": "Test script",
            "tools": [{"def": echo, "name": "echo"}],
            "issued_ms": NOW - 60000,
            "expires_ms": NOW - 60000 + 7 * DAY_MS,
            "stop": 1,
        }
        body.update(change)
        return caller_ticket.sign_ticket(signer.presence_scalar, body)

    def make_proof(
        minted: dict[str, Any], signer: bytes = holder, **change: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "ticket": minted["id"],
            "method": request["method"],
            "target": request["target"],
            "body": caller_ticket.body_digest(request["body"].encode()),
            "ms": NOW - 1000,
            "nonce": b64(rng.bytes(16)),
        }
        body.update(change)
        return caller_ticket.sign_proof(signer, body)

    def step(
        ticket: dict[str, Any], proof: dict[str, Any] | None = None, **req: Any
    ) -> dict[str, Any]:
        out = {
            "authorization": caller_ticket.authorization_header(ticket),
            "proof": caller_ticket.proof_header(
                proof if proof is not None else make_proof(ticket)
            ),
            "request": {**request, **req},
        }
        return out

    def verify(
        name: str, want: str | None, ticket_step: dict[str, Any], **state_change: Any
    ) -> None:
        case_input = {"state": {**state, **state_change}, **ticket_step}
        expect = caller_ticket.run("caller_verify", case_input)
        got = expect.get("error")
        if got != want:
            raise SystemExit(f"{name}: expected {want}, reference gave {expect}")
        cases.append(
            {
                "name": name,
                "kind": "caller_verify",
                "input": case_input,
                "expect": expect,
            }
        )

    ticket = make_ticket()
    verify("valid_mcp_tool", None, step(ticket))
    both = make_ticket(
        tools=[{"def": None, "name": "a2a.message"}, {"def": echo, "name": "echo"}]
    )
    verify("valid_a2a_message", None, step(both, tool="a2a.message"))
    verify("other_tool_refused", "caller.tool_not_covered", step(ticket, tool="search"))
    verify(
        "changed_definition_refused",
        "caller.tool_changed",
        step(ticket),
        catalog={"echo": search},
    )
    verify(
        "a2a_not_covered_refused",
        "caller.tool_not_covered",
        step(ticket, tool="a2a.message"),
    )
    verify("expired", "caller.expired", step(ticket), now_ms=NOW + 8 * DAY_MS)
    verify(
        "lifetime_31_days",
        "caller.malformed",
        step(make_ticket(expires_ms=NOW - 60000 + 31 * DAY_MS)),
    )
    verify(
        "lifetime_30_days_accepted",
        None,
        step(make_ticket(expires_ms=NOW - 60000 + 30 * DAY_MS)),
    )
    verify(
        "future_issued_ms",
        "caller.not_yet_valid",
        step(make_ticket(issued_ms=NOW + 300001, expires_ms=NOW + 300001 + DAY_MS)),
    )
    verify("bad_presence_signature", "caller.bad_signature", step(make_ticket(bob)))
    verify("wrong_mesh", "caller.mesh_mismatch", step(make_ticket(mesh=other_mesh)))
    verify("wrong_node", "caller.wrong_node", step(make_ticket(node=alice.id)))
    removed = make_ticket(bob, device=bob.id)
    verify("removed_device", "caller.device_removed", step(removed), removed=[bob.id])
    verify("raised_stop", "caller.stopped", step(ticket), stop=2)
    verify("revoked", "caller.revoked", step(ticket), revoked=[ticket["id"]])
    verify(
        "proof_method_mismatch",
        "caller.proof_mismatch",
        step(ticket, make_proof(ticket, method="GET")),
    )
    verify(
        "proof_target_mismatch",
        "caller.proof_mismatch",
        step(ticket, make_proof(ticket, target="/mcp?session=2")),
    )
    verify(
        "proof_body_mismatch",
        "caller.proof_mismatch",
        step(ticket, make_proof(ticket, body=caller_ticket.body_digest(b"other body"))),
    )
    verify(
        "proof_ticket_mismatch",
        "caller.proof_mismatch",
        step(ticket, make_proof(ticket, ticket=b64(rng.bytes(16)))),
    )
    verify(
        "stale_proof",
        "caller.proof_stale",
        step(ticket, make_proof(ticket, ms=NOW - 120001)),
    )
    verify(
        "proof_before_verifier_start",
        "caller.proof_stale",
        step(ticket),
        started_ms=NOW - 500,
    )
    verify(
        "bad_holder_signature",
        "caller.proof_bad_signature",
        step(ticket, make_proof(ticket, stranger)),
    )

    replay = step(ticket)
    sequence = {"state": state, "steps": [replay, replay]}
    expect = caller_ticket.run("caller_verify_sequence", sequence)
    if [r.get("error", "ok") for r in expect["ok"]["results"]] != [
        "ok",
        "caller.replayed",
    ]:
        raise SystemExit("replayed_nonce: unexpected " + json.dumps(expect))
    cases.append(
        {
            "name": "replayed_nonce",
            "kind": "caller_verify_sequence",
            "input": sequence,
            "expect": expect,
        }
    )

    verify("unknown_ticket_field", "caller.malformed", step(make_ticket(extra=True)))
    verify(
        "unknown_proof_field",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, extra=True)),
    )
    verify(
        "header_too_long",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + "A" * 8200},
    )
    verify(
        "unsorted_tools",
        "caller.malformed",
        step(
            make_ticket(
                tools=[{"def": search, "name": "search"}, {"def": echo, "name": "echo"}]
            )
        ),
    )
    verify(
        "wrong_scheme",
        "caller.malformed",
        {
            **step(ticket),
            "authorization": "Bearer " + caller_ticket.b64url_encode(b"{}"),
        },
    )
    missing = step(ticket)
    del missing["proof"]
    verify("missing_proof", "caller.proof_malformed", missing)
    several = make_ticket(mesh=other_mesh)
    verify(
        "first_failure_wins",
        "caller.mesh_mismatch",
        step(several, make_proof(several, stranger, ms=NOW - 999999)),
        now_ms=NOW + 8 * DAY_MS,
    )
    verify(
        "missing_catalog_tool_refused",
        "caller.tool_changed",
        step(ticket),
        catalog={"search": search},
    )

    caller = b64(rng.bytes(16))
    fold_input = {
        "caller": caller,
        "grants": ["*/echo/*"],
        "revokes": ["caller:" + caller + "/*/*", "agent-b/search/mesh_read"],
        "action": {"agent": "agent-a", "tool": "echo", "tool_def": echo},
        "effect_kind": "mesh_read",
    }
    cases.append(
        {
            "name": "caller_revoke_fold",
            "kind": "caller_revoke_fold",
            "input": fold_input,
            "expect": caller_ticket.run("caller_revoke_fold", fold_input),
        }
    )
    return {
        "format": "astral.vectors/v1",
        "name": "caller-v1",
        "spec": "106/contracts/caller-ticket.md",
        "cases": cases,
    }


def render() -> str:
    return json.dumps(generate(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            raise SystemExit("caller-v1 vectors differ")
    else:
        OUTPUT.write_text(content)


if __name__ == "__main__":
    main()
