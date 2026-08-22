#!/usr/bin/env python3
"""Build and install the exact local Astral component composition.

The root ``pyproject.toml`` is the only install-order declaration. Component
wheels are built from initialized local submodules with index access,
dependency resolution, and PEP 517 build isolation disabled. A generated lock
binds the source inputs to the exact wheel bytes; installation then accepts
only those wheels and verifies pip's PEP 610 archive digest plus required
installed package data.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import urllib.parse
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

CONTRACT_FORMAT = "astraldeep.local-components/v1"
LOCK_FORMAT = "astraldeep.component-wheel-lock/v1"
INSTALLER_FORMAT = "pip-wheel/v1"
BUILD_TOOL_REQUIREMENTS = (
    "setuptools==80.9.0",
    "wheel==0.45.1",
    "hatchling==1.27.0",
    "uv_build==0.12.3",
)
EXPECTED_INSTALL_ORDER = (
    "astral-primitives",
    "astral-projection",
    "astral-plane",
    "lets",
)
EXPECTED_IDENTITIES = {
    "astral-primitives": (
        "components/AstralPrimitives",
        "https://github.com/AstralDeep/AstralPrimitives.git",
    ),
    "astral-projection": (
        "components/AstralProjection",
        "https://github.com/AstralDeep/AstralProjection.git",
    ),
    "astral-plane": (
        "components/AstralPlane",
        "https://github.com/AstralDeep/AstralPlane.git",
    ),
    "lets": ("components/LETS", "https://github.com/AstralDeep/LETS.git"),
}

_CONTROL_KEYS = frozenset(
    {
        "format",
        "manifest",
        "installer",
        "wheel-lock-format",
        "install-order",
        "build-tools",
    }
)
_COMPONENT_FIELDS = frozenset(
    {
        "distribution",
        "version",
        "path",
        "contract",
        "availability",
        "import",
        "extras",
        "build-inputs",
        "required-wheel-paths",
    }
)
_TRANSIENT_NAMES = frozenset(
    {
        ".coverage",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
    }
)
_FORBIDDEN_BUILD_NAMES = frozenset(
    {
        ".env",
        "key.properties",
        "keystore.properties",
        "local.properties",
    }
)
_FORBIDDEN_BUILD_SUFFIXES = frozenset(
    {".db", ".jks", ".key", ".keystore", ".log", ".p12", ".pem", ".pfx", ".sqlite"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DISTRIBUTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$"
)
_DEPENDENCY_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class ComponentInstallError(RuntimeError):
    """The local component contract or an install artifact is invalid."""


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """One exact first-party package in deterministic install order."""

    key: str
    distribution: str
    version: str
    relative_path: str
    contract: str
    availability: str
    import_name: str
    extras: tuple[str, ...]
    build_inputs: tuple[str, ...]
    required_wheel_paths: tuple[str, ...]

    def source_root(self, repository_root: Path) -> Path:
        return repository_root / Path(self.relative_path)


@dataclass(frozen=True, slots=True)
class LocalContract:
    """Validated executable form of the root TOML declaration."""

    repository_root: Path
    manifest_path: Path
    components: tuple[ComponentSpec, ...]


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ComponentInstallError(f"could not read TOML {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ComponentInstallError(f"TOML root is not a table: {path}")
    return document


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ComponentInstallError(f"could not read JSON {path}: {exc}") from exc


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComponentInstallError(f"{field} must be a non-empty relative path")
    if (
        len(value) > 4096
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ComponentInstallError(f"{field} must use a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ComponentInstallError(f"{field} is not a safe relative POSIX path")
    return path.as_posix()


def _string_list(value: Any, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ComponentInstallError(f"{field} must be an array of non-empty strings")
    if not allow_empty and not value:
        raise ComponentInstallError(f"{field} must not be empty")
    if len(set(value)) != len(value):
        raise ComponentInstallError(f"{field} contains duplicate values")
    return tuple(value)


def _component_spec(key: str, value: Any) -> ComponentSpec:
    if not isinstance(value, dict):
        raise ComponentInstallError(f"component {key!r} must be a TOML table")
    if set(value) != _COMPONENT_FIELDS:
        missing = sorted(_COMPONENT_FIELDS - set(value))
        unexpected = sorted(set(value) - _COMPONENT_FIELDS)
        raise ComponentInstallError(
            f"component {key!r} has invalid fields; missing={missing}, "
            f"unexpected={unexpected}"
        )

    distribution = value["distribution"]
    version = value["version"]
    contract = value["contract"]
    availability = value["availability"]
    import_name = value["import"]
    scalar_values = {
        "distribution": distribution,
        "version": version,
        "contract": contract,
        "availability": availability,
        "import": import_name,
    }
    if any(not isinstance(item, str) or not item for item in scalar_values.values()):
        raise ComponentInstallError(f"component {key!r} has an invalid scalar field")
    if not _DISTRIBUTION_PATTERN.fullmatch(distribution):
        raise ComponentInstallError(f"component {key!r} has an invalid distribution")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ComponentInstallError(f"component {key!r} has an invalid version")
    if not import_name.isidentifier():
        raise ComponentInstallError(f"component {key!r} has an invalid import name")

    relative_path = _safe_relative_path(value["path"], field=f"{key}.path")
    path_parts = PurePosixPath(relative_path).parts
    if len(path_parts) != 2 or path_parts[0] != "components":
        raise ComponentInstallError(
            f"component {key!r} must be an immediate child of components/"
        )

    build_inputs = tuple(
        _safe_relative_path(item, field=f"{key}.build-inputs")
        for item in _string_list(value["build-inputs"], field=f"{key}.build-inputs")
    )
    required_paths = tuple(
        _safe_relative_path(item, field=f"{key}.required-wheel-paths")
        for item in _string_list(
            value["required-wheel-paths"], field=f"{key}.required-wheel-paths"
        )
    )
    return ComponentSpec(
        key=key,
        distribution=distribution,
        version=version,
        relative_path=relative_path,
        contract=contract,
        availability=availability,
        import_name=import_name,
        extras=_string_list(value["extras"], field=f"{key}.extras", allow_empty=True),
        build_inputs=build_inputs,
        required_wheel_paths=required_paths,
    )


def _manifest_mapping(manifest: Any, key: str) -> dict[str, Any]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get(key), dict):
        raise ComponentInstallError(f"composition manifest has no {key!r} mapping")
    return manifest[key]


def _validate_manifest(contract: LocalContract, manifest: Any) -> None:
    components = _manifest_mapping(manifest, "components")
    availability = _manifest_mapping(manifest, "availability")
    expected_keys = {component.key for component in contract.components}
    if set(components) != expected_keys or set(availability) != expected_keys:
        raise ComponentInstallError(
            "local component keys do not exactly match the composition manifest"
        )
    for component in contract.components:
        entry = components[component.key]
        if not isinstance(entry, dict):
            raise ComponentInstallError(
                f"manifest component {component.key!r} is not an object"
            )
        expected = {
            "path": component.relative_path,
            "contract_version": component.contract,
        }
        for field, value in expected.items():
            if entry.get(field) != value:
                raise ComponentInstallError(
                    f"manifest {component.key}.{field} does not match the local contract"
                )
        repository = entry.get("repository")
        commit = entry.get("commit")
        expected_path, expected_repository = EXPECTED_IDENTITIES[component.key]
        if (
            component.relative_path != expected_path
            or repository != expected_repository
            or not isinstance(commit, str)
            or not _SHA1_PATTERN.fullmatch(commit)
        ):
            raise ComponentInstallError(
                f"manifest identity for {component.key!r} is invalid"
            )
        if availability[component.key] != component.availability:
            raise ComponentInstallError(
                f"manifest availability for {component.key!r} does not match"
            )


def _parse_gitmodules(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.RawConfigParser(strict=True, interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ComponentInstallError(f"could not read .gitmodules: {exc}") from exc
    modules: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        values = {key.lower(): value.strip() for key, value in parser.items(section)}
        if "branch" in values:
            raise ComponentInstallError(
                f"floating branch selector is forbidden in {section!r}"
            )
        if set(values) != {"path", "url"}:
            raise ComponentInstallError(
                f".gitmodules section {section!r} must contain only path and url"
            )
        relative_path = values.get("path")
        if not relative_path or relative_path in modules:
            raise ComponentInstallError(".gitmodules contains a missing or duplicate path")
        modules[relative_path] = values
    return modules


def _gitlink(repository_root: Path, relative_path: str) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "--", relative_path],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ComponentInstallError(f"could not inspect gitlink {relative_path}") from exc
    if result.returncode != 0:
        raise ComponentInstallError(f"could not inspect gitlink {relative_path}")
    fields = result.stdout.strip().split()
    if len(fields) != 4 or fields[0] != "160000" or fields[2] != "0":
        raise ComponentInstallError(f"{relative_path} is not one exact stage-0 gitlink")
    return fields[1]


def _validate_git_declarations(
    contract: LocalContract, manifest: Any, *, require_gitlinks: bool
) -> None:
    modules = _parse_gitmodules(contract.repository_root / ".gitmodules")
    manifest_components = _manifest_mapping(manifest, "components")
    expected_paths = {component.relative_path for component in contract.components}
    if set(modules) != expected_paths:
        raise ComponentInstallError(
            ".gitmodules paths do not exactly match the local component contract"
        )
    for component in contract.components:
        manifest_entry = manifest_components[component.key]
        expected_url = manifest_entry.get("repository")
        if modules[component.relative_path].get("url") != expected_url:
            raise ComponentInstallError(
                f".gitmodules URL for {component.key!r} does not match the manifest"
            )

    has_git_metadata = (contract.repository_root / ".git").exists()
    if require_gitlinks and not has_git_metadata:
        raise ComponentInstallError("git metadata is required to validate component pins")
    if not has_git_metadata:
        return
    for component in contract.components:
        expected_commit = manifest_components[component.key].get("commit")
        if _gitlink(contract.repository_root, component.relative_path) != expected_commit:
            raise ComponentInstallError(
                f"gitlink for {component.key!r} does not match the manifest commit"
            )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ComponentInstallError(f"could not inspect build input {path}") from exc
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_flag)


def _resolved_child(root: Path, relative_path: str, *, field: str) -> Path:
    candidate = root / Path(relative_path)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ComponentInstallError(f"{field} escapes or is absent") from exc
    current = candidate
    while current != root:
        if _is_reparse_point(current):
            raise ComponentInstallError(f"{field} crosses a reparse point")
        current = current.parent
    return resolved


def _dependency_name(requirement: str) -> str | None:
    match = _DEPENDENCY_NAME_PATTERN.match(requirement)
    return _canonical_distribution(match.group(1)) if match else None


def _validate_component_sources(contract: LocalContract) -> None:
    order = {component.key: index for index, component in enumerate(contract.components)}
    distribution_owners = {
        _canonical_distribution(component.distribution): component.key
        for component in contract.components
    }
    for component in contract.components:
        source_root = _resolved_child(
            contract.repository_root,
            component.relative_path,
            field=f"{component.key}.path",
        )
        if not source_root.is_dir():
            raise ComponentInstallError(f"component {component.key!r} is not initialized")
        project = _read_toml(source_root / "pyproject.toml").get("project")
        if not isinstance(project, dict):
            raise ComponentInstallError(f"component {component.key!r} has no project table")
        if project.get("name") != component.distribution:
            raise ComponentInstallError(
                f"component {component.key!r} distribution does not match"
            )
        if project.get("version") != component.version:
            raise ComponentInstallError(f"component {component.key!r} version does not match")
        optional = project.get("optional-dependencies", {})
        if not isinstance(optional, dict) or any(extra not in optional for extra in component.extras):
            raise ComponentInstallError(
                f"component {component.key!r} does not declare every required extra"
            )

        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise ComponentInstallError(
                f"component {component.key!r} has invalid dependency metadata"
            )
        for requirement in dependencies:
            dependency_owner = distribution_owners.get(_dependency_name(requirement) or "")
            if dependency_owner is not None and order[dependency_owner] >= order[component.key]:
                raise ComponentInstallError(
                    f"install order places {component.key!r} before local dependency "
                    f"{dependency_owner!r}"
                )

        for build_input in component.build_inputs:
            _resolved_child(source_root, build_input, field=f"{component.key}.build-inputs")


def load_contract(
    repository_root: Path,
    *,
    require_sources: bool,
    require_gitlinks: bool = False,
) -> LocalContract:
    """Load and validate the root declaration without resolving dependencies."""

    repository_root = repository_root.resolve(strict=True)
    root_metadata = _read_toml(repository_root / "pyproject.toml")
    project = root_metadata.get("project")
    if not isinstance(project, dict) or project.get("dependencies") != []:
        raise ComponentInstallError(
            "root project dependencies must stay empty; first-party components use local wheels"
        )
    try:
        local = root_metadata["tool"]["astraldeep"]["local-components"]
    except (KeyError, TypeError) as exc:
        raise ComponentInstallError("root local-component contract is missing") from exc
    if not isinstance(local, dict):
        raise ComponentInstallError("root local-component contract is not a table")
    if local.get("format") != CONTRACT_FORMAT:
        raise ComponentInstallError("unsupported local-component contract format")
    if local.get("installer") != INSTALLER_FORMAT:
        raise ComponentInstallError("local-component installer contract is not exact")
    if local.get("wheel-lock-format") != LOCK_FORMAT:
        raise ComponentInstallError("local-component wheel-lock format is not exact")
    build_tools = _string_list(local.get("build-tools"), field="build-tools")
    if build_tools != BUILD_TOOL_REQUIREMENTS:
        raise ComponentInstallError("local-component build tools are not exactly pinned")

    order = _string_list(local.get("install-order"), field="install-order")
    if order != EXPECTED_INSTALL_ORDER:
        raise ComponentInstallError("install-order is not the canonical component order")
    component_values = {key: value for key, value in local.items() if key not in _CONTROL_KEYS}
    if set(order) != set(component_values) or len(order) != len(component_values):
        raise ComponentInstallError(
            "install-order must contain every local component exactly once"
        )
    components = tuple(_component_spec(key, component_values[key]) for key in order)

    manifest_relative = _safe_relative_path(local.get("manifest"), field="manifest")
    manifest_path = _resolved_child(repository_root, manifest_relative, field="manifest")
    contract = LocalContract(repository_root, manifest_path, components)
    manifest = _read_json(manifest_path)
    _validate_manifest(contract, manifest)
    _validate_git_declarations(contract, manifest, require_gitlinks=require_gitlinks)
    if require_sources:
        _validate_component_sources(contract)
    return contract


def _iter_build_files(component: ComponentSpec, repository_root: Path) -> Iterable[Path]:
    source_root = component.source_root(repository_root).resolve(strict=True)
    seen: set[str] = set()
    for declared_input in component.build_inputs:
        build_input = _resolved_child(
            source_root,
            declared_input,
            field=f"{component.key}.build-inputs",
        )
        candidates = (build_input,) if build_input.is_file() else build_input.rglob("*")
        for candidate in sorted(candidates, key=lambda item: item.as_posix()):
            relative = candidate.relative_to(source_root).as_posix()
            parts = PurePosixPath(relative).parts
            if any(part in _TRANSIENT_NAMES for part in parts) or candidate.suffix == ".pyc":
                continue
            if (
                candidate.name.casefold() in _FORBIDDEN_BUILD_NAMES
                or candidate.suffix.casefold() in _FORBIDDEN_BUILD_SUFFIXES
            ):
                raise ComponentInstallError(
                    f"sensitive/runtime file is forbidden in build inputs for {component.key!r}"
                )
            if _is_reparse_point(candidate):
                raise ComponentInstallError(
                    f"build input for {component.key!r} crosses a reparse point"
                )
            if not candidate.is_file():
                continue
            if relative in seen:
                raise ComponentInstallError(
                    f"overlapping build inputs repeat {relative!r} for {component.key!r}"
                )
            seen.add(relative)
            yield candidate
    if not seen:
        raise ComponentInstallError(f"component {component.key!r} has no build input files")


def source_digest(component: ComponentSpec, repository_root: Path) -> str:
    """Digest explicitly declared, non-transient build inputs in canonical order."""

    source_root = component.source_root(repository_root).resolve(strict=True)
    framed: list[tuple[str, bytes]] = []
    for path in _iter_build_files(component, repository_root):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ComponentInstallError(
                f"could not read build input for {component.key!r}"
            ) from exc
        framed.append((path.relative_to(source_root).as_posix(), content))
    digest = hashlib.sha256()
    for relative_path, content in sorted(framed):
        encoded_path = relative_path.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded_path)))
        digest.update(encoded_path)
        digest.update(struct.pack(">Q", len(content)))
        digest.update(content)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ComponentInstallError(f"could not digest artifact {path.name}") from exc
    return digest.hexdigest()


def _wheel_metadata(path: Path) -> tuple[str, str, frozenset[str]]:
    try:
        with zipfile.ZipFile(path) as wheel:
            names = wheel.namelist()
            if len(names) != len(set(names)):
                raise ComponentInstallError(f"wheel {path.name} has duplicate members")
            for name in names:
                pure = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                ):
                    raise ComponentInstallError(f"wheel {path.name} has an unsafe member")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ComponentInstallError(
                    f"wheel {path.name} does not contain one metadata record"
                )
            metadata = Parser().parsestr(
                wheel.read(metadata_names[0]).decode("utf-8", errors="strict")
            )
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ComponentInstallError(f"could not inspect wheel {path.name}") from exc
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ComponentInstallError(f"wheel {path.name} has incomplete metadata")
    return name, version, frozenset(names)


def _validate_wheel(path: Path, component: ComponentSpec) -> None:
    name, version, members = _wheel_metadata(path)
    if _canonical_distribution(name) != _canonical_distribution(component.distribution):
        raise ComponentInstallError(
            f"wheel {path.name} is not distribution {component.distribution!r}"
        )
    if version != component.version:
        raise ComponentInstallError(
            f"wheel {path.name} is not version {component.version!r}"
        )
    missing = sorted(set(component.required_wheel_paths) - set(members))
    if missing:
        raise ComponentInstallError(
            f"wheel for {component.key!r} is missing required package data: {missing}"
        )


def _pip_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(("PIP_", "UV_")) or key in {"PYTHONHOME", "PYTHONPATH"}:
            environment.pop(key, None)
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            # ZIP-based wheel timestamps cannot represent dates before 1980.
            "SOURCE_DATE_EPOCH": "315532800",
            "TZ": "UTC",
            "UV_NO_CONFIG": "1",
            "UV_OFFLINE": "1",
        }
    )
    executable_directory = str(Path(os.path.abspath(sys.executable)).parent)
    inherited_path = tuple(
        entry
        for entry in environment.get("PATH", "").split(os.pathsep)
        if entry and entry != executable_directory
    )
    environment["PATH"] = os.pathsep.join((executable_directory, *inherited_path))
    return environment


def _run(arguments: Sequence[str], *, cwd: Path) -> None:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=_pip_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ComponentInstallError("local component command could not execute") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise ComponentInstallError(
            f"local component command exited {result.returncode}: {detail}"
        )


def _lock_document(
    contract: LocalContract, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "format": LOCK_FORMAT,
        "installer": INSTALLER_FORMAT,
        "manifest_sha256": _sha256(contract.manifest_path),
        "components": entries,
    }


def _write_lock(path: Path, document: dict[str, Any]) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        path.write_text(payload, encoding="utf-8", newline="")
    except OSError as exc:
        raise ComponentInstallError(f"could not write component wheel lock {path}") from exc


def build_wheels(contract: LocalContract, wheel_directory: Path, lock_path: Path) -> None:
    """Build exactly one offline wheel per component and bind every digest."""

    wheel_directory = wheel_directory.resolve()
    lock_path = lock_path.resolve()
    if lock_path.parent != wheel_directory:
        raise ComponentInstallError("component wheel lock must be inside the wheel directory")
    if wheel_directory.exists():
        if not wheel_directory.is_dir() or any(wheel_directory.iterdir()):
            raise ComponentInstallError("component wheel directory must be absent or empty")
    else:
        wheel_directory.mkdir(parents=True)

    entries: list[dict[str, Any]] = []
    for component in contract.components:
        source_sha256 = source_digest(component, contract.repository_root)
        with tempfile.TemporaryDirectory(prefix=f"astral-{component.key}-wheel-") as temp:
            temporary_wheels = Path(temp)
            _run(
                (
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-deps",
                    "--no-index",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(temporary_wheels),
                    str(component.source_root(contract.repository_root)),
                ),
                cwd=contract.repository_root,
            )
            produced = tuple(sorted(temporary_wheels.glob("*.whl")))
            if len(produced) != 1:
                raise ComponentInstallError(
                    f"build for {component.key!r} produced {len(produced)} wheels"
                )
            _validate_wheel(produced[0], component)
            destination = wheel_directory / produced[0].name
            if destination.exists():
                raise ComponentInstallError(f"duplicate wheel filename {destination.name}")
            shutil.copyfile(produced[0], destination)

        if source_digest(component, contract.repository_root) != source_sha256:
            raise ComponentInstallError(
                f"wheel build mutated declared source inputs for {component.key!r}"
            )

        entries.append(
            {
                "component": component.key,
                "distribution": component.distribution,
                "version": component.version,
                "source_path": component.relative_path,
                "source_sha256": source_sha256,
                "wheel": destination.name,
                "wheel_sha256": _sha256(destination),
            }
        )
    _write_lock(lock_path, _lock_document(contract, entries))


def _load_lock(contract: LocalContract, lock_path: Path) -> tuple[dict[str, Any], ...]:
    document = _read_json(lock_path)
    if not isinstance(document, dict) or set(document) != {
        "format",
        "installer",
        "manifest_sha256",
        "components",
    }:
        raise ComponentInstallError("component wheel lock has invalid top-level fields")
    if document["format"] != LOCK_FORMAT or document["installer"] != INSTALLER_FORMAT:
        raise ComponentInstallError("component wheel lock has an unsupported format")
    if (
        not isinstance(document["manifest_sha256"], str)
        or not _SHA256_PATTERN.fullmatch(document["manifest_sha256"])
        or document["manifest_sha256"] != _sha256(contract.manifest_path)
    ):
        raise ComponentInstallError("component wheel lock does not match the manifest digest")
    entries = document["components"]
    if not isinstance(entries, list) or len(entries) != len(contract.components):
        raise ComponentInstallError("component wheel lock has the wrong component count")

    validated: list[dict[str, Any]] = []
    expected_fields = {
        "component",
        "distribution",
        "version",
        "source_path",
        "source_sha256",
        "wheel",
        "wheel_sha256",
    }
    for component, entry in zip(contract.components, entries, strict=True):
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ComponentInstallError(
                f"wheel lock entry for {component.key!r} has invalid fields"
            )
        expected = {
            "component": component.key,
            "distribution": component.distribution,
            "version": component.version,
            "source_path": component.relative_path,
        }
        if any(entry[field] != value for field, value in expected.items()):
            raise ComponentInstallError(
                f"wheel lock entry for {component.key!r} does not match the contract"
            )
        if (
            not isinstance(entry["wheel"], str)
            or not entry["wheel"]
            or any(token in entry["wheel"] for token in ("/", "\\", ":"))
            or Path(entry["wheel"]).name != entry["wheel"]
        ):
            raise ComponentInstallError(f"wheel lock path for {component.key!r} is unsafe")
        if not entry["wheel"].endswith(".whl"):
            raise ComponentInstallError(f"wheel lock path for {component.key!r} is not a wheel")
        for digest_field in ("source_sha256", "wheel_sha256"):
            if not isinstance(entry[digest_field], str) or not _SHA256_PATTERN.fullmatch(
                entry[digest_field]
            ):
                raise ComponentInstallError(
                    f"wheel lock {digest_field} for {component.key!r} is invalid"
                )
        validated.append(entry)
    return tuple(validated)


def install_wheels(contract: LocalContract, lock_path: Path) -> None:
    """Install only digest-locked local wheels, without resolving dependencies."""

    entries = _load_lock(contract, lock_path)
    for component, entry in zip(contract.components, entries, strict=True):
        source_root = component.source_root(contract.repository_root)
        if source_root.is_dir() and (
            source_digest(component, contract.repository_root) != entry["source_sha256"]
        ):
            raise ComponentInstallError(
                f"source digest for {component.key!r} does not match the wheel lock"
            )
        wheel = lock_path.parent / entry["wheel"]
        if not wheel.is_file() or _sha256(wheel) != entry["wheel_sha256"]:
            raise ComponentInstallError(
                f"wheel digest for {component.key!r} does not match the lock"
            )
        _validate_wheel(wheel, component)
        _run(
            (
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-deps",
                "--no-index",
                "--force-reinstall",
                str(wheel.resolve()),
            ),
            cwd=contract.repository_root,
        )


def _direct_url_digest(distribution: importlib.metadata.Distribution) -> tuple[str, str]:
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise ComponentInstallError(
            f"installed {distribution.metadata['Name']!r} has no direct local archive record"
        )
    try:
        document = json.loads(raw)
        archive = document["archive_info"]
        url = document["url"]
        digest = archive.get("hashes", {}).get("sha256")
        if digest is None:
            legacy = archive.get("hash", "")
            digest = legacy.removeprefix("sha256=") if legacy.startswith("sha256=") else None
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ComponentInstallError("installed direct URL metadata is malformed") from exc
    if not isinstance(url, str) or not isinstance(digest, str):
        raise ComponentInstallError("installed direct URL metadata is incomplete")
    return url, digest


def _record_digest(path: Path) -> str:
    raw = hashlib.sha256(path.read_bytes()).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _verify_installed_files(
    distribution: importlib.metadata.Distribution, component: ComponentSpec
) -> None:
    files = distribution.files
    if files is None:
        raise ComponentInstallError(
            f"installed {component.distribution!r} has no RECORD inventory"
        )
    by_path = {PurePosixPath(str(item)).as_posix(): item for item in files}
    missing = sorted(set(component.required_wheel_paths) - set(by_path))
    if missing:
        raise ComponentInstallError(
            f"installed {component.key!r} is missing required package data: {missing}"
        )
    for relative_path in component.required_wheel_paths:
        package_path = by_path[relative_path]
        if package_path.hash is None or package_path.hash.mode != "sha256":
            raise ComponentInstallError(
                f"installed package data {relative_path!r} is not RECORD-bound"
            )
        installed_path = distribution.locate_file(package_path)
        if not installed_path.is_file() or _record_digest(installed_path) != package_path.hash.value:
            raise ComponentInstallError(
                f"installed package data {relative_path!r} failed RECORD verification"
            )


def _verify_import_origin(component: ComponentSpec) -> None:
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(component.import_name)
    except Exception as exc:
        raise ComponentInstallError(
            f"installed import {component.import_name!r} could not be loaded "
            f"({type(exc).__name__})"
        ) from exc
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str) or not module_file:
        raise ComponentInstallError(f"installed import {component.import_name!r} is unavailable")
    origin = Path(module_file).resolve()
    install_roots = {
        Path(value).resolve()
        for key, value in sysconfig.get_paths().items()
        if key in {"platlib", "purelib"} and value
    }
    if not any(origin == root or root in origin.parents for root in install_roots):
        raise ComponentInstallError(
            f"import {component.import_name!r} did not resolve from installed package data"
        )


def verify_install(contract: LocalContract, lock_path: Path) -> None:
    """Verify installed distributions remain bound to the generated wheel lock."""

    entries = _load_lock(contract, lock_path)
    for component, entry in zip(contract.components, entries, strict=True):
        source_root = component.source_root(contract.repository_root)
        if source_root.is_dir() and (
            source_digest(component, contract.repository_root) != entry["source_sha256"]
        ):
            raise ComponentInstallError(
                f"source digest for {component.key!r} does not match the wheel lock"
            )
        try:
            distribution = importlib.metadata.distribution(component.distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ComponentInstallError(
                f"distribution {component.distribution!r} is not installed"
            ) from exc
        if distribution.version != component.version:
            raise ComponentInstallError(
                f"installed {component.distribution!r} has the wrong version"
            )
        installed_name = distribution.metadata.get("Name", "")
        if _canonical_distribution(installed_name) != _canonical_distribution(
            component.distribution
        ):
            raise ComponentInstallError(
                f"installed metadata for {component.key!r} has the wrong name"
            )
        url, digest = _direct_url_digest(distribution)
        parsed_url = urllib.parse.urlparse(url)
        wheel_name = Path(urllib.parse.unquote(parsed_url.path)).name
        if (
            parsed_url.scheme != "file"
            or parsed_url.netloc not in {"", "localhost"}
            or wheel_name != entry["wheel"]
        ):
            raise ComponentInstallError(
                f"installed {component.key!r} did not originate from its locked wheel"
            )
        if digest != entry["wheel_sha256"]:
            raise ComponentInstallError(
                f"installed archive digest for {component.key!r} does not match the lock"
            )
        _verify_installed_files(distribution, component)
        _verify_import_origin(component)


def pip_check(repository_root: Path) -> None:
    """Require the preinstalled runtime dependency closure to be complete."""

    _run(
        (sys.executable, "-m", "pip", "--disable-pip-version-check", "check"),
        cwd=repository_root,
    )


def sync_components(contract: LocalContract) -> None:
    """Build, install, and verify through an ephemeral digest lock."""

    with tempfile.TemporaryDirectory(prefix="astral-component-wheels-") as temp:
        wheel_directory = Path(temp)
        lock_path = wheel_directory / "astral-component-wheels.lock.json"
        build_wheels(contract, wheel_directory, lock_path)
        install_wheels(contract, lock_path)
        verify_install(contract, lock_path)
        pip_check(contract.repository_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate the local contract")
    validate.add_argument("--root", type=Path, default=Path.cwd())
    validate.add_argument(
        "--declarations-only",
        action="store_true",
        help="validate manifest/.gitmodules/gitlinks without initialized sources",
    )
    validate.add_argument(
        "--require-gitlinks",
        action="store_true",
        help="fail when parent Git metadata is unavailable",
    )

    build = subparsers.add_parser("build", help="build digest-locked local wheels")
    build.add_argument("--root", type=Path, default=Path.cwd())
    build.add_argument("--wheel-dir", type=Path, required=True)
    build.add_argument("--lock", type=Path)

    install = subparsers.add_parser("install", help="install digest-locked wheels")
    install.add_argument("--root", type=Path, default=Path.cwd())
    install.add_argument("--lock", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify installed wheel provenance")
    verify.add_argument("--root", type=Path, default=Path.cwd())
    verify.add_argument("--lock", type=Path, required=True)

    sync = subparsers.add_parser("sync", help="build, install, and verify components")
    sync.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        require_sources = arguments.command in {"build", "sync"} or (
            arguments.command == "validate" and not arguments.declarations_only
        )
        require_gitlinks = bool(
            arguments.command == "validate" and arguments.require_gitlinks
        )
        contract = load_contract(
            arguments.root,
            require_sources=require_sources,
            require_gitlinks=require_gitlinks,
        )
        if arguments.command == "validate":
            qualifier = "declarations" if arguments.declarations_only else "initialized sources"
            print(
                f"local component contract valid: {len(contract.components)} exact {qualifier}"
            )
        elif arguments.command == "build":
            wheel_directory = arguments.wheel_dir.resolve()
            lock_path = (
                arguments.lock.resolve()
                if arguments.lock is not None
                else wheel_directory / "astral-component-wheels.lock.json"
            )
            build_wheels(contract, wheel_directory, lock_path)
            print(f"built {len(contract.components)} digest-locked local component wheels")
        elif arguments.command == "install":
            install_wheels(contract, arguments.lock.resolve(strict=True))
            print(f"installed {len(contract.components)} exact local component wheels")
        elif arguments.command == "verify":
            verify_install(contract, arguments.lock.resolve(strict=True))
            print(f"verified {len(contract.components)} digest-bound component installs")
        else:
            sync_components(contract)
            print(f"synchronized {len(contract.components)} exact local components")
    except (ComponentInstallError, OSError) as exc:
        print(f"component-install: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
