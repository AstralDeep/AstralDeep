"""Generates the activation manifest vectors from one valid document and one mutation per refusal, running every case through assembly.py.
`--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
assembly = importlib.import_module("assembly")

VECTORS = HERE.parent / "vectors"
NAME = "assembly-v0.json"
SPEC = "106/contracts/assembly.md"
CAPABILITIES = ["chat", "mesh-peer", "tool-dispatch"]
CONTRACT = {"format": "astral.caller/v1", "path": "contracts/caller-ticket.md", "sha256": "0f" * 32}
EXCEPTION = {"id": "arm64-pack", "date": "2026-11-18", "review": "owner", "summary": "The Linux arm64 pack is left out."}


def base() -> dict[str, Any]:
    return {
        "format": assembly.FORMAT,
        "assembly": "example.personal-node",
        "ports": {
            "identity": {"adapters": ["mesh-caller"], "contracts": [dict(CONTRACT)]},
            "authority": {"adapters": ["kernel-authz"], "contracts": []},
            "persistence": {"adapters": ["plane"], "contracts": []},
            "tool-provider": {"adapters": ["executor", "mcp-servers"], "contracts": []},
            "ui-adaptation": {"adapters": ["rote"], "contracts": []},
        },
        "capabilities": {"offered": sorted(CAPABILITIES), "required_controls": ["consent-gate", "egress-allowlist"]},
        "doors": {
            "mesh": {"principal": "owner-device", "turns": True, "notification_types": [], "consent": "full"},
            "/mcp": {"principal": "caller-ticket", "turns": False, "notification_types": [], "consent": "none"},
        },
        "exceptions": [],
    }


def changed(change: Callable[[dict[str, Any]], Any]) -> Any:
    document = copy.deepcopy(base())
    result = change(document)
    return document if result is None else result


def put(path: tuple, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(document: dict[str, Any]) -> None:
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return apply


def drop(path: tuple) -> Callable[[dict[str, Any]], None]:
    def apply(document: dict[str, Any]) -> None:
        target = document
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return apply


def caller_door(**members: Any) -> dict[str, Any]:
    door = {"principal": "caller-ticket", "turns": False, "notification_types": [], "consent": "none"}
    door.update(members)
    return door


VALIDATE: tuple = (
    ("valid", lambda document: None),
    ("valid_with_an_exception", put(("exceptions",), [dict(EXCEPTION)])),
    ("valid_caller_turn_door_redacted", put(("doors", "/turns"), caller_door(turns=True, consent="own-redacted"))),
    ("valid_owner_door_with_no_consent", put(("doors", "mesh", "consent"), "none")),
    ("not_an_object", lambda document: [document]),
    ("format_absent", drop(("format",))),
    ("format_other_version", put(("format",), "astral.assembly/v1")),
    ("unknown_section", put(("flags",), {})),
    ("section_missing", drop(("doors",))),
    ("assembly_bad_name", put(("assembly",), "Example Node")),
    ("ports_not_an_object", put(("ports",), [])),
    ("unknown_port", put(("ports", "transport"), {"adapters": ["iroh"], "contracts": []})),
    ("port_missing", drop(("ports", "authority"))),
    ("port_unknown_key", put(("ports", "identity", "flags"), [])),
    ("port_member_missing", drop(("ports", "identity", "contracts"))),
    ("adapters_empty", put(("ports", "identity", "adapters"), [])),
    ("adapters_repeated", put(("ports", "tool-provider", "adapters"), ["executor", "executor"])),
    ("adapter_bad_name", put(("ports", "identity", "adapters"), ["Mesh Caller"])),
    ("two_identity_adapters", put(("ports", "identity", "adapters"), ["mesh-caller", "institutional"])),
    ("two_persistence_adapters", put(("ports", "persistence", "adapters"), ["plane", "kernel-log"])),
    ("contracts_not_a_list", put(("ports", "identity", "contracts"), {})),
    ("contract_not_an_object", put(("ports", "identity", "contracts"), ["astral.caller/v1"])),
    ("contract_extra_member", put(("ports", "identity", "contracts"), [dict(CONTRACT, version=1)])),
    ("contract_member_missing", put(("ports", "identity", "contracts"), [{"format": "astral.caller/v1", "path": "a.md"}])),
    ("contract_absolute_path", put(("ports", "identity", "contracts"), [dict(CONTRACT, path="/etc/passwd")])),
    ("contract_parent_path", put(("ports", "identity", "contracts"), [dict(CONTRACT, path="specs/../secret.md")])),
    ("contract_backslash_path", put(("ports", "identity", "contracts"), [dict(CONTRACT, path="specs\\a.md")])),
    ("contract_unnormalized_path", put(("ports", "identity", "contracts"), [dict(CONTRACT, path="specs//a.md")])),
    ("contract_uppercase_digest", put(("ports", "identity", "contracts"), [dict(CONTRACT, sha256="0F" * 32)])),
    ("contract_short_digest", put(("ports", "identity", "contracts"), [dict(CONTRACT, sha256="0f" * 31)])),
    ("capabilities_unknown_key", put(("capabilities", "optional"), [])),
    ("capabilities_member_missing", drop(("capabilities", "required_controls"))),
    ("offered_unsorted", put(("capabilities", "offered"), ["tool-dispatch", "chat"])),
    ("offered_repeated", put(("capabilities", "offered"), ["chat", "chat"])),
    ("unknown_control", put(("capabilities", "required_controls"), ["consent-gate", "firewall"])),
    ("doors_empty", put(("doors",), {})),
    ("doors_not_an_object", put(("doors",), [])),
    ("door_empty_name", put(("doors", ""), caller_door())),
    ("door_unknown_key", put(("doors", "/mcp", "events"), [])),
    ("door_member_missing", drop(("doors", "/mcp", "turns"))),
    ("door_unknown_principal", put(("doors", "/mcp", "principal"), "contact")),
    ("door_turns_not_boolean", put(("doors", "/mcp", "turns"), 0)),
    ("door_notification_bad_name", put(("doors", "/mcp", "notification_types"), ["Progress"])),
    ("door_unknown_consent", put(("doors", "/mcp", "consent"), "partial")),
    ("caller_door_full_consent", put(("doors", "/mcp", "consent"), "full")),
    ("caller_door_redacted_without_turns", put(("doors", "/mcp", "consent"), "own-redacted")),
    ("caller_turn_door_full_consent", put(("doors", "/turns"), caller_door(turns=True, consent="full"))),
    ("owner_door_redacted_consent", put(("doors", "mesh", "consent"), "own-redacted")),
    ("outside_service_door_redacted", put(("doors", "connection"), caller_door(principal="outside-service", turns=True, consent="own-redacted"))),
    ("exceptions_not_a_list", put(("exceptions",), {})),
    ("exception_not_an_object", put(("exceptions",), ["arm64-pack"])),
    ("exception_member_missing", put(("exceptions",), [{"id": "x", "date": "2026-11-18", "review": "owner"}])),
    ("exception_empty_review", put(("exceptions",), [dict(EXCEPTION, review="")])),
    ("exception_bad_date", put(("exceptions",), [dict(EXCEPTION, date="18 Nov 2026")])),
    ("first_failure_wins_port_before_door", lambda document: (put(("ports", "identity", "adapters"), [])(document), put(("doors",), {})(document))[0]),
)

RUNTIME: tuple = (
    ("runtime_valid", lambda document: None, CAPABILITIES, []),
    ("runtime_capabilities_in_another_order", lambda document: None, list(reversed(CAPABILITIES)), []),
    ("runtime_capability_not_offered", lambda document: None, CAPABILITIES + ["mesh-mailbox"], []),
    ("runtime_offers_a_capability_the_node_lacks", lambda document: None, ["chat"], []),
    ("runtime_door_serves_an_undeclared_notification", lambda document: None, CAPABILITIES, ["notifications/progress"]),
    ("runtime_manifest_declares_an_unserved_notification", put(("doors", "/mcp", "notification_types"), ["notifications/progress"]), CAPABILITIES, []),
    ("runtime_owner_door_notifications_are_not_compared", put(("doors", "mesh", "notification_types"), ["consent.requested"]), CAPABILITIES, []),
    ("runtime_invalid_document_is_refused_first", drop(("ports",)), CAPABILITIES, []),
)


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for name, change in VALIDATE:
        case_input = {"document": changed(change)}
        cases.append({"name": name, "kind": "validate", "input": case_input, "expect": assembly.run("validate", case_input)})
    for name, change, capabilities, notification_types in RUNTIME:
        case_input = {"document": changed(change), "capabilities": capabilities, "notification_types": notification_types}
        cases.append({"name": name, "kind": "runtime", "input": case_input, "expect": assembly.run("runtime", case_input)})
    return {"spec": SPEC, "format": assembly.FORMAT, "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    text = json.dumps(generate(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    path = VECTORS / NAME
    if arguments.check:
        if not path.exists() or path.read_text() != text:
            print(f"{NAME} differs from the generator output", file=sys.stderr)
            return 1
        return 0
    path.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
