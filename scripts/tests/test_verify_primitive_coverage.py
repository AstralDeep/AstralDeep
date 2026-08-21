"""Tests for the feature-074 primitive-vocabulary decision gate."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_primitive_coverage.py"

spec = importlib.util.spec_from_file_location("verify_primitive_coverage", SCRIPT)
assert spec and spec.loader
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


def _fixture(tmp_path: Path, *, version: str = "0.3.0") -> tuple[Path, Path]:
    primitives = tmp_path / "AstralPrimitives"
    source = primitives / "src" / "astralprims"
    source.mkdir(parents=True)
    (primitives / "pyproject.toml").write_text(
        f'[project]\nname = "astralprims"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (source / "primitives.py").write_text(
        "from typing import Literal\n"
        "class Text:\n"
        "    type: Literal['text'] = 'text'\n"
        "class Button:\n"
        "    type: Literal['button'] = 'button'\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "ui_protocol.json"
    return primitives, manifest


def _write_manifest(path: Path, component_types: list[str]) -> None:
    path.write_text(
        json.dumps({"version": 1, "component_types": component_types}),
        encoding="utf-8",
    )


def test_existing_vocabulary_reuses_release(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button", "skeleton"])

    result = coverage.build_inventory(manifest, primitives)

    assert result["decision"] == "reuse-existing-vocabulary"
    assert result["primitiveTypes"] == ["button", "text"]
    assert result["projectionLocalTypes"] == [
        {
            "type": "skeleton",
            "reason": "Projection-owned transient loading state",
        }
    ]
    assert result["unknownTypes"] == []


def test_unknown_primitive_requires_change(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button", "unreviewed_widget"])

    result = coverage.build_inventory(manifest, primitives)

    assert result["decision"] == "astralprimitives-change-required"
    assert result["unknownTypes"] == ["unreviewed_widget"]


def test_version_below_floor_is_refused(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path, version="0.2.9")
    _write_manifest(manifest, ["text", "button"])

    with pytest.raises(coverage.CoverageError, match="below required floor"):
        coverage.build_inventory(manifest, primitives)


def test_missing_public_primitive_in_manifest_requires_change(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text"])

    result = coverage.build_inventory(manifest, primitives)

    assert result["decision"] == "astralprimitives-change-required"
    assert result["missingManifestTypes"] == ["button"]


def test_decision_never_claims_a_different_package_version(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path, version="0.2.9")
    _write_manifest(manifest, ["text", "button"])

    result = coverage.build_inventory(manifest, primitives, minimum_version="0.2.0")

    assert result["packageVersion"] == "0.2.9"
    assert result["decision"] == "reuse-existing-vocabulary"


@pytest.mark.parametrize(
    ("component_types", "message"),
    [
        ([], "non-empty array"),
        ("text", "non-empty array"),
        (["text", ""], "non-empty strings only"),
        (["text", 1], "non-empty strings only"),
        (["text", "text"], "duplicate values"),
    ],
)
def test_manifest_component_types_are_strict(
    tmp_path: Path,
    component_types: object,
    message: str,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    manifest.write_text(
        json.dumps({"version": 1, "component_types": component_types}),
        encoding="utf-8",
    )

    with pytest.raises(coverage.CoverageError, match=message):
        coverage.build_inventory(manifest, primitives)


def test_manifest_must_be_an_object(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)
    manifest.write_text("[]\n", encoding="utf-8")

    with pytest.raises(coverage.CoverageError, match="manifest must be an object"):
        coverage.build_inventory(manifest, primitives)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{not-json", "could not read JSON"),
        (b"\xff", "could not read JSON"),
    ],
)
def test_manifest_read_errors_fail_closed(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    manifest.write_bytes(payload)

    with pytest.raises(coverage.CoverageError, match=message):
        coverage.build_inventory(manifest, primitives)


def test_missing_manifest_path_is_an_os_error(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)

    with pytest.raises(FileNotFoundError):
        coverage.build_inventory(manifest, primitives)


@pytest.mark.parametrize("version", ["1.0", "01.0.0", "1.0.0-"])
def test_versions_must_use_strict_semver(tmp_path: Path, version: str) -> None:
    primitives, manifest = _fixture(tmp_path, version=version)
    _write_manifest(manifest, ["text", "button"])

    with pytest.raises(coverage.CoverageError, match="not strict SemVer"):
        coverage.build_inventory(manifest, primitives)


def test_prerelease_is_below_its_final_release(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path, version="0.3.0-rc.1")
    _write_manifest(manifest, ["text", "button"])

    with pytest.raises(coverage.CoverageError, match="below required floor"):
        coverage.build_inventory(manifest, primitives, minimum_version="0.3.0")


def test_build_metadata_does_not_lower_release_precedence(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path, version="0.3.0+build.7")
    _write_manifest(manifest, ["text", "button"])

    result = coverage.build_inventory(manifest, primitives, minimum_version="0.3.0")

    assert result["decision"] == "reuse-existing-vocabulary"


def test_prerelease_identifiers_follow_semver_precedence() -> None:
    assert coverage._version_key("0.3.0-alpha.2") < coverage._version_key(
        "0.3.0-alpha.10"
    )
    assert coverage._version_key("0.3.0-1") < coverage._version_key("0.3.0-alpha")


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        ('[project]\nname = "wrong"\nversion = "0.3.0"\n', "declare project"),
        ('[project]\nname = "astralprims"\n', "version is missing"),
        ('project = "astralprims"\n', "declare project"),
        ("[project\n", "could not read TOML"),
    ],
)
def test_package_metadata_is_validated(
    tmp_path: Path,
    pyproject: str,
    message: str,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])
    (primitives / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    with pytest.raises(coverage.CoverageError, match=message):
        coverage.build_inventory(manifest, primitives)


@pytest.mark.parametrize(
    ("source_text", "message"),
    [
        ("def broken(:\n", "could not parse primitive definitions"),
        ("VALUE = 1\n", "no primitive type declarations"),
        (
            "class First:\n"
            "    type: str = 'text'\n"
            "class Second:\n"
            "    type: str = 'text'\n",
            "duplicate primitive type declaration",
        ),
    ],
)
def test_primitive_source_failures_are_rejected(
    tmp_path: Path,
    source_text: str,
    message: str,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])
    (primitives / "src" / "astralprims" / "primitives.py").write_text(
        source_text,
        encoding="utf-8",
    )

    with pytest.raises(coverage.CoverageError, match=message):
        coverage.build_inventory(manifest, primitives)


def test_missing_primitive_source_is_rejected(tmp_path: Path) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])
    (primitives / "src" / "astralprims" / "primitives.py").unlink()

    with pytest.raises(
        coverage.CoverageError,
        match="could not parse primitive definitions",
    ):
        coverage.build_inventory(manifest, primitives)


def test_irrelevant_class_members_do_not_become_primitive_types(
    tmp_path: Path,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])
    source = primitives / "src" / "astralprims" / "primitives.py"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "class Ignored:\n"
        + "    other: str = 'value'\n"
        + "    type = 'not-an-annotated-field'\n"
        + "    type_name: str\n",
        encoding="utf-8",
    )

    result = coverage.build_inventory(manifest, primitives)

    assert result["primitiveTypes"] == ["button", "text"]


def test_primitives_root_must_be_a_directory(tmp_path: Path) -> None:
    manifest = tmp_path / "ui_protocol.json"
    _write_manifest(manifest, ["text"])
    root = tmp_path / "not-a-directory"
    root.write_text("file\n", encoding="utf-8")

    with pytest.raises(coverage.CoverageError, match="not a directory"):
        coverage.build_inventory(manifest, root)


def test_cli_exit_codes_and_machine_readable_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])

    assert (
        coverage.main(
            ["--manifest", str(manifest), "--primitives-root", str(primitives)]
        )
        == 0
    )
    success = json.loads(capsys.readouterr().out)
    assert success["decision"] == "reuse-existing-vocabulary"

    _write_manifest(manifest, ["text", "button", "unknown"])
    assert (
        coverage.main(
            ["--manifest", str(manifest), "--primitives-root", str(primitives)]
        )
        == 1
    )
    change = json.loads(capsys.readouterr().out)
    assert change["unknownTypes"] == ["unknown"]

    assert (
        coverage.main(
            [
                "--manifest",
                str(tmp_path / "missing.json"),
                "--primitives-root",
                str(primitives),
            ]
        )
        == 2
    )
    failure = capsys.readouterr()
    assert failure.out == ""
    assert failure.err.startswith("verify_primitive_coverage: ")


def test_script_entrypoint_propagates_main_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primitives, manifest = _fixture(tmp_path)
    _write_manifest(manifest, ["text", "button"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--manifest",
            str(manifest),
            "--primitives-root",
            str(primitives),
        ],
    )

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["decision"] == (
        "reuse-existing-vocabulary"
    )
