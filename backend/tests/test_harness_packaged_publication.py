"""A real isolated package mirror can carry externally authenticated record data without self-hashing it.
Synthetic mirror records exercise publication shape only and never qualify production or invoke a provider.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from orchestrator.harness import production_profiles as module
from tests.test_harness_packaged_artifacts import manifest_fact, record


def publish(mirror, value):
    target = mirror / "orchestrator/harness/deployment_allowlist.py"
    target.write_text('"""Synthetic isolated publication fixture, never a production qualification record."""\n'
                      + "QUALIFIED_DEPLOYMENTS = (" + repr(value) + ",)\n")


def inspect_package(mirror):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
    env["PYTHONPATH"] = str(mirror) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-B", "-c",
        "from orchestrator.harness.production_profiles import deployment_for; "
        "value=deployment_for('standard_local'); print(value.identity.artifact_digest)"],
        cwd=mirror, env=env, text=True, capture_output=True, timeout=15)


def test_actual_record_file_is_publishable_and_exact_implementation_tamper_is_denied(tmp_path):
    original = Path(module.__file__).resolve().parents[2]
    mirror = tmp_path / "backend"
    shutil.copytree(original, mirror, ignore=shutil.ignore_patterns("tests", "data", "tmp", "knowledge",
        "__pycache__", ".env", "*.pem", "*.key"))
    config = mirror.parent / "config/astral-composition.json"
    config.parent.mkdir()
    shutil.copyfile(original.parent / "config/astral-composition.json", config)
    entries = tuple((name, hashlib.sha256((mirror.parent / module._implementation_path(name).relative_to(
                        original.parent)).read_bytes()).hexdigest())
                    for name in sorted(module._REQUIRED_IMPLEMENTATION))
    artifact = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
    value = record(manifest_fact())
    value["implementation"] = entries
    value["identity"]["artifact_digest"] = artifact
    publish(mirror, value)
    result = inspect_package(mirror)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == artifact
    for metadata in (config, mirror / "requirements.txt"):
        original_bytes = metadata.read_bytes()
        metadata.write_bytes(original_bytes + b"\n")
        changed = inspect_package(mirror)
        assert changed.returncode != 0 and "harness_artifact_unavailable" in changed.stderr
        metadata.write_bytes(original_bytes)
    implementation = mirror / "orchestrator/harness/production_profiles.py"
    implementation.write_text(implementation.read_text() + "\n")
    changed = inspect_package(mirror)
    assert changed.returncode != 0 and "harness_artifact_unavailable" in changed.stderr
    assert module.public_profiles()[0]["available"] is False
