#!/usr/bin/env python3
"""Decide whether an extracted UI vocabulary needs AstralPrimitives changes.

The check reads source and contract metadata only; it does not import the
package under review. Every UI-protocol type must be either a public primitive
in the requested AstralPrimitives release or an explicitly bounded,
Projection-owned system component.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

CONTRACT = "astral.primitive-coverage/v1"
MINIMUM_VERSION = "0.3.0"

# These are not agent-authored AstralPrimitives. They are deterministic,
# Projection-owned system/chrome states with separate render/adaptation tests.
PROJECTION_LOCAL_TYPES = {
    "download_card": "Projection-owned verified desktop-release chrome",
    "generative": "Projection-owned bounded grammar; never raw model HTML",
    "skeleton": "Projection-owned transient loading state",
}


class CoverageError(RuntimeError):
    """The vocabulary decision could not be made safely."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--primitives-root", required=True, type=Path)
    parser.add_argument("--minimum-version", default=MINIMUM_VERSION)
    return parser


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverageError(f"could not read JSON {path}: {exc}") from exc


def _read_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CoverageError(f"could not read TOML {path}: {exc}") from exc


def _strict_string_set(value: object, *, label: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise CoverageError(f"{label} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise CoverageError(f"{label} must contain non-empty strings only")
    if len(value) != len(set(value)):
        raise CoverageError(f"{label} contains duplicate values")
    return set(value)


_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _version_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    match = _VERSION_RE.fullmatch(value)
    if not match:
        raise CoverageError(f"version is not strict SemVer: {value!r}")
    identifiers: list[tuple[int, int | str]] = []
    prerelease = match.group("prerelease")
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    raise CoverageError(f"version is not strict SemVer: {value!r}")
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
    # Releases sort after prereleases. Build metadata is intentionally omitted
    # because SemVer declares it irrelevant to precedence.
    release_precedence = 1 if prerelease is None else 0
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        release_precedence,
        tuple(identifiers),
    )


def _package_version(primitives_root: Path) -> str:
    document = _read_toml(primitives_root / "pyproject.toml")
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != "astralprims":
        raise CoverageError(
            "primitives pyproject does not declare project 'astralprims'"
        )
    version = project.get("version")
    if not isinstance(version, str):
        raise CoverageError("astralprims project version is missing")
    return version


def _source_primitive_types(primitives_root: Path) -> set[str]:
    source = primitives_root / "src" / "astralprims" / "primitives.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise CoverageError(f"could not parse primitive definitions: {exc}") from exc

    discovered: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "type"
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            type_name = statement.value.value
            if type_name in discovered:
                raise CoverageError(
                    f"duplicate primitive type declaration: {type_name}"
                )
            discovered.add(type_name)
    if not discovered:
        raise CoverageError("no primitive type declarations were discovered")
    return discovered


def build_inventory(
    manifest_path: Path,
    primitives_root: Path,
    minimum_version: str = MINIMUM_VERSION,
) -> dict[str, object]:
    root = primitives_root.resolve(strict=True)
    if not root.is_dir():
        raise CoverageError(f"primitives root is not a directory: {root}")

    manifest = _read_json(manifest_path.resolve(strict=True))
    if not isinstance(manifest, dict):
        raise CoverageError("UI protocol manifest must be an object")
    manifest_types = _strict_string_set(
        manifest.get("component_types"), label="component_types"
    )

    version = _package_version(root)
    if _version_key(version) < _version_key(minimum_version):
        raise CoverageError(
            f"astralprims {version} is below required floor {minimum_version}"
        )

    primitive_types = _source_primitive_types(root)
    projection_types = manifest_types & PROJECTION_LOCAL_TYPES.keys()
    unknown_types = manifest_types - primitive_types - PROJECTION_LOCAL_TYPES.keys()
    missing_manifest_types = primitive_types - manifest_types

    return {
        "format": CONTRACT,
        "package": "astralprims",
        "packageVersion": version,
        "minimumVersion": minimum_version,
        "manifestVersion": manifest.get("version"),
        "primitiveTypes": sorted(manifest_types & primitive_types),
        "projectionLocalTypes": [
            {"type": name, "reason": PROJECTION_LOCAL_TYPES[name]}
            for name in sorted(projection_types)
        ],
        "unknownTypes": sorted(unknown_types),
        "missingManifestTypes": sorted(missing_manifest_types),
        "decision": (
            "reuse-existing-vocabulary"
            if not unknown_types and not missing_manifest_types
            else "astralprimitives-change-required"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inventory = build_inventory(
            args.manifest, args.primitives_root, args.minimum_version
        )
    except (CoverageError, OSError) as exc:
        print(f"verify_primitive_coverage: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0 if inventory["decision"] == "reuse-existing-vocabulary" else 1


if __name__ == "__main__":
    raise SystemExit(main())
