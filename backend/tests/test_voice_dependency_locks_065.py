"""Supply-chain guards for Feature 065 Windows and contract-tool locks."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ROOT = REPO_ROOT / "components" / "AstralProjection" / "windows-client"
WINDOWS_INPUT = WINDOWS_ROOT / "requirements.in"
WINDOWS_LOCK = WINDOWS_ROOT / "requirements-release.lock.txt"
CONTRACT_ROOT = REPO_ROOT / "tooling" / "contract-ci"
CONTRACT_INPUT = CONTRACT_ROOT / "requirements.in"
CONTRACT_LOCK = CONTRACT_ROOT / "requirements.lock.txt"
FEATURE_075_BASE_COMMIT = "6d9931dbc43c6c9ff2f0435000c91dd1106e9409"
FEATURE_075_DEPENDENCY_AUTHORITIES = {
    "Dockerfile": "292dbb485a05b3af0734a18d7822fbc8b574d0bf22e8a8b85e1047860d2364fe",
    "Dockerfile.voice": "86c246452f4c2d720f70a49b26d57f1ddd47e53d506173f87e316a6f7a83e943",
    "backend/requirements.txt": (
        "6cb717ba80c99dfe33728b247e835ad68d38cbce94ddf51c56cdcbe590a753ff"
    ),
    "backend/security_benchmark/requirements-eval.txt": (
        "bb0a37d68c78018092f71ac1b64ae83b6bdf297330670edf4e18d82ff9a8d65c"
    ),
    "backend/tests/fixtures/runtime_reliability_060/runtime-lock-contract.json": (
        "8cfc0c8d2c83149f30e295cf8269ed88e2ab244b1c477453cbdbfac8bf44fdb6"
    ),
    "backend/voice_agent/models/silero_vad.onnx": (
        "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
    ),
    "backend/voice_agent/requirements-test.in": (
        "6f8f0601ae8495c1fa96617e9b1cf0eda31fba04a61f96b3a3f0de38da3a9f8f"
    ),
    "backend/voice_agent/requirements-test.lock.txt": (
        "755d9407a376ea9a64307f65fba53d125fdaa808c80e858be898f004c7336215"
    ),
    "backend/voice_agent/requirements.in": (
        "aa59e2e6b8bae7fb23b758d2ce2a31fd995fbf15fd3e566c09d7b5d6c04ab6a1"
    ),
    "backend/voice_agent/requirements.lock.txt": (
        "fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3"
    ),
    "pyproject.toml": (
        "e78a2c21410fbbd77a49374fe0a0d7803b900294f811619681c6c6871e0d97d8"
    ),
    "tooling/contract-ci/requirements.in": (
        "3a6ed2112bd654add123d3732b0c0c72bc95129ac2b228b9efe2e3beb8a12d4e"
    ),
    "tooling/contract-ci/requirements.lock.txt": (
        "5f8429e4d43b3ee30d87c587a115c844590a8c73050fdd4f426f1314015d01b4"
    ),
    "tooling/python-ci/requirements.in": (
        "d4b2d288fe8cb11b805ed1f37375cdc583ac7e4c5cf23c4d17e284f459285064"
    ),
    "tooling/python-ci/requirements.lock.txt": (
        "53a13f8fdc29757212ffe792ce361a8e17ef4e4acd52c3f723b726c84f62d15f"
    ),
}
DEEP_DEPENDENCY_AUTHORITY_POLICY = {
    "roots": frozenset({".github", "backend", "scripts", "tooling"}),
    "excluded_parts": frozenset(
        {"components", "docs", "examples", "fixtures", "snapshots", "specs", "tests"}
    ),
    "lookalike_tokens": frozenset(
        {"backup", "copy", "example", "fixture", "old", "sample", "snapshot"}
    ),
    "exact_paths": frozenset(
        {
            "backend/tests/fixtures/runtime_reliability_060/runtime-lock-contract.json"
        }
    ),
    "manifest_names": frozenset(
        {
            "Cargo.lock",
            "Cargo.toml",
            "Directory.Build.props",
            "Directory.Build.targets",
            "Directory.Packages.props",
            "Package.resolved",
            "Package.swift",
            "Pipfile",
            "Pipfile.lock",
            "build.gradle",
            "build.gradle.kts",
            "bun.lock",
            "bun.lockb",
            "global.json",
            "go.mod",
            "go.sum",
            "gradle.lockfile",
            "libs.versions.toml",
            "npm-shrinkwrap.json",
            "package-lock.json",
            "package.json",
            "packages.config",
            "packages.lock.json",
            "pnpm-lock.yaml",
            "poetry.lock",
            "pom.xml",
            "pyproject.toml",
            "settings.gradle",
            "settings.gradle.kts",
            "setup.cfg",
            "setup.py",
            "uv.lock",
            "yarn.lock",
        }
    ),
    "project_suffixes": frozenset({".csproj", ".fsproj", ".vbproj"}),
    "model_suffixes": frozenset(
        {
            ".bin",
            ".gguf",
            ".mlmodel",
            ".mlpackage",
            ".onnx",
            ".ort",
            ".pt",
            ".pth",
            ".safetensors",
            ".tflite",
        }
    ),
}
_REQUIREMENTS_AUTHORITY_RE = re.compile(
    r"requirements(?:-[a-z0-9][a-z0-9_.-]*)?(?:\.lock)?\.(?:in|txt)$"
)
_DOCKERFILE_AUTHORITY_RE = re.compile(r"Dockerfile(?:\.[A-Za-z0-9_-]+)*$")

if not (REPO_ROOT / "specs").is_dir():
    pytest.skip(
        "repo-root dependency manifests are not part of the product image",
        allow_module_level=True,
    )


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_requirements(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "--only-binary"))
    ]


def _pin(requirement: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", requirement)
    if match:
        return _normalized(match.group(1)), match.group(2)
    direct = re.match(
        r"^lets-agent @ https://github\.com/AstralDeep/LETS/releases/download/"
        r"v([0-9]+\.[0-9]+\.[0-9]+)/lets_agent-"
        r"([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl(?:\s|$)",
        requirement,
    )
    assert direct, f"requirement is not an exact approved pin: {requirement}"
    assert direct.group(1) == direct.group(2)
    return "lets-agent", direct.group(2)


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in _logical_requirements(path):
        name, version = _pin(requirement)
        assert name not in pins, f"duplicate requirement: {name}"
        pins[name] = version
    return pins


def _hashes(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for requirement in _logical_requirements(path):
        name, _version = _pin(requirement)
        values = set(re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", requirement))
        assert values, f"requirement has no SHA-256 hash: {requirement}"
        result[name] = values
    return result


def _deep_dependency_authority_kind(relative_path: str) -> str | None:
    """Classify only tracked, Deep-owned dependency and model authorities."""

    normalized = PurePosixPath(relative_path).as_posix()
    policy = DEEP_DEPENDENCY_AUTHORITY_POLICY
    if normalized in policy["exact_paths"]:
        return "exact-evidence"

    path = PurePosixPath(normalized)
    parts = path.parts
    if not parts or parts[0] == "components":
        return None
    if len(parts) > 1 and parts[0] not in policy["roots"]:
        return None
    if any(part.lower() in policy["excluded_parts"] for part in parts[:-1]):
        return None

    name = path.name
    suffix = path.suffix.lower()
    if set(re.split(r"[._-]+", name.lower())) & policy["lookalike_tokens"]:
        return None
    if name in policy["manifest_names"] or suffix in policy["project_suffixes"]:
        return "package-manifest"
    if _REQUIREMENTS_AUTHORITY_RE.fullmatch(name.lower()):
        return "python-requirements"
    if _DOCKERFILE_AUTHORITY_RE.fullmatch(name) and not name.endswith(".dockerignore"):
        return "container-build"
    if suffix in policy["model_suffixes"] and any(
        part.lower() in {"model", "models"} for part in parts[:-1]
    ):
        return "model-artifact"
    return None


def _git_tracked_paths(*args: str) -> set[str]:
    return set(
        subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
    )


def _git_blob(commit: str, relative_path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def test_windows_livekit_direct_pin_and_win_amd64_wheel_are_exact() -> None:
    assert _pins(WINDOWS_INPUT)["livekit"] == "1.1.14"
    locked = _pins(WINDOWS_LOCK)
    hashes = _hashes(WINDOWS_LOCK)
    assert len(locked) == 69
    assert len(set(locked) - {"macholib"}) == 68
    assert locked["livekit"] == "1.1.14"
    assert {
        name: (locked[name], hashes[name])
        for name in ("aiofiles", "livekit", "numpy", "protobuf", "types-protobuf")
    } == {
        "aiofiles": (
            "25.1.0",
            {"abe311e527c862958650f9438e859c1fa7568a141b22abcd015e120e86a85695"},
        ),
        "livekit": (
            "1.1.14",
            {"b8f8d38f131956297923e520bc4375bc9ebfa255cab7f125cb7755bfca71df24"},
        ),
        "numpy": (
            "2.4.6",
            {"1e254a00cdf42b1e4d5b3d68d33af63268d41340d8885df2ab6470f2e1500147"},
        ),
        "protobuf": (
            "7.35.1",
            {"230a75ddfc2de4806e56696ce9640c1cdfdb6543b7cfce98d42a4c0a0e7bdb87"},
        ),
        "types-protobuf": (
            "7.34.1.20260518",
            {"a0a5337413347166439c0e07cbc26c6164d091401c6f01b1dfd8cdb966c4dd8f"},
        ),
    }
    assert all(locked.get(name) == version for name, version in _pins(WINDOWS_INPUT).items())


def test_contract_validator_lock_is_exact_complete_and_hash_locked() -> None:
    assert _pins(CONTRACT_INPUT) == {
        "jsonschema": "4.25.1",
        "openapi-spec-validator": "0.7.2",
    }
    locked = _pins(CONTRACT_LOCK)
    hashes = _hashes(CONTRACT_LOCK)
    assert len(locked) == 19
    assert set(locked) == set(hashes)
    assert locked["jsonschema"] == "4.25.1"
    assert locked["openapi-spec-validator"] == "0.7.2"
    assert (
        "3fba0169e345c7175110351d456342c364814cfcf3b964ba4587f22915230a63"
        in hashes["jsonschema"]
    )
    assert (
        "4bbdc0894ec85f1d1bea1d6d9c8b2c3c8d7ccaa13577ef40da9c006c9fd0eb60"
        in hashes["openapi-spec-validator"]
    )


def test_contract_validator_dependencies_stay_out_of_product_manifests() -> None:
    product_manifests = (
        REPO_ROOT / "backend" / "requirements.txt",
        WINDOWS_ROOT / "requirements.in",
        WINDOWS_ROOT / "AstralDeep.spec",
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "Dockerfile.voice",
    )
    for path in product_manifests:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "tooling/contract-ci" not in text, path
        assert "openapi-spec-validator" not in text, path
        assert re.search(r"(?m)^jsonschema(?:\[.*\])?\s*[=<>~!]", text) is None, path


def test_feature_075_adds_no_runtime_model_development_or_lock_drift() -> None:
    """Freeze every tracked Deep dependency/model authority at the 075 base."""

    base_tracked = _git_tracked_paths(
        "ls-tree", "-r", "--name-only", FEATURE_075_BASE_COMMIT
    )
    current_tracked = _git_tracked_paths("ls-files")
    base_observed = {
        path for path in base_tracked if _deep_dependency_authority_kind(path) is not None
    }
    current_observed = {
        path for path in current_tracked if _deep_dependency_authority_kind(path) is not None
    }
    expected = set(FEATURE_075_DEPENDENCY_AUTHORITIES)

    assert base_observed == expected
    assert current_observed == expected
    for relative, expected_sha256 in FEATURE_075_DEPENDENCY_AUTHORITIES.items():
        base_sha256 = hashlib.sha256(
            _git_blob(FEATURE_075_BASE_COMMIT, relative)
        ).hexdigest()
        current_sha256 = hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        assert base_sha256 == expected_sha256, relative
        assert current_sha256 == base_sha256, relative


def test_deep_dependency_authority_policy_is_scoped_and_complete() -> None:
    accepted = {
        "backend/service/setup.py": "package-manifest",
        "tooling/lint/setup.cfg": "package-manifest",
        "backend/service/Pipfile": "package-manifest",
        "backend/service/poetry.lock": "package-manifest",
        "backend/web/npm-shrinkwrap.json": "package-manifest",
        "backend/native/build.gradle.kts": "package-manifest",
        "backend/native/settings.gradle": "package-manifest",
        "backend/native/gradle/libs.versions.toml": "package-manifest",
        "backend/native/pom.xml": "package-manifest",
        "tooling/audit/go.mod": "package-manifest",
        "tooling/audit/Cargo.lock": "package-manifest",
        "backend/native/Package.resolved": "package-manifest",
        "backend/native/packages.lock.json": "package-manifest",
        "backend/native/Helper.csproj": "package-manifest",
        ".github/actions/contract/package.json": "package-manifest",
        "scripts/release/Cargo.toml": "package-manifest",
        "Dockerfile.local-arm64": "container-build",
        "backend/voice_agent/models/vad.ort": "model-artifact",
        "backend/voice_agent/models/tokenizer.bin": "model-artifact",
    }
    rejected = {
        "component-internal": "components/AstralProjection/package.json",
        "documentation": "docs/examples/requirements.txt",
        "spec-example": "specs/075-client-local-speech/package-lock.json",
        "fixture-lookalike": "backend/tests/fixtures/requirements-snapshot.txt",
        "snapshot-lookalike": "backend/snapshots/Pipfile",
        "filename-lookalike": "backend/service/requirements-prod.snapshot.txt",
        "model-fixture": "backend/voice_agent/fixtures/tokenizer.bin",
        "docker-ignore": "Dockerfile.voice.dockerignore",
        "unknown-root": "vendor/tool/package.json",
        "requirements-notes": "backend/voice_agent/requirements-notes.md",
    }

    assert {
        path: _deep_dependency_authority_kind(path)
        for path in accepted
    } == accepted
    assert all(
        _deep_dependency_authority_kind(path) is None for path in rejected.values()
    )
