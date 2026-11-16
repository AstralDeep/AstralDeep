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
strict = importlib.import_module("ed25519_strict")
tools = importlib.import_module("astral_mesh_ref.tools")
testkit = importlib.import_module("astral_mesh_ref.testkit")
DetRng, TestDevice, seed, scalar = (
    testkit.DetRng,
    testkit.TestDevice,
    testkit.seed,
    testkit.scalar,
)
b64 = caller_ticket.b64url_encode

NOW = 1794445200000
DAY_MS = 86400000
OUTPUT = HERE.parent / "vectors" / "caller-v1.json"
HOLDER_LABEL = "caller-v1/holder"
STRANGER_LABEL = "caller-v1/stranger"
STRANGER_PRESENCE_LABEL = "caller-v1/stranger-presence"
ORDER_PAIRS = [
    ("malformed", "proof_malformed"),
    ("proof_malformed", "mesh"),
    ("mesh", "node"),
    ("node", "removed"),
    ("removed", "minter"),
    ("minter", "bad_signature"),
    ("bad_signature", "stopped"),
    ("stopped", "expired"),
    ("stopped", "not_yet_valid"),
    ("expired", "revoked"),
    ("not_yet_valid", "revoked"),
    ("revoked", "proof_mismatch"),
    ("proof_mismatch", "proof_stale"),
    ("proof_stale", "bad_holder"),
]
CODE_OF = {
    "malformed": "caller.malformed",
    "proof_malformed": "caller.proof_malformed",
    "mesh": "caller.mesh_mismatch",
    "node": "caller.wrong_node",
    "removed": "caller.device_removed",
    "minter": "caller.wrong_minter",
    "bad_signature": "caller.bad_signature",
    "stopped": "caller.stopped",
    "expired": "caller.expired",
    "not_yet_valid": "caller.not_yet_valid",
    "revoked": "caller.revoked",
    "proof_mismatch": "caller.proof_mismatch",
    "proof_stale": "caller.proof_stale",
    "bad_holder": "caller.proof_bad_signature",
}


def _definition(name: str) -> str:
    return tools.definition_hash(
        name, "Public test tool " + name, {"type": "object"}, {"readOnlyHint": True}
    )


def _high_s(signature_b64: str) -> str:
    raw = caller_ticket.b64url_decode(signature_b64)
    s = int.from_bytes(raw[32:], "big")
    return b64(raw[:32] + (crypto.P256_ORDER - s).to_bytes(32, "big"))


def bad_holder_keys() -> dict[str, bytes]:
    small = {
        "identity_point": bytes([1]) + bytes(31),
        "order_2_point": bytes.fromhex("ec" + "ff" * 30 + "7f"),
        "order_4_point": bytes(32),
        "order_8_point": bytes.fromhex(
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"
        ),
    }
    assert all(
        (point := strict.decode_point(v)) is not None and strict.small_order(point)
        for v in small.values()
    )
    odd = next(
        y
        for y in range(2, 19)
        if strict.decode_point(y.to_bytes(32, "little")) is not None
        and strict.usable_public_key(y.to_bytes(32, "little"))
    )
    dark = next(
        y
        for y in range(2, 200)
        if strict.decode_point(y.to_bytes(32, "little")) is None
    )
    return {
        **small,
        "non_canonical_encoding": (odd + strict.P).to_bytes(32, "little"),
        "off_curve": dark.to_bytes(32, "little"),
    }


def generate() -> dict[str, Any]:
    alice = TestDevice("local-v2/alice")
    bob = TestDevice("local-v2/bob", kind="pocket")
    carol = TestDevice("local-v2/carol", kind="satellite")
    node = TestDevice("local-v2/node", kind="node")
    people = (alice, bob, carol, node)
    mesh = crypto.sha256_id(b"astral-test/caller-v1/mesh")
    other_mesh = crypto.sha256_id(b"astral-test/caller-v1/other-mesh")
    holder = seed(HOLDER_LABEL)
    stranger = seed(STRANGER_LABEL)
    stranger_scalar = scalar(STRANGER_PRESENCE_LABEL)
    rng = DetRng("caller-v1")
    cases: list[dict[str, Any]] = []
    echo, search = _definition("echo"), _definition("search")

    state = {
        "mesh": mesh,
        "node": node.id,
        "members": {
            d.id: {"kind": d.kind, "presence": b64(d.presence_public)} for d in people
        },
        "removed": [],
        "stop": 1,
        "revokes": [],
        "grants": [],
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

    def make_ticket(
        device: Any = alice, signing: int | None = None, **change: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "id": b64(rng.bytes(16)),
            "mesh": mesh,
            "device": device.id,
            "node": node.id,
            "holder": b64(crypto.ed25519_public(holder)),
            "label": "Test script",
            "tools": [{"def": echo, "name": "echo"}],
            "issued_ms": NOW - 60000,
            "expires_ms": NOW - 60000 + 7 * DAY_MS,
            "stop": 1,
        }
        body.update(change)
        key = device.presence_scalar if signing is None else signing
        return caller_ticket.sign_ticket(key, body)

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

    def step(minted: dict[str, Any], proof: Any = None, **req: Any) -> dict[str, Any]:
        return {
            "authorization": caller_ticket.authorization_header(minted),
            "proof": caller_ticket.proof_header(
                proof if proof is not None else make_proof(minted)
            ),
            "request": {**request, **req},
        }

    def record(name: str, kind: str, case_input: dict[str, Any], want: Any) -> None:
        expect = caller_ticket.run(kind, case_input)
        if kind == "caller_verify_sequence":
            got = [r.get("error", "ok") for r in expect["ok"]["results"]]
        else:
            got = expect.get("error", "ok")
        if got != want:
            raise SystemExit(f"{name}: expected {want}, reference gave {expect}")
        cases.append(
            {"name": name, "kind": kind, "input": case_input, "expect": expect}
        )

    def verify(
        name: str, want: str, ticket_step: dict[str, Any], **state_change: Any
    ) -> None:
        record(
            name,
            "caller_verify",
            {"state": {**state, **state_change}, **ticket_step},
            want,
        )

    def sequence(name: str, steps: list[dict[str, Any]], want: list[str]) -> None:
        record(name, "caller_verify_sequence", {"state": state, "steps": steps}, want)

    holder_keys = bad_holder_keys()
    ticket = make_ticket()
    verify("valid_mcp_tool", "ok", step(ticket))
    both = make_ticket(
        tools=[{"def": None, "name": "a2a.message"}, {"def": echo, "name": "echo"}]
    )
    verify("valid_a2a_message", "ok", step(both, tool="a2a.message"))
    verify("pocket_minter_accepted", "ok", step(make_ticket(bob)))
    verify("satellite_minter_accepted", "ok", step(make_ticket(carol)))
    verify("other_tool_refused", "caller.tool_not_covered", step(ticket, tool="search"))
    verify(
        "changed_definition_refused",
        "caller.tool_changed",
        step(ticket),
        catalog={"echo": search},
    )
    verify(
        "missing_catalog_tool_refused",
        "caller.tool_changed",
        step(ticket),
        catalog={"search": search},
    )
    verify(
        "a2a_not_covered_refused",
        "caller.tool_not_covered",
        step(ticket, tool="a2a.message"),
    )
    verify("tools_list_returns_covered_tools", "ok", step(both, tool=None))
    verify(
        "tools_list_skips_tool_checks",
        "ok",
        step(ticket, tool=None),
        catalog={"echo": search},
    )
    verify(
        "tools_list_still_checks_stop",
        "caller.stopped",
        step(ticket, tool=None),
        stop=2,
    )

    verify("expired", "caller.expired", step(make_ticket(expires_ms=NOW - 1)))
    verify(
        "expiry_exactly_now",
        "caller.expired",
        step(make_ticket(expires_ms=NOW)),
    )
    verify("expiry_one_ms_after_now", "ok", step(make_ticket(expires_ms=NOW + 1)))
    verify(
        "lifetime_31_days",
        "caller.malformed",
        step(make_ticket(expires_ms=NOW - 60000 + 31 * DAY_MS)),
    )
    verify(
        "lifetime_30_days_accepted",
        "ok",
        step(make_ticket(expires_ms=NOW - 60000 + 30 * DAY_MS)),
    )
    verify(
        "lifetime_30_days_plus_1_ms",
        "caller.malformed",
        step(make_ticket(expires_ms=NOW - 60000 + 30 * DAY_MS + 1)),
    )
    verify(
        "expires_equal_to_issued",
        "caller.malformed",
        step(make_ticket(expires_ms=NOW - 60000)),
    )
    verify(
        "expires_before_issued",
        "caller.malformed",
        step(make_ticket(expires_ms=NOW - 60001)),
    )
    verify(
        "future_issued_ms",
        "caller.not_yet_valid",
        step(make_ticket(issued_ms=NOW + 300001, expires_ms=NOW + 300001 + DAY_MS)),
    )
    verify(
        "future_issued_ms_at_limit",
        "ok",
        step(make_ticket(issued_ms=NOW + 300000, expires_ms=NOW + 300000 + DAY_MS)),
    )
    verify(
        "bad_presence_signature",
        "caller.bad_signature",
        step(make_ticket(bob, signing=stranger_scalar)),
    )
    high = make_ticket()
    high["sig"] = _high_s(high["sig"])
    verify("high_s_presence_signature", "caller.bad_signature", step(high))
    verify("wrong_mesh", "caller.mesh_mismatch", step(make_ticket(mesh=other_mesh)))
    verify("wrong_node", "caller.wrong_node", step(make_ticket(node=alice.id)))
    removed = make_ticket(bob)
    verify("removed_device", "caller.device_removed", step(removed), removed=[bob.id])
    stranger_id = crypto.sha256_id(b"astral-test/caller-v1/unknown-device")
    unknown = caller_ticket.sign_ticket(
        alice.presence_scalar,
        {k: v for k, v in make_ticket().items() if k != "sig"}
        | {"device": stranger_id},
    )
    verify("unknown_minting_device", "caller.device_removed", step(unknown))
    verify("node_minter_refused", "caller.wrong_minter", step(make_ticket(node)))
    verify("raised_stop", "caller.stopped", step(ticket), stop=2)
    verify(
        "revoked",
        "caller.revoked",
        step(ticket),
        revokes=["caller:" + ticket["id"] + "/*/*"],
    )
    verify(
        "revoke_for_one_tool_ignored",
        "ok",
        step(ticket),
        revokes=["caller:" + ticket["id"] + "/echo/*"],
    )
    verify(
        "revoke_for_one_scope_ignored",
        "ok",
        step(ticket),
        revokes=["caller:" + ticket["id"] + "/*/mesh_read"],
    )
    verify(
        "revoke_of_another_caller_ignored",
        "ok",
        step(ticket),
        revokes=["caller:" + b64(rng.bytes(16)) + "/*/*"],
    )

    verify(
        "later_grant_does_not_restore_revoked",
        "caller.revoked",
        step(ticket),
        revokes=["caller:" + ticket["id"] + "/*/*"],
        grants=["caller:" + ticket["id"] + "/*/*"],
    )
    verify(
        "lowercase_scheme_refused",
        "caller.malformed",
        {
            **step(ticket),
            "authorization": "astralcaller "
            + caller_ticket.authorization_header(ticket).removeprefix(
                caller_ticket.SCHEME
            ),
        },
    )
    long_name = "e" * 64
    long_tool = make_ticket(tools=[{"def": echo, "name": long_name}])
    verify(
        "tool_name_64_accepted",
        "ok",
        step(long_tool, tool=long_name),
        catalog={long_name: echo},
    )
    verify(
        "method_16_characters_accepted",
        "ok",
        step(ticket, make_proof(ticket, method="M" * 16), method="M" * 16),
    )
    long_target = "/" + "t" * 2047
    verify(
        "target_2048_characters_accepted",
        "ok",
        step(ticket, make_proof(ticket, target=long_target), target=long_target),
    )
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
        "proof_at_window_start", "ok", step(ticket, make_proof(ticket, ms=NOW - 120000))
    )
    verify("proof_ahead_5s", "ok", step(ticket, make_proof(ticket, ms=NOW + 5000)))
    verify(
        "proof_ahead_beyond_5s",
        "caller.proof_stale",
        step(ticket, make_proof(ticket, ms=NOW + 5001)),
    )
    verify(
        "proof_just_after_start_pause_accepted",
        "ok",
        step(ticket),
        started_ms=NOW - 1000 - 5001,
    )
    verify(
        "proof_at_start_pause_end_refused",
        "caller.proof_stale",
        step(ticket),
        started_ms=NOW - 1000 - 5000,
    )
    good = make_proof(ticket)
    raw = caller_ticket.b64url_decode(good["sig"])
    high = int.from_bytes(raw[32:], "little") + strict.L
    verify(
        "proof_non_canonical_s",
        "caller.proof_bad_signature",
        step(
            ticket,
            {**good, "sig": b64(raw[:32] + high.to_bytes(32, "little"))},
        ),
    )
    verify(
        "bad_holder_signature",
        "caller.proof_bad_signature",
        step(ticket, make_proof(ticket, stranger)),
    )

    nonce = b64(rng.bytes(16))
    again = step(ticket, make_proof(ticket, nonce=nonce))
    sequence("replayed_nonce", [again, again], ["ok", "caller.replayed"])
    other = make_ticket()
    sequence(
        "same_nonce_on_another_ticket_accepted",
        [again, step(other, make_proof(other, nonce=nonce))],
        ["ok", "ok"],
    )
    sequence(
        "restart_refuses_proof_accepted_before",
        [again, {**again, "restart": {"started_ms": NOW - 500}}],
        ["ok", "caller.proof_stale"],
    )
    ahead = step(ticket, make_proof(ticket, ms=NOW + 5000, nonce=nonce))
    sequence(
        "replay_store_keeps_a_nonce_to_the_end_of_its_window",
        [
            ahead,
            {**ahead, "state": {"now_ms": NOW + 124999}},
            {**ahead, "state": {"now_ms": NOW + 125000}},
            {**ahead, "state": {"now_ms": NOW + 125001}},
        ],
        ["ok", "caller.replayed", "caller.replayed", "caller.proof_stale"],
    )
    sequence(
        "replay_key_ignores_the_target",
        [
            again,
            step(
                ticket,
                make_proof(ticket, nonce=nonce, target="/mcp?session=2"),
                target="/mcp?session=2",
            ),
        ],
        ["ok", "caller.replayed"],
    )
    sequence(
        "replay_key_ignores_the_proof_time",
        [again, step(ticket, make_proof(ticket, nonce=nonce, ms=NOW - 2000))],
        ["ok", "caller.replayed"],
    )
    future = step(ticket, make_proof(ticket, ms=NOW + 5000, nonce=nonce))
    sequence(
        "restart_refuses_future_proof_accepted_before",
        [
            future,
            {**future, "restart": {"started_ms": NOW + 1000, "now_ms": NOW + 1000}},
        ],
        ["ok", "caller.proof_stale"],
    )
    sequence(
        "restart_at_proof_time_minus_5s_refuses",
        [future, {**future, "restart": {"started_ms": NOW}}],
        ["ok", "caller.proof_stale"],
    )
    sequence(
        "restart_just_before_proof_time_minus_5s_accepts",
        [future, {**future, "restart": {"started_ms": NOW - 1}}],
        ["ok", "ok"],
    )
    sequence(
        "restart_values_win_over_the_same_steps_state_patch",
        [
            again,
            {
                **again,
                "state": {"started_ms": NOW},
                "restart": {"started_ms": NOW - 3600000},
            },
        ],
        ["ok", "ok"],
    )
    sequence(
        "restart_keeps_earlier_state_patches",
        [
            {**again, "state": {"catalog": {"echo": search}}},
            {**again, "restart": {"started_ms": NOW - 3600000}},
        ],
        ["caller.tool_changed", "caller.tool_changed"],
    )
    forged = step(ticket, make_proof(ticket, stranger, nonce=nonce))
    sequence(
        "failed_signature_does_not_use_the_nonce",
        [forged, again],
        ["caller.proof_bad_signature", "ok"],
    )
    sequence(
        "uncovered_tool_uses_the_nonce",
        [{**again, "request": {**request, "tool": "search"}}, again],
        ["caller.tool_not_covered", "caller.replayed"],
    )
    sequence(
        "order_bad_holder_before_replayed",
        [again, forged],
        ["ok", "caller.proof_bad_signature"],
    )
    sequence(
        "order_replayed_before_tool_not_covered",
        [again, {**again, "request": {**request, "tool": "search"}}],
        ["ok", "caller.replayed"],
    )
    sequence(
        "order_replayed_before_tool_changed",
        [again, {**again, "state": {"catalog": {"echo": search}}}],
        ["ok", "caller.replayed"],
    )

    defects = {
        "malformed": lambda c: c["ticket"].update(extra=True),
        "proof_malformed": lambda c: c["proof"].update(extra=True),
        "mesh": lambda c: c["ticket"].update(mesh=other_mesh),
        "node": lambda c: c["ticket"].update(node=alice.id),
        "removed": lambda c: c.update(removed=True),
        "minter": lambda c: c.update(device=node),
        "bad_signature": lambda c: c.update(signing=stranger_scalar),
        "stopped": lambda c: c["state"].update(stop=2),
        "expired": lambda c: c["ticket"].update(expires_ms=NOW - 1),
        "not_yet_valid": lambda c: c["ticket"].update(
            issued_ms=NOW + 300001, expires_ms=NOW + 300001 + DAY_MS
        ),
        "revoked": lambda c: c.update(revoke=True),
        "proof_mismatch": lambda c: c["proof"].update(method="GET"),
        "proof_stale": lambda c: c["proof"].update(ms=NOW - 120001),
        "bad_holder": lambda c: c.update(proof_key=stranger),
    }

    def chained(first: str, second: str) -> None:
        ctx: dict[str, Any] = {
            "ticket": {},
            "proof": {},
            "state": {},
            "device": alice,
            "signing": None,
            "proof_key": holder,
        }
        defects[first](ctx)
        defects[second](ctx)
        minted = make_ticket(ctx["device"], ctx["signing"], **ctx["ticket"])
        if ctx.get("removed"):
            ctx["state"]["removed"] = [ctx["device"].id]
        if ctx.get("revoke"):
            ctx["state"]["revokes"] = ["caller:" + minted["id"] + "/*/*"]
        proof = make_proof(minted, ctx["proof_key"], **ctx["proof"])
        if "proof_malformed" in (first, second):
            proof = {**proof, "extra": True}
        verify(
            f"order_{first}_before_{second}",
            CODE_OF[first],
            step(minted, proof),
            **ctx["state"],
        )

    for first, second in ORDER_PAIRS:
        chained(first, second)

    verify("unknown_ticket_field", "caller.malformed", step(make_ticket(extra=True)))
    verify(
        "unknown_proof_field",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, extra=True)),
    )
    verify("ticket_version_2", "caller.malformed", step(make_ticket(v=2)))
    verify("ticket_version_true", "caller.malformed", step(make_ticket(v=True)))
    verify("empty_label", "caller.malformed", step(make_ticket(label="")))
    verify(
        "label_64_code_points_accepted",
        "ok",
        step(make_ticket(label="\U0001f600" * 64)),
    )
    verify(
        "label_65_code_points",
        "caller.malformed",
        step(make_ticket(label="\U0001f600" * 65)),
    )
    verify(
        "label_control_character",
        "caller.malformed",
        step(make_ticket(label="a\u0007b")),
    )
    verify(
        "label_format_character",
        "caller.malformed",
        step(make_ticket(label="a\u200bb")),
    )
    verify("no_tools", "caller.malformed", step(make_ticket(tools=[])))
    many = [{"def": _definition(f"t{n:02d}"), "name": f"t{n:02d}"} for n in range(31)]
    verify(
        "tools_32_accepted",
        "ok",
        step(make_ticket(tools=[{"def": echo, "name": "echo"}, *many])),
    )
    verify(
        "tools_33",
        "caller.malformed",
        step(
            make_ticket(
                tools=[
                    {"def": echo, "name": "echo"},
                    *many,
                    {"def": echo, "name": "zzz"},
                ]
            )
        ),
    )
    verify(
        "duplicate_tools",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "echo"}] * 2)),
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
        "tool_name_starts_with_digit",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "1echo"}])),
    )
    verify(
        "tool_name_too_long",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "e" * 65}])),
    )
    verify(
        "tool_name_with_dot",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "echo.tool"}])),
    )
    verify(
        "tool_without_definition",
        "caller.malformed",
        step(make_ticket(tools=[{"def": None, "name": "echo"}])),
    )
    verify(
        "a2a_with_definition",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "a2a.message"}])),
    )
    verify(
        "tool_with_extra_field",
        "caller.malformed",
        step(make_ticket(tools=[{"def": echo, "name": "echo", "x": 1}])),
    )
    for name, key in holder_keys.items():
        verify(
            "holder_key_" + name,
            "caller.malformed",
            step(make_ticket(holder=b64(key))),
        )
    forger = make_ticket(holder=b64(holder_keys["identity_point"]))
    forged = caller_ticket.sign_proof(
        holder, {k: v for k, v in make_proof(forger).items() if k != "sig"}
    )
    forged["sig"] = b64(holder_keys["identity_point"] + bytes(32))
    verify("small_order_holder_forged_proof", "caller.malformed", step(forger, forged))
    verify(
        "holder_key_31_bytes",
        "caller.malformed",
        step(make_ticket(holder=b64(bytes(31)))),
    )

    good = caller_ticket.authorization_header(ticket)
    verify(
        "header_too_long",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + "A" * 8200},
    )
    verify(
        "proof_header_too_long",
        "caller.proof_malformed",
        {**step(ticket), "proof": "A" * 8193},
    )
    verify(
        "wrong_scheme",
        "caller.malformed",
        {
            **step(ticket),
            "authorization": "Bearer " + caller_ticket.b64url_encode(b"{}"),
        },
    )
    verify(
        "bearer_scheme_with_valid_ticket",
        "caller.malformed",
        {
            **step(ticket),
            "authorization": "Bearer " + good.removeprefix(caller_ticket.SCHEME),
        },
    )
    spaced = json.dumps({k: v for k, v in ticket.items()}, indent=1).encode()
    verify(
        "ticket_not_canonical_spacing",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + b64(spaced)},
    )
    unordered = json.dumps(ticket, separators=(",", ":")).encode()
    verify(
        "ticket_not_canonical_key_order",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + b64(unordered)},
    )
    verify(
        "ticket_not_base64",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + "not base64!"},
    )
    verify(
        "ticket_not_utf8",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + b64(b"\xff\xfe\xfd")},
    )
    verify(
        "ticket_not_an_object",
        "caller.malformed",
        {**step(ticket), "authorization": caller_ticket.SCHEME + b64(b"[1]")},
    )
    proof_value = make_proof(ticket)
    verify(
        "proof_not_canonical",
        "caller.proof_malformed",
        {**step(ticket), "proof": b64(json.dumps(proof_value, indent=1).encode())},
    )
    verify(
        "proof_not_base64",
        "caller.proof_malformed",
        {**step(ticket), "proof": "not base64!"},
    )
    verify(
        "proof_not_utf8",
        "caller.proof_malformed",
        {**step(ticket), "proof": b64(b"\xff\xfe\xfd")},
    )
    verify(
        "proof_method_17_characters",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, method="M" * 17)),
    )
    verify(
        "proof_target_2049_characters",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, target="/" + "t" * 2048)),
    )
    verify(
        "proof_target_without_slash",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, target="mcp")),
    )
    verify(
        "proof_version_2",
        "caller.proof_malformed",
        step(ticket, make_proof(ticket, v=2)),
    )
    missing = step(ticket)
    del missing["proof"]
    verify("missing_proof", "caller.proof_malformed", missing)
    several = make_ticket(mesh=other_mesh, expires_ms=NOW - 1)
    verify(
        "first_failure_wins",
        "caller.mesh_mismatch",
        step(several, make_proof(several, stranger, ms=NOW - 999999)),
    )

    fold_ids = b64(rng.bytes(16))
    fold_input = {
        "caller": fold_ids,
        "grants": [],
        "revokes": [
            "caller:" + fold_ids + "/*/*",
            "caller:" + b64(rng.bytes(16)) + "/echo/*",
        ],
        "action": {"agent": "agent-a", "tool": "echo", "tool_def": echo},
        "effect_kind": "mesh_read",
    }
    record("caller_revoke_fold", "caller_revoke_fold", fold_input, "ok")
    record(
        "caller_revoke_fold_control_refuses_covered_agent",
        "caller_revoke_fold",
        {**fold_input, "revokes": fold_input["revokes"] + ["agent-a/*/*"]},
        "ticket.grant_revoked",
    )

    record(
        "caller_revoke_fold_later_grant_keeps_revoke",
        "caller_revoke_fold",
        {**fold_input, "grants": ["caller:" + fold_ids + "/*/*"]},
        "ok",
    )
    own = caller_ticket.authorization_header(ticket).removeprefix(caller_ticket.SCHEME)
    ticket_text = caller_ticket.TICKET_TEXT + own
    record("ticket_form_accepted", "caller_ticket_text", {"text": ticket_text}, "ok")
    record(
        "ticket_form_wrong_prefix",
        "caller_ticket_text",
        {"text": "astral-caller-holder:v1:" + own},
        "caller.malformed",
    )
    record(
        "ticket_form_wrong_version",
        "caller_ticket_text",
        {"text": ticket_text.replace(":v1:", ":v2:")},
        "caller.malformed",
    )
    record(
        "ticket_form_not_canonical",
        "caller_ticket_text",
        {
            "text": caller_ticket.TICKET_TEXT
            + b64(json.dumps(ticket, indent=1).encode())
        },
        "caller.malformed",
    )
    empty = make_ticket(tools=[])
    record(
        "ticket_form_canonical_but_invalid",
        "caller_ticket_text",
        {
            "text": caller_ticket.TICKET_TEXT
            + caller_ticket.authorization_header(empty).removeprefix(
                caller_ticket.SCHEME
            )
        },
        "caller.malformed",
    )
    record(
        "ticket_form_wrong_case_prefix",
        "caller_ticket_text",
        {"text": "Astral-Caller-Ticket:v1:" + own},
        "caller.malformed",
    )
    record(
        "ticket_form_over_limit",
        "caller_ticket_text",
        {"text": caller_ticket.TICKET_TEXT + "A" * 8192},
        "caller.malformed",
    )

    holder_public = b64(crypto.ed25519_public(holder))
    holder_text = caller_ticket.HOLDER_TEXT + holder_public
    key_text = caller_ticket.KEY_TEXT + b64(holder)
    record("holder_form_accepted", "caller_holder_text", {"text": holder_text}, "ok")
    record(
        "key_form_refused_by_minting",
        "caller_holder_text",
        {"text": key_text},
        "caller.malformed",
    )
    record(
        "holder_form_31_bytes",
        "caller_holder_text",
        {"text": caller_ticket.HOLDER_TEXT + b64(bytes(31))},
        "caller.malformed",
    )
    record(
        "holder_form_padded",
        "caller_holder_text",
        {"text": holder_text + "="},
        "caller.malformed",
    )
    record(
        "holder_form_wrong_version",
        "caller_holder_text",
        {"text": holder_text.replace(":v1:", ":v2:")},
        "caller.malformed",
    )
    for name, key in holder_keys.items():
        record(
            "holder_form_" + name,
            "caller_holder_text",
            {"text": caller_ticket.HOLDER_TEXT + b64(key)},
            "caller.malformed",
        )
    record("key_form_derives_holder", "caller_key_text", {"text": key_text}, "ok")
    record(
        "key_form_31_bytes",
        "caller_key_text",
        {"text": caller_ticket.KEY_TEXT + b64(bytes(31))},
        "caller.malformed",
    )
    record(
        "holder_form_refused_as_key",
        "caller_key_text",
        {"text": holder_text},
        "caller.malformed",
    )

    return {
        "format": "astral.vectors/v1",
        "name": "caller-v1",
        "spec": "106/contracts/caller-ticket.md",
        "test_keys": {
            "note": "Public test values only. A consumer recomputes proof and ticket bytes from these.",
            "holder_seed_hex": holder.hex(),
            "stranger_seed_hex": stranger.hex(),
            "stranger_presence_scalar_hex": f"{stranger_scalar:064x}",
            "presence_scalar_hex": {
                d.name.removeprefix("local-v2/"): f"{d.presence_scalar:064x}"
                for d in people
            },
        },
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
