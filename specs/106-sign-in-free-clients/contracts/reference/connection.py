"""Reference for connections carried on settings.set: the key grammar, the record and state rules, the consent digest, the fold, and the per-device use, name and egress checks.
It reuses the preserved 105 reference for LETS-CJ/1, hashes and signatures and the caller ticket label table, and vectors/connection-v1.json pins it for every other implementation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "105-decentralized-personal-mesh/contracts/reference"
    ),
)
import caller_ticket
from astral_mesh_ref import crypto
from astral_mesh_ref import fold as mesh_fold
from astral_mesh_ref.cj import b64url_decode, canonical
from astral_mesh_ref.errors import MeshError

RECORD_PREFIX = "connection/"
STATE_PREFIX = "connection.state/"
RESERVED_PREFIXES = ("connection/", "connection.")
CARRIER = "settings.set"
LABELS = {"confidentiality": "private", "integrity": "owner"}
CONSENT_KIND = "connection"
CONSENT_CONTEXT = "astral.consent/v1"
CONSENT_FIELDS = {"clock", "device", "digest", "kind", "sig", "v"}
CONSENT_WINDOW_MS = 600000
MAX_RECORD_BYTES = 32768
MAX_NAME = 80
MAX_URL = 2048
MAX_TOOLS = 128
MAX_SCOPES = 32
MAX_EGRESS = 16
NAME_BUDGET = 64
SLUG_MAX = 49
SLUG_FALLBACK = "agent"
ASK_PREFIX = "ask_"
IMPLEMENTED = {"mcp-http": ("oauth", "none")}
FLOWS = ("private", "phi")
STATES = ("not_signed_in", "signed_in", "needs_attention")
REASONS = ("refresh_refused", "expired", "scope", "refused")
LOOPBACK = ("127.0.0.1", "[::1]")
DEFAULT_PORT = {"https": 443, "http": 80}
MAX_PORT = 65535
EGRESS_DEFAULT_PORT = 443
RECORD_FIELDS = {
    "v",
    "id",
    "name",
    "kind",
    "endpoint",
    "local",
    "device",
    "auth",
    "tools",
    "flow",
    "egress",
}
AUTH_FIELDS = {"scheme", "issuer", "scopes", "resource", "client_id"}
STATE_FIELDS = {"v", "state", "reason", "issuer", "ms"}
PUT_CHECKS = (
    "connection.malformed",
    "connection.consent_missing",
    "connection.consent_wrong_action",
    "connection.consent_unknown_device",
    "connection.consent_node_device",
    "connection.consent_foreign_feed",
    "connection.consent_bad_signature",
    "connection.consent_stale",
    "connection.consent_reused",
)
STATE_CHECKS = (
    "connection.malformed",
    "connection.writer_not_member",
    "connection.state_foreign",
)
VIEW_REASONS = (
    "connection.version_unsupported",
    "connection.kind_unsupported",
    "connection.record_unsupported",
    "connection.scheme_unsupported",
    "connection.server_id_collision",
)
USE_CODES = (
    "connection.local_elsewhere",
    "connection.egress_refused",
    "tool.name_too_long",
    "tool.name_invalid",
)
CODES = tuple(dict.fromkeys(PUT_CHECKS + STATE_CHECKS + VIEW_REASONS + USE_CODES))

ID_HEX = re.compile(r"[0-9a-f]{32}")
DEVICE_ID = re.compile(r"sha256:[0-9a-f]{64}")
KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
PORT = re.compile(r"[1-9][0-9]{0,4}")
SEGMENT = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-F]{2})+")
ESCAPE = re.compile(r"%([0-9A-F]{2})")
UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
URL = re.compile(
    r"(https|http)://(\[::1\]|[^/?#:@\[\]]+)(?::([^/?#]*))?((?:/[^?#]*)?)(\?[^#]*)?"
)
ENTRY = re.compile(r"(\[::1\]|[^:\[\]]+)(?::([^:]*))?")
SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,256}")
CLIENT_ID = re.compile(r"[\x21-\x7e]{1,1024}")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
OFFERED_NAME = re.compile(r"[A-Za-z0-9_-]+")


class RemovedApproval(MeshError):
    pass


def _fail(code: str, detail: str = "") -> MeshError:
    return MeshError(code, detail)


def _int(value: Any) -> bool:
    return type(value) is int


def reserved_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(RESERVED_PREFIXES)


def parse_key(key: Any) -> dict[str, Any]:
    if not reserved_key(key):
        return {"form": "other"}
    if key.startswith(RECORD_PREFIX) and ID_HEX.fullmatch(key[len(RECORD_PREFIX) :]):
        return {"form": "record", "id": key[len(RECORD_PREFIX) :]}
    if key.startswith(STATE_PREFIX):
        parts = key[len(STATE_PREFIX) :].split("/")
        if (
            len(parts) == 2
            and ID_HEX.fullmatch(parts[0])
            and DEVICE_ID.fullmatch(parts[1])
        ):
            return {"form": "state", "id": parts[0], "device": parts[1]}
    raise _fail("connection.malformed", "reserved key")


def dns_host(host: str) -> bool:
    labels = host.split(".")
    return (
        len(host) <= 253
        and len(labels) >= 2
        and all(HOST_LABEL.fullmatch(label) for label in labels)
        and not NUMERIC_LABEL.fullmatch(labels[-1])
    )


def port_number(text: str) -> int | None:
    return int(text) if PORT.fullmatch(text) and int(text) <= MAX_PORT else None


def _port(text: str | None, scheme: str) -> int | None:
    if text is None:
        return None
    number = port_number(text)
    if number is None or number == DEFAULT_PORT[scheme]:
        raise ValueError("port")
    return number


def path_ok(path: str, trailing_slash: bool) -> bool:
    if path == "":
        return True
    if trailing_slash and path.endswith("/"):
        path = path[:-1]
        if path == "":
            return True
    if not path.startswith("/"):
        return False
    return all(
        SEGMENT.fullmatch(segment)
        and segment not in (".", "..")
        and not any(
            chr(int(code, 16)) in UNRESERVED for code in ESCAPE.findall(segment)
        )
        for segment in path[1:].split("/")
    )


def split_url(text: Any, form: str) -> dict[str, Any] | None:
    if (
        not isinstance(text, str)
        or len(text) > MAX_URL
        or any(not "\x21" <= c <= "\x7e" for c in text)
    ):
        return None
    match = URL.fullmatch(text)
    if match is None:
        return None
    scheme, host, port_text, path, query = match.groups()
    if host not in LOOPBACK and not dns_host(host):
        return None
    try:
        port = _port(port_text, scheme)
    except ValueError:
        return None
    if form != "fetch" and (query is not None or not path_ok(path, form == "issuer")):
        return None
    origin = f"{scheme}://{host}" + ("" if port is None else f":{port}")
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "path": path,
        "origin": origin,
        "loopback": host in LOOPBACK,
    }


def secure_url(text: Any, form: str, local: bool) -> dict[str, Any] | None:
    parsed = split_url(text, form)
    if parsed is None:
        return None
    if parsed["loopback"]:
        return parsed if local else None
    return parsed if parsed["scheme"] == "https" else None


def entry_for(parsed: dict[str, Any]) -> str:
    port = parsed["port"] or DEFAULT_PORT[parsed["scheme"]]
    return parsed["host"] if port == EGRESS_DEFAULT_PORT else f"{parsed['host']}:{port}"


def parse_entry(entry: Any, local: bool) -> tuple[str, int] | None:
    match = ENTRY.fullmatch(entry) if isinstance(entry, str) else None
    if match is None:
        return None
    host, port_text = match.groups()
    if host in LOOPBACK:
        if not local:
            return None
    elif not dns_host(host):
        return None
    if port_text is None:
        return host, EGRESS_DEFAULT_PORT
    number = port_number(port_text)
    if number is None or number == EGRESS_DEFAULT_PORT:
        return None
    return host, number


def exec_host(entry: str) -> dict[str, Any]:
    parsed = parse_entry(entry, True)
    if parsed is None:
        raise _fail("connection.egress_refused", entry)
    return {"host": parsed[0], "port": parsed[1]}


def _name_ok(name: Any) -> bool:
    return (
        isinstance(name, str)
        and 1 <= len(name) <= MAX_NAME
        and not any(caller_ticket.refused_in_label(c) for c in name)
    )


def check_common(key_id: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise _fail("connection.malformed", "record must be an object")
    try:
        size = len(canonical(value))
    except MeshError as error:
        raise _fail("connection.malformed", error.code) from error
    if size > MAX_RECORD_BYTES:
        raise _fail("connection.malformed", "record over 32 KiB")
    if not _int(value.get("v")) or value["v"] < 1:
        raise _fail("connection.malformed", "v")
    if value.get("id") != key_id:
        raise _fail("connection.malformed", "id")
    if not _name_ok(value.get("name")):
        raise _fail("connection.malformed", "name")
    if not isinstance(value.get("kind"), str) or not KIND.fullmatch(value["kind"]):
        raise _fail("connection.malformed", "kind")


def _sorted_unique(items: list[Any], key: Any = None) -> bool:
    keys = [key(i) for i in items] if key else items
    return all(a < b for a, b in zip(keys, keys[1:]))


def tools_ok(tools: Any) -> bool:
    return (
        isinstance(tools, list)
        and len(tools) <= MAX_TOOLS
        and all(
            isinstance(t, dict)
            and set(t) == {"name", "hash"}
            and isinstance(t["name"], str)
            and TOOL_NAME.fullmatch(t["name"])
            and crypto.is_id(t["hash"])
            for t in tools
        )
        and _sorted_unique(tools, lambda t: t["name"])
    )


def _scopes_ok(scopes: Any) -> bool:
    return (
        isinstance(scopes, list)
        and len(scopes) <= MAX_SCOPES
        and all(isinstance(s, str) and SCOPE.fullmatch(s) for s in scopes)
        and _sorted_unique(scopes)
    )


def _mcp_http_values(value: dict[str, Any]) -> bool:
    local = value["local"]
    if not isinstance(local, bool):
        return False
    device = value["device"]
    if local != (isinstance(device, str) and bool(DEVICE_ID.fullmatch(device))):
        return False
    if not local and device is not None:
        return False
    endpoint = split_url(value["endpoint"], "endpoint")
    if endpoint is None or endpoint["loopback"] != local:
        return False
    if not local and endpoint["scheme"] != "https":
        return False
    auth = value["auth"]
    issuer = None
    if auth["scheme"] == "oauth":
        issuer = secure_url(auth["issuer"], "issuer", local)
        if (
            issuer is None
            or auth["resource"] != value["endpoint"]
            or not _scopes_ok(auth["scopes"])
            or not (
                auth["client_id"] is None
                or (
                    isinstance(auth["client_id"], str)
                    and CLIENT_ID.fullmatch(auth["client_id"])
                )
            )
        ):
            return False
    elif (auth["issuer"], auth["resource"], auth["scopes"], auth["client_id"]) != (
        None,
        None,
        [],
        None,
    ):
        return False
    if not tools_ok(value["tools"]) or value["flow"] not in FLOWS:
        return False
    egress = value["egress"]
    if not isinstance(egress, list) or not 1 <= len(egress) <= MAX_EGRESS:
        return False
    parsed = [parse_entry(e, local) for e in egress]
    if any(p is None for p in parsed) or not _sorted_unique(parsed):
        return False
    needed = {entry_for(endpoint)} | ({entry_for(issuer)} if issuer else set())
    return needed <= set(egress)


def classify(value: dict[str, Any]) -> tuple[str, str | None]:
    if value["v"] != 1:
        return "unsupported", "connection.version_unsupported"
    if value["kind"] not in IMPLEMENTED:
        return "unsupported", "connection.kind_unsupported"
    auth = value.get("auth")
    if (
        set(value) != RECORD_FIELDS
        or not isinstance(auth, dict)
        or set(auth) != AUTH_FIELDS
        or not isinstance(auth["scheme"], str)
    ):
        return "unsupported", "connection.record_unsupported"
    if auth["scheme"] not in IMPLEMENTED[value["kind"]]:
        return "unsupported", "connection.scheme_unsupported"
    if not _mcp_http_values(value):
        return "unsupported", "connection.record_unsupported"
    return "ok", None


def check_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in (
        STATE_FIELDS,
        STATE_FIELDS | {"tools"},
    ):
        raise _fail("connection.malformed", "state fields")
    if not _int(value["v"]) or value["v"] != 1 or value["state"] not in STATES:
        raise _fail("connection.malformed", "state")
    attention = value["state"] == "needs_attention"
    if (value["reason"] in REASONS) != attention or (
        not attention and value["reason"] is not None
    ):
        raise _fail("connection.malformed", "reason")
    if (
        value["issuer"] is not None
        and secure_url(value["issuer"], "issuer", True) is None
    ):
        raise _fail("connection.malformed", "issuer")
    if not _int(value["ms"]) or value["ms"] < 0:
        raise _fail("connection.malformed", "ms")
    if "tools" in value and (
        value["state"] != "signed_in" or not tools_ok(value["tools"])
    ):
        raise _fail("connection.malformed", "tools")
    return value


def digest(value: Any) -> str:
    return crypto.object_id(value)


def server_id(record_id: str) -> str:
    return "c" + record_id[:8]


def exposed_name(record_id: str, tool: str) -> str:
    return f"{server_id(record_id)}__{tool}"


def offered_name(record_id: str, tool: str) -> str:
    name = exposed_name(record_id, tool)
    if len(name) > NAME_BUDGET:
        raise _fail("tool.name_too_long", name)
    if not OFFERED_NAME.fullmatch(name):
        raise _fail("tool.name_invalid", name)
    return name


def ask_tool(name: str) -> str:
    words: list[str] = []
    word = ""
    for char in name:
        if "A" <= char <= "Z":
            char = chr(ord(char) + 32)
        if "a" <= char <= "z" or "0" <= char <= "9":
            word += char
        elif word:
            words.append(word)
            word = ""
    if word:
        words.append(word)
    slug = "_".join(words)[:SLUG_MAX].rstrip("_")
    return ASK_PREFIX + (slug or SLUG_FALLBACK)


def egress_allows(egress: list[str], url: str) -> str:
    parsed = split_url(url, "fetch")
    if parsed is None or (parsed["scheme"] != "https" and not parsed["loopback"]):
        raise _fail("connection.egress_refused", "url")
    entry = entry_for(parsed)
    if entry not in egress:
        raise _fail("connection.egress_refused", entry)
    return entry


def is_connection_event(event: dict[str, Any]) -> bool:
    body = event.get("body")
    return (
        event.get("type") == CARRIER
        and isinstance(body, dict)
        and reserved_key(body.get("key"))
    )


def _check_consent_shape(consent: Any) -> dict[str, Any]:
    clock = consent.get("clock") if isinstance(consent, dict) else None
    if (
        not isinstance(consent, dict)
        or set(consent) != CONSENT_FIELDS
        or not _int(consent["v"])
        or consent["v"] != 1
        or not all(
            isinstance(consent[f], str) for f in ("kind", "digest", "device", "sig")
        )
        or not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(not _int(v) or v < 0 for v in clock.values())
    ):
        raise _fail("connection.malformed", "consent")
    return consent


def _signed_by(consent: dict[str, Any], presence: str) -> bool:
    try:
        key = b64url_decode(presence)
    except MeshError:
        return False
    return crypto.verify_object("p256", key, CONSENT_CONTEXT, consent)


def _in_window(consent: dict[str, Any], event: dict[str, Any]) -> bool:
    ms = event["clock"]["ms"]
    return ms - CONSENT_WINDOW_MS <= consent["clock"]["ms"] <= ms


def _check_put(
    event: dict[str, Any],
    key_id: str,
    members: dict[str, Any],
    removed: dict[str, Any],
    used: set[bytes],
) -> dict[str, Any]:
    body = event["body"]
    check_common(key_id, body["value"])
    if "consent" not in body:
        raise _fail("connection.consent_missing")
    consent = _check_consent_shape(body["consent"])
    if consent["kind"] != CONSENT_KIND or consent["digest"] != digest(body["value"]):
        raise _fail("connection.consent_wrong_action")
    device = consent["device"]
    member = members.get(device)
    if member is None:
        gone = removed.get(device)
        if (
            gone
            and gone["kind"] != "node"
            and event["feed"] in gone["feeds"]
            and _signed_by(consent, gone["presence"])
            and _in_window(consent, event)
        ):
            raise RemovedApproval("connection.consent_unknown_device", device)
        raise _fail("connection.consent_unknown_device", device)
    if member["kind"] == "node":
        raise _fail("connection.consent_node_device")
    if event["feed"] not in member["feeds"]:
        raise _fail("connection.consent_foreign_feed")
    if not _signed_by(consent, member["presence"]):
        raise _fail("connection.consent_bad_signature")
    if not _in_window(consent, event):
        raise _fail("connection.consent_stale")
    if _consent_key(consent) in used:
        raise _fail("connection.consent_reused")
    return consent


def _consent_key(consent: dict[str, Any]) -> bytes:
    return canonical({k: v for k, v in consent.items() if k != "sig"})


def _writer_counts(event: dict[str, Any], feed_owner: dict[str, str]) -> str:
    owner = feed_owner.get(event["feed"])
    if owner is None:
        raise _fail("connection.writer_not_member", event["feed"])
    return owner


def _check_event(
    event: dict[str, Any],
    members: dict[str, Any],
    removed: dict[str, Any],
    feed_owner: dict[str, str],
    roster_owner: dict[str, str],
    used: set[bytes],
) -> dict[str, Any]:
    body = event["body"]
    if event.get("scope") != "mesh" or event.get("labels") != LABELS:
        raise _fail("connection.malformed", "scope or labels")
    key = parse_key(body["key"])
    if key["form"] == "record":
        if set(body) == {"key", "value"} and body["value"] is None:
            _writer_counts(event, roster_owner)
            return {"form": "remove", "id": key["id"]}
        if (
            set(body) not in ({"key", "value"}, {"key", "value", "consent"})
            or body["value"] is None
        ):
            raise _fail("connection.malformed", "body")
        consent = _check_put(event, key["id"], members, removed, used)
        return {"form": "put", "id": key["id"], "consent": consent}
    if set(body) != {"key", "value"}:
        raise _fail("connection.malformed", "body")
    check_state(body["value"])
    writer = _writer_counts(event, feed_owner)
    if writer != key["device"]:
        raise _fail("connection.state_foreign")
    return {"form": "state", "id": key["id"], "device": writer}


def fold(state: dict[str, Any]) -> dict[str, Any]:
    members, removed = state["members"], state["removed"]
    legacy = set(state.get("legacy_servers", []))
    feed_owner = {f: d for d, m in members.items() for f in m["feeds"]}
    roster_owner = {f: d for d, m in {**removed, **members}.items() for f in m["feeds"]}
    heads: dict[str, tuple[Any, dict[str, Any], dict[str, Any]]] = {}
    pending: dict[str, tuple[Any, dict[str, Any]]] = {}
    states: dict[str, dict[str, tuple[Any, Any]]] = {}
    ignored: dict[str, str] = {}
    used: set[bytes] = set()
    for event in sorted(
        (e for e in state["events"] if is_connection_event(e)), key=mesh_fold.order_key
    ):
        rank = mesh_fold.order_key(event)
        try:
            checked = _check_event(
                event, members, removed, feed_owner, roster_owner, used
            )
        except MeshError as error:
            ignored[event["id"]] = error.code
            if isinstance(error, RemovedApproval):
                pending[parse_key(event["body"]["key"])["id"]] = (rank, event)
            continue
        if checked["form"] == "state":
            per = states.setdefault(checked["id"], {})
            per[checked["device"]] = (rank, event["body"]["value"])
        else:
            if checked["form"] == "put":
                used.add(_consent_key(checked["consent"]))
            heads[checked["id"]] = (rank, event, checked)
    connections = []
    for record_id, (_, event, checked) in sorted(heads.items()):
        if checked["form"] != "put":
            continue
        value = event["body"]["value"]
        status, reason = classify(value)
        connections.append(
            {
                "id": record_id,
                "server": server_id(record_id),
                "event": event["id"],
                "approved_by": checked["consent"]["device"],
                "record": value,
                "status": status,
                "reason": reason,
                "states": {
                    d: v for d, (_, v) in sorted(states.get(record_id, {}).items())
                },
            }
        )
    seen: set[str] = set(legacy)
    for entry in connections:
        if entry["server"] in seen and entry["status"] == "ok":
            entry["status"], entry["reason"] = (
                "unsupported",
                "connection.server_id_collision",
            )
        seen.add(entry["server"])
    approvals = [
        {
            "id": record_id,
            "event": event["id"],
            "device": event["body"]["consent"]["device"],
            "record": event["body"]["value"],
        }
        for record_id, (rank, event) in sorted(pending.items())
        if record_id not in heads or heads[record_id][0] < rank
    ]
    return {
        "connections": connections,
        "removed_approvals": approvals,
        "ignored": dict(sorted(ignored.items())),
    }


def use(entry: dict[str, Any], device: str) -> dict[str, Any]:
    if entry["status"] != "ok":
        raise _fail(entry["reason"])
    record = entry["record"]
    if record["local"] and record["device"] != device:
        raise _fail("connection.local_elsewhere", device)
    return {}


def coexistence(case_input: dict[str, Any]) -> dict[str, Any]:
    events = case_input["events"]
    plain = [e for e in events if not is_connection_event(e)]
    return {
        "authority": mesh_fold.authority(events, case_input["presence_keys"]),
        "settings_with": mesh_fold.settings(events),
        "settings_without": mesh_fold.settings(plain),
    }


def _record_result(case_input: dict[str, Any]) -> dict[str, Any]:
    value = case_input["value"]
    check_common(case_input["id"], value)
    status, reason = classify(value)
    return {
        "status": status,
        "reason": reason,
        "digest": digest(value),
        "server": server_id(case_input["id"]),
    }


def _state_result(value: Any) -> dict[str, Any]:
    check_state(value)
    return {}


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as error:
        return {"error": error.code}


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    calls = {
        "connection_key": lambda: parse_key(case_input["key"]),
        "connection_record": lambda: _record_result(case_input),
        "connection_state": lambda: _state_result(case_input["value"]),
        "connection_fold": lambda: fold(case_input),
        "connection_use": lambda: use(case_input["connection"], case_input["device"]),
        "connection_exposed_name": lambda: {
            "name": offered_name(case_input["id"], case_input["tool"])
        },
        "connection_ask_slug": lambda: {"tool": ask_tool(case_input["name"])},
        "connection_egress": lambda: {
            "entry": egress_allows(case_input["egress"], case_input["url"])
        },
        "connection_exec_host": lambda: exec_host(case_input["entry"]),
        "connection_coexistence": lambda: coexistence(case_input),
    }
    return _guard(calls[kind])
