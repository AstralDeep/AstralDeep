"""Supply-chain and image-boundary guards for the Feature 065 voice worker."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VOICE_ROOT = REPO_ROOT / "backend" / "voice_agent"
VOICE_INPUT = VOICE_ROOT / "requirements.in"
VOICE_LOCK = VOICE_ROOT / "requirements.lock.txt"
VOICE_TEST_INPUT = VOICE_ROOT / "requirements-test.in"
VOICE_TEST_LOCK = VOICE_ROOT / "requirements-test.lock.txt"
VOICE_DOCKERFILE = REPO_ROOT / "Dockerfile.voice"
VOICE_DOCKERIGNORE = REPO_ROOT / "Dockerfile.voice.dockerignore"
NATIVE_AUDIT = REPO_ROOT / "tooling" / "voice-worker" / "verify_no_libgomp.py"
SILERO_MODEL = VOICE_ROOT / "models" / "silero_vad.onnx"
SILERO_LICENSE = VOICE_ROOT / "licenses" / "SILERO_VAD_LICENSE"
SILERO_PROVENANCE = VOICE_ROOT / "licenses" / "SILERO_VAD_PROVENANCE.json"

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
PROHIBITED_WORKER_DISTRIBUTIONS = {
    "av",
    "blingfire",
    "livekit-agents",
    "livekit-api",
    "livekit-blingfire",
    "livekit-local-inference",
    "livekit-plugins-silero",
    "openai",
    "torch",
}

if not (REPO_ROOT / "specs").is_dir():
    pytest.skip(
        "repo-root voice-worker packaging is not part of the product image",
        allow_module_level=True,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_requirements(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--only-binary"))
    ]


def _pins_and_hashes(
    path: Path, *, require_hashes: bool = True
) -> tuple[dict[str, str], dict[str, set[str]]]:
    pins: dict[str, str] = {}
    hashes: dict[str, set[str]] = {}
    for requirement in _logical_requirements(path):
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", requirement)
        assert match, f"requirement is not an exact pin: {requirement}"
        name = _normalized(match.group(1))
        assert name not in pins, f"duplicate requirement: {name}"
        pins[name] = match.group(2)
        hashes[name] = set(
            re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", requirement)
        )
        if require_hashes:
            assert hashes[name], f"requirement has no SHA-256 hash: {requirement}"
    return pins, hashes


def test_voice_worker_direct_pins_and_nine_distribution_closure_are_exact() -> None:
    direct, _ = _pins_and_hashes(VOICE_INPUT, require_hashes=False)
    locked, hashes = _pins_and_hashes(VOICE_LOCK)

    assert direct == {
        "livekit": "1.1.14",
        "numpy": "2.4.6",
        "onnxruntime": "1.28.0",
        "websockets": "17.0.1",
    }
    assert locked == EXPECTED_RUNTIME
    assert set(locked) == set(hashes)
    assert not (set(locked) & PROHIBITED_WORKER_DISTRIBUTIONS)
    assert "--only-binary :all:" in VOICE_LOCK.read_text(encoding="utf-8")
    assert _sha256(VOICE_LOCK) == (
        "fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3"
    )


def test_worker_native_wheels_cover_approved_amd64_and_arm64_artifacts() -> None:
    locked, hashes = _pins_and_hashes(VOICE_LOCK)
    expected = {
        "livekit": (
            "1.1.14",
            {
                "80962c4a22ddbf0e0ebd3563fc090fce42df66b39b90de68b161b7db01970f68",
                "299146efefad5f67751cd15b8225bae759be0d7ad2f0b4ae1a22c15860d93cf9",
            },
        ),
        "numpy": (
            "2.4.6",
            {
                "89cd468399cfd2504718f0ba50e410dca55a170b61a02ad92bb18c8a65186e93",
                "0ab0a9c4ffb1a6d95ef519fe4247dba8eb6b18ad93999f76b7f657039acabd47",
            },
        ),
        "onnxruntime": (
            "1.28.0",
            {
                "a166b78ee04f3a37fa1ef82034b6a3ce96d9684e582d4d30b296de83e9998bb5",
                "8d66f9ceb29909c70839e4e4fb3435c7b490050d8f162bd5f3aba4ca01ee517f",
            },
        ),
        "websockets": (
            "17.0.1",
            {
                "d41e9845514754a42d1d83b2fca9d27fee2ca7b3b0bee6843ba5a9bb2b6e25ac",
                "d9aac6081513f02eac3f8caace800dbfc5c608b69e4a7bef69e414eabfc95aa1",
            },
        ),
    }
    for name, (version, required_hashes) in expected.items():
        assert locked[name] == version
        assert required_hashes <= hashes[name]


def test_silero_model_license_and_provenance_are_exact() -> None:
    provenance = json.loads(SILERO_PROVENANCE.read_text(encoding="utf-8"))
    assert SILERO_MODEL.stat().st_size == 2_327_524
    assert _sha256(SILERO_MODEL) == (
        "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
    )
    assert _sha256(SILERO_LICENSE) == (
        "51c19c8be941a3fb00ccf58f0bf9053de9f7237a0b37327896eabad32dffe873"
    )
    assert _sha256(SILERO_PROVENANCE) == (
        "144e92f17546c15e8c71956947cb0d53acf98f08ba4bfe156a721b30685ebe0c"
    )
    assert provenance == {
        "commit": "fba061dc5559f696e62171e9a0741782b0fdc23c",
        "license_path": "LICENSE",
        "upstream_license_sha256": (
            "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
        ),
        "vendored_license_sha256": (
            "51c19c8be941a3fb00ccf58f0bf9053de9f7237a0b37327896eabad32dffe873"
        ),
        "model_path": "src/silero_vad/data/silero_vad.onnx",
        "model_sha256": (
            "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
        ),
        "model_size_bytes": 2_327_524,
        "repository": "https://github.com/snakers4/silero-vad",
        "tag": "v6.0",
    }


def test_livekit_api_is_orchestrator_only() -> None:
    backend_requirements = (REPO_ROOT / "backend" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    worker_input = VOICE_INPUT.read_text(encoding="utf-8")
    worker_lock = VOICE_LOCK.read_text(encoding="utf-8")
    assert re.search(r"(?m)^livekit-api==1[.]2[.]0$", backend_requirements)
    assert not re.search(r"(?m)^livekit-api\s*[=<>~!]", worker_input)
    assert not re.search(r"(?m)^livekit-api\s*[=<>~!]", worker_lock)


def test_voice_worker_async_test_dependency_is_exact_hash_locked_and_isolated() -> None:
    direct, _ = _pins_and_hashes(VOICE_TEST_INPUT, require_hashes=False)
    locked, hashes = _pins_and_hashes(VOICE_TEST_LOCK)

    assert direct == {"pytest-asyncio": "1.4.0"}
    assert locked == {
        "iniconfig": "2.3.0",
        "packaging": "26.2",
        "pluggy": "1.6.0",
        "pygments": "2.20.0",
        "pytest": "9.1.1",
        "pytest-asyncio": "1.4.0",
        "typing-extensions": "4.16.0",
    }
    assert set(locked) == set(hashes)
    assert (
        "933ca923a23075a87fb7070c0ec272a6848489824d887c85c812670932835aa1"
        in hashes["pytest-asyncio"]
    )
    assert (
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"
        in hashes["typing-extensions"]
    )
    assert _sha256(VOICE_TEST_LOCK) == (
        "755d9407a376ea9a64307f65fba53d125fdaa808c80e858be898f004c7336215"
    )


def test_voice_runtime_and_test_targets_are_locked_and_isolated() -> None:
    text = VOICE_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "python:3.11.15-slim-bookworm@sha256:"
        "b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba"
    ) in text
    assert " AS runtime" in text
    assert " AS test" in text

    def stage(name: str) -> str:
        match = re.search(
            rf"(?ms)^FROM [^\n]+ AS {re.escape(name)}\n(?P<body>.*?)(?=^FROM |\Z)",
            text,
        )
        assert match, name
        return match.group("body")

    dependencies = stage("runtime-dependencies")
    pruned_dependencies = stage("runtime-pruned-dependencies")
    pruned_system = stage("runtime-pruned-system")
    test_dependencies = stage("test-dependencies")
    runtime = stage("runtime")
    test = stage("test")

    assert "backend/voice_agent/requirements.lock.txt" in dependencies
    assert "backend/voice_agent/models/silero_vad.onnx" in dependencies
    assert "--require-hashes" in dependencies
    assert "--only-binary=:all:" in dependencies
    assert "--no-deps" in dependencies
    assert "InferenceSession" in dependencies
    assert "CPUExecutionProvider" in dependencies

    assert "pip uninstall --yes pip setuptools" in pruned_dependencies
    assert 'for distribution in ("pip", "setuptools", "wheel")' in pruned_dependencies
    assert "pip uninstall --yes setuptools wheel packaging pip" in pruned_system

    for lock in (
        "backend/voice_agent/requirements.lock.txt",
        "tooling/python-ci/requirements.lock.txt",
        "backend/voice_agent/requirements-test.lock.txt",
    ):
        assert lock in test_dependencies
    assert "--require-hashes" in test_dependencies
    assert "--only-binary=:all:" in test_dependencies
    assert "--no-deps" in test_dependencies

    assert "FROM runtime-pruned-system AS runtime" in text
    assert "runtime-pruned-dependencies" in runtime
    assert "tooling/python-ci/requirements.lock.txt" not in runtime
    assert "requirements-test.lock.txt" not in runtime
    assert "pip install" not in runtime
    assert "USER 10001:10001" in runtime
    assert (
        'ENTRYPOINT ["/opt/voice-venv/bin/python", "-m", "voice_agent.main"]' in runtime
    )
    assert "source=tooling/voice-worker/verify_no_libgomp.py" in runtime
    assert "--root /opt/voice-venv --root /usr" in runtime
    assert (
        "COPY backend/shared/streaming_egress.py /voice-source/streaming_egress.py"
    ) in text
    assert (
        "COPY backend/shared/voice_transcript.py /voice-source/voice_transcript.py"
    ) in text
    assert (
        "COPY backend/shared/watch_ticket.py /voice-source/watch_ticket.py"
    ) in text

    assert "FROM runtime AS test" in text
    assert "test-dependencies" in test
    assert "/opt/voice-test-venv" in test
    assert "backend/voice_agent/tests/" in test
    assert "backend/voice_agent/requirements.lock.txt" in test
    assert "pip install" not in test
    assert "USER 10001:10001" in test


def test_voice_docker_context_is_a_strict_non_sensitive_allowlist() -> None:
    rules = [
        line.strip()
        for line in VOICE_DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules[0] == "**"
    assert set(rules[1:]) == {
        "!backend/",
        "!backend/pytest.ini",
        "!backend/shared/",
        "!backend/shared/streaming_egress.py",
        "!backend/shared/voice_transcript.py",
        "!backend/shared/watch_ticket.py",
        "!backend/voice_agent/",
        "!backend/voice_agent/*.py",
        "!backend/voice_agent/licenses/",
        "!backend/voice_agent/licenses/SILERO_VAD_LICENSE",
        "!backend/voice_agent/licenses/SILERO_VAD_PROVENANCE.json",
        "!backend/voice_agent/models/",
        "!backend/voice_agent/models/silero_vad.onnx",
        "!backend/voice_agent/requirements.lock.txt",
        "!backend/voice_agent/requirements-test.lock.txt",
        "!backend/voice_agent/tests/",
        "!backend/voice_agent/tests/*.py",
        "!tooling/",
        "!tooling/python-ci/",
        "!tooling/python-ci/requirements.lock.txt",
        "!tooling/voice-worker/",
        "!tooling/voice-worker/verify_no_libgomp.py",
    }
    assert not any(
        sensitive in rule
        for rule in rules[1:]
        for sensitive in (
            ".env",
            ".git",
            "CLOSURE.json",
            "backend/data",
            "backend/tmp",
            "build",
            "coverage",
            "knowledge",
            "node_modules",
            "specs",
        )
    )


def test_native_audit_tool_has_basic_elf_fixture_coverage() -> None:
    assert NATIVE_AUDIT.is_file()
    source = NATIVE_AUDIT.read_text(encoding="utf-8")
    assert "DT_NEEDED" in source
    assert "libgomp" in source
