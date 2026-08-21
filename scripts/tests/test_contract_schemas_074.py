"""Behavioral tests for feature-074 implementation-owned JSON schemas."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPOSITORY_ROOT / "contracts"
PLANNING_CONTRACTS = (
    REPOSITORY_ROOT / "specs" / "074-multirepo-lets-integration" / "contracts"
)
SHA1 = "1" * 40
SHA256 = "2" * 64


def _validator(name: str) -> Draft202012Validator:
    document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(
        document, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


@pytest.mark.parametrize(
    ("implementation_name", "planning_name"),
    [
        ("system-composition.schema.json", "composition-manifest.schema.json"),
        ("case-study-evidence.schema.json", "case-study-evidence.schema.json"),
    ],
)
def test_promoted_schema_matches_planning_contract_except_identity(
    implementation_name: str, planning_name: str
) -> None:
    implementation = json.loads(
        (CONTRACTS / implementation_name).read_text(encoding="utf-8")
    )
    planning = json.loads(
        (PLANNING_CONTRACTS / planning_name).read_text(encoding="utf-8")
    )
    implementation.pop("$id")
    planning.pop("$id")
    assert implementation == planning


def _composition() -> dict[str, object]:
    def component(repository: str, path: str, contract: str) -> dict[str, object]:
        return {
            "repository": repository,
            "path": path,
            "commit": SHA1,
            "contract_version": contract,
            "artifact_sha256": None,
        }

    lets = component(
        "https://github.com/AstralDeep/LETS.git",
        "components/LETS",
        "1.0.10",
    )
    lets["ref"] = "v1.0.10"
    return {
        "format": "astral.composition/v1",
        "astraldeep_contract_version": "astraldeep.composition/v1",
        "components": {
            "astral-projection": component(
                "https://github.com/AstralDeep/AstralProjection.git",
                "components/AstralProjection",
                "astralprojection.contract/v1",
            ),
            "astral-plane": component(
                "https://github.com/AstralDeep/AstralPlane.git",
                "components/AstralPlane",
                "astralplane.contract/v1",
            ),
            "astral-primitives": component(
                "https://github.com/AstralDeep/AstralPrimitives.git",
                "components/AstralPrimitives",
                "0.3.0",
            ),
            "lets": lets,
        },
        "availability": {
            "astral-projection": "required-embedded",
            "astral-plane": "required-embedded",
            "astral-primitives": "required-embedded",
            "lets": "external-feature-gated",
        },
        "compatibility": {
            "ui_protocol": {"version": "1", "sha256": SHA256},
            "data_plane": {
                "contract_version": "astralplane.contract/v1",
                "schema_revision": "067.001",
                "read_compatible_from": "066.001",
                "migration_sha256": SHA256,
                "blob_layout_version": "astralplane.blob-layout/v1",
            },
            "primitives": {"package_version": "0.3.0", "contract_sha256": SHA256},
            "lets": {
                "release": "v1.0.10",
                "api_version": "v1",
                "openapi_sha256": SHA256,
                "receipt_wire_type": "lets.receipt/v1",
                "scope_profile_version": "astral.tools/v1",
            },
        },
    }


def _evidence() -> dict[str, object]:
    return {
        "format": "lets.case-study-evidence/v1",
        "evidence_class": "astral-integration",
        "lets_release": "v1.0.10",
        "repositories": {
            "astraldeep": SHA1,
            "astral-projection": SHA1,
            "astral-plane": SHA1,
            "astral-primitives": SHA1,
            "lets": SHA1,
        },
        "composition_sha256": SHA256,
        "policy_digest": f"sha256:{SHA256}",
        "machine_digest": f"sha256:{SHA256}",
        "config_epoch": 1,
        "scope_profile": "astral.tools/v1",
        "mode": "enforce",
        "environment": {"os": "Windows", "python": "3.11.15", "workers": 1},
        "commands": [
            {
                "id": "case-study",
                "argv": ["python", "benchmarks/astraldeep/run_case_study.py"],
                "exit_code": 0,
                "started_at": "2026-08-13T23:00:00Z",
                "finished_at": "2026-08-13T23:01:00Z",
                "stdout_sha256": SHA256,
                "stderr_sha256": SHA256,
            }
        ],
        "artifacts": [
            {
                "kind": "raw",
                "relative_path": "raw/run.json",
                "sha256": SHA256,
                "bytes": 1,
            }
        ],
        "measurements": [
            {
                "name": "authorization latency",
                "unit": "ms",
                "sample_count": 1,
                "summary": {"p50": 1.0},
                "source_artifact": "raw/run.json",
            }
        ],
        "sanitization": {
            "profile": "astral.case-study-public/v1",
            "scanner_sha256": SHA256,
            "findings": 0,
        },
        "reproduced_at": "2026-08-13T23:01:00Z",
    }


def test_composition_accepts_exported_component_contract_identifiers() -> None:
    _validator("system-composition.schema.json").validate(_composition())


@pytest.mark.parametrize(
    ("component", "contract"),
    [
        ("astral-projection", "astralprojection.contract/v2"),
        ("astral-plane", "astralplane.contract/v2"),
    ],
)
def test_composition_rejects_wrong_component_contract(
    component: str, contract: str
) -> None:
    document = _composition()
    document["components"][component]["contract_version"] = contract  # type: ignore[index]
    with pytest.raises(ValidationError):
        _validator("system-composition.schema.json").validate(document)


def test_composition_requires_signed_lets_ref() -> None:
    document = _composition()
    del document["components"]["lets"]["ref"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        _validator("system-composition.schema.json").validate(document)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("host", "foo/v1"),
        ("plane_compatibility", "astralplane.contract/v2"),
        ("primitives_component", "9.9.9"),
        ("lets_component", "9.9.9"),
    ],
)
def test_composition_rejects_cross_component_contract_mismatch(
    target: str, value: str
) -> None:
    document = _composition()
    if target == "host":
        document["astraldeep_contract_version"] = value
    elif target == "plane_compatibility":
        document["compatibility"]["data_plane"]["contract_version"] = value  # type: ignore[index]
    elif target == "primitives_component":
        document["components"]["astral-primitives"]["contract_version"] = value  # type: ignore[index]
    else:
        document["components"]["lets"]["contract_version"] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        _validator("system-composition.schema.json").validate(document)


def test_case_study_accepts_sanitized_digest_bound_evidence() -> None:
    _validator("case-study-evidence.schema.json").validate(_evidence())


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("artifact", "..\\secret.txt"),
        ("source", "../../not-retained.json"),
        ("argument", "--token=secret-value"),
        ("environment", "api_token"),
        ("artifact", "raw//run.json"),
        ("artifact", "raw/CON"),
        ("artifact", "raw/file.txt:stream"),
    ],
)
def test_case_study_rejects_unsafe_paths_and_secret_shaped_metadata(
    mutation: str, value: str
) -> None:
    document = copy.deepcopy(_evidence())
    if mutation == "artifact":
        document["artifacts"][0]["relative_path"] = value  # type: ignore[index]
    elif mutation == "source":
        document["measurements"][0]["source_artifact"] = value  # type: ignore[index]
    elif mutation == "argument":
        document["commands"][0]["argv"].append(value)  # type: ignore[index]
    else:
        document["environment"][value] = "redacted"  # type: ignore[index]
    assert list(_validator("case-study-evidence.schema.json").iter_errors(document))


def test_case_study_requires_output_digests_and_sanitization_attestation() -> None:
    document = _evidence()
    del document["commands"][0]["stdout_sha256"]  # type: ignore[index]
    del document["sanitization"]
    errors = list(_validator("case-study-evidence.schema.json").iter_errors(document))
    assert len(errors) >= 2


def test_case_study_rejects_secret_shaped_notes_and_exclusions() -> None:
    document = _evidence()
    document["notes"] = "password=secret-value"
    document["measurements"][0]["exclusions"] = ["api_key: secret-value"]  # type: ignore[index]
    assert list(_validator("case-study-evidence.schema.json").iter_errors(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("branch", "feature.lock"),
        ("sourceRef", "refs/heads/feature..other"),
        ("selectionRoot", "backend//rote"),
    ],
)
def test_extraction_schema_rejects_noncanonical_refs_and_paths(
    field: str, value: str
) -> None:
    document = {
        "format": "astral.extraction-provenance/v1",
        "digestAlgorithm": "sha256",
        "source": {
            "repository": "https://github.com/AstralDeep/AstralDeep.git",
            "commit": SHA1,
            "tree": SHA1,
        },
        "destination": {
            "repository": "https://github.com/AstralDeep/AstralProjection.git",
            "branch": "codex/074-extract-projection",
            "legacyBaseline": {
                "sourceRef": "refs/heads/main",
                "commit": SHA1,
                "observedAt": "2026-08-13T23:00:00Z",
            },
        },
        "selectionRoots": ["backend/rote"],
        "entries": [
            {
                "sourcePath": "backend/rote/__init__.py",
                "destinationPath": "backend/rote/__init__.py",
                "mode": "100644",
                "blob": SHA1,
                "bytes": 1,
            }
        ],
        "manifestSha256": SHA256,
    }
    if field == "branch":
        document["destination"]["branch"] = value  # type: ignore[index]
    elif field == "sourceRef":
        document["destination"]["legacyBaseline"]["sourceRef"] = value  # type: ignore[index]
    else:
        document["selectionRoots"] = [value]
    assert list(_validator("extraction-provenance.schema.json").iter_errors(document))
