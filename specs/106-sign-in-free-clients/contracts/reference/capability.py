"""Reference for capability.md: validates an astral.capability/v1 manifest, a registry index
entry, and a uses-constraint resolution against an installed ledger, in the contract's order."""

from __future__ import annotations

import hashlib
import re
import tomllib
from typing import Any

FORMAT = "astral.capability/v1"
INDEX_FORMAT = "astral.capability-index/v1"
ID = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}\Z")
SEMVER = re.compile(r"\d+\.\d+\.\d+\Z")
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
SCRIPT_PATH = re.compile(r"tools/[A-Za-z0-9._/-]{1,120}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
KINDS = ("tool", "kernel")
NETWORK = ("none", "egress-proxy")
PHI = ("none", "reviewed")
BASELINE = "astral.core/v1"


class CapabilityError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        raise CapabilityError("capability.malformed")
    try:
        manifest = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CapabilityError("capability.malformed") from exc
    cap = manifest.get("capability")
    if not isinstance(cap, dict):
        raise CapabilityError("capability.malformed")
    if not ID.fullmatch(str(cap.get("id", ""))):
        raise CapabilityError("capability.malformed")
    if not SEMVER.fullmatch(str(cap.get("version", ""))):
        raise CapabilityError("capability.malformed")
    kind = cap.get("kind")
    if kind not in KINDS:
        raise CapabilityError("capability.malformed")
    if not isinstance(cap.get("description"), str) or not 1 <= len(cap["description"]) <= 200:
        raise CapabilityError("capability.malformed")
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict):
        raise CapabilityError("capability.malformed")
    scopes = permissions.get("scopes")
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 8 or any(not isinstance(s, str) for s in scopes):
        raise CapabilityError("capability.malformed")
    output_files = permissions.get("output_files", [])
    if not isinstance(output_files, list) or len(output_files) > 16 or any(
        not isinstance(ext, str) or not re.fullmatch(r"[a-z0-9]{1,16}", ext) for ext in output_files
    ):
        raise CapabilityError("capability.malformed")
    if permissions.get("network") not in NETWORK or permissions.get("phi") not in PHI:
        raise CapabilityError("capability.malformed")
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or not isinstance(limits.get("tool_timeout_seconds"), int) or limits["tool_timeout_seconds"] <= 0:
        raise CapabilityError("capability.malformed")
    tools = manifest.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= 32:
        raise CapabilityError("capability.malformed")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or not TOOL_NAME.fullmatch(str(tool.get("name", "")))
            or not SCRIPT_PATH.fullmatch(str(tool.get("script", "")))
            or tool.get("scope") not in scopes
        ):
            raise CapabilityError("capability.malformed")
    runtime = manifest.get("runtime")
    if kind == "kernel":
        if not isinstance(runtime, dict):
            raise CapabilityError("capability.malformed")
        if not re.fullmatch(r"\d+\.\d+", str(runtime.get("python", ""))):
            raise CapabilityError("capability.malformed")
        wheels = runtime.get("wheels")
        if not isinstance(wheels, list) or not 1 <= len(wheels) <= 64 or any(
            not isinstance(w, str) or "==" not in w for w in wheels
        ):
            raise CapabilityError("capability.malformed")
    elif runtime is not None:
        raise CapabilityError("capability.malformed")
    return {"accepted": True, "id": cap["id"], "version": cap["version"], "kind": kind}


def validate_index_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise CapabilityError("capability.malformed")
    if not ID.fullmatch(str(entry.get("id", ""))) or not SEMVER.fullmatch(str(entry.get("version", ""))):
        raise CapabilityError("capability.malformed")
    if entry.get("kind") not in KINDS:
        raise CapabilityError("capability.malformed")
    if not isinstance(entry.get("tools"), list) or any(not isinstance(t, str) for t in entry["tools"]):
        raise CapabilityError("capability.malformed")
    if not SHA256.fullmatch(str(entry.get("manifest_sha256", ""))) or not SHA256.fullmatch(
        str(entry.get("package_sha256", ""))
    ):
        raise CapabilityError("capability.malformed")
    signer = entry.get("signer")
    if signer is not None and not isinstance(signer, str):
        raise CapabilityError("capability.malformed")
    if entry.get("revoked") and not isinstance(entry.get("revoked_reason"), str):
        raise CapabilityError("capability.malformed")
    return {"accepted": True, "id": entry["id"], "version": entry["version"]}


def check_install(
    entry: dict[str, Any],
    fetched_manifest_sha256: str,
    signer: str | None,
    allow_signers: list[str] | None,
    host_python: str,
    phi_excluded: bool,
    revoked: bool,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    # The order is the contract's: digest, signer, host compatibility, revocation. Compatibility
    # reads the fetched manifest, because the index entry carries no runtime or PHI fields.
    if entry.get("manifest_sha256") != fetched_manifest_sha256:
        raise CapabilityError("capability.digest")
    if signer is None or not allow_signers or signer not in allow_signers:
        raise CapabilityError("capability.signer")
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    if isinstance(runtime, dict):
        required = str(runtime.get("python", ""))
        if required and host_python != required and not host_python.startswith(required + "."):
            raise CapabilityError("capability.incompatible")
    permissions = manifest.get("permissions") if isinstance(manifest, dict) else None
    if isinstance(permissions, dict) and phi_excluded and permissions.get("phi") == "reviewed":
        raise CapabilityError("capability.incompatible")
    if revoked:
        raise CapabilityError("capability.revoked")
    return {"installed": True, "id": entry["id"], "version": entry["version"]}


def resolve(uses: list[str], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = []
    for reference in uses:
        if "@" not in reference:
            raise CapabilityError("capability.missing")
        capability_id, constraint = reference.split("@", 1)
        matches = [
            entry
            for entry in ledger
            if entry.get("id") == capability_id and _satisfies(entry.get("version", ""), constraint)
        ]
        if not matches:
            raise CapabilityError("capability.missing")
        # Resolution is deterministic: the highest satisfying version wins.
        best = max(matches, key=lambda entry: tuple(int(part) for part in str(entry.get("version", "")).split(".")))
        resolved.append({"id": capability_id, "version": best["version"]})
    return {"resolved": resolved}


def _satisfies(version: str, constraint: str) -> bool:
    if SEMVER.fullmatch(constraint):
        return version == constraint
    if constraint.startswith("^"):
        floor = constraint[1:].split(".")
        candidate = version.split(".")
        if not SEMVER.fullmatch(version) or candidate[0] != floor[0]:
            return False
        minor = int(floor[1]) if len(floor) > 1 else 0
        patch = int(floor[2]) if len(floor) > 2 else 0
        return (int(candidate[1]), int(candidate[2])) >= (minor, patch)
    return False


def run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    try:
        if kind == "validate_manifest":
            raw = case["raw"]
            return validate_manifest(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        if kind == "validate_index_entry":
            return validate_index_entry(case["entry"])
        if kind == "check_install":
            return check_install(
                case["entry"],
                case["fetched_manifest_sha256"],
                case.get("signer"),
                case.get("allow_signers"),
                case["host_python"],
                case["phi_excluded"],
                case["revoked"],
                case["manifest"],
            )
        if kind == "resolve":
            return resolve(case["uses"], case["ledger"])
    except CapabilityError as error:
        return {"code": error.code}
    raise ValueError(kind)
