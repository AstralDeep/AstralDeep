# Installs marketplace capabilities into a node's ledger and attaches them to agent packages,
# per the astral.capability/v1 contract: verify digest, signer, compatibility and revocation in
# that order, then resolve an agent's uses references to the highest installed satisfying version.

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from .manifest import DeclaredTool, Mount

LEDGER_FORMAT = "astral.capability-ledger/v1"
INDEX_FORMAT = "astral.capability-index/v1"
ID = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}\Z")
SEMVER = re.compile(r"\d+\.\d+\.\d+\Z")
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
BASELINE = "astral.core/v1"


class CapabilityError(Exception):
    def __init__(self, code: str, reason: str = ""):
        super().__init__(reason or code)
        self.code = code
        self.reason = reason or code


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(raw: bytes) -> dict[str, Any]:
    # The manifest rules of astral.capability/v1, section 1; unknown tables or keys are malformed.
    try:
        manifest = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CapabilityError("capability.malformed", f"the manifest does not parse: {exc}") from exc
    cap = manifest.get("capability")
    if not isinstance(cap, dict) or not ID.fullmatch(str(cap.get("id", ""))):
        raise CapabilityError("capability.malformed", "capability.id is missing or malformed")
    if not SEMVER.fullmatch(str(cap.get("version", ""))):
        raise CapabilityError("capability.malformed", "capability.version must be MAJOR.MINOR.PATCH")
    if cap.get("kind") not in ("tool", "kernel"):
        raise CapabilityError("capability.malformed", "capability.kind must be tool or kernel")
    if not isinstance(cap.get("description"), str) or not 1 <= len(cap["description"]) <= 200:
        raise CapabilityError("capability.malformed", "capability.description must be 1-200 characters")
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict):
        raise CapabilityError("capability.malformed", "the permissions table is missing")
    scopes = permissions.get("scopes")
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 8 or \
            any(not isinstance(s, str) or not s for s in scopes):
        raise CapabilityError("capability.malformed", "permissions.scopes must be 1-8 scope strings")
    output_files = permissions.get("output_files", [])
    if not isinstance(output_files, list) or len(output_files) > 16 or any(
        not isinstance(ext, str) or not re.fullmatch(r"[a-z0-9]{1,16}", ext) for ext in output_files
    ):
        raise CapabilityError("capability.malformed", "permissions.output_files must be short lowercase extensions")
    if permissions.get("network") not in ("none", "egress-proxy"):
        raise CapabilityError("capability.malformed", "permissions.network must be none or egress-proxy")
    if permissions.get("phi") not in ("none", "reviewed"):
        raise CapabilityError("capability.malformed", "permissions.phi must be none or reviewed")
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or isinstance(limits.get("tool_timeout_seconds"), bool) or \
            not isinstance(limits.get("tool_timeout_seconds"), int) or limits["tool_timeout_seconds"] <= 0:
        raise CapabilityError("capability.malformed", "limits.tool_timeout_seconds must be a positive integer")
    tools = manifest.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= 32:
        raise CapabilityError("capability.malformed", "at least one and at most 32 tools are required")
    for tool in tools:
        if not isinstance(tool, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(tool.get("name", ""))):
            raise CapabilityError("capability.malformed", "a tool name is malformed")
        if not re.fullmatch(r"tools/[A-Za-z0-9._/-]{1,120}", str(tool.get("script", ""))):
            raise CapabilityError("capability.malformed", f"tool {tool.get('name')} has a malformed script path")
        if tool.get("scope") not in scopes:
            raise CapabilityError("capability.malformed", f"tool {tool.get('name')} carries an undeclared scope")
    runtime = manifest.get("runtime")
    if cap["kind"] == "kernel":
        if not isinstance(runtime, dict) or not re.fullmatch(r"\d+\.\d+", str(runtime.get("python", ""))):
            raise CapabilityError("capability.malformed", "a kernel declares runtime.python as major.minor")
        wheels = runtime.get("wheels")
        if not isinstance(wheels, list) or not 1 <= len(wheels) <= 64 or any(
            not isinstance(w, str) or "==" not in w for w in wheels
        ):
            raise CapabilityError("capability.malformed", "runtime.wheels must be pinned name==version specifiers")
    elif runtime is not None:
        raise CapabilityError("capability.malformed", "only a kernel declares a runtime")
    return manifest


def load_ledger(path: str | Path) -> list[dict[str, Any]]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    if document.get("format") != LEDGER_FORMAT or not isinstance(document.get("installed"), list):
        raise CapabilityError("capability.malformed", f"{ledger_path} is not a {LEDGER_FORMAT} ledger")
    return document["installed"]


def save_ledger(path: str | Path, installed: list[dict[str, Any]]) -> None:
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    body = {"format": LEDGER_FORMAT, "installed": installed}
    tmp = ledger_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ledger_path)


def _satisfies(version: str, constraint: str) -> bool:
    if SEMVER.fullmatch(constraint):
        return version == constraint
    if not constraint.startswith("^"):
        return False
    floor = constraint[1:].split(".")
    candidate = version.split(".")
    if not SEMVER.fullmatch(version) or candidate[0] != floor[0]:
        return False
    minor = int(floor[1]) if len(floor) > 1 else 0
    patch = int(floor[2]) if len(floor) > 2 else 0
    return (int(candidate[1]), int(candidate[2])) >= (minor, patch)


def _highest(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return max(entries, key=lambda entry: tuple(int(part) for part in str(entry["version"]).split(".")))


def _index_entry(registry_dir: Path, capability_id: str, constraint: str) -> dict[str, Any]:
    index_path = registry_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("format") != INDEX_FORMAT or not isinstance(index.get("entries"), list):
        raise CapabilityError("capability.malformed", f"{index_path} is not a {INDEX_FORMAT} index")
    published = [e for e in index["entries"] if e.get("id") == capability_id
                 and _satisfies(str(e.get("version", "")), constraint)]
    if not published:
        raise CapabilityError("capability.missing",
                              f"no installed registry version of {capability_id} satisfies {constraint}")
    return _highest(published)


def _extract_package(archive: Path, destination: Path) -> None:
    # Members must be plain files inside the destination; a link or an escape refuses the install.
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and not target.is_relative_to(root):
                raise CapabilityError("capability.malformed", f"the package escapes its folder: {member.name}")
            if member.issym() or member.islnk() or not member.isfile():
                raise CapabilityError("capability.malformed", f"the package carries a non-file entry: {member.name}")
            total += member.size
            if total > MAX_PACKAGE_BYTES:
                raise CapabilityError("capability.malformed", "the package is larger than 64 MB")
        for member in members:
            extracted = tar.extractfile(member)
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                shutil.copyfileobj(extracted, handle)


def install(registry_dir: str | Path, reference: str, ledger_path: str | Path, *, allow_signers: list[str] | None,
            host_python: str, phi_excluded: bool) -> dict[str, Any]:
    # The contract's verification order: digest, signer, compatibility, revocation; then materialize and record.
    if "@" not in reference:
        raise CapabilityError("capability.missing", f"{reference} carries no version constraint")
    capability_id, constraint = reference.split("@", 1)
    if capability_id == BASELINE:
        raise CapabilityError("capability.malformed", "the baseline capability is never installed")
    entry = _index_entry(Path(registry_dir), capability_id, constraint)
    existing = load_ledger(ledger_path)
    already = [e for e in existing if e.get("id") == capability_id and e.get("version") == entry.get("version")]
    if already and Path(already[0].get("path", "")).exists():
        return already[0]
    version = str(entry.get("version"))
    package_path = Path(registry_dir) / "capabilities" / capability_id / version / f"{capability_id}-{version}.tar.gz"
    raw_package = package_path.read_bytes()
    if _sha256(raw_package) != entry.get("package_sha256"):
        raise CapabilityError("capability.digest", "the package does not match the index digest")
    with tarfile.open(package_path, "r:gz") as tar:
        manifest_member = next((m for m in tar.getmembers() if m.name.endswith("capability.toml") and m.isfile()), None)
        if manifest_member is None:
            raise CapabilityError("capability.malformed", "the package carries no capability.toml")
        manifest_raw = tar.extractfile(manifest_member).read()
    manifest = validate_manifest(manifest_raw)
    if _sha256(manifest_raw) != entry.get("manifest_sha256"):
        raise CapabilityError("capability.digest", "the manifest does not match the index digest")
    if manifest["capability"]["id"] != capability_id or manifest["capability"]["version"] != entry.get("version"):
        raise CapabilityError("capability.malformed", "the manifest does not name the index entry")
    signer = entry.get("signer")
    if signer is None or not allow_signers or signer not in allow_signers:
        raise CapabilityError("capability.signer", f"the signer {signer!r} is not in the node's allow-signers policy")
    if manifest["capability"]["kind"] == "kernel":
        required = manifest["runtime"]["python"]
        if host_python != required and not host_python.startswith(required + "."):
            raise CapabilityError("capability.incompatible",
                                  f"the kernel needs python {required}; the host provides {host_python}")
    if phi_excluded and manifest["permissions"]["phi"] == "reviewed":
        raise CapabilityError("capability.incompatible", "a reviewed-PHI capability is refused on a PHI-excluded host")
    if entry.get("revoked"):
        raise CapabilityError("capability.revoked", str(entry.get("revoked_reason") or "the entry is revoked"))
    destination = Path(ledger_path).parent / "capabilities" / capability_id / str(entry["version"])
    if destination.exists():
        shutil.rmtree(destination)
    _extract_package(package_path, destination)
    record = {"id": capability_id, "version": entry["version"], "installed": date.today().isoformat(),
              "path": str(destination), "manifest_sha256": entry["manifest_sha256"]}
    save_ledger(ledger_path, existing + [record])
    return record


def resolve(uses: list[str], ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Highest satisfying installed version per reference; a missing one refuses the agent's build.
    resolved = []
    for reference in uses:
        if "@" not in reference:
            raise CapabilityError("capability.missing", f"{reference} carries no version constraint")
        capability_id, constraint = reference.split("@", 1)
        matches = [e for e in ledger if e.get("id") == capability_id
                   and _satisfies(str(e.get("version", "")), constraint)]
        if not matches:
            raise CapabilityError("capability.missing",
                                  f"no installed version of {capability_id} satisfies {constraint}")
        best = _highest(matches)
        best = {**best, "manifest": _manifest_of(Path(best["path"]))}
        resolved.append(best)
    return resolved


def _manifest_of(installed_path: Path) -> dict[str, Any]:
    raw = (installed_path / "capability.toml").read_bytes()
    return validate_manifest(raw)


def attach(uses: list[str], ledger: list[dict[str, Any]], capabilities_root: Path,
           dockerfile_text: str | None) -> tuple[list[DeclaredTool], Mount]:
    # Returns the capability's tools as declared tools with {capabilities} commands, plus the mount
    # the agent's sandbox needs; a kernel whose wheels the agent image does not install is refused.
    attached_tools: list[DeclaredTool] = []
    for entry in resolve(uses, ledger):
        manifest = entry["manifest"]
        version_path = capabilities_root / entry["id"] / entry["version"]
        if not version_path.exists():
            raise CapabilityError("capability.missing", f"the installed payload of {entry['id']} is missing its folder")
        if manifest["capability"]["kind"] == "kernel":
            wheels = manifest["runtime"]["wheels"]
            if dockerfile_text is None:
                raise CapabilityError("capability.incompatible",
                                      f"{entry['id']} needs a sandbox dockerfile that installs {', '.join(wheels)}")
            for wheel in wheels:
                if wheel not in dockerfile_text:
                    raise CapabilityError("capability.incompatible",
                                          f"the sandbox dockerfile does not install {wheel}")
        for tool in manifest["tools"]:
            script = "/".join([entry["id"], entry["version"], tool["script"]])
            schema_path = version_path / "tools" / f"{tool['name']}.schema.json"
            parameters = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() \
                else {"type": "object", "properties": {}}
            attached_tools.append(DeclaredTool(
                name=tool["name"], description=str(manifest["capability"]["description"]),
                command=["python", "-I", "{capabilities}/" + script],
                parameters=parameters,
                scope=tool["scope"]))
    return attached_tools, Mount(source=str(capabilities_root), target="/capabilities")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astralvessel.capability",
                                     description="install marketplace capabilities")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", help="verify and install a capability into the ledger")
    install_parser.add_argument("reference", help="capability id with a version constraint")
    install_parser.add_argument("--registry", required=True, help="the registry checkout holding index.json")
    install_parser.add_argument("--ledger", required=True, help="the node's capability ledger file")
    install_parser.add_argument("--allow-signers", default="",
                                help="comma-separated signer identities; empty refuses every install")
    install_parser.add_argument("--host-python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    install_parser.add_argument("--phi-excluded", action="store_true", help="refuse reviewed-PHI capabilities")
    args = parser.parse_args(argv)
    allow = [part.strip() for part in args.allow_signers.split(",") if part.strip()] or None
    try:
        record = install(args.registry, args.reference, args.ledger, allow_signers=allow,
                         host_python=args.host_python, phi_excluded=args.phi_excluded)
    except CapabilityError as refused:
        print(f"{refused.code}: {refused.reason}", file=sys.stderr)
        return 1
    print(f"installed {record['id']} {record['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
