"""Generates the connection and adapter vectors from public fixed test keys, clocks and ids, running every case through the reference modules.
It reuses the roster v2 test devices and the preserved 105 helpers, and `--check` fails when a committed file differs and writes nothing.
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
connection = importlib.import_module("connection")
connection_oauth = importlib.import_module("connection_oauth")
cj = importlib.import_module("astral_mesh_ref.cj")
consent_mod = importlib.import_module("astral_mesh_ref.consent")
crypto = importlib.import_module("astral_mesh_ref.crypto")
testkit = importlib.import_module("astral_mesh_ref.testkit")
tools = importlib.import_module("astral_mesh_ref.tools")
b64 = cj.b64url_encode
DetRng, TestDevice, hlc = testkit.DetRng, testkit.TestDevice, testkit.hlc

VECTORS = HERE.parent / "vectors"
SPEC = "106/contracts/connection.md"
T0 = 1794445200000
MIN = 60000
E = "https://mcp.research.example/mcp"
ORIGIN = "https://mcp.research.example"
RM = ORIGIN + "/.well-known/oauth-protected-resource"
AS = "https://login.research.example"
AS2 = "https://login2.research.example"
LOCAL_E = "http://127.0.0.1:8765/mcp"
LOCAL_AS = "http://127.0.0.1:8766"
REDIRECT = "https://astraldeep.github.io/oauth/callback"

ALICE = TestDevice("local-v2/alice")
BOB = TestDevice("local-v2/bob", kind="pocket")
CAROL = TestDevice("local-v2/carol", kind="satellite")
NODE = TestDevice("local-v2/node", kind="node")
DAVE = TestDevice("local-v2/dave")
MALLORY = TestDevice("connection-v1/mallory")
CURRENT = (ALICE, BOB, CAROL, NODE)


def _entry(device: Any) -> dict[str, Any]:
    return {
        "kind": device.kind,
        "presence": b64(device.presence_public),
        "feeds": [device.feed],
    }


MEMBERS = {d.id: _entry(d) for d in CURRENT}
REMOVED = {DAVE.id: _entry(DAVE)}


def _hash(name: str) -> str:
    return tools.definition_hash(
        name, "Public test tool " + name, {"type": "object"}, {"readOnlyHint": True}
    )


TOOLS = [
    {"hash": _hash("lookup"), "name": "lookup"},
    {"hash": _hash("search"), "name": "search"},
]


def _sorted_egress(entries: list[str]) -> list[str]:
    return sorted(entries, key=lambda e: connection.parse_entry(e, True))


def record(record_id: str, name: str = "First", **change: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "mcp-http",
        "endpoint": E,
        "local": False,
        "device": None,
        "auth": {
            "scheme": "oauth",
            "issuer": AS,
            "scopes": ["mcp.read", "mcp.tools"],
            "resource": E,
            "client_id": None,
        },
        "tools": TOOLS,
        "flow": "phi",
        "egress": ["login.research.example", "mcp.research.example"],
    }
    for key, item in change.items():
        if key == "auth":
            value["auth"] = {**value["auth"], **item}
        else:
            value[key] = item
    return copy.deepcopy(value)


def local_record(
    record_id: str, name: str = "Local", device: Any = ALICE, **change: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "endpoint": LOCAL_E,
        "local": True,
        "device": device.id,
        "egress": ["127.0.0.1:8765", "127.0.0.1:8766"],
        **change,
    }
    fields["auth"] = {"issuer": LOCAL_AS, "resource": LOCAL_E, **change.get("auth", {})}
    return record(record_id, name, **fields)


def a2a_record(record_id: str, name: str = "Agent", **extra: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "a2a",
        "card": {"url": "https://agent.research.example/.well-known/agent-card.json"},
        **extra,
    }


def none_record(record_id: str, name: str = "Open", **change: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"egress": ["mcp.research.example"], **change}
    fields["auth"] = {
        "scheme": "none",
        "issuer": None,
        "scopes": [],
        "resource": None,
        **change.get("auth", {}),
    }
    return record(record_id, name, **fields)


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
            "id": crypto.sha256_id(f"connection-v1/{self.case}/{name}".encode()),
            "labels": labels or dict(connection.LABELS),
            "scope": scope,
            "seq": seq,
            "type": type_,
        }
        self.events.append(event)
        return event


def make_consent(
    value: Any,
    signer: Any,
    ms: int,
    kind: str = "connection",
    digest: str | None = None,
    scalar: int | None = None,
) -> dict[str, Any]:
    return consent_mod.build(
        kind,
        digest or crypto.object_id(value),
        signer.id,
        scalar or signer.presence_scalar,
        hlc(ms),
    )


def tampered(consent: dict[str, Any]) -> dict[str, Any]:
    raw = bytearray(cj.b64url_decode(consent["sig"]))
    raw[5] ^= 1
    return {**consent, "sig": b64(bytes(raw))}


def high_s(consent: dict[str, Any]) -> dict[str, Any]:
    raw = cj.b64url_decode(consent["sig"])
    s = int.from_bytes(raw[32:], "big")
    return {
        **consent,
        "sig": b64(raw[:32] + (crypto.P256_ORDER - s).to_bytes(32, "big")),
    }


def put(
    log: Log,
    name: str,
    writer: Any,
    ms: int,
    value: Any,
    consent: dict[str, Any] | None = None,
    key_id: str | None = None,
    **event: Any,
) -> dict[str, Any]:
    key = "connection/" + (key_id or value["id"])
    body = {
        "key": key,
        "value": value,
        "consent": consent or make_consent(value, writer, ms - 1000),
    }
    return log.add(name, writer, ms, body, **event)


def remove(
    log: Log, name: str, writer: Any, ms: int, record_id: str, **extra: Any
) -> dict[str, Any]:
    return log.add(
        name, writer, ms, {"key": "connection/" + record_id, "value": None, **extra}
    )


def state_value(
    state: str = "signed_in", reason: str | None = None, ms: int = T0, **extra: Any
) -> dict[str, Any]:
    return {"v": 1, "state": state, "reason": reason, "issuer": AS, "ms": ms, **extra}


def put_state(
    log: Log, name: str, writer: Any, device: Any, ms: int, value: Any
) -> dict[str, Any]:
    key = f"connection.state/{RID}/{device.id}"
    return log.add(name, writer, ms, {"key": key, "value": value})


RNG = DetRng("connection-v1")
RID = RNG.bytes(16).hex()
RID2 = RNG.bytes(16).hex()
LOWER = RID[:8] + "0" * 24
HIGHER = RID[:8] + "f" * 24


def generate_connection() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
        expect = connection.run(kind, case_input)
        cases.append(
            {"name": name, "kind": kind, "input": case_input, "expect": expect}
        )
        return expect

    for name, key in (
        ("key_record", "connection/" + RID),
        ("key_state", f"connection.state/{RID}/{ALICE.id}"),
        ("key_other_setting", "theme"),
        ("key_bare_connection_word", "connection"),
        ("key_plural_prefix", "connections/" + RID),
        ("key_uppercase_hex", "connection/" + RID.upper()),
        ("key_id_31_hex", "connection/" + RID[:31]),
        ("key_id_33_hex", "connection/" + RID + "0"),
        ("key_trailing_slash", "connection/" + RID + "/"),
        ("key_record_with_device", f"connection/{RID}/{ALICE.id}"),
        ("key_state_without_device", "connection.state/" + RID),
        ("key_state_device_uppercase", f"connection.state/{RID}/{ALICE.id.upper()}"),
        ("key_state_device_without_prefix", f"connection.state/{RID}/{ALICE.id[7:]}"),
        ("key_unknown_reserved_family", "connection.card/" + RID),
    ):
        add(name, "connection_key", {"key": key})

    def rec(name: str, value: Any) -> None:
        add(name, "connection_record", {"id": RID, "value": value})

    base = record(RID)
    rec("record_mcp_oauth_valid", base)
    rec("record_mcp_none_valid", none_record(RID))
    rec(
        "record_client_id_accepted",
        record(RID, auth={"client_id": "astral-uk-research"}),
    )
    rec("record_local_ipv4_valid", local_record(RID))
    rec(
        "record_local_ipv6_valid",
        local_record(
            RID,
            endpoint="http://[::1]:8765/mcp",
            auth={"issuer": "http://[::1]:8766", "resource": "http://[::1]:8765/mcp"},
            egress=["[::1]:8765", "[::1]:8766"],
        ),
    )
    rec(
        "record_local_https_loopback_valid",
        local_record(
            RID,
            endpoint="https://127.0.0.1:8765/mcp",
            auth={"resource": "https://127.0.0.1:8765/mcp"},
        ),
    )
    rec("record_local_names_node_valid", local_record(RID, device=NODE))
    rec(
        "record_local_with_public_issuer_valid",
        local_record(
            RID,
            auth={"issuer": AS},
            egress=_sorted_egress(["127.0.0.1:8765", "login.research.example"]),
        ),
    )
    rec(
        "record_egress_extra_port_entry_valid",
        record(
            RID,
            egress=_sorted_egress(
                [
                    "api.research.example:8443",
                    "login.research.example",
                    "mcp.research.example",
                ]
            ),
        ),
    )
    port_e = "https://mcp.research.example:8443/mcp"
    rec(
        "record_endpoint_with_port_valid",
        record(
            RID,
            endpoint=port_e,
            auth={"resource": port_e},
            egress=["login.research.example", "mcp.research.example:8443"],
        ),
    )
    rec(
        "record_endpoint_without_path_valid",
        record(RID, endpoint=ORIGIN, auth={"resource": ORIGIN}),
    )
    puny = "https://xn--mnchen-3ya.research.example/mcp"
    rec(
        "record_endpoint_punycode_valid",
        record(
            RID,
            endpoint=puny,
            auth={"resource": puny},
            egress=["login.research.example", "xn--mnchen-3ya.research.example"],
        ),
    )
    rec(
        "record_issuer_trailing_slash_valid",
        record(RID, auth={"issuer": AS + "/"}),
    )
    rec(
        "record_tool_name_with_dot_valid",
        record(RID, tools=[{"hash": _hash("files.read"), "name": "files.read"}]),
    )
    rec("record_empty_tools_valid", record(RID, tools=[]))
    rec("record_flow_private_valid", record(RID, flow="private"))
    many = [{"hash": _hash(f"t{i:03d}"), "name": f"t{i:03d}"} for i in range(129)]
    rec("record_tools_128_valid", record(RID, tools=many[:128]))
    rec("record_tools_129", record(RID, tools=many))
    scopes = [f"s{i:02d}" for i in range(33)]
    rec("record_scopes_32_valid", record(RID, auth={"scopes": scopes[:32]}))
    rec("record_scopes_33", record(RID, auth={"scopes": scopes}))
    hosts = [f"h{i:02d}.research.example" for i in range(15)]
    sixteen = _sorted_egress(
        ["login.research.example", "mcp.research.example"] + hosts[:14]
    )
    rec("record_egress_16_valid", record(RID, egress=sixteen))
    rec(
        "record_egress_17",
        record(
            RID,
            egress=_sorted_egress(
                ["login.research.example", "mcp.research.example"] + hosts
            ),
        ),
    )
    rec("record_name_80_valid", record(RID, name="N" * 80))
    host_then_port = [
        "login.research.example",
        "mcp.research.example:8443",
        "mcp.research.example.cdn",
    ]
    rec(
        "record_egress_sorted_by_host_then_port_valid",
        record(RID, endpoint=port_e, auth={"resource": port_e}, egress=host_then_port),
    )
    rec(
        "record_egress_sorted_as_text",
        record(
            RID,
            endpoint=port_e,
            auth={"resource": port_e},
            egress=sorted(host_then_port),
        ),
    )
    key_auth = {"scheme": "key", "issuer": None, "scopes": [], "resource": None}
    rec(
        "record_scheme_key_unsupported",
        record(RID, auth=key_auth, egress=["mcp.research.example"]),
    )
    rec("record_unknown_scheme_unsupported", record(RID, auth={"scheme": "dpop"}))
    rec("record_kind_a2a_unsupported", a2a_record(RID))
    rec("record_kind_unknown_unsupported", record(RID, kind="carrier-pigeon"))
    rec("record_version_2_unsupported", record(RID, v=2))
    rec("record_order_version_before_kind", a2a_record(RID, v=2))
    rec("record_order_kind_before_fields", a2a_record(RID, unknown=True))
    rec(
        "record_order_fields_before_scheme",
        record(RID, auth=key_auth, egress=["mcp.research.example"], extra=True),
    )
    rec(
        "record_order_scheme_before_values",
        record(
            RID, auth=key_auth, endpoint=E + "?x=1", egress=["mcp.research.example"]
        ),
    )
    rec("record_unknown_field", record(RID, extra="x"))
    missing = record(RID)
    del missing["flow"]
    rec("record_missing_field", missing)
    rec("record_auth_unknown_field", record(RID, auth={"dpop": True}))

    def endpoint_case(name: str, endpoint: str, **change: Any) -> None:
        rec(name, record(RID, endpoint=endpoint, auth={"resource": endpoint}, **change))

    endpoint_case("record_endpoint_query", E + "?tenant=uk")
    endpoint_case("record_endpoint_fragment", E + "#top")
    endpoint_case("record_endpoint_userinfo", "https://owner@mcp.research.example/mcp")
    endpoint_case("record_endpoint_trailing_slash", E + "/")
    endpoint_case("record_endpoint_uppercase_host", "https://MCP.research.example/mcp")
    endpoint_case(
        "record_endpoint_default_port", "https://mcp.research.example:443/mcp"
    )
    endpoint_case("record_endpoint_http_not_local", "http://mcp.research.example/mcp")
    rec(
        "record_endpoint_localhost_name",
        local_record(
            RID,
            endpoint="http://localhost:8765/mcp",
            auth={"resource": "http://localhost:8765/mcp"},
        ),
    )
    endpoint_case("record_endpoint_private_lan_address", "https://192.168.1.20/mcp")
    endpoint_case("record_endpoint_empty_segment", "https://mcp.research.example//mcp")
    endpoint_case(
        "record_endpoint_dot_segment", "https://mcp.research.example/a/../mcp"
    )
    endpoint_case(
        "record_endpoint_lowercase_percent", "https://mcp.research.example/m%2fcp"
    )
    endpoint_case(
        "record_endpoint_port_leading_zero", "https://mcp.research.example:08443/mcp"
    )
    endpoint_case(
        "record_endpoint_port_out_of_range", "https://mcp.research.example:65536/mcp"
    )
    endpoint_case(
        "record_endpoint_trailing_dot_host", "https://mcp.research.example./mcp"
    )
    endpoint_case("record_endpoint_single_label_host", "https://mcp/mcp")
    endpoint_case("record_endpoint_numeric_last_label", "https://mcp.research.1234/mcp")
    endpoint_case("record_endpoint_2049_characters", E + "/" + "a" * (2048 - len(E)))
    rec("record_local_without_device", local_record(RID) | {"device": None})
    rec("record_device_without_local", record(RID, device=ALICE.id))
    rec("record_local_dns_endpoint", record(RID, local=True, device=ALICE.id))
    rec("record_local_device_malformed", local_record(RID) | {"device": "alice"})
    rec(
        "record_resource_differs",
        record(RID, auth={"resource": "https://other.research.example/mcp"}),
    )
    rec("record_resource_null_for_oauth", record(RID, auth={"resource": None}))
    rec("record_issuer_null_for_oauth", record(RID, auth={"issuer": None}))
    rec("record_issuer_for_none", none_record(RID, auth={"issuer": AS}))
    rec("record_scopes_for_none", none_record(RID, auth={"scopes": ["mcp.read"]}))
    rec("record_client_id_for_none", none_record(RID, auth={"client_id": "astral"}))
    rec(
        "record_issuer_http_not_local",
        record(RID, auth={"issuer": "http://login.research.example"}),
    )
    rec("record_issuer_query", record(RID, auth={"issuer": AS + "?tenant=uk"}))
    rec("record_client_id_with_space", record(RID, auth={"client_id": "astral uk"}))
    rec(
        "record_scopes_unsorted",
        record(RID, auth={"scopes": ["mcp.tools", "mcp.read"]}),
    )
    rec(
        "record_scopes_duplicate",
        record(RID, auth={"scopes": ["mcp.read", "mcp.read"]}),
    )
    rec("record_scope_with_quote", record(RID, auth={"scopes": ['mcp"read']}))
    rec("record_tools_unsorted", record(RID, tools=list(reversed(TOOLS))))
    rec("record_tools_duplicate", record(RID, tools=[TOOLS[0], TOOLS[0]]))
    rec(
        "record_tool_bad_hash",
        record(RID, tools=[{"hash": "sha256:00", "name": "lookup"}]),
    )
    rec(
        "record_tool_extra_field",
        record(RID, tools=[{**TOOLS[0], "effect": "mesh_read"}]),
    )
    rec(
        "record_tool_name_129",
        record(RID, tools=[{"hash": _hash("x"), "name": "x" * 129}]),
    )
    rec("record_flow_unknown", record(RID, flow="secret"))
    rec(
        "record_egress_missing_endpoint_host",
        record(RID, egress=["login.research.example"]),
    )
    rec(
        "record_egress_missing_issuer_host",
        record(RID, egress=["mcp.research.example"]),
    )
    rec(
        "record_egress_port_443_spelled",
        record(
            RID,
            egress=[
                "login.research.example",
                "mcp.research.example",
                "mcp.research.example:443",
            ],
        ),
    )
    rec(
        "record_egress_duplicate",
        record(
            RID,
            egress=[
                "login.research.example",
                "login.research.example",
                "mcp.research.example",
            ],
        ),
    )
    rec(
        "record_egress_ipv4_not_local",
        record(
            RID, egress=["10.0.0.4", "login.research.example", "mcp.research.example"]
        ),
    )
    rec(
        "record_egress_loopback_not_local",
        record(
            RID,
            egress=["127.0.0.1:8765", "login.research.example", "mcp.research.example"],
        ),
    )
    rec(
        "record_egress_wildcard",
        record(
            RID,
            egress=[
                "*.research.example",
                "login.research.example",
                "mcp.research.example",
            ],
        ),
    )
    rec(
        "record_egress_uppercase",
        record(RID, egress=["LOGIN.research.example", "mcp.research.example"]),
    )
    rec("record_egress_empty", record(RID, egress=[]))
    sized = a2a_record(RID, pad="")
    sized["pad"] = "p" * (connection.MAX_RECORD_BYTES - len(cj.canonical(sized)))
    rec("record_at_size_limit", sized)
    rec("record_over_size_limit", {**sized, "pad": sized["pad"] + "p"})
    rec("record_not_object", ["not", "an", "object"])
    rec("record_version_true", record(RID, v=True))
    rec("record_version_0", record(RID, v=0))
    rec("record_version_text", record(RID, v="1"))
    rec("record_id_mismatch", record(RID2))
    no_id = record(RID)
    del no_id["id"]
    rec("record_id_missing", no_id)
    rec("record_name_empty", record(RID, name=""))
    rec("record_name_81", record(RID, name="N" * 81))
    rec("record_name_control", record(RID, name="First\u0007"))
    rec("record_name_bidi_override", record(RID, name="First‮roc"))
    rec("record_name_not_text", record(RID, name=7))
    rec("record_kind_uppercase", record(RID, kind="MCP-HTTP"))
    no_kind = record(RID)
    del no_kind["kind"]
    rec("record_kind_missing", no_kind)

    def state(name: str, value: Any) -> None:
        add(name, "connection_state", {"value": value})

    state("state_not_signed_in_valid", state_value("not_signed_in", issuer=None))
    state("state_signed_in_valid", state_value())
    state("state_signed_in_without_issuer_valid", state_value(issuer=None))
    state("state_signed_in_with_tools_valid", state_value(tools=TOOLS))
    state("state_tools_128_valid", state_value(tools=many[:128]))
    for reason in connection.REASONS:
        state(
            f"state_needs_attention_{reason}_valid",
            state_value("needs_attention", reason),
        )
    state(
        "state_tools_on_needs_attention",
        state_value("needs_attention", "scope", tools=TOOLS),
    )
    state("state_tools_129", state_value(tools=many))
    state("state_tools_unsorted", state_value(tools=list(reversed(TOOLS))))
    state("state_needs_attention_without_reason", state_value("needs_attention"))
    state("state_reason_on_signed_in", state_value(reason="expired"))
    state("state_unknown_state", state_value("signing_in"))
    state("state_unknown_reason", state_value("needs_attention", "auth_required"))
    state("state_unknown_field", state_value(device=ALICE.id))
    state("state_version_2", state_value(v=2))
    state("state_negative_ms", state_value(ms=-1))
    state(
        "state_issuer_http_not_loopback",
        state_value(issuer="http://login.research.example"),
    )
    state("state_null", None)

    def fold(
        name: str, log: Log, legacy: list[str] | None = None, reverse: bool = False
    ) -> dict[str, Any]:
        events = list(reversed(log.events)) if reverse else log.events
        return add(
            name,
            "connection_fold",
            {
                "members": MEMBERS,
                "removed": REMOVED,
                "events": events,
                "legacy_servers": legacy or [],
            },
        )

    first, second = record(RID, "First"), record(RID, "Second")
    t1, t2, t3 = T0, T0 + 2 * MIN, T0 + 3 * MIN

    log = Log("valid")
    put(log, "p", ALICE, t1, first)
    fold("fold_valid_put", log)
    log = Log("node")
    put(log, "p", NODE, t1, first)
    fold("fold_put_by_node_member", log)
    for name, offset in (
        ("fold_consent_older_than_10_minutes", -600001),
        ("fold_consent_10_minutes_old_accepted", -600000),
        ("fold_consent_after_event_clock", 1),
        ("fold_consent_at_event_clock_accepted", 0),
    ):
        log = Log(name)
        put(log, "p", ALICE, t1, first, make_consent(first, ALICE, t1 + offset))
        fold(name, log)
    log = Log("digest")
    put(log, "p", ALICE, t1, first, make_consent(second, ALICE, t1))
    fold("fold_wrong_digest", log)
    log = Log("kind")
    put(log, "p", ALICE, t1, first, make_consent(first, ALICE, t1, kind="grant"))
    fold("fold_wrong_consent_kind", log)
    log = Log("tampered")
    put(log, "p", ALICE, t1, first, tampered(make_consent(first, ALICE, t1)))
    fold("fold_tampered_signature", log)
    log = Log("high-s")
    put(log, "p", ALICE, t1, first, high_s(make_consent(first, ALICE, t1)))
    fold("fold_high_s_signature", log)

    for name, carrier in (
        ("fold_recarried_older_value_ignored", BOB),
        ("fold_recarried_by_node_ignored", NODE),
    ):
        log = Log(name)
        original = put(log, "p1", ALICE, t1, first)
        put(log, "p2", ALICE, t2, second)
        log.add("p3", carrier, t3, copy.deepcopy(original["body"]))
        fold(name, log)

    log = Log("removed-approver")
    put(log, "p1", ALICE, t1, first)
    put(log, "p2", DAVE, t2, second)
    fold("fold_put_consenting_device_removed", log)
    log = Log("removed-only")
    put(log, "p1", DAVE, t1, first)
    fold("fold_removed_approval_without_older_put", log)
    log = Log("removed-superseded")
    put(log, "p1", DAVE, t1, first)
    put(log, "p2", ALICE, t2, second)
    fold("fold_removed_approval_superseded", log)
    log = Log("unknown-device")
    put(log, "p1", MALLORY, t1, first)
    fold("fold_unknown_consent_device", log)
    log = Log("forged-removed")
    put(log, "p1", BOB, t1, first, make_consent(first, DAVE, t1))
    fold("fold_forged_removed_approval", log)

    log = Log("remove")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", BOB, t2, RID)
    fold("fold_removal_without_consent", log)
    log = Log("remove-node")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", NODE, t2, RID)
    fold("fold_removal_by_node", log)
    log = Log("remove-removed")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", DAVE, t2, RID)
    fold("fold_removal_by_removed_device_ignored", log)
    log = Log("reput")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", BOB, t2, RID)
    put(log, "p2", ALICE, t3, second)
    fold("fold_reput_after_removal", log)
    log = Log("later")
    put(log, "p1", ALICE, t1, first)
    put(log, "p2", BOB, t2, second)
    fold("fold_later_put_wins", log)
    for name, reverse in (
        ("fold_later_invalid_put_does_not_win", False),
        ("fold_arrival_order_independent", True),
    ):
        log = Log("later-invalid")
        put(log, "p1", ALICE, t1, first)
        put(log, "p2", BOB, t2, second, tampered(make_consent(second, BOB, t2)))
        fold(name, log, reverse=reverse)
    log = Log("tie")
    put(log, "a", ALICE, t1, record(RID, "alice"))
    put(log, "b", BOB, t1, record(RID, "bob"))
    fold("fold_equal_clock_breaks_by_feed", log)
    log = Log("counter")
    put(log, "a", ALICE, t1, record(RID, "High"), n=1)
    put(log, "b", BOB, t1, record(RID, "Low"), n=0)
    fold("fold_counter_breaks_equal_milliseconds", log)

    def stated(name: str, *steps: tuple[str, Any, Any, int, Any]) -> None:
        log = Log(name)
        put(log, "p", ALICE, t1, first)
        for event_name, writer, device, ms, value in steps:
            put_state(log, event_name, writer, device, ms, value)
        fold(name, log)

    stated("fold_state_by_own_device", ("s", ALICE, ALICE, t2, state_value()))
    stated("fold_state_written_by_another_device", ("s", BOB, ALICE, t2, state_value()))
    stated("fold_state_of_removed_member", ("s", DAVE, DAVE, t2, state_value()))
    stated(
        "fold_order_writer_before_state_foreign", ("s", DAVE, ALICE, t2, state_value())
    )
    stated(
        "fold_latest_state_wins",
        ("s1", ALICE, ALICE, t2, state_value()),
        ("s2", ALICE, ALICE, t3, state_value("needs_attention", "refresh_refused", t3)),
    )
    stated(
        "fold_later_unreadable_state_does_not_win",
        ("s1", ALICE, ALICE, t2, state_value()),
        ("s2", ALICE, ALICE, t3, state_value("needs_attention", "auth_required", t3)),
    )
    stated(
        "fold_states_of_several_devices",
        ("s1", ALICE, ALICE, t2, state_value()),
        ("s2", BOB, BOB, t3, state_value("needs_attention", "expired", t3)),
    )
    log = Log("node-tools")
    node_local = local_record(RID, device=NODE, tools=[])
    put(log, "p", ALICE, t1, node_local)
    put_state(log, "s", NODE, NODE, t2, state_value(issuer=LOCAL_AS, tools=TOOLS))
    fold("fold_state_with_tools_from_node", log)
    log = Log("unlisted-state")
    put_state(log, "s", ALICE, ALICE, t2, state_value())
    fold("fold_state_for_unlisted_connection_dropped", log)

    log = Log("a2a")
    put(log, "p", ALICE, t1, a2a_record(RID))
    fold("fold_unknown_kind_unsupported", log)
    log = Log("a2a-later")
    put(log, "p1", ALICE, t1, first)
    put(log, "p2", ALICE, t2, a2a_record(RID, "Second"))
    fold("fold_unsupported_put_wins_over_older_supported", log)
    log = Log("key")
    put(
        log,
        "p",
        ALICE,
        t1,
        record(RID, "Keyed", auth=key_auth, egress=["mcp.research.example"]),
    )
    fold("fold_scheme_key_unsupported", log)
    log = Log("collision")
    put(log, "p1", ALICE, t1, record(HIGHER, "Higher"))
    put(log, "p2", BOB, t2, record(LOWER, "Lower"))
    fold("fold_server_id_collision", log)
    log = Log("collision-unsupported")
    put(log, "p1", ALICE, t1, record(HIGHER, "Higher"))
    put(log, "p2", BOB, t2, a2a_record(LOWER, "Lower"))
    fold("fold_collision_with_unsupported_lower", log)
    log = Log("collision-keeps-reason")
    put(log, "p1", ALICE, t1, record(LOWER, "Lower"))
    put(log, "p2", BOB, t2, a2a_record(HIGHER, "Higher"))
    fold("fold_collision_keeps_an_earlier_reason", log)
    log = Log("collision-legacy")
    put(log, "p1", ALICE, t1, first)
    fold("fold_collision_with_legacy_server", log, legacy=["c" + RID[:8]])
    log = Log("local-node")
    put(log, "p", ALICE, t1, local_record(RID, device=NODE))
    fold("fold_local_record_names_node", log)

    log = Log("mem")
    mem_body = {
        "key": "connection/" + RID,
        "value": first,
        "consent": make_consent(first, ALICE, t1),
    }
    log.add("m", ALICE, t1, mem_body, type_="mem.remember")
    fold("fold_mem_remember_reserved_key_not_read", log)
    log = Log("other")
    log.add("s", ALICE, t1, {"key": "theme", "value": "dark"})
    log.add(
        "g",
        ALICE,
        t2,
        {"agent": "agent.mail", "scope": "mesh", "tool": "mail.send"},
        type_="auth.grant",
    )
    fold("fold_other_events_not_read", log)
    log = Log("malformed-key")
    put(log, "p", ALICE, t1, first, key_id=RID.upper())
    fold("fold_malformed_reserved_key", log)
    log = Log("family")
    log.add("p", ALICE, t1, {"key": "connection.card/" + RID, "value": first})
    fold("fold_unknown_reserved_family", log)
    log = Log("labels")
    put(
        log,
        "p",
        ALICE,
        t1,
        first,
        labels={"confidentiality": "phi", "integrity": "owner"},
    )
    fold("fold_wrong_labels", log)
    log = Log("scope")
    put(log, "p", ALICE, t1, first, scope=crypto.sha256_id(b"connection-v1/chat"))
    fold("fold_chat_scope", log)
    log = Log("remove-consent")
    put(log, "p", ALICE, t1, first)
    remove(log, "x", BOB, t2, RID, consent=make_consent(None, BOB, t2))
    fold("fold_remove_with_consent_field", log)
    log = Log("no-consent")
    log.add("p", ALICE, t1, {"key": "connection/" + RID, "value": first})
    fold("fold_put_without_consent", log)
    empty_name = record(RID, name="")
    log = Log("consent-shape")
    put(log, "p", ALICE, t1, first, {**make_consent(first, ALICE, t1), "note": "x"})
    fold("fold_consent_shape_malformed", log)
    log = Log("record-malformed")
    put(log, "p", ALICE, t1, empty_name)
    fold("fold_put_record_malformed", log)
    log = Log("id-differs")
    put(log, "p", ALICE, t1, record(RID2), key_id=RID)
    fold("fold_put_id_differs_from_key", log)
    log = Log("order-1")
    log.add("p", ALICE, t1, {"key": "connection/" + RID, "value": empty_name})
    fold("fold_order_malformed_before_consent_missing", log)
    log = Log("order-2")
    put(log, "p", ALICE, t1, empty_name, make_consent(first, ALICE, t1))
    fold("fold_order_malformed_before_wrong_action", log)
    log = Log("order-3")
    put(log, "p", MALLORY, t1, first, make_consent(second, MALLORY, t1))
    fold("fold_order_wrong_action_before_unknown_device", log)
    log = Log("order-4")
    put(log, "p", NODE, t1, first, make_consent(first, NODE, t1, kind="grant"))
    fold("fold_order_wrong_action_before_node_device", log)
    log = Log("order-5")
    put(log, "p", ALICE, t1, first, make_consent(first, NODE, t1))
    fold("fold_order_node_device_before_foreign_feed", log)
    log = Log("order-6")
    put(log, "p", BOB, t1, first, tampered(make_consent(first, ALICE, t1)))
    fold("fold_order_foreign_feed_before_bad_signature", log)
    log = Log("order-7")
    put(log, "p", ALICE, t1, first, tampered(make_consent(first, ALICE, t1 - 600001)))
    fold("fold_order_bad_signature_before_stale", log)

    def entry_of(value: Any, legacy: list[str] | None = None) -> dict[str, Any]:
        log = Log("use")
        put(log, "p", ALICE, t1, value)
        listed = connection.fold(
            {
                "members": MEMBERS,
                "removed": REMOVED,
                "events": log.events,
                "legacy_servers": legacy or [],
            }
        )
        return listed["connections"][0]

    for name, value, device, legacy in (
        ("use_remote_on_any_device", first, BOB, None),
        ("use_local_on_its_device", local_record(RID), ALICE, None),
        ("use_local_elsewhere", local_record(RID), BOB, None),
        ("use_local_on_the_node_it_names", local_record(RID, device=NODE), NODE, None),
        (
            "use_node_local_from_an_owner_device",
            local_record(RID, device=NODE),
            ALICE,
            None,
        ),
        ("use_unsupported", a2a_record(RID), ALICE, None),
        ("use_collision", first, ALICE, ["c" + RID[:8]]),
    ):
        add(
            name,
            "connection_use",
            {"connection": entry_of(value, legacy), "device": device.id},
        )

    for name, tool in (
        ("exposed_simple", "search"),
        ("exposed_tool_53_accepted", "t" * 53),
        ("exposed_tool_54_too_long", "t" * 54),
        ("exposed_dot_invalid", "files.read"),
        ("exposed_length_before_characters", "x" * 50 + ".abc"),
        ("exposed_leading_digit_accepted", "1password"),
        ("exposed_longest_ask_tool_fits", "ask_" + "a" * 49),
    ):
        add(name, "connection_exposed_name", {"id": RID, "tool": tool})

    for name, display in (
        ("slug_simple", "Travel agent"),
        ("slug_punctuation_runs", "UK -- Research!! Agent"),
        ("slug_non_ascii", "Café Ñandú"),
        ("slug_digits", "Agent 007"),
        ("slug_leading_and_trailing_symbols", "  (Helper)  "),
        ("slug_hyphen_and_underscore_are_separators", "multi-agent_desk"),
        ("slug_all_symbols", "!!!"),
        ("slug_non_ascii_only", "日本語"),
        ("slug_long_name_cut_to_49", "a" * 80),
        ("slug_cut_lands_on_separator", "a" * 48 + " " + "b" * 31),
    ):
        add(name, "connection_ask_slug", {"name": display})

    remote = ["login.research.example", "mcp.research.example"]
    for name, egress, url in (
        ("egress_endpoint_allowed", remote, E),
        ("egress_token_endpoint_allowed", remote, AS + "/oauth/token"),
        ("egress_bare_entry_is_port_443", remote, AS + "/token?realm=uk"),
        ("egress_host_not_listed", remote, "https://evil.example/token"),
        ("egress_port_differs", remote, "https://login.research.example:8443/token"),
        ("egress_http_to_dns_host", remote, "http://login.research.example/token"),
        (
            "egress_loopback_http_allowed",
            ["127.0.0.1:8765", "127.0.0.1:8766"],
            LOCAL_AS + "/token",
        ),
        ("egress_ipv6_loopback_allowed", ["[::1]:8765"], "http://[::1]:8765/mcp"),
        ("egress_http_default_port_is_80", ["127.0.0.1:80"], "http://127.0.0.1/mcp"),
        ("egress_uppercase_host", remote, "https://Login.research.example/token"),
        (
            "egress_userinfo",
            remote,
            "https://login.research.example@evil.example/token",
        ),
        (
            "egress_default_port_spelled",
            remote,
            "https://login.research.example:443/token",
        ),
    ):
        add(name, "connection_egress", {"egress": egress, "url": url})
    for name, entry in (
        ("exec_bare_entry_is_443", "mcp.research.example"),
        ("exec_port_80", "mcp.research.example:80"),
        ("exec_other_port", "mcp.research.example:8443"),
        ("exec_loopback", "127.0.0.1:8765"),
    ):
        add(name, "connection_exec_host", {"entry": entry})

    log = Log("coexistence")
    log.add("theme", ALICE, t1, {"key": "theme", "value": "dark"})
    log.add(
        "home", BOB, t1, {"key": "home", "value": "Lexington"}, type_="mem.remember"
    )
    grant = log.add(
        "grant",
        ALICE,
        t1 + 1,
        {"agent": "agent.mail", "scope": "mesh", "tool": "mail.send"},
        type_="auth.grant",
    )
    log.add(
        "consent",
        CAROL,
        t1 + 2,
        {
            "consent": make_consent(
                None, ALICE, t1 + 2, kind="grant", digest=grant["id"]
            ),
            "grant": grant["id"],
        },
        type_="auth.consent",
    )
    log.add(
        "revoke",
        BOB,
        t1 + 3,
        {"agent": "agent.files", "scope": "mesh", "tool": "files.delete"},
        type_="auth.revoke",
    )
    put(log, "put", ALICE, t2, first)
    put(log, "invalid", BOB, t3, second, tampered(make_consent(second, BOB, t3)))
    put_state(log, "state", ALICE, ALICE, t3 + 1, state_value())
    remove(log, "remove", CAROL, t3 + 2, RID2)
    add(
        "coexistence_deployed_folds",
        "connection_coexistence",
        {"events": log.events, "presence_keys": {ALICE.id: b64(ALICE.presence_public)}},
    )

    return {
        "format": "astral.vectors/v1",
        "name": "connection-v1",
        "spec": SPEC,
        "test_devices": {
            name: {
                "id": d.id,
                "label": d.name,
                "kind": d.kind,
                "feed": d.feed,
                "presence_scalar_hex": f"{d.presence_scalar:064x}",
            }
            for name, d in (
                ("alice", ALICE),
                ("bob", BOB),
                ("carol", CAROL),
                ("node", NODE),
                ("dave", DAVE),
                ("mallory", MALLORY),
            )
        },
        "cases": cases,
    }


def _meta(issuer: str, methods: Any = ("S256",), **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/token",
        **extra,
    }
    if methods is not None:
        document["code_challenge_methods_supported"] = list(methods)
    return document


def _ok(document: Any) -> dict[str, Any]:
    return {"status": 200, "json": document}


def _prm(resource: str = E, servers: Any = (AS,), **extra: Any) -> dict[str, Any]:
    return {"resource": resource, "authorization_servers": list(servers), **extra}


def generate_oauth() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> None:
        expect = connection_oauth.run(kind, case_input)
        cases.append(
            {"name": name, "kind": kind, "input": case_input, "expect": expect}
        )

    for name, header in (
        (
            "challenge_bearer_quoted",
            f'Bearer resource_metadata="{RM}/mcp", scope="a b"',
        ),
        ("challenge_scheme_case_insensitive", 'bEaReR scope="a"'),
        ("challenge_param_names_lowercased", f'Bearer Resource_Metadata="{RM}"'),
        (
            "challenge_token_value",
            'Bearer error=insufficient_scope, scope="files.write"',
        ),
        ("challenge_quoted_pair", 'Bearer realm="say \\"hi\\""'),
        ("challenge_basic_before_bearer", 'Basic realm="x", Bearer scope="a"'),
        (
            "challenge_basic_params_before_bearer",
            'Basic realm="x", charset="UTF-8", Bearer error="invalid_token"',
        ),
        ("challenge_token68_before_bearer", 'Negotiate abc==, Bearer scope="a"'),
        (
            "challenge_two_bearer_first_wins",
            'Bearer scope="first", Bearer scope="second"',
        ),
        ("challenge_bearer_without_params", "Bearer"),
        ("challenge_spaces_around_equals", 'Bearer scope = "a"'),
        ("challenge_no_bearer", 'Basic realm="x"'),
        ("challenge_absent", None),
        ("challenge_empty", ""),
        ("challenge_duplicate_param", 'Bearer scope="a", scope="b"'),
        ("challenge_unterminated_quote", 'Bearer scope="a'),
        ("challenge_non_ascii", 'Bearer scope="é"'),
        ("challenge_param_without_scheme", 'scope="a"'),
    ):
        add(name, "oauth_challenge", {"header": header})

    def bearer(url: str) -> str:
        return f'Bearer resource_metadata="{url}"'

    for name, endpoint, header in (
        ("rm_header_on_origin", E, bearer(RM + "/mcp")),
        ("rm_header_exact_well_known", E, bearer(RM)),
        (
            "rm_header_offsite_host",
            E,
            bearer("https://evil.example/.well-known/oauth-protected-resource"),
        ),
        (
            "rm_header_subdomain",
            E,
            bearer(
                "https://auth.mcp.research.example/.well-known/oauth-protected-resource"
            ),
        ),
        (
            "rm_header_other_port",
            E,
            bearer(
                "https://mcp.research.example:8443/.well-known/oauth-protected-resource"
            ),
        ),
        (
            "rm_header_http",
            E,
            bearer("http://mcp.research.example/.well-known/oauth-protected-resource"),
        ),
        (
            "rm_header_default_port_spelled",
            E,
            bearer(
                "https://mcp.research.example:443/.well-known/oauth-protected-resource"
            ),
        ),
        (
            "rm_header_uppercase_host",
            E,
            bearer("https://MCP.research.example/.well-known/oauth-protected-resource"),
        ),
        ("rm_header_path_prefix_lookalike", E, bearer(RM + "x")),
        (
            "rm_header_other_well_known",
            E,
            bearer(ORIGIN + "/.well-known/openid-configuration"),
        ),
        ("rm_header_query", E, bearer(RM + "?x=1")),
        ("rm_header_fragment", E, bearer(RM + "#x")),
        (
            "rm_header_userinfo",
            E,
            bearer(
                "https://mcp.research.example@evil.example/.well-known/oauth-protected-resource"
            ),
        ),
        ("rm_header_dot_segment", E, bearer(RM + "/../../evil")),
        ("rm_header_relative", E, bearer("/.well-known/oauth-protected-resource")),
        ("rm_no_header_path_then_root", E, None),
        ("rm_no_header_nested_path", ORIGIN + "/api/mcp", None),
        ("rm_no_header_root_endpoint", ORIGIN, None),
        ("rm_basic_challenge_falls_back", E, 'Basic realm="x"'),
        (
            "rm_malformed_header_falls_back",
            E,
            'Bearer resource_metadata="https://evil.example/x',
        ),
        ("rm_bearer_without_resource_metadata", E, 'Bearer scope="a"'),
    ):
        add(
            name,
            "oauth_resource_metadata_urls",
            {"endpoint": endpoint, "header": header},
        )

    def prm(
        name: str,
        responses: dict[str, Any],
        header: Any = None,
        endpoint: str = E,
        local: bool = False,
    ) -> None:
        add(
            name,
            "oauth_resource_metadata",
            {
                "endpoint": endpoint,
                "local": local,
                "header": header,
                "responses": responses,
            },
        )

    prm("prm_found_from_header", {RM + "/mcp": _ok(_prm())}, bearer(RM + "/mcp"))
    prm(
        "prm_path_missing_root_found",
        {RM + "/mcp": {"status": 404}, RM: _ok(_prm())},
        "Bearer",
    )
    prm(
        "prm_header_url_missing_has_no_fallback",
        {RM + "/mcp": {"status": 404}, RM: _ok(_prm())},
        bearer(RM + "/mcp"),
    )
    prm("prm_none_found", {})
    prm("prm_body_not_object_is_missing", {RM + "/mcp": _ok([1]), RM: _ok(_prm())})
    prm(
        "prm_resource_mismatch",
        {RM + "/mcp": _ok(_prm("https://other.research.example/mcp"))},
    )
    prm("prm_resource_trailing_slash_mismatch", {RM + "/mcp": _ok(_prm(E + "/"))})
    prm(
        "prm_order_offsite_before_fetch",
        {
            "https://evil.example/.well-known/oauth-protected-resource": _ok(_prm()),
            RM + "/mcp": _ok(_prm()),
        },
        bearer("https://evil.example/.well-known/oauth-protected-resource"),
    )
    prm("prm_order_resource_before_servers", {RM + "/mcp": _ok(_prm(E + "/x", []))})
    prm("prm_no_authorization_servers", {RM + "/mcp": _ok({"resource": E})})
    prm(
        "prm_invalid_issuers_dropped",
        {
            RM + "/mcp": _ok(
                _prm(
                    servers=[
                        "http://login.research.example",
                        AS,
                        AS + "?tenant=uk",
                        "https://login2.research.example/realms/uk",
                    ]
                )
            )
        },
    )
    prm(
        "prm_all_issuers_invalid",
        {RM + "/mcp": _ok(_prm(servers=["http://login.research.example", 42]))},
    )
    prm("prm_duplicate_issuers_kept_once", {RM + "/mcp": _ok(_prm(servers=[AS, AS]))})
    local_rm = "http://127.0.0.1:8765/.well-known/oauth-protected-resource"
    prm(
        "prm_loopback_issuer_accepted_when_local",
        {local_rm + "/mcp": _ok(_prm(LOCAL_E, [LOCAL_AS]))},
        endpoint=LOCAL_E,
        local=True,
    )
    prm(
        "prm_loopback_issuer_dropped_when_not_local",
        {RM + "/mcp": _ok(_prm(servers=[LOCAL_AS, AS]))},
    )
    prm(
        "prm_invalid_scopes_supported_ignored",
        {RM + "/mcp": _ok(_prm(scopes_supported=["mcp.read", "bad scope"]))},
    )

    for name, issuer in (
        ("as_urls_no_path", AS),
        ("as_urls_root_slash", "https://tenant.research.example/"),
        ("as_urls_with_path", AS + "/realms/uk"),
        ("as_urls_path_trailing_slash", AS + "/tenant1/"),
    ):
        add(name, "oauth_as_metadata_urls", {"issuer": issuer})

    def oauth_url(issuer: str) -> str:
        return connection_oauth.as_metadata_urls(issuer)[0]

    def oidc_url(issuer: str) -> str:
        return connection_oauth.as_metadata_urls(issuer)[1]

    def select(
        name: str,
        issuers: list[str],
        responses: dict[str, Any],
        record_issuer: str | None = None,
    ) -> None:
        add(
            name,
            "oauth_select_server",
            {
                "issuers": issuers,
                "responses": responses,
                "record_issuer": record_issuer,
            },
        )

    both = [AS, AS2]
    good2 = {oauth_url(AS2): _ok(_meta(AS2))}
    select("select_first_with_s256", both, {oauth_url(AS): _ok(_meta(AS))})
    select(
        "select_skips_missing_metadata",
        both,
        {oauth_url(AS): {"status": 404}, oidc_url(AS): {"status": 404}, **good2},
    )
    select(
        "select_skips_absent_s256", both, {oauth_url(AS): _ok(_meta(AS, None)), **good2}
    )
    select(
        "select_skips_empty_s256", both, {oauth_url(AS): _ok(_meta(AS, [])), **good2}
    )
    select(
        "select_skips_plain_only",
        both,
        {oauth_url(AS): _ok(_meta(AS, ["plain"])), **good2},
    )
    select(
        "select_stops_at_first_usable",
        both,
        {
            oauth_url(AS): _ok(_meta(AS)),
            oauth_url(AS2): _ok(_meta("https://evil.example")),
        },
    )
    select(
        "select_issuer_mismatch_aborts",
        both,
        {oauth_url(AS): _ok(_meta("https://evil.example")), **good2},
    )
    no_issuer = _meta(AS)
    del no_issuer["issuer"]
    select("select_issuer_missing_in_metadata", [AS], {oauth_url(AS): _ok(no_issuer)})
    realm = AS + "/realms/uk"
    realm_urls = connection_oauth.as_metadata_urls(realm)
    select(
        "select_oidc_path_insertion",
        [realm],
        {realm_urls[0]: {"status": 404}, realm_urls[1]: _ok(_meta(realm))},
    )
    select(
        "select_oidc_path_appending_last",
        [realm],
        {
            realm_urls[0]: {"status": 404},
            realm_urls[1]: {"status": 404},
            realm_urls[2]: _ok(_meta(realm)),
        },
    )
    select(
        "select_first_answer_decides_the_server",
        [AS],
        {oauth_url(AS): _ok(_meta(AS, None)), oidc_url(AS): _ok(_meta(AS))},
    )
    select(
        "select_non_object_body_tries_next_url",
        [AS],
        {oauth_url(AS): {"status": 200, "json": "text"}, oidc_url(AS): _ok(_meta(AS))},
    )
    select(
        "select_redirect_answer_tries_next_url",
        [AS],
        {oauth_url(AS): {"status": 302}, oidc_url(AS): _ok(_meta(AS))},
    )
    select("select_all_unreachable", both, {})
    select(
        "select_unreachable_and_no_s256", both, {oauth_url(AS2): _ok(_meta(AS2, None))}
    )
    select(
        "select_unreachable_and_not_found",
        both,
        {oauth_url(AS2): {"status": 404}, oidc_url(AS2): {"status": 404}},
    )
    select("select_unreachable_then_usable", both, good2)
    select(
        "select_record_issuer_listed_later",
        both,
        {oauth_url(AS): _ok(_meta(AS)), **good2},
        AS2,
    )
    select(
        "select_record_issuer_not_listed", [AS], {oauth_url(AS): _ok(_meta(AS))}, AS2
    )
    select(
        "select_record_issuer_without_s256",
        both,
        {oauth_url(AS): _ok(_meta(AS, None)), **good2},
        AS,
    )
    select("select_record_issuer_unreachable", both, good2, AS)

    register = AS + "/register"
    for name, client_id, metadata in (
        ("reg_record_client_id", "astral-uk", {}),
        (
            "reg_record_client_id_over_metadata_document",
            "astral-uk",
            {
                "client_id_metadata_document_supported": True,
                "registration_endpoint": register,
            },
        ),
        (
            "reg_metadata_document",
            None,
            {"client_id_metadata_document_supported": True},
        ),
        (
            "reg_metadata_document_flag_must_be_true",
            None,
            {
                "client_id_metadata_document_supported": "true",
                "registration_endpoint": register,
            },
        ),
        (
            "reg_metadata_document_over_dynamic",
            None,
            {
                "client_id_metadata_document_supported": True,
                "registration_endpoint": register,
            },
        ),
        ("reg_dynamic", None, {"registration_endpoint": register}),
        ("reg_nothing_offered", None, {}),
        ("reg_registration_endpoint_not_text", None, {"registration_endpoint": True}),
    ):
        add(name, "oauth_registration", {"client_id": client_id, "metadata": metadata})

    created = {"status": 201, "json": {"client_id": "dyn-1"}}
    refused = {"status": 400, "json": {"error": "invalid_client_metadata"}}
    for name, answers in (
        ("dyn_native_accepted", [created]),
        ("dyn_native_400_then_web_accepted", [refused, created]),
        ("dyn_both_refused", [refused, refused]),
        ("dyn_native_500_has_no_retry", [{"status": 500}]),
        ("dyn_native_unreachable", [None]),
        ("dyn_accepted_without_client_id", [{"status": 201, "json": {}}]),
        ("dyn_web_unreachable", [refused, None]),
    ):
        add(
            name,
            "oauth_dynamic_registration",
            {"redirect_uri": REDIRECT, "responses": answers},
        )

    thirty_three = " ".join(f"s{i:02d}" for i in range(33))
    for name, challenge, supported in (
        ("scopes_from_challenge", "mcp.tools mcp.read", ["other"]),
        ("scopes_challenge_runs_of_spaces", " b  a ", None),
        ("scopes_from_scopes_supported", None, ["mcp.read"]),
        ("scopes_none", None, None),
        ("scopes_empty_challenge_falls_through", "", ["mcp.read"]),
        ("scopes_invalid_challenge_falls_through", 'a "b"', ["mcp.read"]),
        ("scopes_invalid_scopes_supported_gives_none", None, ["ok", "bad\\scope"]),
        ("scopes_sorted_and_unique", "c a b a", None),
        ("scopes_32_accepted", " ".join(f"s{i:02d}" for i in range(32)), None),
        ("scopes_33_refused", thirty_three, None),
    ):
        add(
            name,
            "oauth_scopes",
            {"challenge": challenge, "scopes_supported": supported},
        )

    for name, supported, values in (
        ("iss_supported_absent", True, []),
        ("iss_supported_equal", True, [AS]),
        ("iss_supported_different", True, [AS2]),
        ("iss_unsupported_absent", False, []),
        ("iss_unsupported_equal", False, [AS]),
        ("iss_unsupported_different", False, [AS2]),
        ("iss_flag_must_be_true", "true", []),
        ("iss_trailing_slash_differs", True, [AS + "/"]),
        ("iss_case_differs", True, ["https://Login.research.example"]),
        ("iss_empty_value", False, [""]),
        ("iss_repeated", True, [AS, AS]),
    ):
        add(
            name, "oauth_iss", {"issuer": AS, "iss_supported": supported, "iss": values}
        )

    def forbidden(
        scope: str | None, error: str = "insufficient_scope"
    ) -> dict[str, Any]:
        header = f'Bearer error="{error}"' + (
            f', scope="{scope}"' if scope is not None else ""
        )
        return {"forbidden": header}

    success = {"success": True}
    thirty = [f"s{i:02d}" for i in range(30)]
    many_new = " ".join(f"n{i:02d}" for i in range(40))
    for name, scopes_, steps in (
        ("stepup_first_allowed_with_union", ["mcp.read"], [forbidden("mcp.write")]),
        (
            "stepup_second_without_success_hits_limit",
            ["mcp.read"],
            [forbidden("mcp.write"), forbidden("mcp.admin")],
        ),
        (
            "stepup_success_resets_the_count",
            ["mcp.read"],
            [forbidden("mcp.write"), success, forbidden("mcp.admin")],
        ),
        (
            "stepup_no_new_scope_signs_in_again",
            ["mcp.read", "mcp.write"],
            [forbidden("mcp.write")],
        ),
        (
            "stepup_invalid_challenge_scope_adds_nothing",
            ["mcp.read"],
            [forbidden("ok bad\\\\scope")],
        ),
        (
            "stepup_not_insufficient_scope",
            ["mcp.read"],
            [forbidden(None, "invalid_token")],
        ),
        (
            "stepup_refusal_does_not_count",
            ["mcp.read"],
            [forbidden(None, "invalid_token"), forbidden("mcp.write")],
        ),
        ("stepup_too_many_scopes", thirty, [forbidden("a b c"), forbidden("a")]),
        (
            "stepup_limit_before_scope_count",
            ["mcp.read"],
            [forbidden("mcp.write"), forbidden(many_new)],
        ),
    ):
        add(name, "oauth_step_up_sequence", {"scopes": scopes_, "steps": steps})

    for name, url in (
        ("bearer_same_url", E),
        ("bearer_other_path_same_origin", ORIGIN + "/other"),
        ("bearer_query_same_origin", E + "?x=1"),
        ("bearer_other_host", "https://evil.example/mcp"),
        ("bearer_subdomain", "https://api.mcp.research.example/mcp"),
        (
            "bearer_host_prefix_lookalike",
            "https://mcp.research.example.evil.example/mcp",
        ),
        ("bearer_other_port", "https://mcp.research.example:8443/mcp"),
        ("bearer_http_downgrade", "http://mcp.research.example/mcp"),
        ("bearer_default_port_spelled", "https://mcp.research.example:443/mcp"),
        ("bearer_userinfo", "https://mcp.research.example@evil.example/mcp"),
    ):
        add(name, "oauth_bearer_target", {"endpoint": E, "url": url})

    return {
        "format": "astral.vectors/v1",
        "name": "connection-oauth-v1",
        "spec": SPEC,
        "cases": cases,
    }


def render() -> dict[str, str]:
    return {
        name: json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        for name, doc in (
            ("connection-v1", generate_connection()),
            ("connection-oauth-v1", generate_oauth()),
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
