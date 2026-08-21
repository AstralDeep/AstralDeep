"""Supply-chain guards for Feature 065 Windows and contract-tool locks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ROOT = REPO_ROOT / "components" / "AstralProjection" / "windows-client"
WINDOWS_INPUT = WINDOWS_ROOT / "requirements.in"
WINDOWS_LOCK = WINDOWS_ROOT / "requirements-release.lock.txt"
CONTRACT_ROOT = REPO_ROOT / "tooling" / "contract-ci"
CONTRACT_INPUT = CONTRACT_ROOT / "requirements.in"
CONTRACT_LOCK = CONTRACT_ROOT / "requirements.lock.txt"

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


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in _logical_requirements(path):
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", requirement)
        assert match, f"requirement is not an exact pin: {requirement}"
        name = _normalized(match.group(1))
        assert name not in pins, f"duplicate requirement: {name}"
        pins[name] = match.group(2)
    return pins


def _hashes(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for requirement in _logical_requirements(path):
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", requirement)
        assert match
        values = set(re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", requirement))
        assert values, f"requirement has no SHA-256 hash: {requirement}"
        result[_normalized(match.group(1))] = values
    return result


def test_windows_livekit_direct_pin_and_win_amd64_wheel_are_exact() -> None:
    assert _pins(WINDOWS_INPUT)["livekit"] == "1.1.14"
    locked = _pins(WINDOWS_LOCK)
    hashes = _hashes(WINDOWS_LOCK)
    assert len(locked) == 67
    assert len(set(locked) - {"macholib"}) == 66
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
