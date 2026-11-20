# The capability marketplace: install verification against the contract order, ledger resolution,
# attach into a deployment, and a tool-kind capability running end to end through the sandbox.

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


def write_registry(root, *, manifest: str, version: str = "1.0.0", signer: str | None = "dev",
                   revoked: bool = False) -> None:
    version_dir = root / "capabilities" / "astral.demo" / version
    (version_dir / "tools").mkdir(parents=True)
    (version_dir / "capability.toml").write_text(manifest.replace("1.0.0", version), encoding="utf-8")
    (version_dir / "tools" / "demo_run.py").write_text(
        "import json, os, sys\nargs = json.loads(sys.stdin.read() or '{}')\nprint('wrote hello')\n"
        "out = os.environ.get('VESSEL_OUTBOX')\n"
        "if out:\n    open(os.path.join(out, 'hello.csv'), 'w').write('a,b\\n1,2\\n')\n",
        encoding="utf-8")
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


MANIFEST = """
[capability]
id = "astral.demo"
version = "1.0.0"
kind = "tool"
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

KERNEL_MANIFEST = MANIFEST.replace('kind = "tool"', 'kind = "kernel"').replace(
    'scope = "tools:files"', 'scope = "tools:execute"').replace(
    'scopes = ["tools:files"]', 'scopes = ["tools:execute"]').replace(
    'output_files = ["csv"]', 'output_files = ["stl"]') + (
    '\n[runtime]\npython = "3.11"\nwheels = ["build123d==0.13.0"]\n')


def ledger_of(tmp_path) -> object:
    return tmp_path / "deploy" / "capabilities-ledger.json"


def test_install_verifies_in_the_contract_order(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST)
    ledger = ledger_of(tmp_path)
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger, allow_signers=None, host_python="3.11",
                           phi_excluded=False)
    assert refused.value.code == "capability.signer"
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger, allow_signers=["other"], host_python="3.11",
                           phi_excluded=False)
    assert refused.value.code == "capability.signer"
    record = capability.install(registry, "astral.demo@^1.0", ledger, allow_signers=["dev"], host_python="3.11",
                                phi_excluded=False)
    assert record["version"] == "1.0.0"
    assert (tmp_path / "deploy" / "capabilities" / "astral.demo" / "1.0.0" / "capability.toml").exists()


def test_install_refuses_a_tampered_package(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST)
    tarball = registry / "capabilities" / "astral.demo" / "1.0.0" / "astral.demo-1.0.0.tar.gz"
    tarball.write_bytes(tarball.read_bytes() + b"tampered")
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), allow_signers=["dev"],
                           host_python="3.11", phi_excluded=False)
    assert refused.value.code == "capability.digest"


def test_install_refuses_revoked_last(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST, revoked=True)
    with pytest.raises(capability.CapabilityError) as refused:
        capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), allow_signers=["dev"],
                           host_python="3.11", phi_excluded=False)
    assert refused.value.code == "capability.revoked"


def test_resolution_picks_the_highest_installed(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST, version="1.2.0")
    capability.install(registry, "astral.demo@^1.0", ledger_of(tmp_path), allow_signers=["dev"],
                       host_python="3.11", phi_excluded=False)
    ledger = capability.load_ledger(ledger_of(tmp_path))
    resolved = capability.resolve(["astral.demo@^1.0"], ledger)
    assert resolved[0]["version"] == "1.2.0"


async def test_a_tool_capability_runs_end_to_end(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST)
    ledger_path = ledger_of(tmp_path)
    capability.install(registry, "astral.demo@^1.0", ledger_path, allow_signers=["dev"], host_python="3.11",
                       phi_excluded=False)
    ledger = capability.load_ledger(ledger_path)
    tools, mount = capability.attach(["astral.demo@^1.0"], ledger, tmp_path / "deploy" / "capabilities",
                                     dockerfile_text=None)
    assert mount.target == "/capabilities" and tools[0].name == "demo"
    assert "{capabilities}/astral.demo/" in tools[0].command[2]
    package_dir = tmp_path / "agent"
    (package_dir / "tools").mkdir(parents=True)
    (package_dir / "vessel.toml").write_text(
        '[agent]\nname = "demo-agent"\ndescription = "uses the demo capability"\n\n'
        '[tools]\nbuiltin = ["list"]\ndeclared = []\n\n'
        "[capabilities]\nuses = [\"astral.demo@^1.0\"]\n", encoding="utf-8")
    package = load_package(package_dir)
    ledger = capability.load_ledger(ledger_path)
    tools, mount = capability.attach(package.manifest.capabilities.uses, ledger,
                                     tmp_path / "deploy" / "capabilities", dockerfile_text=None)
    package.declared.extend(tools)
    monkeypatch.setenv("VESSEL_WORKDIR", str(tmp_path))
    runner = LocalRunner(package, capabilities_root=str(tmp_path / "deploy" / "capabilities"))
    out, failed = await runner.run("demo", {}, workspace=None)
    assert not failed and "wrote hello" in out, out


def test_the_payload_writes_into_the_outbox(tmp_path):
    # The unconstrained dev runner has no outbox, so the payload contract is verified directly here;
    # the confined collection runs in the Landlock CI lane.
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST)
    ledger_path = ledger_of(tmp_path)
    record = capability.install(registry, "astral.demo@^1.0", ledger_path, allow_signers=["dev"],
                                host_python="3.11", phi_excluded=False)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    env = {**os.environ, "VESSEL_OUTBOX": str(outbox), "PYTHONDONTWRITEBYTECODE": "1"}
    script = Path(record["path"]) / "tools" / "demo_run.py"
    done = subprocess.run([sys.executable, str(script)], input=b"{}", env=env, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()
    assert (outbox / "hello.csv").read_text() == "a,b\n1,2\n"


def test_kernel_capability_needs_the_wheels_in_the_agent_image(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=KERNEL_MANIFEST)
    ledger_path = ledger_of(tmp_path)
    capability.install(registry, "astral.demo@^1.0", ledger_path, allow_signers=["dev"], host_python="3.11",
                       phi_excluded=False)
    ledger = capability.load_ledger(ledger_path)
    with pytest.raises(capability.CapabilityError) as refused:
        capability.attach(["astral.demo@^1.0"], ledger, tmp_path / "deploy" / "capabilities", dockerfile_text=None)
    assert refused.value.code == "capability.incompatible"
    tools, _ = capability.attach(["astral.demo@^1.0"], ledger, tmp_path / "deploy" / "capabilities",
                                 dockerfile_text="FROM astralvessel\nRUN pip install build123d==0.13.0\n")
    assert tools[0].name == "demo"


def test_a_deployment_attaches_the_capability_tools(tmp_path):
    registry = tmp_path / "registry"
    write_registry(registry, manifest=MANIFEST)
    ledger_path = ledger_of(tmp_path)
    capability.install(registry, "astral.demo@^1.0", ledger_path, allow_signers=["dev"], host_python="3.11",
                       phi_excluded=False)
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
