"""Feature 065 frozen-Windows media closure and offline manifest evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "AstralDeep.spec"
INPUT = ROOT / "requirements.in"
LOCK = ROOT / "requirements-release.lock.txt"
MANIFEST = ROOT / "deployment" / "runtime-manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_spec_collects_qtmultimedia_and_livekit_native_closure() -> None:
    source = SPEC.read_text(encoding="utf-8")

    assert '"PySide6.QtMultimedia"' in source
    assert 'collect_submodules("livekit.rtc")' in source
    assert 'collect_dynamic_libs("livekit")' in source
    assert 'collect_data_files("livekit", include_py_files=False)' in source
    excludes = source[source.index("excludes = [") : source.index("a = Analysis(")]
    assert "PySide6.QtMultimedia" not in excludes


def test_runtime_manifest_offline_evidence_binds_livekit_release_lock() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    lock = LOCK.read_text(encoding="utf-8")

    assert "livekit==1.1.14" in INPUT.read_text(encoding="utf-8")
    assert "livekit==1.1.14" in lock
    assert "b8f8d38f131956297923e520bc4375bc9ebfa255cab7f125cb7755bfca71df24" in lock
    assert manifest["requirements_input_sha256"] == _sha256(INPUT)
    assert manifest["requirements_lock_sha256"] == _sha256(LOCK)
    assert manifest["required_runtime_lock_sha256"] == _sha256(LOCK)
    assert manifest["target_platform"] == "win_amd64"
    assert manifest["python_version"] == "3.11"
