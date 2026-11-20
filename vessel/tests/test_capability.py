# The capability marketplace: folder-model install (the GitHub pull lands in the same layout),
# the optional index model for tarball registries, resolution, attach, and a tool capability
# running end to end through the sandbox.

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from astralvessel import capability
from astralvessel.deployment import load_deployment
from astralvessel.manifest import load_package
from astralvessel.sandbox import LocalRunner

KEY = "c" * 32

MANIFEST = """
[capability]
id = "astral.demo"
version = "{version}"
kind = "{kind}"
description = "A demo capability writing one csv file."

[permissions]
scopes = ["tools:files"]
output_files = ["csv"]
network = "none"
phi = "none"

[limits]
tool_timeout_seconds = 30

[[tools]]
name = "demo"
script = "tools/demo_run.py"
scope = "tools:files"
"""

TOOL_SCRIPT = ("import json, os, sys\nargs = json.loads(sys.stdin.read() or '{}')\nprint('wrote hello')\n"
               "out = os.environ.get('VESSEL_OUTBOX')\n"
               "if out:\n    open(os.path.join(out, 'hello.csv'), 'w').write('a,b\\n1,2\\n')\n")


def write_folder_registry(root: Path, version: str = "1.0.0", kind: str = "tool",
                          wheels: list[str] | None = None) -> Path:
    version_dir = root / "capabilities" / "astral.demo" / version
    (version_dir / "tools").mkdir(parents=True)
    body = MANIFEST.replace("{version}", version).replace("{kind}", kind)
    if wheels:
        body += '\n[runtime]\npython = "3.11"\nwheels = ["' + '", "'.join(wheels) + '"]\n'
    (version_dir / "capability.toml").write_text(body, encoding="utf-8")
    (version_dir / "tools" / "demo_run.py").write_text(TOOL_SCRIPT, encoding="utf-8")
    return root


def write_index_registry(root: Path, *, manifest: str, version: str = "1.0.0", signer: str | None = "dev",
                         revoked: bool = False) -> None:
    version_dir = root / "capabilities" / "astral.demo" / version
    (version_dir / "tools").mkdir(parents=True, exist_ok=True)
    (version_dir / "capability.toml").write_text(manifest.replace("1.0.0", version), encoding="utf-8")
    (version_dir / "tools" / "demo_run.py").write_text(TOOL_SCRIPT, encoding="utf-8")
    tar_path = version_dir / f"astral.demo-{version}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(version_dir / "capability.toml", arcname="capability.toml")
        tar.add(version_dir / "tools" / "demo_run.py", arcname="tools/demo_run.py")
    entry = {"id": "astral.demo", "version": version, "kind": "tool", "tools": ["demo"],
             "manifest_sha256": capability._sha256((version_dir / "capability.toml").read_bytes()),
             "package_sha256": capability._sha256(tar_path.read_bytes()), "signer": signer,
             "published": "2026-11-20"}
    if revoked:
        entry.update({"revoked": True, "revoked_reason": "superseded"})
    index_path = root / "index.json"
    index = {"format": capability.INDEX_FORMAT, "entries": []}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    index["entries"] = [e for e in index["entries"] if e["id"] != "astral.demo"] + [entry]
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def ledger_of(tmp_path) -> Path:
    return tmp_path / "deploy" / "capabilities-ledger.json"


def attach_into(tmp_path: Path, package_dir: Path):
    package = load_package(package_dir)
    ledger = capability.load_ledger(ledger_of(tmp_path))
    tools, mount = capability.attach(package.manifest.capabilities.uses, ledger,
                                     tmp_path / "deploy" / "capabilities")
    package.declared.extend(tools)
    return package, mount


def test_a_folder_registry_installs_without_a_ceremony(tmp_path):
    registry = write_folder_registry(tmp_path / "registry")
    record = capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), phi_excluded=False,
                                create_venv=False, source_label="folder-test")
    assert record["version"] == "1.0.0" and record["source"] == "folder-test"
    payload = tmp_path / "deploy" / "capabilities" / "astral.demo" / "1.0.0"
    assert (payload / "capability.toml").exists() and (payload / "tools" / "demo_run.py").exists()
    ledger = capability.load_ledger(ledger_of(tmp_path))
    assert ledger[0]["source"] == "folder-test"


def test_an_index_registry_still_verifies_digests_and_revocation(tmp_path):
    registry = tmp_path / "registry"
    write_index_registry(registry, manifest=MANIFEST.replace("{version}", "1.0.0").replace("{kind}", "tool"),
                         revoked=True)
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    assert refused.value.code == "capability.revoked"
    write_index_registry(registry, manifest=MANIFEST.replace("{version}", "1.0.0").replace("{kind}", "tool"))
    tarball = registry / "capabilities" / "astral.demo" / "1.0.0" / "astral.demo-1.0.0.tar.gz"
    tampered = tarball.read_bytes() + b"tampered"
    tarball.write_bytes(tampered)
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    assert refused.value.code == "capability.digest"


def test_resolution_picks_the_highest_installed(tmp_path):
    registry = write_folder_registry(tmp_path / "registry", version="1.2.0")
    capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    ledger = capability.load_ledger(ledger_of(tmp_path))
    resolved = capability.resolve(["astral.demo@^1.0"], ledger)
    assert resolved[0]["version"] == "1.2.0"
    with pytest.raises(capability.CapabilityError) as refused:
        capability.resolve(["astral.demo@^2.0"], ledger)
    assert refused.value.code == "capability.missing"


async def test_a_tool_capability_runs_end_to_end(tmp_path, monkeypatch):
    registry = write_folder_registry(tmp_path / "registry")
    capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    package_dir = tmp_path / "agent"
    (package_dir / "tools").mkdir(parents=True)
    (package_dir / "vessel.toml").write_text(
        '[agent]\nname = "demo-agent"\ndescription = "uses the demo capability"\n\n'
        '[tools]\nbuiltin = ["list"]\ndeclared = []\n\n'
        "[capabilities]\nuses = [\"astral.demo@^1.0\"]\n", encoding="utf-8")
    package, mount = attach_into(tmp_path, package_dir)
    assert mount.target == "/capabilities" and [t.name for t in package.declared] == ["demo"]
    monkeypatch.setenv("VESSEL_WORKDIR", str(tmp_path))
    runner = LocalRunner(package, capabilities_root=str(tmp_path / "deploy" / "capabilities"))
    out, failed = await runner.run("demo", {}, workspace=None)
    assert not failed and "wrote hello" in out, out


def test_the_payload_writes_into_the_outbox(tmp_path):
    registry = write_folder_registry(tmp_path / "registry")
    record = capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    env = {**os.environ, "VESSEL_OUTBOX": str(outbox), "PYTHONDONTWRITEBYTECODE": "1"}
    script = Path(record["path"]) / "tools" / "demo_run.py"
    done = subprocess.run([sys.executable, str(script)], input=b"{}", env=env, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()
    assert (outbox / "hello.csv").read_text() == "a,b\n1,2\n"


def test_a_kernel_capability_materializes_its_runtime_venv(tmp_path):
    registry = write_folder_registry(tmp_path / "registry", kind="kernel", wheels=["six==1.16.0"])
    record = capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=True)
    venv_python = Path(record["path"]) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert venv_python.exists()
    tools, _ = capability.attach(["astral.demo@^1.0"], capability.load_ledger(ledger_of(tmp_path)),
                                 tmp_path / "deploy" / "capabilities")
    assert tools[0].command[0] == str(venv_python)


def test_a_kernel_without_a_venv_falls_back_to_the_sandbox_interpreter(tmp_path):
    registry = write_folder_registry(tmp_path / "registry", kind="kernel", wheels=["six==1.16.0"])
    capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    tools, _ = capability.attach(["astral.demo@^1.0"], capability.load_ledger(ledger_of(tmp_path)),
                                 tmp_path / "deploy" / "capabilities")
    assert tools[0].command[0] == "python" and tools[0].command[2].startswith("{capabilities}/")


def test_a_deployment_attaches_the_capability_tools(tmp_path):
    registry = write_folder_registry(tmp_path / "registry")
    capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), create_venv=False)
    deploy = tmp_path / "deploy"
    package = deploy / "agents" / "demo-agent"
    (package / "tools").mkdir(parents=True)
    (package / "vessel.toml").write_text(
        '[agent]\nname = "demo-agent"\ndescription = "uses the demo capability"\n\n'
        '[tools]\nbuiltin = ["list"]\ndeclared = []\n\n'
        "[capabilities]\nuses = [\"astral.demo@^1.0\"]\n", encoding="utf-8")
    (deploy / "deployment.toml").write_text(
        '[[agents]]\nid = "demo-agent"\npackage = "agents/demo-agent"\n', encoding="utf-8")
    deployment = load_deployment(deploy / "deployment.toml")
    agent = deployment.agents["demo-agent"]
    assert [t.name for t in agent.package.declared] == ["demo"]
    assert any(m.target == "/capabilities" for m in agent.package.manifest.sandbox.mounts)
    before = agent.fingerprint
    agent.capabilities.append({"id": "astral.demo", "version": "9.9.9"})
    assert agent.fingerprint != before


def test_list_available_reads_what_a_source_publishes(tmp_path):
    registry = write_folder_registry(tmp_path / "registry", version="1.0.0")
    available = capability.list_available(registry)
    assert available == [{"id": "astral.demo", "version": "1.0.0", "kind": "tool",
                          "description": "A demo capability writing one csv file."}]
