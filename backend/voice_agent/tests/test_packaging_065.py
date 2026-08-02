"""In-image assertions for the isolated Feature 065 voice-worker test target."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import onnxruntime as ort


EXPECTED_RUNTIME = {
    "aiofiles": "25.1.0",
    "flatbuffers": "25.12.19",
    "livekit": "1.1.14",
    "numpy": "2.4.6",
    "onnxruntime": "1.28.0",
    "packaging": "26.2",
    "protobuf": "7.35.1",
    "types-protobuf": "7.34.1.20260518",
    "websockets": "17.0.1",
}
SILERO_SHA256 = "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
SILERO_LICENSE_SHA256 = (
    "51c19c8be941a3fb00ccf58f0bf9053de9f7237a0b37327896eabad32dffe873"
)
SILERO_PROVENANCE_SHA256 = (
    "144e92f17546c15e8c71956947cb0d53acf98f08ba4bfe156a721b30685ebe0c"
)
PROHIBITED = (
    "av",
    "blingfire",
    "livekit-agents",
    "livekit-api",
    "livekit-local-inference",
    "livekit-plugins-silero",
    "openai",
    "torch",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _distribution_file(distribution: str, suffix: tuple[str, ...]) -> Path:
    metadata = importlib.metadata.distribution(distribution)
    matches = [
        file
        for file in metadata.files or ()
        if tuple(file.parts[-len(suffix) :]) == suffix
    ]
    assert len(matches) == 1, (distribution, suffix, matches)
    return Path(metadata.locate_file(matches[0]))


def _runtime_probe(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/opt/voice-venv/bin/python", "-c", source],
        check=False,
        capture_output=True,
        text=True,
    )


def test_runtime_distribution_versions_are_the_reviewed_eight() -> None:
    expected = json.dumps(EXPECTED_RUNTIME, sort_keys=True)
    result = _runtime_probe(
        "import importlib.metadata as m, json; "
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}, "
        "sort_keys=True))"
    )
    assert result.returncode == 0, result.stderr or result.stdout
    actual = {
        re.sub(r"[-_.]+", "-", name).lower(): version
        for name, version in json.loads(result.stdout).items()
    }
    assert actual == json.loads(expected)


def test_silero_payload_and_image_notices_are_exact() -> None:
    model = Path("/opt/voice-assets/silero_vad.onnx")
    notices = Path("/usr/share/licenses/silero-vad")
    license_file = notices / "LICENSE"
    provenance = notices / "PROVENANCE.json"
    manifest = (notices / "ARTIFACTS.sha256").read_text(encoding="utf-8")

    assert model.stat().st_size == 2_327_524
    assert _sha256(model) == SILERO_SHA256
    assert _sha256(license_file) == SILERO_LICENSE_SHA256
    assert _sha256(provenance) == SILERO_PROVENANCE_SHA256
    assert f"{SILERO_SHA256}  silero_vad.onnx" in manifest
    assert f"{SILERO_LICENSE_SHA256}  LICENSE" in manifest
    assert f"{SILERO_PROVENANCE_SHA256}  PROVENANCE.json" in manifest

    websocket_license = _distribution_file("websockets", ("licenses", "LICENSE"))
    assert _sha256(websocket_license) == (
        "3d6a0c050d8bec52fabad502e45fb25bd02bcadbd70dea34d447b6a0ff4e6da8"
    )


def test_real_silero_inference_and_livekit_audio_frame() -> None:
    from livekit import rtc
    from websockets.asyncio.client import connect

    assert callable(connect)

    session = ort.InferenceSession(
        "/opt/voice-assets/silero_vad.onnx",
        providers=["CPUExecutionProvider"],
    )
    output, state = session.run(
        None,
        {
            "input": np.zeros((1, 512), dtype=np.float32),
            "state": np.zeros((2, 1, 128), dtype=np.float32),
            "sr": np.array(16_000, dtype=np.int64),
        },
    )
    assert output.shape == (1, 1)
    assert state.shape == (2, 1, 128)
    assert np.isfinite(output).all()
    assert np.isfinite(state).all()

    frame = rtc.AudioFrame.create(
        sample_rate=16_000, num_channels=1, samples_per_channel=512
    )
    assert frame.sample_rate == 16_000
    assert frame.num_channels == 1
    assert frame.samples_per_channel == 512
    assert frame.data.nbytes == 1_024


def test_runtime_has_no_prohibited_distribution_or_import() -> None:
    result = _runtime_probe(
        f"""
import importlib.metadata
import importlib.util

for name in {PROHIBITED!r}:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise SystemExit(f"prohibited runtime distribution: {{name}}")
for module in ("av", "blingfire", "livekit.agents", "livekit.api", "openai", "torch"):
    try:
        found = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        found = None
    if found is not None:
        raise SystemExit(f"prohibited runtime module: {{module}}")
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_runtime_has_only_the_renamed_audited_shared_sources() -> None:
    result = _runtime_probe(
        """
import importlib.util
from voice_agent.streaming_egress import FixedOriginHttpTransport
from voice_agent.voice_transcript import canonical_transcript
from voice_agent.watch_ticket import derive_watch_nonce

assert callable(FixedOriginHttpTransport)
assert canonical_transcript("  hello  ") == "hello"
assert callable(derive_watch_nonce)
if importlib.util.find_spec("shared") is not None:
    raise SystemExit("product shared package leaked into voice worker")
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_test_tooling_is_present_only_in_the_explicit_test_target() -> None:
    assert importlib.metadata.version("pytest") == "9.1.1"
    assert importlib.metadata.version("pytest-cov") == "7.0.0"
    assert importlib.metadata.version("pytest-asyncio") == "1.4.0"

    result = _runtime_probe(
        """
import importlib.metadata
for name in ("coverage", "pytest", "pytest-asyncio", "pytest-cov", "ruff"):
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        continue
    raise SystemExit(f"test distribution leaked: {name}")
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_runtime_interpreters_have_no_installation_tooling() -> None:
    probes = (
        (Path("/opt/voice-venv/bin/python"), ("pip", "setuptools", "wheel")),
        (
            Path("/usr/local/bin/python"),
            ("packaging", "pip", "setuptools", "wheel"),
        ),
    )
    for python, forbidden in probes:
        probe = subprocess.run(
            [
                str(python),
                "-c",
                f"""
import importlib.metadata
import importlib.util

for distribution in {forbidden!r}:
    try:
        importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise SystemExit(f"runtime distribution leaked: {{distribution}}")
for module in {forbidden!r}:
    if importlib.util.find_spec(module) is not None:
        raise SystemExit(f"runtime module leaked: {{module}}")
""",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr or probe.stdout
        assert not list(python.parent.glob("pip*"))
        assert not list(python.parent.glob("easy_install*"))


def test_shipped_native_objects_do_not_declare_libgomp() -> None:
    audit = Path("/opt/voice-audit/verify_no_libgomp.py")
    result = subprocess.run(
        [
            "/opt/voice-test-venv/bin/python",
            str(audit),
            "--root",
            "/opt/voice-venv",
            "--root",
            "/usr",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert re.search(r"scanned [1-9][0-9]* ELF objects?", result.stdout)
