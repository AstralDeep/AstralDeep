"""Validates the node's activation manifest, config/astral-assembly.json, against the astral.assembly/v0 contract and the capability and notification constants the doors serve.
boot_posture.py refuses to start on any problem, mcp_server_endpoint.py reads the caller notification types from here, and the contract reference tests run the same checks with the pinned contract digests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, List, Sequence

FORMAT = "astral.assembly/v0"
FILE = "astral-assembly.json"
SECTIONS = ("format", "assembly", "ports", "capabilities", "doors", "exceptions")
PORTS = ("identity", "authority", "persistence", "tool-provider", "ui-adaptation")
MULTI_ADAPTER_PORTS = frozenset({"tool-provider"})
PORT_KEYS = frozenset({"adapters", "contracts"})
CONTRACT_KEYS = frozenset({"format", "path", "sha256"})
CAPABILITY_KEYS = frozenset({"offered", "required_controls"})
DOOR_KEYS = frozenset({"principal", "turns", "notification_types", "consent"})
EXCEPTION_KEYS = frozenset({"id", "date", "review", "summary"})
PRINCIPALS = ("owner-device", "caller-ticket", "outside-service")
OWNER = "owner-device"
CONSENT_FULL, CONSENT_REDACTED, CONSENT_NONE = "full", "own-redacted", "none"
CONTROLS = frozenset({"consent-gate", "egress-allowlist", "emergency-stop", "phi-gate", "ticket-verification"})
CALLER_NOTIFICATION_TYPES: tuple = ()

_NAME = re.compile(r"[a-z0-9][a-z0-9./-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class AssemblyError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _unique_pairs(pairs: list) -> dict:
    document: dict = {}
    for key, value in pairs:
        if key in document:
            raise AssemblyError("assembly.malformed")
        document[key] = value
    return document


def load(path: Path) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise AssemblyError("assembly.missing") from None
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError:
        raise AssemblyError("assembly.malformed") from None


def _keys(value: Any, expected: Iterable[str], unknown: str, missing: str) -> dict:
    if not isinstance(value, dict):
        raise AssemblyError("assembly.malformed")
    wanted = set(expected)
    if set(value) - wanted:
        raise AssemblyError(unknown)
    if wanted - set(value):
        raise AssemblyError(missing)
    return value


def _names(value: Any, code: str = "assembly.malformed") -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not _NAME.match(item) for item in value):
        raise AssemblyError(code)
    if len(set(value)) != len(value):
        raise AssemblyError(code)
    return value


def _contract(value: Any) -> None:
    if not isinstance(value, dict):
        raise AssemblyError("assembly.contract_malformed")
    entry = _keys(value, CONTRACT_KEYS, "assembly.contract_malformed", "assembly.contract_malformed")
    path = entry["path"]
    if not isinstance(entry["format"], str) or not entry["format"] or not isinstance(path, str) or not path:
        raise AssemblyError("assembly.contract_malformed")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path or pure.as_posix() != path:
        raise AssemblyError("assembly.contract_malformed")
    if not isinstance(entry["sha256"], str) or not _SHA256.match(entry["sha256"]):
        raise AssemblyError("assembly.contract_malformed")


def _port(name: str, value: Any) -> None:
    port = _keys(value, PORT_KEYS, "assembly.unknown_key", "assembly.malformed")
    adapters = _names(port["adapters"])
    if not adapters:
        raise AssemblyError("assembly.malformed")
    if len(adapters) > 1 and name not in MULTI_ADAPTER_PORTS:
        raise AssemblyError("assembly.ambiguous")
    if not isinstance(port["contracts"], list):
        raise AssemblyError("assembly.contract_malformed")
    for entry in port["contracts"]:
        _contract(entry)


def _door(value: Any) -> None:
    door = _keys(value, DOOR_KEYS, "assembly.unknown_key", "assembly.malformed")
    if door["principal"] not in PRINCIPALS:
        raise AssemblyError("assembly.unknown_principal")
    if not isinstance(door["turns"], bool):
        raise AssemblyError("assembly.malformed")
    _names(door["notification_types"])
    consent = door["consent"]
    if consent not in (CONSENT_FULL, CONSENT_REDACTED, CONSENT_NONE):
        raise AssemblyError("assembly.malformed")
    if door["principal"] == OWNER:
        allowed = (CONSENT_FULL, CONSENT_NONE)
    elif door["principal"] == "caller-ticket" and door["turns"]:
        allowed = (CONSENT_REDACTED, CONSENT_NONE)
    else:
        allowed = (CONSENT_NONE,)
    if consent not in allowed:
        raise AssemblyError("assembly.consent_visibility")


def _exception(value: Any) -> None:
    if not isinstance(value, dict):
        raise AssemblyError("assembly.exception_malformed")
    entry = _keys(value, EXCEPTION_KEYS, "assembly.exception_malformed", "assembly.exception_malformed")
    if any(not isinstance(entry[key], str) or not entry[key] for key in EXCEPTION_KEYS):
        raise AssemblyError("assembly.exception_malformed")
    if not _DATE.match(entry["date"]):
        raise AssemblyError("assembly.exception_malformed")


def validate(document: Any) -> dict:
    if not isinstance(document, dict):
        raise AssemblyError("assembly.malformed")
    if document.get("format") != FORMAT:
        raise AssemblyError("assembly.unknown_format")
    _keys(document, SECTIONS, "assembly.unknown_section", "assembly.section_missing")
    if not isinstance(document["assembly"], str) or not _NAME.match(document["assembly"]):
        raise AssemblyError("assembly.malformed")
    ports = _keys(document["ports"], PORTS, "assembly.unknown_port", "assembly.port_missing")
    for name in PORTS:
        _port(name, ports[name])
    capabilities = _keys(document["capabilities"], CAPABILITY_KEYS, "assembly.unknown_key", "assembly.malformed")
    offered = _names(capabilities["offered"])
    controls = _names(capabilities["required_controls"])
    if offered != sorted(offered) or controls != sorted(controls):
        raise AssemblyError("assembly.malformed")
    if set(controls) - CONTROLS:
        raise AssemblyError("assembly.unknown_control")
    doors = document["doors"]
    if not isinstance(doors, dict) or not doors or any(not isinstance(key, str) or not key for key in doors):
        raise AssemblyError("assembly.malformed")
    for door in doors.values():
        _door(door)
    if not isinstance(document["exceptions"], list):
        raise AssemblyError("assembly.exception_malformed")
    for entry in document["exceptions"]:
        _exception(entry)
    return document


def check_runtime(document: dict, capabilities: Sequence[str], caller_notification_types: Sequence[str]) -> None:
    if document["capabilities"]["offered"] != sorted(set(capabilities)):
        raise AssemblyError("assembly.capability_mismatch")
    for door in document["doors"].values():
        if door["principal"] == "caller-ticket" and door["notification_types"] != list(caller_notification_types):
            raise AssemblyError("assembly.notification_mismatch")


def check_contracts(document: dict, root: Path) -> None:
    for port in document["ports"].values():
        for entry in port["contracts"]:
            try:
                content = (Path(root) / entry["path"]).read_bytes()
            except OSError:
                raise AssemblyError("assembly.contract_missing") from None
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise AssemblyError("assembly.contract_digest")


def require(path: Path, capabilities: Sequence[str], caller_notification_types: Sequence[str]) -> dict:
    document = validate(load(path))
    check_runtime(document, capabilities, caller_notification_types)
    return document
