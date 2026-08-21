"""Static ownership guards for the AstralProjection cutover."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PROJECTION = ROOT / "components" / "AstralProjection"
HOST_SURFACES = frozenset(
    {
        "admin_tools",
        "agents",
        "attachments",
        "audit",
        "authoring",
        "drafts",
        "llm",
        "llm_system",
        "personalization",
        "pulse",
        "remote_machines",
        "theme",
        "tour",
        "workspace_timeline",
    }
)
PROJECTION_OWNED_ROOTS = (
    "backend/webrender",
    "backend/rote",
    "windows-client",
    "android-client",
    "apple-clients",
    "tooling/web-ci",
)
PLANE_SEMANTIC_ADAPTERS = frozenset(
    {
        "backend/audit/repository.py",
        "backend/feedback/repository.py",
        "backend/llm_config/user_store.py",
        "backend/onboarding/repository.py",
        "backend/orchestrator/attachments/message_attachment_repo.py",
        "backend/orchestrator/attachments/parser_repo.py",
        "backend/orchestrator/attachments/repository.py",
        "backend/orchestrator/attachments/blob_access.py",
        "backend/orchestrator/attachments/materialization.py",
        "backend/orchestrator/attachments/purge.py",
        "backend/orchestrator/plane_repository_context.py",
        "backend/personalization/repository.py",
        "backend/qual_audit/database.py",
        "backend/scheduler/store.py",
    }
)


def test_projection_owned_sources_exist_only_in_component() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("repository ownership metadata is absent from the product image")
    for relative in PROJECTION_OWNED_ROOTS:
        tracked = subprocess.run(
            ["git", "ls-files", "--", relative],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        # Filter paths deleted in the working-tree cutover so this guard works
        # both before and after the parent stages the removals.
        remaining = {
            path
            for line in tracked.stdout.splitlines()
            if line and (path := ROOT / line).is_file()
        }
        assert remaining == set(), relative
        assert (PROJECTION / relative).exists(), f"missing Projection owner: {relative}"

    assert not (BACKEND / "shared" / "ui_protocol.json").exists()
    assert (PROJECTION / "contracts" / "ui_protocol.json").is_file()


def test_deep_host_surface_registry_owns_only_orchestrator_adapters() -> None:
    host_root = BACKEND / "orchestrator" / "projection_surfaces"
    assert {path.stem for path in host_root.glob("*.py")} == HOST_SURFACES | {
        "__init__"
    }

    registry = ast.parse((host_root / "__init__.py").read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value
        for node in registry.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    mapping = constants["SURFACE_MODULES"]
    assert isinstance(mapping, ast.Dict)
    registered = {
        ast.literal_eval(key): ast.literal_eval(value)
        for key, value in zip(mapping.keys, mapping.values, strict=True)
    }
    assert {
        module.removeprefix("orchestrator.projection_surfaces.")
        for module in registered.values()
        if module.startswith("orchestrator.projection_surfaces.")
    } == HOST_SURFACES
    assert registered["guide"] == "webrender.chrome.surfaces.guide"


def test_installed_projection_surface_package_does_not_import_deep() -> None:
    from webrender.chrome import surfaces

    registry = Path(surfaces.__file__).read_text(encoding="utf-8")
    assert "orchestrator" not in registry
    assert "SURFACE_MODULES" not in registry
    assert "collect_handlers" not in registry


def test_ownership_contract_declares_no_generated_mutable_copies() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "component-ownership.json").read_text(encoding="utf-8")
    )
    assert contract["generatedCopies"] == []
    adapters = {
        item["path"]: item for item in contract["compatibilityAdapters"]
    }
    assert "backend/orchestrator/projection_surfaces/" in adapters


def test_plane_semantic_adapters_are_not_mislabeled_as_durable_copies() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "component-ownership.json").read_text(encoding="utf-8")
    )
    adapters = {
        item["path"]: item for item in contract["compatibilityAdapters"]
    }
    assert PLANE_SEMANTIC_ADAPTERS <= adapters.keys()
    for path in PLANE_SEMANTIC_ADAPTERS:
        assert (ROOT / path).is_file()
        assert adapters[path]["upstreamOwner"] == "astralplane"
        rule = adapters[path]["rule"].lower()
        assert "no sql" in rule
        assert "driver access" in rule
        assert "durable repository implementation" in rule

    durable_domain = next(
        domain
        for domain in contract["ownershipDomains"]
        if domain["id"] == "durable-data-plane"
    )
    deep_forbidden = next(
        item["paths"]
        for item in durable_domain["forbiddenCopies"]
        if item["repository"] == "astraldeep"
    )
    assert PLANE_SEMANTIC_ADAPTERS.isdisjoint(deep_forbidden)
    assert {
        "backend/orchestrator/attachments/store.py",
        "backend/shared/database.py",
    } <= set(deep_forbidden)


def test_deep_runtime_has_no_import_of_removed_surface_modules() -> None:
    forbidden = tuple(f"webrender.chrome.surfaces.{name}" for name in HOST_SURFACES)
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        assert not any(name in source for name in forbidden), path
