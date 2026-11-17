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
a2a_card = importlib.import_module("a2a_card")
cj = importlib.import_module("astral_mesh_ref.cj")
consent_mod = importlib.import_module("astral_mesh_ref.consent")
crypto = importlib.import_module("astral_mesh_ref.crypto")
testkit = importlib.import_module("astral_mesh_ref.testkit")
tools = importlib.import_module("astral_mesh_ref.tools")
b64 = cj.b64url_encode
DetRng, TestDevice, hlc = testkit.DetRng, testkit.TestDevice, testkit.hlc

VECTORS = HERE.parent / "vectors"
PHASE0 = HERE.parents[1] / "phase-0/a2a-reader/fixtures"
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


class SecondFeed:
    def __init__(self, device: Any, label: str) -> None:
        self.id = device.id
        self.device = device
        self.feed = b64(crypto.ed25519_public(testkit.seed(label)))


BOB_2 = SecondFeed(BOB, "local-v2/bob/ops-2")
MEMBERS = {d.id: _entry(d) for d in CURRENT}
MEMBERS[BOB.id]["feeds"].append(BOB_2.feed)
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


def _fixture(*parts: str) -> Any:
    return json.loads(PHASE0.joinpath(*parts).read_text(encoding="utf-8"))


A2A_CARD = _fixture("valid", "extended_card.json")
A2A_ORIGIN = "https://agent.example.org"
A2A_URL = A2A_ORIGIN + "/.well-known/agent-card.json"
A2A_SIGNER = a2a_card.jwk_thumbprint(_fixture("keys", "fixture-signer-one.public.jwk.json"))
A2A_AS = "https://login.example.org"


def a2a_full_record(
    record_id: str, name: str = "Agent desk", **change: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "a2a",
        "card_url": A2A_URL,
        "card": {
            "hash": a2a_card.card_digest(A2A_CARD),
            "origin": A2A_ORIGIN,
            "signer": None,
            "extended_hash": None,
        },
        "auth": {
            "scheme": "none",
            "issuer": None,
            "scopes": [],
            "resource": None,
            "client_id": None,
        },
        "tools": [],
        "flow": "private",
        "egress": ["agent.example.org"],
    }
    for key, item in change.items():
        if key in ("auth", "card"):
            value[key] = {**value[key], **item}
        else:
            value[key] = item
    return copy.deepcopy(value)


def stdio_record(record_id: str, name: str = "Local tools", **change: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "mcp-stdio",
        "command": "/usr/local/bin/files-server",
        "args": ["--root", "/Users/owner/Documents"],
        "env": ["FILES_TOKEN"],
        "device": ALICE.id,
        "tools": TOOLS,
        "flow": "private",
        "egress": ["files.research.example"],
        **change,
    }


def http_api_record(record_id: str, name: str = "Lab API", **change: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "http-api",
        "endpoint": "https://api.research.example/v2",
        "key": {
            "item": "connection.key." + record_id,
            "header": "Authorization",
            "format": "Bearer {key}",
        },
        "tools": TOOLS,
        "flow": "phi",
        "egress": ["api.research.example"],
        **change,
    }


def model_record(record_id: str, name: str = "Lab model", **change: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "id": record_id,
        "name": name,
        "kind": "model",
        "endpoint": "https://models.research.example/v1",
        "key": {"item": "model.provider.lab-models"},
        "flow": "private",
        "egress": ["models.research.example"],
        **change,
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
RID3 = RNG.bytes(16).hex()
RID4 = RNG.bytes(16).hex()
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
        "record_endpoint_encoded_dot_segment", "https://mcp.research.example/%2E%2E/mcp"
    )
    endpoint_case(
        "record_endpoint_encoded_letter", "https://mcp.research.example/%6Dcp"
    )
    endpoint_case(
        "record_endpoint_reserved_escape_valid", "https://mcp.research.example/m%2Fcp"
    )
    endpoint_case(
        "record_endpoint_port_leading_zero", "https://mcp.research.example:08443/mcp"
    )
    endpoint_case(
        "record_endpoint_port_out_of_range",
        "https://mcp.research.example:65536/mcp",
        egress=["login.research.example", "mcp.research.example:65536"],
    )
    endpoint_case(
        "record_endpoint_trailing_dot_host", "https://mcp.research.example./mcp"
    )
    endpoint_case("record_endpoint_single_label_host", "https://mcp/mcp")
    endpoint_case("record_endpoint_numeric_last_label", "https://mcp.research.1234/mcp")
    endpoint_case("record_endpoint_2049_characters", E + "/" + "a" * (2048 - len(E)))
    endpoint_case(
        "record_endpoint_2048_characters_valid", E + "/" + "a" * (2047 - len(E))
    )
    port_max = "https://mcp.research.example:65535/mcp"
    rec(
        "record_endpoint_port_65535_valid",
        record(
            RID,
            endpoint=port_max,
            auth={"resource": port_max},
            egress=["login.research.example", "mcp.research.example:65535"],
        ),
    )
    long_host = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    for name, host in (
        ("record_endpoint_host_253_valid", long_host),
        ("record_endpoint_host_254", long_host + "d"),
        ("record_endpoint_label_63_valid", "a" * 63 + ".research.example"),
        ("record_endpoint_label_64", "a" * 64 + ".research.example"),
    ):
        url = f"https://{host}/mcp"
        rec(
            name,
            record(
                RID,
                endpoint=url,
                auth={"resource": url},
                egress=sorted(["login.research.example", host]),
            ),
        )
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
    rec(
        "record_tool_name_128_valid",
        record(RID, tools=[{"hash": _hash("x"), "name": "x" * 128}]),
    )
    rec("record_scope_256_valid", record(RID, auth={"scopes": ["s" * 256]}))
    rec("record_scope_257", record(RID, auth={"scopes": ["s" * 257]}))
    rec("record_client_id_1024_valid", record(RID, auth={"client_id": "c" * 1024}))
    rec("record_client_id_1025", record(RID, auth={"client_id": "c" * 1025}))
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
    rec("record_name_bidi_override", record(RID, name="First\u202eroc"))
    rec(
        "record_name_zwj_emoji",
        record(RID, name="Team \U0001f468\u200d\U0001f469"),
    )
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
        name: str,
        log: Log,
        legacy: list[str] | None = None,
        reverse: bool = False,
        kind: str = "connection_fold",
    ) -> dict[str, Any]:
        events = list(reversed(log.events)) if reverse else log.events
        return add(
            name,
            kind,
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

    log = Log("same-feed-recarry")
    original = put(log, "p1", ALICE, t1, first)
    put(log, "p2", ALICE, t2, second)
    log.add("p3", ALICE, t3, copy.deepcopy(original["body"]))
    fold("fold_same_feed_recarry_within_window", log)
    log = Log("order-stale-reused")
    original = put(log, "p1", ALICE, t1, first)
    put(log, "p2", ALICE, t2, second)
    log.add("p3", ALICE, t1 + 11 * MIN, copy.deepcopy(original["body"]))
    fold("fold_order_stale_before_reused", log)
    log = Log("stranger-feed")
    put(log, "p1", MALLORY, t1, first, make_consent(first, ALICE, t1))
    fold("fold_stranger_feed_carries_member_consent", log)
    log = Log("second-feed-put")
    put(log, "p1", BOB_2, t1, first, make_consent(first, BOB, t1))
    fold("fold_put_from_a_second_feed", log)
    log = Log("second-feed-state")
    put(log, "p1", ALICE, t1, first)
    log.add(
        "s",
        BOB_2,
        t2,
        {"key": f"connection.state/{RID}/{BOB.id}", "value": state_value()},
    )
    fold("fold_state_from_a_second_feed", log)
    log = Log("second-feed-remove")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", BOB_2, t2, RID)
    fold("fold_remove_from_a_second_feed", log)

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
    log = Log("removed-tampered")
    put(log, "p1", DAVE, t1, first, tampered(make_consent(first, DAVE, t1)))
    fold("fold_removed_approval_with_bad_signature_not_listed", log)
    log = Log("removed-stale")
    put(log, "p1", DAVE, t1, first, make_consent(first, DAVE, t1 - 600001))
    fold("fold_removed_approval_outside_the_window_not_listed", log)
    log = Log("removed-then-remove")
    put(log, "p1", DAVE, t1, first)
    remove(log, "x", BOB, t2, RID)
    fold("fold_removed_approval_before_a_remove_not_listed", log)
    log = Log("remove-then-removed")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", BOB, t2, RID)
    put(log, "p2", DAVE, t3, second)
    fold("fold_removed_approval_after_a_remove_listed", log)
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
    fold("fold_removal_by_removed_device_stands", log)
    log = Log("remove-unknown")
    put(log, "p1", ALICE, t1, first)
    remove(log, "x", MALLORY, t2, RID)
    fold("fold_removal_by_unknown_feed_ignored", log)
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

    def entry_of(
        value: Any, legacy: list[str] | None = None, extended: bool = False
    ) -> dict[str, Any]:
        log = Log("use")
        put(log, "p", ALICE, t1, value)
        listed = connection.fold(
            {
                "members": MEMBERS,
                "removed": REMOVED,
                "events": log.events,
                "legacy_servers": legacy or [],
            },
            extended,
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
        ("slug_non_ascii", "Caf\u00e9 \u00d1and\u00fa"),
        ("slug_digits", "Agent 007"),
        ("slug_leading_and_trailing_symbols", "  (Helper)  "),
        ("slug_hyphen_and_underscore_are_separators", "multi-agent_desk"),
        ("slug_all_symbols", "!!!"),
        ("slug_non_ascii_only", "\u65e5\u672c\u8a9e"),
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
        ("exec_entry_not_canonical", "mcp.research.example:443"),
    ):
        add(name, "connection_exec_host", {"entry": entry})

    def ext(name: str, value: Any) -> None:
        add(name, "connection_record_ext", {"id": RID, "value": value})

    def without(value: dict[str, Any], *fields: str) -> dict[str, Any]:
        return {k: v for k, v in value.items() if k not in fields}

    a2a_ok = a2a_full_record(RID)
    a2a_tool = {"name": "ask_agent_desk", "hash": a2a_card.tool_hash(A2A_CARD)}
    a2a_oauth = a2a_full_record(
        RID,
        auth={"scheme": "oauth", "issuer": A2A_AS, "scopes": ["read"]},
        egress=["agent.example.org", "login.example.org"],
    )
    for name, value in (
        ("record_phase2_a2a_full_unsupported", a2a_ok),
        ("record_phase2_stdio_unsupported", stdio_record(RID)),
        ("record_phase2_http_api_unsupported", http_api_record(RID)),
        ("record_phase2_model_unsupported", model_record(RID)),
    ):
        rec(name, value)
    for name, value in (
        ("record_ext_a2a_unsigned_valid", a2a_ok),
        (
            "record_ext_a2a_signed_valid",
            a2a_full_record(
                RID,
                card={
                    "signer": A2A_SIGNER,
                    "extended_hash": a2a_card.card_digest(A2A_CARD),
                },
            ),
        ),
        ("record_ext_a2a_key_valid", a2a_full_record(RID, auth={"scheme": "key"})),
        ("record_ext_a2a_oauth_valid", a2a_oauth),
        (
            "record_ext_a2a_oauth_client_id_valid",
            {**a2a_oauth, "auth": {**a2a_oauth["auth"], "client_id": "astral-uk"}},
        ),
        ("record_ext_a2a_ask_tool_valid", a2a_full_record(RID, tools=[a2a_tool])),
        ("record_ext_a2a_flow_phi_valid", a2a_full_record(RID, flow="phi")),
        (
            "record_ext_a2a_tool_name_not_the_ask_slug",
            a2a_full_record(RID, tools=[{**a2a_tool, "name": "ask_other"}]),
        ),
        (
            "record_ext_a2a_two_tools",
            a2a_full_record(
                RID, tools=[a2a_tool, {**a2a_tool, "name": "ask_agent_desk_2"}]
            ),
        ),
        (
            "record_ext_a2a_card_origin_differs",
            a2a_full_record(RID, card={"origin": "https://other.example.org"}),
        ),
        (
            "record_ext_a2a_card_hash_not_an_id",
            a2a_full_record(RID, card={"hash": "sha256:abc"}),
        ),
        (
            "record_ext_a2a_signer_not_a_thumbprint",
            a2a_full_record(RID, card={"signer": "short"}),
        ),
        (
            "record_ext_a2a_extended_hash_not_an_id",
            a2a_full_record(RID, card={"extended_hash": "x"}),
        ),
        (
            "record_ext_a2a_card_extra_field",
            a2a_full_record(RID, card={"approved": True}),
        ),
        (
            "record_ext_a2a_card_missing_field",
            {**a2a_ok, "card": without(a2a_ok["card"], "signer")},
        ),
        (
            "record_ext_a2a_card_url_http",
            a2a_full_record(
                RID, card_url="http://agent.example.org/.well-known/agent-card.json"
            ),
        ),
        (
            "record_ext_a2a_card_url_loopback",
            a2a_full_record(
                RID,
                card_url="http://127.0.0.1:8765/card",
                card={"origin": "http://127.0.0.1:8765"},
                egress=["127.0.0.1:8765"],
            ),
        ),
        (
            "record_ext_a2a_card_url_with_query",
            a2a_full_record(RID, card_url=A2A_URL + "?v=1"),
        ),
        (
            "record_ext_a2a_egress_lacks_card_host",
            a2a_full_record(RID, egress=["other.example.org"]),
        ),
        (
            "record_ext_a2a_oauth_issuer_not_in_egress",
            {**a2a_oauth, "egress": ["agent.example.org"]},
        ),
        (
            "record_ext_a2a_oauth_resource_set",
            {**a2a_oauth, "auth": {**a2a_oauth["auth"], "resource": A2A_URL}},
        ),
        (
            "record_ext_a2a_oauth_loopback_issuer",
            {
                **a2a_oauth,
                "auth": {**a2a_oauth["auth"], "issuer": "http://127.0.0.1:8766"},
                "egress": ["127.0.0.1:8766", "agent.example.org"],
            },
        ),
        (
            "record_ext_a2a_none_with_scopes",
            a2a_full_record(RID, auth={"scopes": ["read"]}),
        ),
        (
            "record_ext_a2a_scheme_basic_unsupported",
            a2a_full_record(RID, auth={"scheme": "basic"}),
        ),
        (
            "record_ext_a2a_unknown_field",
            {**a2a_ok, "device": None},
        ),
        ("record_ext_a2a_flow_unknown", a2a_full_record(RID, flow="public")),
        ("record_ext_mcp_key_valid", record(RID, auth={"scheme": "key", "issuer": None, "scopes": [], "resource": None}, egress=["mcp.research.example"])),
        (
            "record_ext_mcp_key_with_issuer",
            record(RID, auth={"scheme": "key", "scopes": [], "resource": None}),
        ),
        ("record_ext_mcp_oauth_still_valid", record(RID)),
        ("record_ext_mcp_dpop_scheme_unsupported", record(RID, auth={"scheme": "dpop"})),
        ("record_ext_kind_unknown_unsupported", record(RID, kind="carrier-pigeon")),
        ("record_ext_version_2_unsupported", stdio_record(RID, v=2)),
    ):
        ext(name, value)

    for name, value in (
        ("record_ext_stdio_valid", stdio_record(RID)),
        ("record_ext_stdio_no_egress_valid", stdio_record(RID, egress=[])),
        ("record_ext_stdio_no_env_valid", stdio_record(RID, env=[], args=[])),
        ("record_ext_stdio_names_node_valid", stdio_record(RID, device=NODE.id)),
        ("record_ext_stdio_empty_arg_valid", stdio_record(RID, args=["", "x"])),
        ("record_ext_stdio_args_64_valid", stdio_record(RID, args=["a"] * 64)),
        ("record_ext_stdio_args_65", stdio_record(RID, args=["a"] * 65)),
        ("record_ext_stdio_arg_4096_valid", stdio_record(RID, args=["a" * 4096])),
        ("record_ext_stdio_arg_4097", stdio_record(RID, args=["a" * 4097])),
        ("record_ext_stdio_arg_not_text", stdio_record(RID, args=[1])),
        ("record_ext_stdio_arg_with_control", stdio_record(RID, args=["a\u0000b"])),
        ("record_ext_stdio_command_empty", stdio_record(RID, command="")),
        ("record_ext_stdio_command_1024_valid", stdio_record(RID, command="c" * 1024)),
        ("record_ext_stdio_command_1025", stdio_record(RID, command="c" * 1025)),
        ("record_ext_stdio_command_newline", stdio_record(RID, command="a\nb")),
        ("record_ext_stdio_command_bidi_override", stdio_record(RID, command="a\u202eb")),
        ("record_ext_stdio_env_32_valid", stdio_record(RID, env=[f"K{i:02d}" for i in range(32)])),
        ("record_ext_stdio_env_33", stdio_record(RID, env=[f"K{i:02d}" for i in range(33)])),
        ("record_ext_stdio_env_name_64_valid", stdio_record(RID, env=["A" * 64])),
        ("record_ext_stdio_env_name_65", stdio_record(RID, env=["A" * 65])),
        ("record_ext_stdio_env_lowercase", stdio_record(RID, env=["files_token"])),
        ("record_ext_stdio_env_leading_digit", stdio_record(RID, env=["1TOKEN"])),
        ("record_ext_stdio_env_unsorted", stdio_record(RID, env=["B", "A"])),
        ("record_ext_stdio_env_duplicate", stdio_record(RID, env=["A", "A"])),
        ("record_ext_stdio_env_path", stdio_record(RID, env=["PATH"])),
        ("record_ext_stdio_env_ld_preload", stdio_record(RID, env=["LD_PRELOAD"])),
        ("record_ext_stdio_env_dyld_insert", stdio_record(RID, env=["DYLD_INSERT_LIBRARIES"])),
        ("record_ext_stdio_device_null", stdio_record(RID, device=None)),
        ("record_ext_stdio_device_not_an_id", stdio_record(RID, device="alice")),
        ("record_ext_stdio_loopback_egress", stdio_record(RID, egress=["127.0.0.1:8765"])),
        ("record_ext_stdio_egress_unsorted", stdio_record(RID, egress=["b.research.example", "a.research.example"])),
        ("record_ext_stdio_egress_17", stdio_record(RID, egress=[f"h{i:02d}.research.example" for i in range(17)])),
        ("record_ext_stdio_flow_unknown", stdio_record(RID, flow="public")),
        ("record_ext_stdio_unknown_field", stdio_record(RID, local=True)),
        ("record_ext_stdio_has_no_auth", stdio_record(RID, auth={"scheme": "none"})),
    ):
        ext(name, value)

    http_ok = http_api_record(RID)
    for name, value in (
        ("record_ext_http_api_valid", http_ok),
        ("record_ext_http_api_api_key_header_valid", http_api_record(RID, key={**http_ok["key"], "header": "X-Api-Key", "format": "{key}"})),
        ("record_ext_http_api_empty_tools_valid", http_api_record(RID, tools=[])),
        ("record_ext_http_api_endpoint_with_port_valid", http_api_record(RID, endpoint="https://api.research.example:8443/v2", egress=["api.research.example:8443"])),
        ("record_ext_http_api_item_names_another_id", http_api_record(RID, key={**http_ok["key"], "item": "connection.key." + RID2})),
        ("record_ext_http_api_item_is_a_provider", http_api_record(RID, key={**http_ok["key"], "item": "model.provider.lab"})),
        ("record_ext_http_api_header_host", http_api_record(RID, key={**http_ok["key"], "header": "Host"})),
        ("record_ext_http_api_header_cookie_any_case", http_api_record(RID, key={**http_ok["key"], "header": "COOKIE"})),
        ("record_ext_http_api_header_with_space", http_api_record(RID, key={**http_ok["key"], "header": "X Api"})),
        ("record_ext_http_api_header_64_valid", http_api_record(RID, key={**http_ok["key"], "header": "X" * 64})),
        ("record_ext_http_api_header_65", http_api_record(RID, key={**http_ok["key"], "header": "X" * 65})),
        ("record_ext_http_api_format_without_placeholder", http_api_record(RID, key={**http_ok["key"], "format": "Bearer"})),
        ("record_ext_http_api_format_two_placeholders", http_api_record(RID, key={**http_ok["key"], "format": "{key}{key}"})),
        ("record_ext_http_api_format_64_valid", http_api_record(RID, key={**http_ok["key"], "format": "a" * 59 + "{key}"})),
        ("record_ext_http_api_format_65", http_api_record(RID, key={**http_ok["key"], "format": "a" * 60 + "{key}"})),
        ("record_ext_http_api_format_with_newline", http_api_record(RID, key={**http_ok["key"], "format": "{key}\n"})),
        ("record_ext_http_api_key_extra_field", http_api_record(RID, key={**http_ok["key"], "scheme": "bearer"})),
        ("record_ext_http_api_key_missing_header", http_api_record(RID, key=without(http_ok["key"], "header"))),
        ("record_ext_http_api_endpoint_http", http_api_record(RID, endpoint="http://api.research.example/v2")),
        ("record_ext_http_api_endpoint_loopback", http_api_record(RID, endpoint="http://127.0.0.1:8765/v2", egress=["127.0.0.1:8765"])),
        ("record_ext_http_api_egress_lacks_endpoint", http_api_record(RID, egress=["other.research.example"])),
        ("record_ext_http_api_tools_unsorted", http_api_record(RID, tools=list(reversed(TOOLS)))),
        ("record_ext_http_api_flow_unknown", http_api_record(RID, flow="public")),
        ("record_ext_http_api_unknown_field", http_api_record(RID, local=False)),
    ):
        ext(name, value)

    model_ok = model_record(RID)
    for name, value in (
        ("record_ext_model_valid", model_ok),
        ("record_ext_model_flow_phi_valid", model_record(RID, flow="phi")),
        ("record_ext_model_item_128_valid", model_record(RID, key={"item": "model.provider." + "a" * 113})),
        ("record_ext_model_item_129", model_record(RID, key={"item": "model.provider." + "a" * 114})),
        ("record_ext_model_item_not_a_provider", model_record(RID, key={"item": "connection.key." + RID})),
        ("record_ext_model_item_without_name", model_record(RID, key={"item": "model.provider."})),
        ("record_ext_model_item_uppercase", model_record(RID, key={"item": "model.provider.Lab"})),
        ("record_ext_model_item_leading_dash", model_record(RID, key={"item": "model.provider.-lab"})),
        ("record_ext_model_key_has_header", model_record(RID, key={"item": "model.provider.lab", "header": "Authorization"})),
        ("record_ext_model_endpoint_http", model_record(RID, endpoint="http://models.research.example/v1")),
        ("record_ext_model_egress_lacks_endpoint", model_record(RID, egress=["other.research.example"])),
        ("record_ext_model_has_tools", model_record(RID, tools=[])),
        ("record_ext_model_flow_unknown", model_record(RID, flow="public")),
    ):
        ext(name, value)

    log = Log("ext-kinds")
    ext_records = {
        "a2a": a2a_full_record(RID),
        "stdio": stdio_record(RID2),
        "http": http_api_record(RID3),
        "model": model_record(RID4),
    }
    for index, (label, value) in enumerate(ext_records.items()):
        put(log, label, ALICE, t1 + index, value)
    fold("fold_phase2_lists_every_new_kind_unsupported", log)
    fold("fold_ext_lists_every_new_kind_ok", log, kind="connection_fold_ext")
    log = Log("ext-later")
    put(log, "p1", ALICE, t1, first)
    put(log, "p2", ALICE, t2, a2a_full_record(RID, "Second"))
    fold("fold_ext_later_a2a_put_wins", log, kind="connection_fold_ext")
    log = Log("ext-bad")
    put(log, "p", ALICE, t1, a2a_full_record(RID, flow="public"))
    fold("fold_ext_bad_values_unsupported", log, kind="connection_fold_ext")
    log = Log("ext-collision")
    put(log, "p1", ALICE, t1, model_record(HIGHER))
    put(log, "p2", BOB, t2, http_api_record(LOWER))
    fold("fold_ext_server_id_collision", log, kind="connection_fold_ext")
    log = Log("ext-state")
    put(log, "p", ALICE, t1, a2a_full_record(RID))
    put_state(
        log,
        "s",
        ALICE,
        ALICE,
        t2,
        state_value("needs_attention", "not_signed_in", t2),
    )
    fold("fold_ext_state_not_signed_in", log, kind="connection_fold_ext")

    for name, value, device in (
        ("use_ext_stdio_on_its_device", stdio_record(RID), ALICE),
        ("use_ext_stdio_elsewhere", stdio_record(RID), BOB),
        ("use_ext_stdio_on_the_node_it_names", stdio_record(RID, device=NODE.id), NODE),
        ("use_ext_a2a_on_any_device", a2a_full_record(RID), BOB),
        ("use_ext_http_api_on_any_device", http_api_record(RID), CAROL),
        ("use_ext_model_on_any_device", model_record(RID), NODE),
    ):
        add(
            name,
            "connection_use",
            {"connection": entry_of(value, None, True), "device": device.id},
        )

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
                "feeds": MEMBERS.get(d.id, REMOVED.get(d.id, _entry(d)))["feeds"],
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
        ("challenge_non_ascii", 'Bearer scope="\u00e9"'),
        ("challenge_param_without_scheme", 'scope="a"'),
        ("challenge_two_header_lines_joined", ['Basic realm="x"', 'Bearer scope="a"']),
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
        (
            "rm_header_encoded_dot_segment",
            E,
            bearer(RM + "/%2E%2E/%2E%2E/other"),
        ),
        ("rm_endpoint_not_canonical", "https://MCP.research.example/mcp", None),
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
        ("as_urls_issuer_not_canonical", AS + "?tenant=uk"),
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
    select(
        "select_no_valid_metadata_is_unreachable",
        both,
        {
            oauth_url(AS): {"status": 404},
            oidc_url(AS): {"status": 302},
            oauth_url(AS2): {"status": 500},
            oidc_url(AS2): {"status": 200, "json": "not metadata"},
        },
    )
    select(
        "select_valid_metadata_without_s256_is_no_s256",
        [AS],
        {oauth_url(AS): _ok(_meta(AS, ["plain"]))},
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
        (
            "dyn_method_none_accepted",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "none"}}],
        ),
        (
            "dyn_method_absent_accepted",
            [{"status": 201, "json": {"client_id": "dyn-1"}}],
        ),
        (
            "dyn_method_client_secret_basic_refused",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "client_secret_basic", "client_secret": "s"}}],
        ),
        (
            "dyn_method_client_secret_post_refused",
            [{"status": 200, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "client_secret_post"}}],
        ),
        (
            "dyn_method_private_key_jwt_refused",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "private_key_jwt"}}],
        ),
        (
            "dyn_method_null_refused",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": None}}],
        ),
        (
            "dyn_method_not_text_refused",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": ["none"]}}],
        ),
        (
            "dyn_method_uppercase_refused",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "None"}}],
        ),
        (
            "dyn_web_retry_method_refused",
            [refused, {"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "client_secret_basic"}}],
        ),
        (
            "dyn_native_method_refused_has_no_retry",
            [{"status": 201, "json": {"client_id": "dyn-1", "token_endpoint_auth_method": "client_secret_basic"}}, created],
        ),
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
    add(
        "bearer_endpoint_not_canonical",
        "oauth_bearer_target",
        {"endpoint": "https://mcp.research.example:443/mcp", "url": E},
    )

    device = "https://device.research.example/device"
    for name, endpoint, issuer, metadata, local in (
        ("egress_entries_full_metadata", E, AS, _meta(AS, registration_endpoint="https://register.research.example/register", device_authorization_endpoint=device), False),
        ("egress_entries_device_authorization_host_joins", E, AS, _meta(AS, device_authorization_endpoint=device), False),
        ("egress_entries_device_host_same_as_issuer_listed_once", E, AS, _meta(AS, device_authorization_endpoint=AS + "/device"), False),
        ("egress_entries_device_host_other_port", E, AS, _meta(AS, device_authorization_endpoint=AS + ":8443/device"), False),
        ("egress_entries_without_device_endpoint", E, AS, _meta(AS), False),
        ("egress_entries_token_host_elsewhere", E, AS, _meta(AS, token_endpoint="https://tokens.research.example/token"), False),
        ("egress_entries_registration_host_joins", E, AS, _meta(AS, registration_endpoint="https://register.research.example/register"), False),
        ("egress_entries_no_issuer", E, None, _meta(AS), False),
        ("egress_entries_non_text_endpoint_ignored", E, AS, _meta(AS, device_authorization_endpoint=5), False),
        ("egress_entries_device_endpoint_http_refused", E, AS, _meta(AS, device_authorization_endpoint="http://device.research.example/device"), False),
        ("egress_entries_device_endpoint_loopback_refused", E, AS, _meta(AS, device_authorization_endpoint="http://127.0.0.1:9000/device"), False),
        ("egress_entries_device_endpoint_uppercase_host_refused", E, AS, _meta(AS, device_authorization_endpoint="https://Device.research.example/device"), False),
        ("egress_entries_device_endpoint_userinfo_refused", E, AS, _meta(AS, device_authorization_endpoint="https://x@device.research.example/device"), False),
        ("egress_entries_authorization_endpoint_http_refused", E, AS, _meta(AS, authorization_endpoint="http://login.research.example/authorize"), False),
        ("egress_entries_local_loopback", LOCAL_E, LOCAL_AS, _meta(LOCAL_AS, device_authorization_endpoint="http://127.0.0.1:8767/device"), True),
        ("egress_entries_endpoint_not_canonical", "https://Mcp.research.example/mcp", AS, _meta(AS), False),
    ):
        add(
            name,
            "oauth_egress_entries",
            {"endpoint": endpoint, "issuer": issuer, "metadata": metadata, "local": local},
        )

    return {
        "format": "astral.vectors/v1",
        "name": "connection-oauth-v1",
        "spec": SPEC,
        "cases": cases,
    }


def render() -> dict[str, str]:
    return {
        name: json.dumps(doc, indent=2, sort_keys=True) + "\n"
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
