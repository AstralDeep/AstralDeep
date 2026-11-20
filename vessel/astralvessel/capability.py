# Installs marketplace capabilities into a node's ledger and attaches them to agent packages,
# per the astral.capability/v1 contract: verify digest, signer, compatibility and revocation in
# that order, then resolve an agent's uses references to the highest installed satisfying version.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
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
        manifest = tomllib.loads(raw.decode("utf-8-sig"))
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


def _folder_version(registry_dir: Path, capability_id: str, constraint: str) -> Path:
    # A source registry is the folder layout itself: capabilities/<id>/<version>/capability.toml.
    versions = []
    for manifest_path in registry_dir.glob(f"capabilities/{capability_id}/*/capability.toml"):
        try:
            manifest = tomllib.loads(manifest_path.read_bytes().decode("utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError):
            continue
        version = str(manifest.get("capability", {}).get("version", ""))
        if _satisfies(version, constraint):
            versions.append((version, manifest_path.parent))
    if not versions:
        raise CapabilityError("capability.missing",
                              f"no published version of {capability_id} satisfies {constraint}")
    return max(versions, key=lambda item: tuple(int(part) for part in item[0].split(".")))[1]


def fetch_github(repo: str, cache_dir: str | Path, ref: str = "main") -> Path:
    # Pulls a capability repository from GitHub and returns the local source folder to install from.
    # repo is "owner/name". The pull is a shallow git clone, so private repositories work with the
    # credentials the machine already has and no token plumbing.
    repo = repo.removeprefix("https://github.com/").removeprefix("http://github.com/").strip("/")
    if repo.count("/") != 1:
        raise CapabilityError("capability.malformed", f"{repo!r} is not owner/name")
    import subprocess

    clone = Path(cache_dir) / repo.replace("/", "_")
    try:
        if (clone / "capabilities").exists():
            subprocess.run(["git", "-C", str(clone), "fetch", "--depth", "1", "origin", ref],
                           capture_output=True, text=True, timeout=300, check=True)
            subprocess.run(["git", "-C", str(clone), "reset", "--hard", "FETCH_HEAD"],
                           capture_output=True, text=True, timeout=120, check=True)
        else:
            shutil.rmtree(clone, ignore_errors=True)
            clone.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", "--branch", ref,
                            f"https://github.com/{repo}", str(clone)],
                           capture_output=True, text=True, timeout=600, check=True)
    except FileNotFoundError as exc:
        raise CapabilityError("capability.missing", "git is required to pull capabilities from GitHub") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-300:]
        raise CapabilityError("capability.missing",
                              f"the GitHub pull of {repo}@{ref} failed: {detail}") from exc
    if not (clone / "capabilities").exists():
        raise CapabilityError("capability.missing", f"{repo}@{ref} carries no capabilities folder")
    return clone


def list_available(source: str | Path) -> list[dict[str, Any]]:
    # What a picker shows: every capability version a source publishes.
    registry = Path(source)
    found: list[dict[str, Any]] = []
    for manifest_path in sorted(registry.glob("capabilities/*/*/capability.toml")):
        try:
            manifest = tomllib.loads(manifest_path.read_bytes().decode("utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError, OSError):
            continue
        cap = manifest.get("capability", {})
        if ID.fullmatch(str(cap.get("id", ""))) and SEMVER.fullmatch(str(cap.get("version", ""))):
            found.append({"id": cap["id"], "version": cap["version"], "kind": cap.get("kind"),
                          "description": cap.get("description", "")})
    return found


def _materialize_venv(payload: Path, wheels: list[str]) -> None:
    # The runtime layer on hosts without containers: one virtual environment per capability version.
    # uv is preferred (fast, no bundled pip needed); the stdlib venv plus pip is the fallback.
    import subprocess
    import venv as venv_module

    venv_dir = payload / ".venv"
    if venv_dir.exists():
        return
    python = payload / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = os.environ.get("VESSEL_UV") or shutil.which("uv")
    try:
        if uv:
            subprocess.run([str(uv), "venv", str(venv_dir), "--python", sys.executable],
                           capture_output=True, text=True, timeout=600, check=True)
            subprocess.run([str(uv), "pip", "install", "-p", str(python), *wheels],
                           capture_output=True, text=True, timeout=1800, check=True)
        else:
            venv_module.create(venv_dir, with_pip=True, clear=True)
            subprocess.run([str(python), "-m", "pip", "install", "--no-cache-dir", *wheels],
                           capture_output=True, text=True, timeout=1800, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-400:]
        shutil.rmtree(venv_dir, ignore_errors=True)
        raise CapabilityError("capability.incompatible", f"the kernel runtime did not install: {detail}") from exc


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


def install(source: str | Path, reference: str, ledger_path: str | Path, *, phi_excluded: bool = False,
            create_venv: bool = True, source_label: str | None = None) -> dict[str, Any]:
    # Installs one capability into the ledger from a capability repository checkout: validate the
    # manifest, copy the payload, and for kernels materialize the runtime venv. The ledger records
    # the source repository and commit as provenance; the folder layout is the catalog.
    if "@" not in reference:
        raise CapabilityError("capability.missing", f"{reference} carries no version constraint")
    capability_id, constraint = reference.split("@", 1)
    if capability_id == BASELINE:
        raise CapabilityError("capability.malformed", "the baseline capability is never installed")
    registry = Path(source)
    existing = load_ledger(ledger_path)
    version_dir = _folder_version(registry, capability_id, constraint)
    version = version_dir.name
    already = [e for e in existing if e.get("id") == capability_id and e.get("version") == version]
    if already and Path(already[0].get("path", "")).exists():
        return already[0]
    manifest = validate_manifest((version_dir / "capability.toml").read_bytes().replace(b"\xef\xbb\xbf", b""))
    if manifest["capability"]["id"] != capability_id or manifest["capability"]["version"] != version:
        raise CapabilityError("capability.malformed", "the manifest does not name its folder")
    if phi_excluded and manifest["permissions"]["phi"] == "reviewed":
        raise CapabilityError("capability.incompatible", "a reviewed-PHI capability is refused on a PHI-excluded host")
    destination = Path(ledger_path).parent / "capabilities" / capability_id / version
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(version_dir, destination,
                    ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    if manifest["capability"]["kind"] == "kernel" and create_venv:
        _materialize_venv(destination, manifest["runtime"]["wheels"])
    provenance = _git_commit(registry)
    record = {"id": capability_id, "version": version, "installed": date.today().isoformat(),
              "path": str(destination), "source": source_label or str(registry)}
    if provenance:
        record["commit"] = provenance
    save_ledger(ledger_path, existing + [record])
    return record


def _git_commit(registry: Path) -> str | None:
    # The checked-out commit is the integrity anchor of a git-registry install.
    import subprocess

    try:
        done = subprocess.run(["git", "-C", str(registry), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30, check=True)
        return done.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def sync(source: str | Path, ledger_path: str | Path, *, phi_excluded: bool = False,
         create_venv: bool = True, source_label: str | None = None) -> list[dict[str, Any]]:
    # A node activating anywhere pulls the owner's whole published set: every capability the source
    # offers, at its highest version, becomes installed; existing installs are kept, not downgraded.
    offers = list_available(source)
    if not offers:
        raise CapabilityError("capability.missing", f"{source} publishes no capabilities")
    wanted: dict[str, str] = {}
    for offer in offers:
        current = wanted.get(offer["id"])
        if current is None or tuple(int(part) for part in offer["version"].split(".")) > \
                tuple(int(part) for part in current.split(".")):
            wanted[offer["id"]] = offer["version"]
    installed = []
    for capability_id, version in sorted(wanted.items()):
        installed.append(install(source, f"{capability_id}@{version}", ledger_path,
                                 phi_excluded=phi_excluded, create_venv=create_venv,
                                 source_label=source_label))
    return installed


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


def attach(uses: list[str], ledger: list[dict[str, Any]], capabilities_root: Path) -> tuple[list[DeclaredTool], Mount]:
    # Returns the capability's tools as declared tools, plus the mount the agent's sandbox needs.
    # A kernel whose runtime venv was materialized runs with that interpreter; otherwise the tool
    # runs with the sandbox interpreter and {capabilities} resolves the payload path.
    attached_tools: list[DeclaredTool] = []
    for entry in resolve(uses, ledger):
        manifest = entry["manifest"]
        version_path = capabilities_root / entry["id"] / entry["version"]
        if not version_path.exists():
            raise CapabilityError("capability.missing", f"the installed payload of {entry['id']} is missing its folder")
        venv_python = version_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for tool in manifest["tools"]:
            script = "{capabilities}/" + "/".join([entry["id"], entry["version"], tool["script"]])
            if venv_python.exists():
                command = [str(venv_python), "-I", str(version_path / tool["script"])]
            else:
                command = ["python", "-I", script]
            schema_path = version_path / "tools" / f"{tool['name']}.schema.json"
            parameters = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() \
                else {"type": "object", "properties": {}}
            attached_tools.append(DeclaredTool(
                name=tool["name"], description=str(manifest["capability"]["description"]),
                command=command, parameters=parameters, scope=tool["scope"]))
    return attached_tools, Mount(source=str(capabilities_root), target="/capabilities")


def run_tool(ledger_path: Path, tool_name: str, script_file: Path, out_dir: Path) -> None:
    # Runs one installed capability tool on this host with the files landing in --out; this is the
    # direct local path, the same execution the node performs inside its sandbox during a turn.
    import subprocess

    installed = None
    for entry in load_ledger(ledger_path):
        manifest = _manifest_of(Path(entry["path"]))
        if any(tool["name"] == tool_name for tool in manifest["tools"]):
            if installed is None or tuple(int(part) for part in entry["version"].split(".")) > \
                    tuple(int(part) for part in installed["version"].split(".")):
                installed = {**entry, "manifest": manifest}
    if installed is None:
        raise CapabilityError("capability.missing", f"no installed capability offers the tool {tool_name}")
    manifest = installed["manifest"]
    tool = next(t for t in manifest["tools"] if t["name"] == tool_name)
    payload = Path(installed["path"])
    interpreter = payload / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.exists():
        interpreter = Path(sys.executable)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VESSEL_")}
    env["VESSEL_OUTBOX"] = str(out_dir)
    done = subprocess.run([str(interpreter), "-I", str(payload / tool["script"])],
                          input=json.dumps({"script": script_file.read_text(encoding="utf-8-sig")}).encode("utf-8"),
                          env=env, capture_output=True, timeout=1800)
    sys.stdout.write(done.stdout.decode("utf-8", "replace"))
    sys.stderr.write(done.stderr.decode("utf-8", "replace"))
    if done.returncode != 0:
        raise SystemExit(done.returncode)
    for produced in sorted(out_dir.iterdir()):
        print(f"wrote {produced}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astralvessel.capability",
                                     description="install marketplace capabilities")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", help="install a capability into the ledger")
    install_parser.add_argument("reference", help="capability id with a version constraint")
    install_parser.add_argument("--from", dest="source", required=True,
                                help="a capability folder, a registry checkout, or gh:owner/repo[@ref] on GitHub")
    install_parser.add_argument("--ledger", required=True, help="the node's capability ledger file")
    install_parser.add_argument("--phi-excluded", action="store_true", help="refuse reviewed-PHI capabilities")
    install_parser.add_argument("--no-venv", action="store_true",
                                 help="skip the kernel runtime venv (container hosts build it into the image)")
    list_parser = sub.add_parser("list", help="list the capability versions a source publishes")
    list_parser.add_argument("--from", dest="source", required=True,
                             help="a capability folder, a registry checkout, or gh:owner/repo[@ref] on GitHub")
    sync_parser = sub.add_parser("sync", help="install every capability the source publishes")
    sync_parser.add_argument("--from", dest="source", required=True,
                             help="a capability folder, a registry checkout, or gh:owner/repo[@ref] on GitHub")
    sync_parser.add_argument("--ledger", required=True, help="the node's capability ledger file")
    sync_parser.add_argument("--phi-excluded", action="store_true", help="refuse reviewed-PHI capabilities")
    sync_parser.add_argument("--no-venv", action="store_true",
                             help="skip the kernel runtime venv (container hosts build it into the image)")
    run_parser = sub.add_parser("run", help="run one installed capability tool locally and keep its files")
    run_parser.add_argument("tool", help="the tool name, like cad")
    run_parser.add_argument("--ledger", required=True, help="the node's capability ledger file")
    run_parser.add_argument("--script", required=True, help="a file with the tool's input script")
    run_parser.add_argument("--out", required=True, help="the folder that receives the produced files")
    args = parser.parse_args(argv)
    ledger = Path(args.ledger).resolve() if args.command in ("install", "sync", "run") else None
    try:
        if args.command == "run":
            run_tool(ledger, args.tool, Path(args.script), Path(args.out))
            return 0
        source = args.source
        label = source
        if source.startswith("gh:") or source.startswith("github:"):
            repo, _, ref = source.split(":", 1)[1].partition("@")
            label = f"github:{repo}@{ref or 'main'}"
            cache = ledger.parent / "registry-cache" if ledger \
                else Path(tempfile.gettempdir()) / "astral-capabilities-cache"
            source = fetch_github(repo, cache, ref or "main")
        if args.command == "list":
            for item in list_available(source):
                print(f"{item['id']}@{item['version']}  {item['kind']:6}  {item['description']}")
            return 0
        if args.command == "sync":
            for record in sync(source, ledger, phi_excluded=args.phi_excluded,
                               create_venv=not args.no_venv, source_label=label):
                print(f"installed {record['id']} {record['version']}")
            return 0
        record = install(source, args.reference, ledger, phi_excluded=args.phi_excluded,
                         create_venv=not args.no_venv, source_label=label)
    except CapabilityError as refused:
        print(f"{refused.code}: {refused.reason}", file=sys.stderr)
        return 1
    print(f"installed {record['id']} {record['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
