"""Declarative checks for AstralDeep's exact local-component contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

EXPECTED: dict[str, dict[str, Any]] = {
    "astral-projection": {
        "distribution": "astralprojection",
        "version": "0.1.0",
        "path": "components/AstralProjection",
        "contract": "astralprojection.contract/v1",
        "availability": "required-embedded",
        "import": "astralprojection",
        "extras": [],
    },
    "astral-plane": {
        "distribution": "astralplane",
        "version": "0.1.0",
        "path": "components/AstralPlane",
        "contract": "astralplane.contract/v1",
        "availability": "required-embedded",
        "import": "astralplane",
        "extras": [],
    },
    "astral-primitives": {
        "distribution": "astralprims",
        "version": "0.3.0",
        "path": "components/AstralPrimitives",
        "contract": "0.3.0",
        "availability": "required-embedded",
        "import": "astralprims",
        "extras": [],
    },
    "lets": {
        "distribution": "lets-agent",
        "version": "1.0.10",
        "path": "components/LETS",
        "contract": "1.0.10",
        "availability": "external-feature-gated",
        "import": "lets",
        "extras": ["client"],
    },
}


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def test_local_component_contract_matches_manifest_and_package_metadata() -> None:
    root_metadata = _toml(REPOSITORY_ROOT / "pyproject.toml")
    local = root_metadata["tool"]["astraldeep"]["local-components"]
    assert local["format"] == "astraldeep.local-components/v1"
    assert local["manifest"] == "config/astral-composition.json"

    manifest_path = REPOSITORY_ROOT / local["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {
        key: value for key, value in local.items() if key not in {"format", "manifest"}
    }
    assert entries == EXPECTED
    assert set(manifest["components"]) == set(EXPECTED)
    assert set(manifest["availability"]) == set(EXPECTED)

    for key, expected in EXPECTED.items():
        declared = manifest["components"][key]
        assert declared["path"] == expected["path"]
        assert declared["contract_version"] == expected["contract"]
        assert manifest["availability"][key] == expected["availability"]

        component_root = (REPOSITORY_ROOT / expected["path"]).resolve(strict=True)
        assert component_root.parent == (REPOSITORY_ROOT / "components").resolve()
        project = _toml(component_root / "pyproject.toml")["project"]
        assert project["name"] == expected["distribution"]
        assert project["version"] == expected["version"]
        for extra in expected["extras"]:
            assert extra in project.get("optional-dependencies", {})
