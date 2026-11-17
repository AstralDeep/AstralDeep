"""Checks that each package moved into this repository with its history stays its own package.
Deep's root lint, ignore rules, backend test discovery, changed-line coverage, image and
requirements never take it over, and its code imports no Deep backend package or AstralPlane.
It also pins the repository's kernel binding files and each package's .github files.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUFF_PIN = REPOSITORY_ROOT / "tooling" / "python-ci" / "requirements.in"
KERNEL_BINDING_FORMAT = "astraldeep.kernel-binding/v1"
KERNEL_BINDINGS = frozenset(
    {"config/astral-kernel-binding.json", "vessel/config/kernel-binding.json"}
)
ALLOWED_BACKEND_IMPORTS = frozenset({"astral_exec"})
REQUIREMENT_FILE = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.(?:txt|in)|pyproject\.toml|uv\.lock)$"
)
DOCKERFILE = re.compile(r"(?:^|/)(?:Dockerfile(?:\.[^/.]+)?|[^/]+\.Dockerfile)$")
PROBE_NAME = "moved-package-probe.md"


@dataclass(frozen=True)
class MovedPackage:
    folder: str
    distribution: str
    command: str | None
    github_files: frozenset[str]
    import_name: str | None = None
    image_copies: frozenset[str] = frozenset()
    named_in: frozenset[str] = frozenset()


MOVED_PACKAGES = (
    MovedPackage(
        folder="vessel",
        distribution="astralvessel",
        command="astralvessel",
        github_files=frozenset(
            {
                "dependabot.yml",
                "workflows/ci.yml",
            }
        ),
    ),
    MovedPackage(
        folder="plane",
        distribution="astralplane",
        command=None,
        github_files=frozenset(
            {
                "PR_TRIAGE.md",
                "pull_request_template.md",
                "ISSUE_TEMPLATE/issue.md",
                "workflows/ci.yml",
                "workflows/pr-ci-notifications.yml",
                "workflows/pr-triage.yml",
            }
        ),
        import_name="astralplane",
        image_copies=frozenset(
            {
                "plane/pyproject.toml",
                "plane/README.md",
                "plane/LICENSE",
                "plane/src/astralplane",
            }
        ),
        named_in=frozenset({"pyproject.toml"}),
    ),
)
MOVED_FOLDERS = tuple(package.folder for package in MOVED_PACKAGES)

by_package = pytest.mark.parametrize(
    "package", MOVED_PACKAGES, ids=lambda package: package.folder
)


def _git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _tracked(*pathspecs: str) -> list[str]:
    output = _git("ls-files", "-z", "--", *pathspecs).stdout
    return [path for path in output.split("\0") if path]


def _package_files(package: MovedPackage) -> list[str]:
    files = _tracked(package.folder)
    assert files, f"{package.folder}/ holds no tracked files"
    return files


def _outside_moved_packages(path: str) -> bool:
    return not any(path.startswith(f"{folder}/") for folder in MOVED_FOLDERS)


def _load_script(name: str) -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"moved_packages_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pinned_ruff() -> Path:
    match = re.search(
        r"^ruff==(\S+)$", RUFF_PIN.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, f"{RUFF_PIN} pins no ruff version"
    sibling = Path(sys.executable).parent / "ruff"
    found = str(sibling) if sibling.is_file() else shutil.which("ruff")
    assert found, (
        f"ruff=={match.group(1)} must be installed to check the root lint scope"
    )
    ruff = Path(found)
    version = subprocess.run(
        [str(ruff), "--version"], check=True, capture_output=True, text=True
    ).stdout.split()
    assert version[-1] == match.group(1), (
        f"root lint is pinned to ruff {match.group(1)}"
    )
    return ruff


def _ruff_files(ruff: Path, target: str) -> set[Path]:
    result = subprocess.run(
        [str(ruff), "check", "--show-files", target],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {Path(line).resolve() for line in result.stdout.splitlines() if line.strip()}


def _imported_roots(source: str, path: str) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _top_level_names(files: list[str], prefix: str) -> set[str]:
    names = set()
    for path in files:
        relative = PurePosixPath(path).relative_to(prefix)
        first = relative.parts[0]
        names.add(first if len(relative.parts) > 1 else PurePosixPath(first).stem)
    return names


def _copy_sources(dockerfile: str) -> list[str]:
    text = (REPOSITORY_ROOT / dockerfile).read_text(encoding="utf-8")
    sources = []
    for instruction in re.sub(r"\\\n", " ", text).splitlines():
        words = instruction.split(None, 1)
        if len(words) < 2 or words[0].upper() not in {"COPY", "ADD"}:
            continue
        arguments = words[1].strip()
        if arguments.startswith("["):
            tokens = json.loads(arguments)
        else:
            tokens = arguments.split()
        flags = [token for token in tokens if token.startswith("--")]
        if any(flag.startswith("--from=") for flag in flags):
            continue
        operands = [token for token in tokens if not token.startswith("--")]
        sources.extend(operands[:-1])
    return sources


@by_package
def test_the_package_keeps_its_own_manifest_and_command(package: MovedPackage) -> None:
    assert f"{package.folder}/pyproject.toml" in _package_files(package)
    manifest = tomllib.loads(
        (REPOSITORY_ROOT / package.folder / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["project"]["name"] == package.distribution
    if package.command is not None:
        assert package.command in manifest["project"]["scripts"]


@by_package
def test_root_lint_excludes_the_package_and_still_lints_it_by_name(
    package: MovedPackage,
) -> None:
    config = tomllib.loads((REPOSITORY_ROOT / "ruff.toml").read_text(encoding="utf-8"))
    assert package.folder in config["exclude"]
    ruff = _pinned_ruff()
    folder = REPOSITORY_ROOT / package.folder
    root_files = _ruff_files(ruff, ".")
    assert (
        REPOSITORY_ROOT / "scripts" / "tests" / "test_moved_packages.py" in root_files
    )
    assert not {path for path in root_files if path.is_relative_to(folder)}
    named = _ruff_files(ruff, package.folder)
    assert named and all(path.is_relative_to(folder) for path in named)


@by_package
def test_no_tracked_package_path_is_ignored_and_new_files_are_not(
    package: MovedPackage,
) -> None:
    files = _package_files(package)
    assert (
        _git("ls-files", "-ci", "--exclude-standard", "--", package.folder).stdout == ""
    )
    directories = {str(PurePosixPath(path).parent) for path in files}
    probes = "\n".join(f"{directory}/{PROBE_NAME}" for directory in sorted(directories))
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "check-ignore", "--stdin"],
        input=probes + "\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout or result.stderr


@by_package
def test_backend_test_discovery_never_reaches_the_package(
    package: MovedPackage,
) -> None:
    _package_files(package)
    backend = REPOSITORY_ROOT / "backend"
    folder = REPOSITORY_ROOT / package.folder
    ini = ConfigParser()
    ini.read(backend / "pytest.ini", encoding="utf-8")
    testpaths = ini["pytest"]["testpaths"].split()
    assert testpaths
    for testpath in testpaths:
        resolved = (backend / testpath).resolve()
        assert resolved.is_relative_to(backend.resolve())
        assert not resolved.is_relative_to(folder.resolve())
    runner = _load_script("run_backend_web_tests")
    suites = runner.suite_paths(REPOSITORY_ROOT)
    assert suites
    assert not any(
        (backend / suite).resolve().is_relative_to(folder.resolve()) for suite in suites
    )
    for cwd, suite, _name in runner.suite_commands(REPOSITORY_ROOT):
        assert not (Path(cwd) / suite).resolve().is_relative_to(folder.resolve())


@by_package
def test_the_changed_line_coverage_gate_classifies_no_package_path(
    package: MovedPackage,
) -> None:
    coverage = _load_script("check_changed_coverage")
    assert coverage.classify_path("scripts/check_changed_coverage.py") is not None
    claimed = {
        path: target.key
        for path in _package_files(package)
        if (target := coverage.classify_path(path)) is not None
    }
    assert claimed == {}


@by_package
def test_the_image_and_deep_requirements_never_take_the_package(
    package: MovedPackage,
) -> None:
    _package_files(package)
    context_rules = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    if not package.image_copies:
        assert package.folder in context_rules.splitlines()
    tracked = [path for path in _tracked() if _outside_moved_packages(path)]
    dockerfiles = [path for path in tracked if DOCKERFILE.search(path)]
    assert "Dockerfile" in dockerfiles
    for dockerfile in dockerfiles:
        for source in _copy_sources(dockerfile):
            normalized = source.strip("\"'").removeprefix("./").rstrip("/")
            assert normalized not in {"", ".", "*"}, (
                f"{dockerfile} copies the whole context"
            )
            if normalized in package.image_copies:
                continue
            assert normalized != package.folder, (
                f"{dockerfile} copies {package.folder}/"
            )
            assert not normalized.startswith(f"{package.folder}/"), (
                f"{dockerfile} copies {source}"
            )
    distribution = re.compile(re.escape(package.distribution), re.IGNORECASE)
    requirements = [path for path in tracked if REQUIREMENT_FILE.search(path)]
    assert "backend/requirements.txt" in requirements
    for path in requirements:
        if path in package.named_in:
            continue
        text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
        assert not distribution.search(text), f"{path} names {package.distribution}"


@by_package
def test_the_package_imports_no_deep_backend_package_or_astralplane(
    package: MovedPackage,
) -> None:
    files = _package_files(package)
    backend_files = _tracked("backend")
    backend_packages = {
        PurePosixPath(path).parts[1]
        for path in backend_files
        if len(PurePosixPath(path).parts) == 3 and path.endswith("/__init__.py")
    }
    backend_modules = {
        PurePosixPath(path).stem
        for path in backend_files
        if len(PurePosixPath(path).parts) == 2 and path.endswith(".py")
    }
    assert "orchestrator" in backend_packages and "shared" in backend_packages
    own_names = _top_level_names(files, package.folder) | {package.import_name}
    forbidden = (
        ({"backend", "astralplane"} | backend_packages | backend_modules)
        - own_names
        - ALLOWED_BACKEND_IMPORTS
    )
    sources = [path for path in files if path.endswith(".py")]
    assert sources
    crossings = {}
    for path in sources:
        source = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
        reached = _imported_roots(source, path) & forbidden
        if reached:
            crossings[path] = sorted(reached)
    assert crossings == {}


def test_the_kernel_binding_files_are_exactly_the_named_set() -> None:
    candidates = _git(
        "grep", "-l", "-F", "-e", KERNEL_BINDING_FORMAT, "--", "*.json", check=False
    ).stdout.splitlines()
    bindings = set()
    for path in candidates:
        declared = json.loads((REPOSITORY_ROOT / path).read_text(encoding="utf-8"))
        if (
            isinstance(declared, dict)
            and declared.get("format") == KERNEL_BINDING_FORMAT
        ):
            bindings.add(path)
    assert bindings == KERNEL_BINDINGS


@by_package
def test_the_package_github_folder_holds_exactly_the_known_files(
    package: MovedPackage,
) -> None:
    prefix = f"{package.folder}/.github"
    held = {str(PurePosixPath(path).relative_to(prefix)) for path in _tracked(prefix)}
    assert held == package.github_files
