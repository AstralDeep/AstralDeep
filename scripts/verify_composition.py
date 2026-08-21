#!/usr/bin/env python3
"""Verify an AstralDeep composition entirely from local, pinned inputs.

The verifier intentionally never fetches, clones, or queries a remote.  It
checks the superproject index, initialized component worktrees, static package
exports, and deterministic contract digests.  A checkout without access to a
private component therefore fails with an actionable access diagnostic instead
of attempting an interactive credential flow.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

CONTRACT = "astral.composition-verification/v1"
SCHEMA_RELATIVE_PATH = Path(
    "specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json"
)

COMPONENT_ORDER = (
    "astral-projection",
    "astral-plane",
    "astral-primitives",
    "lets",
)
PRIVATE_COMPONENTS = frozenset({"astral-projection", "astral-plane"})
EXPECTED_MODULE_NAMES = {
    "astral-projection": "AstralProjection",
    "astral-plane": "AstralPlane",
    "astral-primitives": "AstralPrimitives",
    "lets": "LETS",
}
EXPECTED_ASTRAL_TOOL_SCOPES = frozenset(
    {
        "tools:execute",
        "tools:files",
        "tools:read",
        "tools:search",
        "tools:system",
        "tools:write",
    }
)
LETS_PUBLIC_EXPORTS = {
    "LETSClient": Path("src/lets/client.py"),
    "ReplicaAuthorizer": Path("src/lets/integrations/__init__.py"),
    "AstralDeepAuthorizer": Path("src/lets/integrations/__init__.py"),
    "Receipt": Path("src/lets/models.py"),
    "ReceiptVerifier": Path("src/lets/executor.py"),
}

_SECTION_PATTERN = re.compile(r'^submodule "(?P<name>[^"\r\n]+)"$')
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LITERAL_SEQUENCE_ITEMS = 4096


class CompositionError(RuntimeError):
    """A local composition input could not be interpreted safely."""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One stable, machine-readable composition failure."""

    code: str
    component: str | None
    message: str
    remediation: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Deterministic result returned by :func:`verify_composition`."""

    manifest_sha256: str | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": CONTRACT,
            "ok": self.ok,
            "manifestSha256": self.manifest_sha256,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


def _diagnostic(
    diagnostics: list[Diagnostic],
    code: str,
    message: str,
    remediation: str,
    *,
    component: str | None = None,
) -> None:
    diagnostics.append(
        Diagnostic(
            code=code,
            component=component,
            message=message,
            remediation=remediation,
        )
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CompositionError(f"could not read JSON {path}: {exc}") from exc


def _read_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CompositionError(f"could not read TOML {path}: {exc}") from exc


def _json_type_matches(instance: object, declared: str) -> bool:
    return {
        "null": instance is None,
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
    }.get(declared, False)


def _resolve_local_ref(root_schema: dict[str, object], reference: str) -> object:
    if not reference.startswith("#/"):
        raise CompositionError(f"schema uses non-local reference {reference!r}")
    current: object = root_schema
    for encoded in reference[2:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise CompositionError(
                f"schema reference cannot be resolved: {reference!r}"
            )
        current = current[token]
    return current


def _schema_errors(
    instance: object,
    schema: object,
    root_schema: dict[str, object],
    *,
    path: str = "$",
) -> list[str]:
    """Validate the assertion vocabulary used by the committed Draft 2020-12 schema."""

    if not isinstance(schema, dict):
        raise CompositionError(f"schema at {path} is not an object")
    errors: list[str] = []

    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise CompositionError(f"schema reference at {path} is not a string")
        errors.extend(
            _schema_errors(
                instance,
                _resolve_local_ref(root_schema, reference),
                root_schema,
                path=path,
            )
        )

    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list):
            raise CompositionError(f"allOf at {path} is not an array")
        for child in all_of:
            errors.extend(_schema_errors(instance, child, root_schema, path=path))

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise CompositionError(f"anyOf at {path} is not a non-empty array")
        candidates = [
            _schema_errors(instance, child, root_schema, path=path) for child in any_of
        ]
        if all(candidate for candidate in candidates):
            errors.append(f"{path}: value does not match any permitted schema")

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not equal the required constant")

    declared_type = schema.get("type")
    if declared_type is not None:
        declared_types = (
            [declared_type] if isinstance(declared_type, str) else declared_type
        )
        if not (
            isinstance(declared_types, list)
            and declared_types
            and all(isinstance(item, str) for item in declared_types)
        ):
            raise CompositionError(f"type at {path} is invalid")
        if not any(_json_type_matches(instance, item) for item in declared_types):
            errors.append(f"{path}: value has the wrong JSON type")
            return errors

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise CompositionError(f"properties at {path} is not an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise CompositionError(f"required at {path} is invalid")
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                errors.append(f"{path}: additional property {key!r} is forbidden")
        for key in sorted(set(instance) & set(properties)):
            errors.extend(
                _schema_errors(
                    instance[key],
                    properties[key],
                    root_schema,
                    path=f"{path}.{key}",
                )
            )

    if isinstance(instance, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(instance) < minimum:
            errors.append(f"{path}: string is shorter than minLength")
        if isinstance(maximum, int) and len(instance) > maximum:
            errors.append(f"{path}: string is longer than maxLength")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise CompositionError(f"pattern at {path} is not a string")
            try:
                matches = re.search(pattern, instance) is not None
            except re.error as exc:
                raise CompositionError(f"pattern at {path} is invalid: {exc}") from exc
            if not matches:
                errors.append(f"{path}: string does not match the required pattern")

    return errors


def _validate_manifest_schema(manifest: object, schema: object) -> list[str]:
    if not isinstance(schema, dict):
        raise CompositionError("composition schema root must be an object")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise CompositionError(
            "composition schema must declare JSON Schema Draft 2020-12"
        )
    return sorted(set(_schema_errors(manifest, schema, schema)))


def _parse_gitmodules(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.RawConfigParser(strict=True, interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise CompositionError(f"could not read .gitmodules: {exc}") from exc

    modules: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        match = _SECTION_PATTERN.fullmatch(section)
        if match is None:
            raise CompositionError(f"invalid .gitmodules section {section!r}")
        values = {key.lower(): value.strip() for key, value in parser.items(section)}
        values["name"] = match.group("name")
        module_path = values.get("path")
        if not module_path:
            raise CompositionError(f".gitmodules section {section!r} has no path")
        if module_path in modules:
            raise CompositionError(f"duplicate .gitmodules path {module_path!r}")
        modules[module_path] = values
    return modules


def _run_git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    forbidden = {"clone", "fetch", "ls-remote", "pull", "push", "remote-ext"}
    if any(argument in forbidden for argument in arguments):
        raise CompositionError(
            "network-capable Git commands are forbidden during verification"
        )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompositionError(f"local Git command failed to execute: {exc}") from exc


def _git_output(cwd: Path, *arguments: str) -> str:
    result = _run_git(cwd, *arguments)
    if result.returncode != 0:
        raise CompositionError("local Git metadata is unavailable")
    return result.stdout.strip()


def _gitlink(root: Path, relative_path: str) -> tuple[str, str] | None:
    output = _git_output(root, "ls-files", "--stage", "--", relative_path)
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1 or "\t" not in lines[0]:
        raise CompositionError(f"ambiguous index entry for {relative_path}")
    metadata, indexed_path = lines[0].split("\t", 1)
    fields = metadata.split()
    if len(fields) != 3 or indexed_path.replace("\\", "/") != relative_path:
        raise CompositionError(f"malformed index entry for {relative_path}")
    mode, commit, stage = fields
    if stage != "0":
        raise CompositionError(f"unmerged index entry for {relative_path}")
    return mode, commit


def _parse_python(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise CompositionError(
            f"could not parse Python contract source {path}: {exc}"
        ) from exc


def _safe_sequence_items(
    nodes: list[ast.expr], values: dict[str, object]
) -> list[object]:
    items: list[object] = []
    for node in nodes:
        if isinstance(node, ast.Starred):
            expanded = _safe_literal(node.value, values)
            if not isinstance(expanded, (tuple, list)):
                raise CompositionError(
                    "contract source starred expansion is not a literal sequence"
                )
            if len(items) + len(expanded) > _MAX_LITERAL_SEQUENCE_ITEMS:
                raise CompositionError(
                    "contract source literal sequence exceeds the bounded item limit"
                )
            items.extend(expanded)
        else:
            if len(items) >= _MAX_LITERAL_SEQUENCE_ITEMS:
                raise CompositionError(
                    "contract source literal sequence exceeds the bounded item limit"
                )
            items.append(_safe_literal(node, values))
    return items


def _safe_literal(node: ast.AST, values: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_safe_sequence_items(node.elts, values))
    if isinstance(node, ast.List):
        return _safe_sequence_items(node.elts, values)
    if isinstance(node, ast.Subscript):
        sequence = _safe_literal(node.value, values)
        index = _safe_literal(node.slice, values)
        if (
            not isinstance(sequence, (tuple, list))
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            raise CompositionError(
                "contract source subscript is not a literal sequence index"
            )
        try:
            return sequence[index]
        except IndexError as exc:
            raise CompositionError(
                "contract source literal sequence index is out of bounds"
            ) from exc
    if isinstance(node, ast.Set):
        return {_safe_literal(item, values) for item in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _safe_literal(key, values): _safe_literal(value, values)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        return frozenset(_safe_literal(node.args[0], values))  # type: ignore[arg-type]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_statements_checksum"
        and len(node.args) == 1
        and not node.keywords
    ):
        statements = _safe_literal(node.args[0], values)
        if not isinstance(statements, tuple) or any(
            not isinstance(item, str) for item in statements
        ):
            raise CompositionError(
                "contract source checksum input is not a literal string tuple"
            )
        canonical = json.dumps(
            statements,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
        and node.func.attr == "dumps"
        and len(node.args) == 1
    ):
        keyword_nodes: dict[str, ast.expr] = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                raise CompositionError(
                    "contract source JSON serialization does not allow keyword expansion"
                )
            if keyword.arg in keyword_nodes:
                raise CompositionError(
                    "contract source JSON serialization keywords are ambiguous"
                )
            keyword_nodes[keyword.arg] = keyword.value
        keywords = {
            name: _safe_literal(value, values) for name, value in keyword_nodes.items()
        }
        if keywords != {"ensure_ascii": True, "separators": (",", ":")}:
            raise CompositionError(
                "contract source JSON serialization is not the reviewed canonical form"
            )
        try:
            return json.dumps(
                _safe_literal(node.args[0], values),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise CompositionError(
                "contract source canonical JSON serialization failed"
            ) from exc
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strip"
        and not node.args
        and not node.keywords
    ):
        value = _safe_literal(node.func.value, values)
        if isinstance(value, str):
            return value.strip()
    raise CompositionError(
        "contract source contains a non-literal compatibility declaration"
    )


def _assignment_target(statement: ast.stmt) -> tuple[str, ast.AST] | None:
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        if statement.value is not None:
            return statement.target.id, statement.value
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id, statement.value
    return None


def _literal_assignments(path: Path) -> dict[str, object]:
    tree = _parse_python(path)
    values: dict[str, object] = {}
    pending: list[tuple[str, ast.AST]] = []
    for statement in tree.body:
        target = _assignment_target(statement)
        if target is not None:
            pending.append(target)
    while pending:
        remaining: list[tuple[str, ast.AST]] = []
        changed = False
        for name, node in pending:
            try:
                values[name] = _safe_literal(node, values)
            except CompositionError:
                remaining.append((name, node))
            else:
                changed = True
        if not changed:
            break
        pending = remaining
    return values


def _public_symbols(path: Path) -> set[str]:
    tree = _parse_python(path)
    discovered: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            discovered.add(statement.name)
        elif isinstance(statement, ast.ImportFrom):
            discovered.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, ast.Import):
            discovered.update(
                alias.asname or alias.name.split(".", 1)[0] for alias in statement.names
            )
    declared = _literal_assignments(path).get("__all__")
    if declared is not None:
        if not isinstance(declared, (tuple, list)) or any(
            not isinstance(item, str) for item in declared
        ):
            raise CompositionError(f"invalid __all__ declaration in {path}")
        return discovered & set(declared)
    return {name for name in discovered if not name.startswith("_")}


def _class_constant(path: Path, class_name: str, field: str) -> object:
    tree = _parse_python(path)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef) and statement.name == class_name:
            values: dict[str, object] = {}
            for child in statement.body:
                target = _assignment_target(child)
                if target is None:
                    continue
                name, value = target
                try:
                    values[name] = _safe_literal(value, values)
                except CompositionError:
                    continue
            if field in values:
                return values[field]
            break
    raise CompositionError(f"{class_name}.{field} is not a literal public contract")


def _project_metadata(
    component_root: Path, expected_name: str
) -> tuple[str, dict[str, object]]:
    document = _read_toml(component_root / "pyproject.toml")
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != expected_name:
        raise CompositionError(f"pyproject does not declare project {expected_name!r}")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise CompositionError(f"project {expected_name!r} has no version")
    return version, project


def _canonical_json_sha256(document: object) -> str:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CompositionError(f"could not canonicalize JSON contract: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def compute_primitives_digest(component_root: Path) -> str:
    """Apply the exact uint32be-path/uint64be-content framing from the schema."""

    source_root = component_root / "src" / "astralprims"
    files = sorted(
        source_root.glob("*.py"),
        key=lambda path: path.relative_to(component_root).as_posix().encode("utf-8"),
    )
    if not files:
        raise CompositionError(
            "AstralPrimitives has no src/astralprims/*.py contract files"
        )
    digest = hashlib.sha256()
    for path in files:
        relative_bytes = path.relative_to(component_root).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
            digest.update(struct.pack(">I", len(relative_bytes)))
            digest.update(relative_bytes)
            digest.update(struct.pack(">Q", len(content)))
            digest.update(content)
        except (OSError, OverflowError, struct.error) as exc:
            raise CompositionError(
                f"could not frame AstralPrimitives contract: {exc}"
            ) from exc
    return digest.hexdigest()


def _plane_migration_digest(component_root: Path) -> str:
    path = component_root / "src" / "astralplane" / "database" / "migrations.py"
    tree = _parse_python(path)
    literals = _literal_assignments(path)
    migrations: dict[str, dict[str, object]] = {}
    registry_names: tuple[str, ...] | None = None
    registry_verifier_checksums: tuple[str | None, str | None] = (None, None)

    for statement in tree.body:
        target = _assignment_target(statement)
        if target is None:
            continue
        name, node = target
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "Migration":
            if node.args:
                raise CompositionError(
                    "Plane migration declaration must use exact keyword fields"
                )
            keywords: dict[str, ast.expr] = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise CompositionError(
                        "Plane migration declaration does not allow keyword expansion"
                    )
                if keyword.arg in keywords:
                    raise CompositionError(
                        "Plane migration declaration keywords are ambiguous"
                    )
                keywords[keyword.arg] = keyword.value
            required = {
                "name",
                "source_revisions",
                "target_revision",
                "checksum",
                "operation",
            }
            unsupported = set(keywords) - required
            if unsupported:
                raise CompositionError(
                    "Plane migration declaration has unsupported keywords"
                )
            if set(keywords) != required:
                raise CompositionError("Plane migration declaration is incomplete")
            if not isinstance(keywords["operation"], ast.Name):
                raise CompositionError(
                    "Plane migration operation is not a static symbol"
                )
            checksum_node = keywords["checksum"]
            if not (
                isinstance(checksum_node, ast.Call)
                and isinstance(checksum_node.func, ast.Name)
                and checksum_node.func.id == "_statements_checksum"
                and len(checksum_node.args) == 1
                and not checksum_node.keywords
            ):
                raise CompositionError(
                    "Plane migration checksum is not derived from statements"
                )
            statements = _safe_literal(checksum_node.args[0], literals)
            if not isinstance(statements, tuple) or any(
                not isinstance(item, str) for item in statements
            ):
                raise CompositionError(
                    "Plane migration statements are not a string tuple"
                )
            statement_bytes = json.dumps(
                statements,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            migrations[name] = {
                "checksum": hashlib.sha256(statement_bytes).hexdigest(),
                "name": _safe_literal(keywords["name"], literals),
                "source_revisions": _safe_literal(
                    keywords["source_revisions"], literals
                ),
                "target_revision": _safe_literal(keywords["target_revision"], literals),
            }
        elif node.func.id == "MigrationRegistry" and len(node.args) == 1:
            entries = node.args[0]
            if not isinstance(entries, ast.Tuple) or any(
                not isinstance(item, ast.Name) for item in entries.elts
            ):
                raise CompositionError(
                    "Plane migration registry is not an explicit tuple"
                )
            registry_names = tuple(item.id for item in entries.elts)  # type: ignore[union-attr]
            keyword_nodes: dict[str, ast.expr] = {}
            for keyword in node.keywords:
                if keyword.arg is None or keyword.arg in keyword_nodes:
                    raise CompositionError(
                        "Plane migration registry keywords are ambiguous"
                    )
                keyword_nodes[keyword.arg] = keyword.value
            allowed_keywords = {
                "current_schema_verifier",
                "current_schema_verifier_checksum",
                "predecessor_schema_verifier",
                "predecessor_schema_verifier_checksum",
            }
            if not set(keyword_nodes).issubset(allowed_keywords):
                raise CompositionError(
                    "Plane migration registry has unsupported keywords"
                )
            checksum_values: list[str | None] = []
            for verifier_name, checksum_name in (
                ("current_schema_verifier", "current_schema_verifier_checksum"),
                (
                    "predecessor_schema_verifier",
                    "predecessor_schema_verifier_checksum",
                ),
            ):
                if (verifier_name in keyword_nodes) != (checksum_name in keyword_nodes):
                    raise CompositionError(
                        "Plane migration registry verifier declaration is incomplete"
                    )
                if verifier_name not in keyword_nodes:
                    checksum_values.append(None)
                    continue
                if not isinstance(keyword_nodes[verifier_name], ast.Name):
                    raise CompositionError(
                        "Plane migration registry verifier is not a static symbol"
                    )
                checksum = _safe_literal(keyword_nodes[checksum_name], literals)
                if not isinstance(checksum, str) or not _SHA256_PATTERN.fullmatch(
                    checksum
                ):
                    raise CompositionError(
                        "Plane migration registry verifier checksum is invalid"
                    )
                checksum_values.append(checksum)
            registry_verifier_checksums = (
                checksum_values[0],
                checksum_values[1],
            )

    if registry_names is None or not registry_names:
        raise CompositionError("Plane migration registry declaration is missing")
    if len(registry_names) != len(set(registry_names)):
        raise CompositionError(
            "Plane migration registry contains duplicate declarations"
        )
    if set(registry_names) != set(migrations):
        raise CompositionError("Plane migration registry and declarations disagree")

    manifest: list[dict[str, object]] = []
    for declaration_name in registry_names:
        migration = migrations[declaration_name]
        sources = migration["source_revisions"]
        if not isinstance(sources, tuple) or any(
            source is not None and not isinstance(source, str) for source in sources
        ):
            raise CompositionError("Plane migration source revisions are invalid")
        manifest.append(
            {
                "checksum": migration["checksum"],
                "name": migration["name"],
                "source_revisions": [
                    "<empty>" if source is None else source for source in sources
                ],
                "target_revision": migration["target_revision"],
            }
        )
    manifest.sort(key=lambda item: str(item["name"]))
    current_checksum, predecessor_checksum = registry_verifier_checksums
    if current_checksum is not None:
        manifest.append(
            {
                "checksum": current_checksum,
                "name": "@current-schema-verifier",
                "source_revisions": [],
                "target_revision": "@current",
            }
        )
    if predecessor_checksum is not None:
        manifest.append(
            {
                "checksum": predecessor_checksum,
                "name": "@predecessor-schema-verifier",
                "source_revisions": [],
                "target_revision": "@predecessor",
            }
        )
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _compare_contract(
    diagnostics: list[Diagnostic],
    component: str,
    field: str,
    declared: object,
    observed: object,
) -> None:
    if declared == observed:
        return
    _diagnostic(
        diagnostics,
        "E_INCOMPATIBLE_CONTRACT",
        f"{field} does not match the initialized component export or digest",
        "Select a compatible component commit or update the composition manifest deliberately.",
        component=component,
    )


def _verify_projection(
    component_root: Path,
    component: dict[str, object],
    compatibility: dict[str, object],
    diagnostics: list[Diagnostic],
) -> None:
    exports = _literal_assignments(component_root / "src/astralprojection/__init__.py")
    protocol = _read_json(component_root / "contracts/ui_protocol.json")
    if not isinstance(protocol, dict):
        raise CompositionError("Projection UI protocol is not a JSON object")
    ui = compatibility.get("ui_protocol")
    if not isinstance(ui, dict):
        raise CompositionError("composition ui_protocol contract is missing")
    version = protocol.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        raise CompositionError("Projection UI protocol version is invalid")
    _compare_contract(
        diagnostics,
        "astral-projection",
        "components.astral-projection.contract_version",
        component.get("contract_version"),
        exports.get("CONTRACT_VERSION"),
    )
    _compare_contract(
        diagnostics,
        "astral-projection",
        "compatibility.ui_protocol.version",
        ui.get("version"),
        str(version),
    )
    _compare_contract(
        diagnostics,
        "astral-projection",
        "compatibility.ui_protocol.sha256",
        ui.get("sha256"),
        _canonical_json_sha256(protocol),
    )


def _verify_plane(
    component_root: Path,
    component: dict[str, object],
    compatibility: dict[str, object],
    diagnostics: list[Diagnostic],
) -> None:
    package_version, _ = _project_metadata(component_root, "astralplane")
    constants = _literal_assignments(
        component_root / "src/astralplane/compatibility.py"
    )
    revisions = _literal_assignments(
        component_root / "src/astralplane/database/revision.py"
    )
    public = _literal_assignments(component_root / "src/astralplane/__init__.py").get(
        "__all__"
    )
    required_exports = {
        "BLOB_LAYOUT_VERSION",
        "CONTRACT_VERSION",
        "MIGRATION_DIGEST",
        "READ_COMPATIBLE_FROM",
        "SCHEMA_REVISION",
    }
    if not isinstance(public, (tuple, list)) or not required_exports.issubset(
        set(public)
    ):
        raise CompositionError(
            "AstralPlane public compatibility exports are incomplete"
        )
    plane = compatibility.get("data_plane")
    if not isinstance(plane, dict):
        raise CompositionError("composition data_plane contract is missing")
    observations = {
        "components.astral-plane.contract_version": (
            component.get("contract_version"),
            constants.get("CONTRACT_VERSION"),
        ),
        "compatibility.data_plane.contract_version": (
            plane.get("contract_version"),
            constants.get("CONTRACT_VERSION"),
        ),
        "compatibility.data_plane.schema_revision": (
            plane.get("schema_revision"),
            revisions.get("SCHEMA_REVISION"),
        ),
        "compatibility.data_plane.read_compatible_from": (
            plane.get("read_compatible_from"),
            revisions.get("READ_COMPATIBLE_FROM"),
        ),
        "compatibility.data_plane.migration_sha256": (
            plane.get("migration_sha256"),
            _plane_migration_digest(component_root),
        ),
        "compatibility.data_plane.blob_layout_version": (
            plane.get("blob_layout_version"),
            constants.get("BLOB_LAYOUT_VERSION"),
        ),
        "astralplane package version": (
            package_version,
            constants.get("PACKAGE_VERSION"),
        ),
    }
    for field, (declared, observed) in observations.items():
        _compare_contract(diagnostics, "astral-plane", field, declared, observed)


def _verify_primitives(
    component_root: Path,
    component: dict[str, object],
    compatibility: dict[str, object],
    diagnostics: list[Diagnostic],
) -> None:
    package_version, _ = _project_metadata(component_root, "astralprims")
    exports = _literal_assignments(component_root / "src/astralprims/__init__.py")
    primitives = compatibility.get("primitives")
    if not isinstance(primitives, dict):
        raise CompositionError("composition primitives contract is missing")
    observations = {
        "components.astral-primitives.contract_version": (
            component.get("contract_version"),
            package_version,
        ),
        "compatibility.primitives.package_version": (
            primitives.get("package_version"),
            package_version,
        ),
        "astralprims.__version__": (package_version, exports.get("__version__")),
        "compatibility.primitives.contract_sha256": (
            primitives.get("contract_sha256"),
            compute_primitives_digest(component_root),
        ),
    }
    for field, (declared, observed) in observations.items():
        _compare_contract(diagnostics, "astral-primitives", field, declared, observed)


def _verify_lets(
    component_root: Path,
    component: dict[str, object],
    compatibility: dict[str, object],
    diagnostics: list[Diagnostic],
) -> None:
    package_version, _ = _project_metadata(component_root, "lets-agent")
    package_exports = _literal_assignments(component_root / "src/lets/__init__.py")
    api_constants = _literal_assignments(component_root / "src/lets/api.py")
    profile_constants = _literal_assignments(
        component_root / "src/lets/integrations/astraldeep.py"
    )
    openapi_path = component_root / "protocol/openapi.yaml"
    try:
        openapi_bytes = openapi_path.read_bytes()
    except OSError as exc:
        raise CompositionError(f"could not read LETS OpenAPI contract: {exc}") from exc
    openapi = _read_json(openapi_path)
    if not isinstance(openapi, dict) or not isinstance(openapi.get("info"), dict):
        raise CompositionError("LETS OpenAPI contract is not a JSON object with info")
    lets_contract = compatibility.get("lets")
    if not isinstance(lets_contract, dict):
        raise CompositionError("composition LETS contract is missing")

    for export, relative_path in LETS_PUBLIC_EXPORTS.items():
        if export not in _public_symbols(component_root / relative_path):
            _diagnostic(
                diagnostics,
                "E_LETS_PUBLIC_EXPORT",
                f"LETS v1.0.10 public export {export!r} is unavailable",
                "Use the signed LETS v1.0.10 public client/executor surface.",
                component="lets",
            )

    receipt_type = _class_constant(
        component_root / "src/lets/models.py", "Receipt", "WIRE_TYPE"
    )
    info = openapi["info"]
    observations = {
        "components.lets.contract_version": (
            component.get("contract_version"),
            package_version,
        ),
        "components.lets.ref": (component.get("ref"), f"v{package_version}"),
        "compatibility.lets.release": (
            lets_contract.get("release"),
            f"v{package_version}",
        ),
        "lets.__version__": (package_version, package_exports.get("__version__")),
        "LETS OpenAPI info.version": (package_version, info.get("version")),
        "compatibility.lets.api_version": (
            lets_contract.get("api_version"),
            api_constants.get("API_VERSION"),
        ),
        "compatibility.lets.openapi_sha256": (
            lets_contract.get("openapi_sha256"),
            hashlib.sha256(openapi_bytes).hexdigest(),
        ),
        "compatibility.lets.receipt_wire_type": (
            lets_contract.get("receipt_wire_type"),
            receipt_type,
        ),
        "LETS Astral tool-scope profile": (
            EXPECTED_ASTRAL_TOOL_SCOPES,
            profile_constants.get("ASTRAL_TOOL_SCOPES"),
        ),
    }
    for field, (declared, observed) in observations.items():
        _compare_contract(diagnostics, "lets", field, declared, observed)


def _verify_component_contract(
    component_name: str,
    component_root: Path,
    component: dict[str, object],
    compatibility: dict[str, object],
    diagnostics: list[Diagnostic],
) -> None:
    try:
        if component_name == "astral-projection":
            _verify_projection(component_root, component, compatibility, diagnostics)
        elif component_name == "astral-plane":
            _verify_plane(component_root, component, compatibility, diagnostics)
        elif component_name == "astral-primitives":
            _verify_primitives(component_root, component, compatibility, diagnostics)
        elif component_name == "lets":
            _verify_lets(component_root, component, compatibility, diagnostics)
    except CompositionError as exc:
        _diagnostic(
            diagnostics,
            "E_INCOMPATIBLE_CONTRACT",
            f"component compatibility could not be established: {exc}",
            "Restore the exact pinned component and rerun local composition verification.",
            component=component_name,
        )


def _component_unavailable(
    diagnostics: list[Diagnostic],
    component_name: str,
    state: str,
) -> None:
    if component_name in PRIVATE_COMPONENTS:
        _diagnostic(
            diagnostics,
            "E_PRIVATE_ACCESS",
            f"private component is {state}; local authorization may be unavailable",
            "Authorize Git for this private repository, then initialize the exact submodule pin.",
            component=component_name,
        )
        return
    code = "E_COMPONENT_MISSING" if state == "missing" else "E_COMPONENT_UNINITIALIZED"
    _diagnostic(
        diagnostics,
        code,
        f"component worktree is {state}",
        "Initialize the exact submodule pin without selecting a branch.",
        component=component_name,
    )


def verify_composition(
    root: Path,
    *,
    manifest_path: Path | None = None,
    schema_path: Path | None = None,
    gitmodules_path: Path | None = None,
) -> VerificationReport:
    """Verify one checkout without using the network or importing component code."""

    root = root.resolve()
    manifest_path = manifest_path or root / "config/astral-composition.json"
    schema_path = schema_path or root / SCHEMA_RELATIVE_PATH
    gitmodules_path = gitmodules_path or root / ".gitmodules"
    diagnostics: list[Diagnostic] = []
    manifest_sha256: str | None = None

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = _read_json(manifest_path)
        schema = _read_json(schema_path)
        schema_errors = _validate_manifest_schema(manifest, schema)
    except (OSError, CompositionError) as exc:
        _diagnostic(
            diagnostics,
            "E_MANIFEST_OR_SCHEMA",
            str(exc),
            "Restore the committed composition manifest and schema.",
        )
        return VerificationReport(manifest_sha256, tuple(diagnostics))

    if schema_errors:
        for error in schema_errors:
            _diagnostic(
                diagnostics,
                "E_SCHEMA_INVALID",
                error,
                "Make the manifest satisfy the committed composition schema.",
            )
        return VerificationReport(
            manifest_sha256,
            tuple(
                sorted(
                    diagnostics,
                    key=lambda item: (item.component or "", item.code, item.message),
                )
            ),
        )
    if not isinstance(manifest, dict):
        raise AssertionError("schema admitted a non-object composition manifest")

    try:
        modules = _parse_gitmodules(gitmodules_path)
    except CompositionError as exc:
        _diagnostic(
            diagnostics,
            "E_GITMODULES_INVALID",
            str(exc),
            "Restore the exact canonical .gitmodules mappings.",
        )
        return VerificationReport(manifest_sha256, tuple(diagnostics))

    for module_path, values in sorted(modules.items()):
        if "branch" in values:
            _diagnostic(
                diagnostics,
                "E_FLOATING_BRANCH",
                f".gitmodules entry for {module_path} contains a floating branch selector",
                "Remove the branch selector; composition is selected only by the gitlink commit.",
            )

    components = manifest["components"]
    compatibility = manifest["compatibility"]
    if not isinstance(components, dict) or not isinstance(compatibility, dict):
        raise AssertionError("schema admitted malformed composition sections")
    expected_paths = {
        str(component["path"])
        for component in components.values()
        if isinstance(component, dict)
    }
    for unexpected in sorted(set(modules) - expected_paths):
        _diagnostic(
            diagnostics,
            "E_UNDECLARED_SUBMODULE",
            f".gitmodules contains undeclared component path {unexpected}",
            "Remove undeclared component mappings from the composition.",
        )

    initialized: dict[str, Path] = {}
    for component_name in COMPONENT_ORDER:
        component = components[component_name]
        if not isinstance(component, dict):
            raise AssertionError("schema admitted a malformed component")
        relative_path = str(component["path"])
        expected_commit = str(component["commit"])
        expected_repository = str(component["repository"])
        expected_module_name = EXPECTED_MODULE_NAMES[component_name]
        module = modules.get(relative_path)
        if module is None:
            _diagnostic(
                diagnostics,
                "E_SUBMODULE_MAPPING_MISSING",
                "canonical .gitmodules mapping is missing",
                "Restore the exact component path and canonical HTTPS repository URL.",
                component=component_name,
            )
        else:
            if module.get("name") != expected_module_name:
                _diagnostic(
                    diagnostics,
                    "E_SUBMODULE_NAME",
                    "submodule section name is not canonical",
                    f"Name the mapping {expected_module_name!r}.",
                    component=component_name,
                )
            if module.get("url") != expected_repository:
                _diagnostic(
                    diagnostics,
                    "E_WRONG_URL",
                    "submodule URL does not match the canonical manifest URL",
                    "Restore the canonical HTTPS URL without credentials or aliases.",
                    component=component_name,
                )

        try:
            gitlink = _gitlink(root, relative_path)
        except CompositionError as exc:
            _diagnostic(
                diagnostics,
                "E_GITLINK_INVALID",
                str(exc),
                "Restore the stage-0 submodule gitlink in the superproject index.",
                component=component_name,
            )
            gitlink = None
        if gitlink is None:
            _diagnostic(
                diagnostics,
                "E_GITLINK_MISSING",
                "superproject index has no component gitlink",
                "Add the exact component commit as a mode-160000 gitlink.",
                component=component_name,
            )
        else:
            mode, indexed_commit = gitlink
            if mode != "160000":
                _diagnostic(
                    diagnostics,
                    "E_GITLINK_MODE",
                    "component index entry is not a mode-160000 gitlink",
                    "Replace copied content with the exact submodule gitlink.",
                    component=component_name,
                )
            if indexed_commit != expected_commit:
                _diagnostic(
                    diagnostics,
                    "E_STALE_GITLINK",
                    "gitlink commit does not match the composition manifest pin",
                    "Update the gitlink and manifest together to one reviewed exact commit.",
                    component=component_name,
                )

        unresolved = root / relative_path
        try:
            component_root = unresolved.resolve(strict=True)
            component_root.relative_to(root)
        except (OSError, ValueError):
            _component_unavailable(diagnostics, component_name, "missing")
            continue
        if not component_root.is_dir():
            _component_unavailable(diagnostics, component_name, "missing")
            continue
        try:
            inside = _git_output(component_root, "rev-parse", "--is-inside-work-tree")
            if inside != "true":
                raise CompositionError("component is not a Git worktree")
            head = _git_output(component_root, "rev-parse", "--verify", "HEAD")
        except CompositionError:
            _component_unavailable(diagnostics, component_name, "uninitialized")
            continue
        initialized[component_name] = component_root
        if head != expected_commit:
            _diagnostic(
                diagnostics,
                "E_WRONG_SHA",
                "initialized component HEAD does not match the exact manifest pin",
                "Checkout the gitlink commit without selecting a branch.",
                component=component_name,
            )
        try:
            origin = _git_output(component_root, "config", "--get", "remote.origin.url")
        except CompositionError:
            origin = ""
        if origin != expected_repository:
            _diagnostic(
                diagnostics,
                "E_WRONG_URL",
                "component origin does not match the canonical manifest URL",
                "Set origin to the canonical credential-free HTTPS URL.",
                component=component_name,
            )
        try:
            dirty = bool(
                _git_output(
                    component_root,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--ignore-submodules=none",
                )
            )
        except CompositionError:
            dirty = True
        if dirty:
            _diagnostic(
                diagnostics,
                "E_DIRTY_COMPONENT",
                "component worktree has tracked or untracked changes",
                "Commit elsewhere or restore the exact clean component pin.",
                component=component_name,
            )

    for component_name in COMPONENT_ORDER:
        component_root = initialized.get(component_name)
        if component_root is None:
            continue
        component = components[component_name]
        if isinstance(component, dict):
            _verify_component_contract(
                component_name,
                component_root,
                component,
                compatibility,
                diagnostics,
            )

    diagnostics.sort(key=lambda item: (item.component or "", item.code, item.message))
    return VerificationReport(manifest_sha256, tuple(diagnostics))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="AstralDeep checkout root (default: script repository root)",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--gitmodules", type=Path)
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = verify_composition(
        arguments.root,
        manifest_path=arguments.manifest,
        schema_path=arguments.schema,
        gitmodules_path=arguments.gitmodules,
    )
    if arguments.json:
        print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
    elif report.ok:
        print(
            "composition verified: four exact clean component pins and compatibility "
            f"contracts ({report.manifest_sha256})"
        )
    else:
        for item in report.diagnostics:
            subject = item.component or "composition"
            print(f"{item.code} [{subject}]: {item.message}", file=sys.stderr)
            print(f"  remediation: {item.remediation}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
