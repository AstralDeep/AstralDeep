"""Checks the root exports hosts import and that facade.json names only real modules.
The root names cover the SQLite URL check and the SQLite structure digest a host reads at boot.
"""

import importlib
import json
from pathlib import Path

import astralplane

FACADE = Path(__file__).resolve().parents[1] / "facade.json"


def test_root_exports_the_sqlite_boot_names() -> None:
    assert "is_sqlite_url" in astralplane.__all__
    assert "SQLITE_SCHEMA_STRUCTURE_DIGEST" in astralplane.__all__
    assert astralplane.is_sqlite_url("sqlite:///tmp/plane.db") is True
    assert astralplane.is_sqlite_url("postgresql://host/db") is False
    assert len(astralplane.SQLITE_SCHEMA_STRUCTURE_DIGEST) == 64


def test_every_root_export_resolves() -> None:
    assert len(astralplane.__all__) == len(set(astralplane.__all__))
    assert [name for name in astralplane.__all__ if not hasattr(astralplane, name)] == []


def test_facade_lists_only_importable_modules() -> None:
    facade = json.loads(FACADE.read_text(encoding="utf-8"))
    assert facade["format"] == "astralplane.facade/v1"
    for name in (*facade["modules"], *facade["packages"]):
        importlib.import_module(name)
    for name in facade["packages"]:
        assert hasattr(importlib.import_module(name), "__path__")


def test_package_version_matches_the_project_metadata() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert f'version = "{astralplane.PACKAGE_VERSION}"' in project.read_text(encoding="utf-8")
