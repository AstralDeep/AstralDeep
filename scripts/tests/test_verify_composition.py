"""Focused tests for the offline feature-074 composition verifier."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_composition.py"
SCHEMA = (
    REPOSITORY_ROOT
    / "specs/074-multirepo-lets-integration/contracts/composition-manifest.schema.json"
)

spec = importlib.util.spec_from_file_location("verify_composition_074", SCRIPT)
assert spec and spec.loader
composition = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = composition
spec.loader.exec_module(composition)


CANONICAL_REPOSITORIES = {
    "astral-projection": "https://github.com/AstralDeep/AstralProjection.git",
    "astral-plane": "https://github.com/AstralDeep/AstralPlane.git",
    "astral-primitives": "https://github.com/AstralDeep/AstralPrimitives.git",
    "lets": "https://github.com/AstralDeep/LETS.git",
}
COMPONENT_PATHS = {
    "astral-projection": "components/AstralProjection",
    "astral-plane": "components/AstralPlane",
    "astral-primitives": "components/AstralPrimitives",
    "lets": "components/LETS",
}
MODULE_NAMES = {
    "astral-projection": "AstralProjection",
    "astral-plane": "AstralPlane",
    "astral-primitives": "AstralPrimitives",
    "lets": "LETS",
}
EXPECTED_PLANE_COMMIT_075 = "4a1d990387428436041dd70d9c417e9e86000b6c"
EXPECTED_PLANE_SCHEMA_REVISION_075 = "075.001"
EXPECTED_PLANE_MIGRATION_SHA256_075 = (
    "755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df"
)
EXPECTED_PROJECTION_COMMIT_077 = (
    "db59c6f01bce72d5b2d5caa94302ad625c82d6df"
)
EXPECTED_PROJECTION_PROTOCOL_SHA256_077 = (
    "b16234ebe788cc26f1f1218da7b03f9a48b84d8852942e4ea2d2efdc1df28a03"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def _gitlink_commit(root: Path, component_path: str) -> str:
    entry = _git(root, "ls-tree", "HEAD", "--", component_path)
    metadata, listed_path = entry.split("\t", maxsplit=1)
    mode, object_type, commit = metadata.split()
    assert (mode, object_type, listed_path) == ("160000", "commit", component_path)
    return commit


def _commit_component(path: Path, repository: str) -> str:
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Composition Test")
    _git(path, "config", "user.email", "composition@example.invalid")
    _git(path, "remote", "add", "origin", repository)
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


def _projection(root: Path) -> None:
    component = root / COMPONENT_PATHS["astral-projection"]
    _write(
        component / "src/astralprojection/__init__.py",
        'CONTRACT_VERSION = "astralprojection.contract/v1"\n'
        '__version__ = "0.1.0"\n'
        '__all__ = ["CONTRACT_VERSION", "__version__"]\n',
    )
    _write(
        component / "contracts/ui_protocol.json",
        json.dumps({"version": 1, "component_types": ["button", "text"]}, indent=2)
        + "\n",
    )


def _plane(root: Path) -> None:
    component = root / COMPONENT_PATHS["astral-plane"]
    _write(
        component / "pyproject.toml",
        '[project]\nname = "astralplane"\nversion = "0.1.0"\n',
    )
    _write(
        component / "src/astralplane/compatibility.py",
        'CONTRACT_VERSION = "astralplane.contract/v1"\n'
        'PACKAGE_VERSION = "0.1.0"\n'
        'BLOB_LAYOUT_VERSION = "astralplane.blob-layout/v1"\n',
    )
    _write(
        component / "src/astralplane/database/revision.py",
        'SCHEMA_PREDECESSOR_REVISION = "066.001"\n'
        'SCHEMA_REVISION = "067.001"\n'
        "READ_COMPATIBLE_FROM = SCHEMA_PREDECESSOR_REVISION\n",
    )
    _write(
        component / "src/astralplane/database/migrations.py",
        'PLANE_SCHEMA_067_STATEMENTS = ("SELECT 1", "SELECT 2")\n'
        "PLANE_SCHEMA_067_MIGRATION = Migration(\n"
        '    name="astralplane-067-test",\n'
        '    source_revisions=("066.001",),\n'
        '    target_revision="067.001",\n'
        "    checksum=_statements_checksum(PLANE_SCHEMA_067_STATEMENTS),\n"
        "    operation=_apply,\n"
        ")\n"
        "MIGRATION_REGISTRY = MigrationRegistry((PLANE_SCHEMA_067_MIGRATION,))\n",
    )
    _write(
        component / "src/astralplane/__init__.py",
        "__all__ = (\n"
        '    "BLOB_LAYOUT_VERSION", "CONTRACT_VERSION", "MIGRATION_DIGEST",\n'
        '    "READ_COMPATIBLE_FROM", "SCHEMA_REVISION",\n'
        ")\n",
    )


def _primitives(root: Path) -> None:
    component = root / COMPONENT_PATHS["astral-primitives"]
    _write(
        component / "pyproject.toml",
        '[project]\nname = "astralprims"\nversion = "0.3.0"\n',
    )
    _write(component / "src/astralprims/__init__.py", '__version__ = "0.3.0"\n')
    _write(component / "src/astralprims/primitives.py", "class Text: pass\n")


def _lets(root: Path) -> None:
    component = root / COMPONENT_PATHS["lets"]
    _write(
        component / "pyproject.toml",
        '[project]\nname = "lets-agent"\nversion = "1.0.11"\n',
    )
    _write(component / "src/lets/__init__.py", '__version__ = "1.0.11"\n')
    _write(component / "src/lets/api.py", 'API_VERSION = "v1"\n')
    _write(component / "src/lets/client.py", "class LETSClient: pass\n")
    _write(
        component / "src/lets/integrations/ports.py",
        "class ReplicaAuthorizer: pass\n",
    )
    _write(
        component / "src/lets/integrations/astraldeep.py",
        "ASTRAL_TOOL_SCOPES = frozenset({\n"
        '    "tools:read", "tools:write", "tools:search",\n'
        '    "tools:system", "tools:files", "tools:execute",\n'
        "})\n"
        "class AstralDeepAuthorizer: pass\n",
    )
    _write(
        component / "src/lets/integrations/__init__.py",
        "from lets.integrations.astraldeep import AstralDeepAuthorizer\n"
        "from lets.integrations.ports import ReplicaAuthorizer\n"
        '__all__ = ["AstralDeepAuthorizer", "ReplicaAuthorizer"]\n',
    )
    _write(
        component / "src/lets/models.py",
        'class Receipt:\n    WIRE_TYPE = "lets.receipt/v1"\n',
    )
    _write(component / "src/lets/executor.py", "class ReceiptVerifier: pass\n")
    _write(
        component / "protocol/openapi.yaml",
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "LETS Warden API", "version": "1.0.11"},
                "paths": {"/v1/info": {}},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


def _manifest(root: Path, commits: dict[str, str]) -> dict[str, Any]:
    projection_contract = json.loads(
        (
            root / COMPONENT_PATHS["astral-projection"] / "contracts/ui_protocol.json"
        ).read_text(encoding="utf-8")
    )
    lets_openapi = root / COMPONENT_PATHS["lets"] / "protocol/openapi.yaml"
    return {
        "format": "astral.composition/v1",
        "astraldeep_contract_version": "astraldeep.composition/v1",
        "components": {
            "astral-projection": {
                "repository": CANONICAL_REPOSITORIES["astral-projection"],
                "path": COMPONENT_PATHS["astral-projection"],
                "commit": commits["astral-projection"],
                "contract_version": "astralprojection.contract/v1",
            },
            "astral-plane": {
                "repository": CANONICAL_REPOSITORIES["astral-plane"],
                "path": COMPONENT_PATHS["astral-plane"],
                "commit": commits["astral-plane"],
                "contract_version": "astralplane.contract/v1",
            },
            "astral-primitives": {
                "repository": CANONICAL_REPOSITORIES["astral-primitives"],
                "path": COMPONENT_PATHS["astral-primitives"],
                "commit": commits["astral-primitives"],
                "contract_version": "0.3.0",
            },
            "lets": {
                "repository": CANONICAL_REPOSITORIES["lets"],
                "path": COMPONENT_PATHS["lets"],
                "commit": commits["lets"],
                "ref": "v1.0.11",
                "contract_version": "1.0.11",
            },
        },
        "availability": {
            "astral-projection": "required-embedded",
            "astral-plane": "required-embedded",
            "astral-primitives": "required-embedded",
            "lets": "external-feature-gated",
        },
        "compatibility": {
            "ui_protocol": {
                "version": "1",
                "sha256": composition._canonical_json_sha256(projection_contract),
            },
            "data_plane": {
                "contract_version": "astralplane.contract/v1",
                "schema_revision": "067.001",
                "read_compatible_from": "066.001",
                "migration_sha256": composition._plane_migration_digest(
                    root / COMPONENT_PATHS["astral-plane"]
                ),
                "blob_layout_version": "astralplane.blob-layout/v1",
            },
            "primitives": {
                "package_version": "0.3.0",
                "contract_sha256": composition.compute_primitives_digest(
                    root / COMPONENT_PATHS["astral-primitives"]
                ),
            },
            "lets": {
                "release": "v1.0.11",
                "api_version": "v1",
                "openapi_sha256": hashlib.sha256(lets_openapi.read_bytes()).hexdigest(),
                "receipt_wire_type": "lets.receipt/v1",
                "scope_profile_version": "astral.tools/v1",
            },
        },
    }


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "AstralDeep"
    _projection(root)
    _plane(root)
    _primitives(root)
    _lets(root)
    commits = {
        name: _commit_component(root / COMPONENT_PATHS[name], repository)
        for name, repository in CANONICAL_REPOSITORIES.items()
    }
    manifest = _manifest(root, commits)
    _write(
        root / "config/astral-composition.json",
        json.dumps(manifest, indent=2) + "\n",
    )
    lines: list[str] = []
    for name in composition.COMPONENT_ORDER:
        lines.extend(
            [
                f'[submodule "{MODULE_NAMES[name]}"]',
                f"\tpath = {COMPONENT_PATHS[name]}",
                f"\turl = {CANONICAL_REPOSITORIES[name]}",
            ]
        )
    _write(root / ".gitmodules", "\n".join(lines) + "\n")
    _git(root, "init", "--quiet")
    for name in composition.COMPONENT_ORDER:
        _git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commits[name]},{COMPONENT_PATHS[name]}",
        )
    return root


def _verify(root: Path) -> Any:
    return composition.verify_composition(root, schema_path=SCHEMA)


def _codes(report: Any, component: str | None = None) -> set[str]:
    return {
        item.code
        for item in report.diagnostics
        if component is None or item.component == component
    }


def _rewrite_manifest(root: Path, mutate: Any) -> None:
    path = root / "config/astral-composition.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    _write(path, json.dumps(document, indent=2) + "\n")


def _repin_component(root: Path, component: str) -> str:
    component_root = root / COMPONENT_PATHS[component]
    _git(component_root, "add", "-A")
    _git(component_root, "commit", "--quiet", "-m", "updated fixture")
    commit = _git(component_root, "rev-parse", "HEAD")
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},{COMPONENT_PATHS[component]}",
    )
    _rewrite_manifest(
        root, lambda document: document["components"][component].update(commit=commit)
    )
    return commit


def test_current_composition_has_exact_pins_canonical_urls_and_contracts() -> None:
    report = composition.verify_composition(REPOSITORY_ROOT)

    assert report.ok, report.to_dict()
    assert report.diagnostics == ()


def test_composition_pins_exact_plane_075_and_projection_077() -> None:
    manifest = json.loads(
        (REPOSITORY_ROOT / "config/astral-composition.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["components"]["astral-plane"]["commit"] == (
        EXPECTED_PLANE_COMMIT_075
    )
    assert manifest["compatibility"]["data_plane"]["schema_revision"] == (
        EXPECTED_PLANE_SCHEMA_REVISION_075
    )
    assert manifest["compatibility"]["data_plane"]["migration_sha256"] == (
        EXPECTED_PLANE_MIGRATION_SHA256_075
    )
    assert manifest["components"]["astral-projection"]["commit"] == (
        EXPECTED_PROJECTION_COMMIT_077
    )
    assert manifest["compatibility"]["ui_protocol"]["sha256"] == (
        EXPECTED_PROJECTION_PROTOCOL_SHA256_077
    )

    assert _gitlink_commit(
        REPOSITORY_ROOT, COMPONENT_PATHS["astral-plane"]
    ) == EXPECTED_PLANE_COMMIT_075
    assert _gitlink_commit(
        REPOSITORY_ROOT, COMPONENT_PATHS["astral-projection"]
    ) == EXPECTED_PROJECTION_COMMIT_077


def test_synthetic_exact_pins_and_no_floating_branch_pass(checkout: Path) -> None:
    report = _verify(checkout)

    assert report.ok, report.to_dict()


def test_primitives_digest_uses_documented_binary_framing(tmp_path: Path) -> None:
    component = tmp_path / "AstralPrimitives"
    first = component / "src/astralprims/a.py"
    second = component / "src/astralprims/z.py"
    _write(first, "alpha\n")
    _write(second, "omega\n")

    framed = bytearray()
    for path in (first, second):
        relative = path.relative_to(component).as_posix().encode("utf-8")
        content = path.read_bytes()
        framed.extend(struct.pack(">I", len(relative)))
        framed.extend(relative)
        framed.extend(struct.pack(">Q", len(content)))
        framed.extend(content)

    assert (
        composition.compute_primitives_digest(component)
        == hashlib.sha256(framed).hexdigest()
    )


def test_floating_gitmodule_branch_fails_closed(checkout: Path) -> None:
    path = checkout / ".gitmodules"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("\turl = ", "\tbranch = main\n\turl = ", 1), encoding="utf-8"
    )

    report = _verify(checkout)

    assert "E_FLOATING_BRANCH" in _codes(report)


def test_inaccessible_private_component_reports_access_without_network(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = checkout / COMPONENT_PATHS["astral-projection"]
    projection.rename(checkout / "private-component-unavailable")
    original_run = subprocess.run
    commands: list[tuple[str, ...]] = []

    def recording_run(arguments: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(tuple(arguments))
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(composition.subprocess, "run", recording_run)
    report = _verify(checkout)

    assert "E_PRIVATE_ACCESS" in _codes(report, "astral-projection")
    assert all(
        not {"clone", "fetch", "ls-remote", "pull", "push"}.intersection(command)
        for command in commands
    )


def test_missing_and_uninitialized_public_components_are_distinct(
    checkout: Path,
) -> None:
    lets = checkout / COMPONENT_PATHS["lets"]
    lets.rename(checkout / "missing-lets")
    missing = _verify(checkout)
    assert "E_COMPONENT_MISSING" in _codes(missing, "lets")

    lets = checkout / COMPONENT_PATHS["lets"]
    lets.mkdir(parents=True)
    uninitialized = _verify(checkout)
    assert "E_COMPONENT_UNINITIALIZED" in _codes(uninitialized, "lets")


def test_dirty_component_fails_closed_without_disclosing_paths(checkout: Path) -> None:
    _write(
        checkout / COMPONENT_PATHS["astral-primitives"] / "local-secret.txt",
        "sensitive\n",
    )

    report = _verify(checkout)
    diagnostics = [
        item for item in report.diagnostics if item.code == "E_DIRTY_COMPONENT"
    ]

    assert [item.component for item in diagnostics] == ["astral-primitives"]
    assert all("local-secret" not in item.message for item in diagnostics)


def test_stale_gitlink_is_attributed_separately(checkout: Path) -> None:
    _git(
        checkout,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'1' * 40},{COMPONENT_PATHS['astral-plane']}",
    )

    report = _verify(checkout)

    assert "E_STALE_GITLINK" in _codes(report, "astral-plane")
    assert "E_WRONG_SHA" not in _codes(report, "astral-plane")


def test_clean_component_at_wrong_sha_is_rejected(checkout: Path) -> None:
    component = checkout / COMPONENT_PATHS["astral-primitives"]
    _write(component / "README.md", "new clean commit\n")
    _git(component, "add", "README.md")
    _git(component, "commit", "--quiet", "-m", "advance without repin")

    report = _verify(checkout)

    assert "E_WRONG_SHA" in _codes(report, "astral-primitives")
    assert "E_DIRTY_COMPONENT" not in _codes(report, "astral-primitives")


def test_noncanonical_gitmodules_and_origin_urls_are_rejected(checkout: Path) -> None:
    path = checkout / ".gitmodules"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            CANONICAL_REPOSITORIES["lets"], "git@github.com:AstralDeep/LETS.git"
        ),
        encoding="utf-8",
    )
    _git(
        checkout / COMPONENT_PATHS["lets"],
        "remote",
        "set-url",
        "origin",
        "git@github.com:AstralDeep/LETS.git",
    )

    report = _verify(checkout)

    assert "E_WRONG_URL" in _codes(report, "lets")
    assert sum(item.code == "E_WRONG_URL" for item in report.diagnostics) == 2


def test_incompatible_contract_digest_fails_closed(checkout: Path) -> None:
    _rewrite_manifest(
        checkout,
        lambda document: document["compatibility"]["ui_protocol"].update(
            sha256="0" * 64
        ),
    )

    report = _verify(checkout)

    assert "E_INCOMPATIBLE_CONTRACT" in _codes(report, "astral-projection")


def test_lets_v1_0_11_public_exports_are_required(checkout: Path) -> None:
    executor = checkout / COMPONENT_PATHS["lets"] / "src/lets/executor.py"
    _write(executor, "class InternalReceiptVerifier: pass\n")
    _repin_component(checkout, "lets")

    report = _verify(checkout)
    export_errors = [
        item for item in report.diagnostics if item.code == "E_LETS_PUBLIC_EXPORT"
    ]

    assert [item.message for item in export_errors] == [
        "LETS v1.0.11 public export 'ReceiptVerifier' is unavailable"
    ]


def test_manifest_schema_failure_is_deterministic(checkout: Path) -> None:
    _rewrite_manifest(checkout, lambda document: document.pop("availability"))

    first = _verify(checkout)
    second = _verify(checkout)

    assert first.to_dict() == second.to_dict()
    assert _codes(first) == {"E_SCHEMA_INVALID"}


def test_unreadable_manifest_and_gitmodules_fail_with_local_diagnostics(
    checkout: Path,
) -> None:
    manifest = checkout / "config/astral-composition.json"
    manifest.write_text("{", encoding="utf-8")
    bad_manifest = _verify(checkout)
    assert _codes(bad_manifest) == {"E_MANIFEST_OR_SCHEMA"}

    _write(
        manifest,
        json.dumps(
            _manifest(
                checkout,
                {
                    name: _git(checkout / path, "rev-parse", "HEAD")
                    for name, path in COMPONENT_PATHS.items()
                },
            ),
            indent=2,
        )
        + "\n",
    )
    (checkout / ".gitmodules").write_text(
        "[not-a-submodule]\npath = broken\n", encoding="utf-8"
    )
    bad_modules = _verify(checkout)
    assert _codes(bad_modules) == {"E_GITMODULES_INVALID"}


@pytest.mark.parametrize(
    ("mutation", "expected_code", "expected_component"),
    [
        ("undeclared", "E_UNDECLARED_SUBMODULE", None),
        ("missing-mapping", "E_SUBMODULE_MAPPING_MISSING", "lets"),
        ("wrong-name", "E_SUBMODULE_NAME", "lets"),
        ("missing-gitlink", "E_GITLINK_MISSING", "lets"),
    ],
)
def test_structural_composition_failures_are_attributed(
    checkout: Path,
    mutation: str,
    expected_code: str,
    expected_component: str | None,
) -> None:
    modules = checkout / ".gitmodules"
    text = modules.read_text(encoding="utf-8")
    lets_section = (
        '[submodule "LETS"]\n'
        f"\tpath = {COMPONENT_PATHS['lets']}\n"
        f"\turl = {CANONICAL_REPOSITORIES['lets']}\n"
    )
    if mutation == "undeclared":
        text += (
            '[submodule "Unexpected"]\n'
            "\tpath = components/Unexpected\n"
            "\turl = https://github.com/AstralDeep/Unexpected.git\n"
        )
        modules.write_text(text, encoding="utf-8")
    elif mutation == "missing-mapping":
        modules.write_text(text.replace(lets_section, ""), encoding="utf-8")
    elif mutation == "wrong-name":
        modules.write_text(
            text.replace('[submodule "LETS"]', '[submodule "NotLETS"]'),
            encoding="utf-8",
        )
    else:
        assert mutation == "missing-gitlink"
        _git(checkout, "update-index", "--force-remove", COMPONENT_PATHS["lets"])

    report = _verify(checkout)

    assert any(
        item.code == expected_code and item.component == expected_component
        for item in report.diagnostics
    )


def test_malformed_component_contract_is_incompatible(checkout: Path) -> None:
    protocol = (
        checkout / COMPONENT_PATHS["astral-projection"] / "contracts/ui_protocol.json"
    )
    _write(protocol, "[]\n")
    _repin_component(checkout, "astral-projection")

    report = _verify(checkout)

    assert "E_INCOMPATIBLE_CONTRACT" in _codes(report, "astral-projection")


def test_cli_reports_success_json_and_actionable_failure(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        composition.main(["--root", str(checkout), "--schema", str(SCHEMA), "--json"])
        == 0
    )
    successful = json.loads(capsys.readouterr().out)
    assert successful["ok"] is True

    assert composition.main(["--root", str(checkout), "--schema", str(SCHEMA)]) == 0
    assert "composition verified" in capsys.readouterr().out

    modules = checkout / ".gitmodules"
    modules.write_text(
        modules.read_text(encoding="utf-8").replace(
            "\turl = ", "\tbranch = main\n\turl = ", 1
        ),
        encoding="utf-8",
    )
    assert composition.main(["--root", str(checkout), "--schema", str(SCHEMA)]) == 1
    failure = capsys.readouterr().err
    assert "E_FLOATING_BRANCH" in failure
    assert "remediation:" in failure


@pytest.mark.parametrize(
    ("instance", "declared", "expected"),
    [
        (None, "null", True),
        ({}, "object", True),
        ([], "array", True),
        ("value", "string", True),
        (1, "integer", True),
        (True, "integer", False),
        (1.5, "number", True),
        (True, "number", False),
        (False, "boolean", True),
        ("value", "unknown", False),
    ],
)
def test_json_type_vocabulary_is_exact(
    instance: object, declared: str, expected: bool
) -> None:
    assert composition._json_type_matches(instance, declared) is expected


def test_json_reader_rejects_duplicate_nonfinite_and_missing_inputs(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    nonfinite = tmp_path / "nonfinite.json"
    _write(duplicate, '{"key": 1, "key": 2}\n')
    _write(nonfinite, '{"value": NaN}\n')

    with pytest.raises(composition.CompositionError, match="duplicate JSON object key"):
        composition._read_json(duplicate)
    with pytest.raises(composition.CompositionError, match="non-finite JSON value"):
        composition._read_json(nonfinite)
    with pytest.raises(composition.CompositionError, match="could not read JSON"):
        composition._read_json(tmp_path / "missing.json")


def test_schema_reference_and_assertion_vocabulary() -> None:
    root_schema = {
        "defs": {
            "slash/name": {"type": "string", "pattern": "^a"},
        }
    }
    assert composition._resolve_local_ref(root_schema, "#/defs/slash~1name") == {
        "type": "string",
        "pattern": "^a",
    }
    with pytest.raises(composition.CompositionError, match="non-local reference"):
        composition._resolve_local_ref(root_schema, "https://example.invalid/schema")
    with pytest.raises(composition.CompositionError, match="cannot be resolved"):
        composition._resolve_local_ref(root_schema, "#/defs/missing")

    schema = {
        "type": "object",
        "required": ["name"],
        "additionalProperties": False,
        "properties": {
            "name": {
                "allOf": [{"type": "string"}],
                "anyOf": [{"const": "abc"}, {"const": "abd"}],
                "minLength": 2,
                "maxLength": 3,
                "pattern": "^a",
            }
        },
    }
    assert composition._schema_errors({"name": "abc"}, schema, schema) == []
    errors = composition._schema_errors({"name": "zzzz", "extra": 1}, schema, schema)
    assert "$: additional property 'extra' is forbidden" in errors
    assert "$.name: value does not match any permitted schema" in errors
    assert "$.name: string is longer than maxLength" in errors
    assert "$.name: string does not match the required pattern" in errors
    assert composition._schema_errors({}, schema, schema) == [
        "$: missing required property 'name'"
    ]
    assert composition._schema_errors(7, {"type": "string"}, {}) == [
        "$: value has the wrong JSON type"
    ]


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    [
        ("x", [], "schema at"),
        ("x", {"$ref": 7}, "reference at"),
        ("x", {"allOf": {}}, "allOf at"),
        ("x", {"anyOf": []}, "anyOf at"),
        ("x", {"type": []}, "type at"),
        ({}, {"properties": []}, "properties at"),
        ({}, {"required": [7]}, "required at"),
        ("x", {"pattern": 7}, "pattern at"),
        ("x", {"pattern": "["}, "pattern at"),
    ],
)
def test_invalid_schema_vocabulary_fails_closed(
    instance: object, schema: object, message: str
) -> None:
    with pytest.raises(composition.CompositionError, match=message):
        composition._schema_errors(instance, schema, {})


def test_manifest_schema_requires_draft_2020_12_object() -> None:
    with pytest.raises(composition.CompositionError, match="schema root"):
        composition._validate_manifest_schema({}, [])
    with pytest.raises(composition.CompositionError, match="Draft 2020-12"):
        composition._validate_manifest_schema({}, {"$schema": "draft-07"})


@pytest.mark.parametrize(
    "content",
    [
        "[not-a-submodule]\npath = components/X\n",
        '[submodule "MissingPath"]\nurl = https://example.invalid/X.git\n',
        (
            '[submodule "One"]\npath = components/X\nurl = https://example.invalid/1.git\n'
            '[submodule "Two"]\npath = components/X\nurl = https://example.invalid/2.git\n'
        ),
        '[submodule "Duplicate"]\npath = components/X\n[submodule "Duplicate"]\npath = components/Y\n',
    ],
)
def test_gitmodules_parser_rejects_ambiguous_or_incomplete_mappings(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / ".gitmodules"
    _write(path, content)
    with pytest.raises(composition.CompositionError):
        composition._parse_gitmodules(path)


def test_git_execution_is_local_only_and_errors_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for command in ("clone", "fetch", "ls-remote", "pull", "push", "remote-ext"):
        with pytest.raises(composition.CompositionError, match="network-capable"):
            composition._run_git(tmp_path, command)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("sensitive executable path")

    monkeypatch.setattr(composition.subprocess, "run", unavailable)
    with pytest.raises(composition.CompositionError, match="local Git command failed"):
        composition._run_git(tmp_path, "status")

    monkeypatch.setattr(
        composition,
        "_run_git",
        lambda *_args: subprocess.CompletedProcess([], 1, "", "private detail"),
    )
    with pytest.raises(
        composition.CompositionError, match="metadata is unavailable"
    ) as error:
        composition._git_output(tmp_path, "status")
    assert "private detail" not in str(error.value)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("160000 abc 0 components/X", "ambiguous index entry"),
        ("160000 abc\tcomponents/X", "malformed index entry"),
        ("160000 abc 0\tcomponents/Y", "malformed index entry"),
        ("160000 abc 1\tcomponents/X", "unmerged index entry"),
    ],
)
def test_gitlink_parser_rejects_ambiguous_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    message: str,
) -> None:
    monkeypatch.setattr(composition, "_git_output", lambda *_args: output)
    with pytest.raises(composition.CompositionError, match=message):
        composition._gitlink(tmp_path, "components/X")
    monkeypatch.setattr(composition, "_git_output", lambda *_args: "")
    assert composition._gitlink(tmp_path, "components/X") is None


def _expression(source: str) -> ast.AST:
    return ast.parse(source, mode="eval").body


def test_static_literal_reader_supports_only_reviewed_expression_forms() -> None:
    values = {
        "BASE": " value ",
        "PREFIX": ("first", "second"),
        "SUFFIX": ["third"],
    }
    assert composition._safe_literal(_expression("BASE"), values) == " value "
    assert composition._safe_literal(_expression("(1, -2)"), values) == (1, -2)
    assert composition._safe_literal(_expression("[1, 2]"), values) == [1, 2]
    assert composition._safe_literal(_expression("{1, 2}"), values) == {1, 2}
    assert composition._safe_literal(_expression("{'a': 1}"), values) == {"a": 1}
    assert composition._safe_literal(
        _expression("(*PREFIX, *SUFFIX, 'fourth')"), values
    ) == ("first", "second", "third", "fourth")
    assert composition._safe_literal(_expression("[*PREFIX, 'third']"), values) == [
        "first",
        "second",
        "third",
    ]
    assert composition._safe_literal(_expression("PREFIX[-1]"), values) == "second"
    checksum = hashlib.sha256(b'["first","second"]').hexdigest()
    assert (
        composition._safe_literal(_expression("_statements_checksum(PREFIX)"), values)
        == checksum
    )
    assert (
        composition._safe_literal(
            _expression("json.dumps(PREFIX, ensure_ascii=True, separators=(',', ':'))"),
            values,
        )
        == '["first","second"]'
    )
    assert composition._safe_literal(
        _expression("frozenset({1, 2})"), values
    ) == frozenset({1, 2})
    assert composition._safe_literal(_expression("BASE.strip()"), values) == "value"
    with pytest.raises(composition.CompositionError, match="non-literal"):
        composition._safe_literal(_expression("factory()"), values)
    with pytest.raises(composition.CompositionError, match="non-literal"):
        composition._safe_literal(_expression("BASE.strip('x')"), values)
    with pytest.raises(composition.CompositionError, match="not a literal sequence"):
        composition._safe_literal(_expression("(*BASE,)"), values)
    with pytest.raises(composition.CompositionError, match="bounded item limit"):
        composition._safe_literal(
            _expression("(*TOO_LARGE,)"),
            {"TOO_LARGE": (None,) * (composition._MAX_LITERAL_SEQUENCE_ITEMS + 1)},
        )
    with pytest.raises(composition.CompositionError, match="out of bounds"):
        composition._safe_literal(_expression("PREFIX[9]"), values)
    with pytest.raises(composition.CompositionError, match="sequence index"):
        composition._safe_literal(_expression("BASE[0]"), values)
    with pytest.raises(composition.CompositionError, match="reviewed canonical form"):
        composition._safe_literal(
            _expression("json.dumps(PREFIX, sort_keys=True)"), values
        )
    with pytest.raises(composition.CompositionError, match="keywords are ambiguous"):
        composition._safe_literal(
            _expression(
                "json.dumps(PREFIX, ensure_ascii=True, ensure_ascii=True, "
                "separators=(',', ':'))"
            ),
            values,
        )
    with pytest.raises(composition.CompositionError, match="keyword expansion"):
        composition._safe_literal(
            _expression(
                "json.dumps(PREFIX, ensure_ascii=True, separators=(',', ':'), "
                "**OPTIONS)"
            ),
            {**values, "OPTIONS": {}},
        )
    with pytest.raises(composition.CompositionError, match="serialization failed"):
        composition._safe_literal(
            _expression(
                "json.dumps(UNSERIALIZABLE, ensure_ascii=True, separators=(',', ':'))"
            ),
            {**values, "UNSERIALIZABLE": ({"not-json"},)},
        )


def test_assignment_and_literal_resolution_are_bounded(tmp_path: Path) -> None:
    tree = ast.parse("A: str\nB: int = 1\nC = 2\nD = E = 3\npass\n")
    assert composition._assignment_target(tree.body[0]) is None
    assert composition._assignment_target(tree.body[1])[0] == "B"
    assert composition._assignment_target(tree.body[2])[0] == "C"
    assert composition._assignment_target(tree.body[3]) is None
    assert composition._assignment_target(tree.body[4]) is None

    path = tmp_path / "constants.py"
    _write(
        path,
        "FORWARD = BASE\n"
        'BASE = " value ".strip()\n'
        "SET = frozenset({1, 2})\n"
        "UNRESOLVED = factory()\n",
    )
    assert composition._literal_assignments(path) == {
        "BASE": "value",
        "FORWARD": "value",
        "SET": frozenset({1, 2}),
    }


def test_public_symbol_and_class_contract_parsing(tmp_path: Path) -> None:
    source = tmp_path / "exports.py"
    _write(
        source,
        "import json\n"
        "import os.path as osp\n"
        "from package import Imported as Alias\n"
        "def function(): pass\n"
        "async def async_function(): pass\n"
        "class Public: pass\n"
        "class _Private: pass\n"
        '__all__ = ("Alias", "function", "async_function", "Public", "missing")\n',
    )
    assert composition._public_symbols(source) == {
        "Alias",
        "function",
        "async_function",
        "Public",
    }

    _write(source, "class Public: pass\nclass _Private: pass\n")
    assert composition._public_symbols(source) == {"Public"}
    _write(source, 'class Public: pass\n__all__ = {"Public"}\n')
    with pytest.raises(composition.CompositionError, match="invalid __all__"):
        composition._public_symbols(source)

    _write(
        source,
        "class Contract:\n    dynamic = factory()\n    VALUE = -2\n",
    )
    assert composition._class_constant(source, "Contract", "VALUE") == -2
    with pytest.raises(composition.CompositionError, match="literal public contract"):
        composition._class_constant(source, "Contract", "dynamic")
    with pytest.raises(composition.CompositionError, match="literal public contract"):
        composition._class_constant(source, "Missing", "VALUE")


@pytest.mark.parametrize(
    "content",
    [
        "not = [valid toml",
        '[project]\nname = "wrong"\nversion = "1"\n',
        '[project]\nname = "expected"\nversion = ""\n',
        'project = "not-a-table"\n',
    ],
)
def test_project_metadata_fails_closed(tmp_path: Path, content: str) -> None:
    root = tmp_path / "component"
    _write(root / "pyproject.toml", content)
    with pytest.raises(composition.CompositionError):
        composition._project_metadata(root, "expected")


@pytest.mark.parametrize("document", [{"bad": {1, 2}}, {"bad": float("nan")}, "\ud800"])
def test_canonical_json_digest_rejects_non_json_values(document: object) -> None:
    with pytest.raises(composition.CompositionError, match="canonicalize JSON"):
        composition._canonical_json_sha256(document)


def test_primitives_digest_requires_readable_contract_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "AstralPrimitives"
    with pytest.raises(composition.CompositionError, match="has no"):
        composition.compute_primitives_digest(root)

    source = root / "src/astralprims/a.py"
    _write(source, "value = 1\n")
    original_read = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == source:
            raise OSError("private path")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(composition.CompositionError, match="could not frame"):
        composition.compute_primitives_digest(root)


def _write_migration(root: Path, source: str) -> Path:
    path = root / "src/astralplane/database/migrations.py"
    _write(path, source)
    return root


def test_plane_migration_digest_includes_static_verifiers_and_starred_statements(
    tmp_path: Path,
) -> None:
    source = (
        'PREFIX = ("SELECT 1",)\n'
        'REMAINDER = ("SELECT 2",)\n'
        "STATEMENTS = (*PREFIX, *REMAINDER)\n"
        'CURRENT = _statements_checksum(("VERIFY CURRENT",))\n'
        'PREDECESSOR_DIGESTS = ("abc",)\n'
        "PREDECESSOR = _statements_checksum((json.dumps("
        "PREDECESSOR_DIGESTS, ensure_ascii=True, separators=(',', ':')),))\n"
        'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
        "checksum=_statements_checksum(STATEMENTS), operation=_apply)\n"
        "MIGRATION_REGISTRY = MigrationRegistry(\n"
        "    (M,),\n"
        "    current_schema_verifier=verify_current,\n"
        "    current_schema_verifier_checksum=CURRENT,\n"
        "    predecessor_schema_verifier=verify_predecessor,\n"
        "    predecessor_schema_verifier_checksum=PREDECESSOR,\n"
        ")\n"
    )
    root = _write_migration(tmp_path / "AstralPlane", source)
    statement_checksum = hashlib.sha256(b'["SELECT 1","SELECT 2"]').hexdigest()
    current_checksum = hashlib.sha256(b'["VERIFY CURRENT"]').hexdigest()
    predecessor_document = json.dumps(
        ("abc",), ensure_ascii=True, separators=(",", ":")
    )
    predecessor_checksum = hashlib.sha256(
        json.dumps(
            (predecessor_document,), ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()
    manifest = [
        {
            "checksum": statement_checksum,
            "name": "x",
            "source_revisions": ["<empty>"],
            "target_revision": "1",
        },
        {
            "checksum": current_checksum,
            "name": "@current-schema-verifier",
            "source_revisions": [],
            "target_revision": "@current",
        },
        {
            "checksum": predecessor_checksum,
            "name": "@predecessor-schema-verifier",
            "source_revisions": [],
            "target_revision": "@predecessor",
        },
    ]
    expected = hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()

    assert composition._plane_migration_digest(root) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            'M = Migration(name="x")\nMIGRATION_REGISTRY = MigrationRegistry((M,))\n',
            "declaration is incomplete",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            'checksum="bad", operation=_apply)\n'
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "checksum is not derived",
        ),
        (
            'S = ["SELECT 1"]\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "statements are not a string tuple",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry([M])\n",
            "registry is not an explicit tuple",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n",
            "registry declaration is missing",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M, M))\n",
            "registry contains duplicate declarations",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            'OTHER = Migration(name="y", source_revisions=(None,), target_revision="2", '
            "checksum=_statements_checksum(S), operation=_apply_other)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "registry and declarations disagree",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(1,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "source revisions are invalid",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,), unsupported=True)\n",
            "unsupported keywords",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry("
            "(M,), current_schema_verifier=verify)\n",
            "verifier declaration is incomplete",
        ),
        (
            'S = ("SELECT 1",)\n'
            'C = "bad"\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry("
            "(M,), current_schema_verifier=verify, "
            "current_schema_verifier_checksum=C)\n",
            "verifier checksum is invalid",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S))\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "declaration is incomplete",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", name="x", source_revisions=(None,), '
            'target_revision="1", checksum=_statements_checksum(S), '
            "operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "keywords are ambiguous",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply, **OPTIONS)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "keyword expansion",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply, unsupported=True)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "unsupported keywords",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration(name="x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=factory())\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "operation is not a static symbol",
        ),
        (
            'S = ("SELECT 1",)\n'
            'M = Migration("x", source_revisions=(None,), target_revision="1", '
            "checksum=_statements_checksum(S), operation=_apply)\n"
            "MIGRATION_REGISTRY = MigrationRegistry((M,))\n",
            "must use exact keyword fields",
        ),
    ],
)
def test_plane_migration_digest_rejects_ambiguous_source(
    tmp_path: Path, source: str, message: str
) -> None:
    root = _write_migration(tmp_path / "AstralPlane", source)
    with pytest.raises(composition.CompositionError, match=message):
        composition._plane_migration_digest(root)


def test_component_contract_specific_malformed_inputs_are_fail_closed(
    checkout: Path,
) -> None:
    manifest = json.loads(
        (checkout / "config/astral-composition.json").read_text("utf-8")
    )

    projection = checkout / COMPONENT_PATHS["astral-projection"]
    _write(projection / "contracts/ui_protocol.json", '{"version": true}\n')
    with pytest.raises(composition.CompositionError, match="version is invalid"):
        composition._verify_projection(
            projection,
            manifest["components"]["astral-projection"],
            manifest["compatibility"],
            [],
        )

    plane = checkout / COMPONENT_PATHS["astral-plane"]
    _write(plane / "src/astralplane/__init__.py", '__all__ = ("CONTRACT_VERSION",)\n')
    diagnostics: list[Any] = []
    composition._verify_component_contract(
        "astral-plane",
        plane,
        manifest["components"]["astral-plane"],
        manifest["compatibility"],
        diagnostics,
    )
    assert [item.code for item in diagnostics] == ["E_INCOMPATIBLE_CONTRACT"]

    primitives = checkout / COMPONENT_PATHS["astral-primitives"]
    with pytest.raises(
        composition.CompositionError, match="primitives contract is missing"
    ):
        composition._verify_primitives(
            primitives,
            manifest["components"]["astral-primitives"],
            {},
            [],
        )

    lets = checkout / COMPONENT_PATHS["lets"]
    _write(lets / "protocol/openapi.yaml", "[]\n")
    with pytest.raises(composition.CompositionError, match="OpenAPI contract"):
        composition._verify_lets(
            lets,
            manifest["components"]["lets"],
            manifest["compatibility"],
            [],
        )


def test_gitlink_mode_and_local_metadata_failures_are_attributed(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = checkout / "ordinary-file"
    _write(marker, "ordinary\n")
    blob = _git(checkout, "hash-object", "-w", str(marker))
    _git(
        checkout,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},{COMPONENT_PATHS['astral-primitives']}",
    )
    mode_report = _verify(checkout)
    assert "E_GITLINK_MODE" in _codes(mode_report, "astral-primitives")

    original_gitlink = composition._gitlink

    def invalid_gitlink(root: Path, relative_path: str) -> Any:
        if relative_path == COMPONENT_PATHS["lets"]:
            raise composition.CompositionError("redacted malformed index")
        return original_gitlink(root, relative_path)

    monkeypatch.setattr(composition, "_gitlink", invalid_gitlink)
    invalid_report = _verify(checkout)
    assert "E_GITLINK_INVALID" in _codes(invalid_report, "lets")


def test_missing_origin_and_status_failure_fail_closed(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(checkout / COMPONENT_PATHS["lets"], "remote", "remove", "origin")
    missing_origin = _verify(checkout)
    assert "E_WRONG_URL" in _codes(missing_origin, "lets")

    original_output = composition._git_output

    def status_fails(cwd: Path, *arguments: str) -> str:
        if cwd.name == "AstralPlane" and arguments and arguments[0] == "status":
            raise composition.CompositionError("status unavailable")
        return original_output(cwd, *arguments)

    monkeypatch.setattr(composition, "_git_output", status_fails)
    failed_status = _verify(checkout)
    assert "E_DIRTY_COMPONENT" in _codes(failed_status, "astral-plane")


def test_non_directory_component_and_false_git_worktree_are_unavailable(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lets = checkout / COMPONENT_PATHS["lets"]
    lets.rename(checkout / "saved-lets")
    _write(lets, "not a directory\n")
    file_report = _verify(checkout)
    assert "E_COMPONENT_MISSING" in _codes(file_report, "lets")

    original_output = composition._git_output

    def not_worktree(cwd: Path, *arguments: str) -> str:
        if cwd.name == "AstralPlane" and arguments[-1:] == ("--is-inside-work-tree",):
            return "false"
        return original_output(cwd, *arguments)

    monkeypatch.setattr(composition, "_git_output", not_worktree)
    false_worktree = _verify(checkout)
    assert "E_PRIVATE_ACCESS" in _codes(false_worktree, "astral-plane")


def test_real_checkout_verification_executes_only_local_git_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = subprocess.run
    commands: list[tuple[str, ...]] = []

    def recording_run(arguments: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(tuple(arguments))
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(composition.subprocess, "run", recording_run)
    report = composition.verify_composition(REPOSITORY_ROOT)

    assert report.ok, report.to_dict()
    assert commands
    assert all(command[0] == "git" for command in commands)
    forbidden = {"clone", "fetch", "ls-remote", "pull", "push", "remote-ext"}
    assert all(not forbidden.intersection(command) for command in commands)
