"""Focused tests for the offline feature-074 composition verifier."""

from __future__ import annotations

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
        '[project]\nname = "lets-agent"\nversion = "1.0.10"\n',
    )
    _write(component / "src/lets/__init__.py", '__version__ = "1.0.10"\n')
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
                "info": {"title": "LETS Warden API", "version": "1.0.10"},
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
                "ref": "v1.0.10",
                "contract_version": "1.0.10",
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
                "release": "v1.0.10",
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


def test_lets_v1_0_10_public_exports_are_required(checkout: Path) -> None:
    executor = checkout / COMPONENT_PATHS["lets"] / "src/lets/executor.py"
    _write(executor, "class InternalReceiptVerifier: pass\n")
    _repin_component(checkout, "lets")

    report = _verify(checkout)
    export_errors = [
        item for item in report.diagnostics if item.code == "E_LETS_PUBLIC_EXPORT"
    ]

    assert [item.message for item in export_errors] == [
        "LETS v1.0.10 public export 'ReceiptVerifier' is unavailable"
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
