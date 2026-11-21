"""Reference for assembly.md: validates an activation manifest in the contract's order and compares it with a node's capability names and caller notification types.
generate_assembly_vectors.py runs every case through run(), and tests/test_assembly.py holds the node's assembly_manifest.py and config/astral-assembly.json to the same answers.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

FORMAT = "astral.assembly/v0"
SECTIONS = {"format", "assembly", "ports", "capabilities", "doors", "exceptions"}
PORTS = ("identity", "authority", "persistence", "tool-provider", "ui-adaptation")
SEVERAL_ADAPTERS = {"tool-provider"}
CONTROLS = {"consent-gate", "egress-allowlist", "emergency-stop", "phi-gate", "ticket-verification"}
CONSENT_BY_PRINCIPAL = {
    ("owner-device", False): {"full", "none"},
    ("owner-device", True): {"full", "none"},
    ("caller-ticket", True): {"own-redacted", "none"},
    ("caller-ticket", False): {"none"},
    ("outside-service", False): {"none"},
    ("outside-service", True): {"none"},
}
NAME = re.compile(r"[a-z0-9][a-z0-9./-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def exact(value: Any, members: set[str], unknown: str, missing: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Refused("assembly.malformed")
    if set(value) - members:
        raise Refused(unknown)
    if members - set(value):
        raise Refused(missing)
    return value


def names(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise Refused("assembly.malformed")
    for item in value:
        if not isinstance(item, str) or NAME.match(item) is None:
            raise Refused("assembly.malformed")
    if len(set(value)) != len(value):
        raise Refused("assembly.malformed")
    return value


def text(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def check_contract(value: Any) -> None:
    bad = "assembly.contract_malformed"
    entry = exact(value, {"format", "path", "sha256"}, bad, bad) if isinstance(value, dict) else None
    if entry is None or not text(entry["format"]) or not text(entry["path"]):
        raise Refused(bad)
    path = PurePosixPath(entry["path"])
    if path.is_absolute() or ".." in path.parts or "\\" in entry["path"] or path.as_posix() != entry["path"]:
        raise Refused(bad)
    if not isinstance(entry["sha256"], str) or SHA256.match(entry["sha256"]) is None:
        raise Refused(bad)


def check_port(name: str, value: Any) -> None:
    port = exact(value, {"adapters", "contracts"}, "assembly.unknown_key", "assembly.malformed")
    adapters = names(port["adapters"])
    if not adapters:
        raise Refused("assembly.malformed")
    if len(adapters) > 1 and name not in SEVERAL_ADAPTERS:
        raise Refused("assembly.ambiguous")
    if not isinstance(port["contracts"], list):
        raise Refused("assembly.contract_malformed")
    for entry in port["contracts"]:
        check_contract(entry)


def check_door(value: Any) -> None:
    door = exact(value, {"principal", "turns", "notification_types", "consent"}, "assembly.unknown_key", "assembly.malformed")
    if door["principal"] not in ("owner-device", "caller-ticket", "outside-service"):
        raise Refused("assembly.unknown_principal")
    if not isinstance(door["turns"], bool):
        raise Refused("assembly.malformed")
    names(door["notification_types"])
    if door["consent"] not in ("full", "own-redacted", "none"):
        raise Refused("assembly.malformed")
    if door["consent"] not in CONSENT_BY_PRINCIPAL[(door["principal"], door["turns"])]:
        raise Refused("assembly.consent_visibility")


def check_exception(value: Any) -> None:
    bad = "assembly.exception_malformed"
    members = {"id", "date", "review", "summary"}
    entry = exact(value, members, bad, bad) if isinstance(value, dict) else None
    if entry is None or not all(text(entry[member]) for member in members) or DATE.match(entry["date"]) is None:
        raise Refused(bad)


def validate(document: Any) -> None:
    if not isinstance(document, dict):
        raise Refused("assembly.malformed")
    if document.get("format") != FORMAT:
        raise Refused("assembly.unknown_format")
    exact(document, SECTIONS, "assembly.unknown_section", "assembly.section_missing")
    if not isinstance(document["assembly"], str) or NAME.match(document["assembly"]) is None:
        raise Refused("assembly.malformed")
    ports = exact(document["ports"], set(PORTS), "assembly.unknown_port", "assembly.port_missing")
    for name in PORTS:
        check_port(name, ports[name])
    capabilities = exact(document["capabilities"], {"offered", "required_controls"}, "assembly.unknown_key", "assembly.malformed")
    offered, controls = names(capabilities["offered"]), names(capabilities["required_controls"])
    if offered != sorted(offered) or controls != sorted(controls):
        raise Refused("assembly.malformed")
    if set(controls) - CONTROLS:
        raise Refused("assembly.unknown_control")
    doors = document["doors"]
    if not isinstance(doors, dict) or not doors or "" in doors:
        raise Refused("assembly.malformed")
    for door in doors.values():
        check_door(door)
    if not isinstance(document["exceptions"], list):
        raise Refused("assembly.exception_malformed")
    for entry in document["exceptions"]:
        check_exception(entry)


def runtime(document: dict[str, Any], capabilities: list[str], notification_types: list[str]) -> None:
    if document["capabilities"]["offered"] != sorted(set(capabilities)):
        raise Refused("assembly.capability_mismatch")
    for door in document["doors"].values():
        if door["principal"] == "caller-ticket" and door["notification_types"] != notification_types:
            raise Refused("assembly.notification_mismatch")


def run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    try:
        validate(case["document"])
        if kind == "runtime":
            runtime(case["document"], case["capabilities"], case["notification_types"])
        elif kind != "validate":
            raise ValueError(kind)
    except Refused as refused:
        return {"code": refused.code}
    return {"ok": True}
