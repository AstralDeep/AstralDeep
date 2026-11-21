"""Tests for the SQLite storage selection in orchestrator/plane_composition.py: profile gating, the personal default location, exact registry pins from config/astral-composition.json, fail-closed refusal of mismatched digests, and a real personal-node composition with no PostgreSQL reachable.
Runs against the pinned AstralPlane package and orchestrator/runtime_composition.py.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

import orchestrator.plane_composition as composition
from astralplane import (
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    MIGRATION_DIGEST,
    READ_COMPATIBLE_FROM,
    SCHEMA_REVISION,
    SQLITE_MIGRATION_DIGEST,
)
from astralplane.database.sqlite_migrations import SQLITE_SCHEMA_STRUCTURE_DIGEST
from orchestrator.plane_composition import (
    PlaneCompositionError,
    PlaneContractExpectation,
    SqliteRegistryExpectation,
    compose_plane_catalog,
    compose_plane_from_environment,
    compose_plane_runtime,
    inspect_plane_composition,
    load_plane_expectation,
    plane_backend,
    resolve_plane_database_url,
)
from orchestrator.runtime_composition import compose_astral_runtime

MANIFEST = Path(__file__).resolve().parents[2] / "config" / "astral-composition.json"
PERSONAL = {"ASTRAL_NODE_PROFILE": "personal"}


def _expectation() -> PlaneContractExpectation:
    return PlaneContractExpectation(
        contract_version=CONTRACT_VERSION,
        schema_revision=SCHEMA_REVISION,
        read_compatible_from=READ_COMPATIBLE_FROM,
        migration_sha256=MIGRATION_DIGEST,
        blob_layout_version=BLOB_LAYOUT_VERSION,
        sqlite=SqliteRegistryExpectation(
            schema_revision=SCHEMA_REVISION,
            migration_sha256=SQLITE_MIGRATION_DIGEST,
            structure_sha256=SQLITE_SCHEMA_STRUCTURE_DIGEST,
        ),
    )


def _environment(root: Path, identity: str | None = None) -> dict[str, str]:
    return {
        **PERSONAL,
        "ASTRAL_NODE_DATA_DIR": str(root / "node"),
        "ATTACHMENT_UPLOAD_ROOT": str(root / "blobs"),
        "PERSONAL_AGENT_ARTIFACT_ROOT": str(root / "agents"),
        "ASTRALPLANE_IDENTITY": identity or f"node-{uuid4().hex}",
    }


def _forbid_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PostgreSQL must not be contacted")

    monkeypatch.setattr(composition, "create_postgres_runtime", refuse)


def test_backend_is_selected_by_the_database_url() -> None:
    assert plane_backend("sqlite:///var/node/plane.sqlite3") == "sqlite"
    assert plane_backend("SQLITE:///var/node/plane.sqlite3") == "sqlite"
    assert plane_backend("postgresql://astral@db.invalid/astral") == "postgresql"


def test_manifest_pins_the_exact_sqlite_registry_of_the_installed_plane() -> None:
    expectation = load_plane_expectation(MANIFEST)
    assert expectation.sqlite == SqliteRegistryExpectation(
        schema_revision=SCHEMA_REVISION,
        migration_sha256=SQLITE_MIGRATION_DIGEST,
        structure_sha256=SQLITE_SCHEMA_STRUCTURE_DIGEST,
    )
    assert inspect_plane_composition(expectation, "sqlite").compatible
    assert inspect_plane_composition(expectation, "postgresql").compatible
    assert expectation.migration_sha256 == MIGRATION_DIGEST != SQLITE_MIGRATION_DIGEST


def test_personal_profile_defaults_to_a_sqlite_file_under_the_node_data_directory(
    tmp_path: Path,
) -> None:
    url = resolve_plane_database_url({**PERSONAL, "ASTRAL_NODE_DATA_DIR": str(tmp_path)})
    assert url == f"sqlite://{tmp_path.resolve() / 'plane.sqlite3'}"
    assert plane_backend(url) == "sqlite"
    default = resolve_plane_database_url(PERSONAL)
    assert default.startswith("sqlite:///")
    assert default.endswith("/data/node/plane.sqlite3")


def test_personal_profile_still_honours_explicit_postgresql_settings() -> None:
    explicit = "postgresql://user:secret@db.invalid:5432/astral"
    assert resolve_plane_database_url({**PERSONAL, "DATABASE_URL": explicit}) == explicit
    split = resolve_plane_database_url({**PERSONAL, "DB_HOST": "db.invalid"})
    assert split.startswith("postgresql://") and "@db.invalid:5432/" in split


def test_shared_deployment_stays_on_postgresql_and_refuses_sqlite() -> None:
    assert resolve_plane_database_url({}).startswith("postgresql://")
    for profile in ({}, {"ASTRAL_NODE_PROFILE": "institution"}):
        with pytest.raises(PlaneCompositionError, match="personal node profile"):
            resolve_plane_database_url(
                {**profile, "DATABASE_URL": "sqlite:///var/node/plane.sqlite3"}
            )


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("schema_revision", "sqlite_schema_revision_mismatch"),
        ("migration_sha256", "sqlite_migration_digest_mismatch"),
        ("structure_sha256", "sqlite_structure_digest_mismatch"),
    ],
)
def test_every_sqlite_registry_mismatch_is_refused(field: str, reason: str) -> None:
    pinned = _expectation().sqlite
    assert pinned is not None
    wrong = "999.999" if field == "schema_revision" else "0" * 64
    expectation = replace(_expectation(), sqlite=replace(pinned, **{field: wrong}))
    report = inspect_plane_composition(expectation, "sqlite")
    assert report.compatible is False
    assert report.reasons == (reason,)
    with pytest.raises(PlaneCompositionError, match=reason):
        compose_plane_catalog(expectation, "sqlite")
    assert inspect_plane_composition(expectation, "postgresql").compatible


def test_sqlite_without_a_pinned_registry_is_refused() -> None:
    expectation = replace(_expectation(), sqlite=None)
    assert inspect_plane_composition(expectation, "sqlite").reasons == (
        "sqlite_registry_unpinned",
    )
    assert inspect_plane_composition(expectation, "postgresql").compatible


def test_postgresql_digest_is_not_the_sqlite_expectation() -> None:
    expectation = replace(_expectation(), migration_sha256="0" * 64)
    assert inspect_plane_composition(expectation, "postgresql").reasons == (
        "migration_digest_mismatch",
    )
    assert inspect_plane_composition(expectation, "sqlite").compatible


def test_unknown_backend_is_refused() -> None:
    with pytest.raises(PlaneCompositionError, match="not recognized"):
        inspect_plane_composition(_expectation(), "mysql")


def test_manifest_sqlite_block_is_required_and_strict(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    path = tmp_path / "composition.json"

    def write(value: object) -> None:
        document["compatibility"]["data_plane_sqlite"] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    block = dict(document["compatibility"]["data_plane_sqlite"])
    del document["compatibility"]["data_plane_sqlite"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PlaneCompositionError, match="must be an object"):
        load_plane_expectation(path)
    write({**block, "extra": "no"})
    with pytest.raises(PlaneCompositionError, match="keys differ"):
        load_plane_expectation(path)
    write({key: value for key, value in block.items() if key != "structure_sha256"})
    with pytest.raises(PlaneCompositionError, match="missing=\\['structure_sha256'\\]"):
        load_plane_expectation(path)
    write({**block, "migration_sha256": "not-a-digest"})
    with pytest.raises(PlaneCompositionError, match="sqlite.migration_sha256"):
        load_plane_expectation(path)


def test_sqlite_composition_creates_a_private_database_without_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    environment = _environment(tmp_path)
    plane = compose_plane_from_environment(MANIFEST, environ=environment)
    try:
        assert plane.contract.backend == "sqlite"
        assert plane.runtime.health().ready
        database = tmp_path / "node" / "plane.sqlite3"
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    finally:
        plane.close()
    reopened = compose_plane_from_environment(
        MANIFEST, environ={**environment, "ASTRALPLANE_IDENTITY": f"node-{uuid4().hex}"}
    )
    try:
        assert reopened.runtime.health().ready
    finally:
        reopened.close()


def test_sqlite_reconciliation_digest_differs_from_postgresql() -> None:
    sqlite_digest = composition._product_reconciler(
        compose_plane_catalog(_expectation(), "sqlite")
    ).composition_digest
    postgresql_digest = composition._product_reconciler(
        compose_plane_catalog(_expectation(), "postgresql")
    ).composition_digest
    assert sqlite_digest != postgresql_digest


def test_mismatched_registry_refuses_before_any_database_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    pinned = _expectation().sqlite
    assert pinned is not None
    stale = replace(_expectation(), sqlite=replace(pinned, structure_sha256="0" * 64))
    target = tmp_path / "node" / "plane.sqlite3"
    with pytest.raises(PlaneCompositionError, match="sqlite_structure_digest_mismatch"):
        compose_plane_runtime(
            stale,
            database_url=f"sqlite://{target}",
            blob_root=tmp_path / "blobs",
            personal_agent_artifact_root=tmp_path / "agents",
            identity=f"node-{uuid4().hex}",
        )
    assert not target.exists()
    assert not target.parent.exists()


def test_a_database_file_that_is_not_plane_sqlite_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    environment = _environment(tmp_path)
    (tmp_path / "node").mkdir()
    foreign = tmp_path / "node" / "plane.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(Exception, match="neither empty nor an AstralPlane SQLite"):
        compose_plane_from_environment(MANIFEST, environ=environment)


def test_a_group_readable_database_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    url = f"sqlite://{tmp_path / 'plane.sqlite3'}"
    database = tmp_path / "plane.sqlite3"
    database.write_bytes(b"")
    monkeypatch.setattr(composition, "_sqlite_path", lambda _url: database)
    database.chmod(0o644)
    with pytest.raises(PlaneCompositionError, match="private to its owner"):
        composition._require_private_sqlite_file(url)
    monkeypatch.setattr(composition, "_sqlite_path", lambda _url: None)
    with pytest.raises(PlaneCompositionError, match="private to its owner"):
        composition._require_private_sqlite_file(url)


def test_a_malformed_sqlite_url_is_left_to_plane_to_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    for url in ("sqlite://host/db.sqlite3", "sqlite:relative.sqlite3"):
        assert composition._sqlite_path(url) is None
        composition._prepare_sqlite_directory(url)
        with pytest.raises(Exception, match="SQLite"):
            compose_plane_runtime(
                _expectation(),
                database_url=url,
                blob_root=tmp_path / "blobs",
                personal_agent_artifact_root=tmp_path / "agents",
                identity=f"node-{uuid4().hex}",
            )


def test_orchestrator_runtime_starts_on_sqlite_with_no_postgresql_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_postgres(monkeypatch)
    for name in ("DATABASE_URL", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"):
        monkeypatch.delenv(name, raising=False)
    runtime = compose_astral_runtime(MANIFEST, environ=_environment(tmp_path))
    try:
        assert runtime.plane.contract.backend == "sqlite"
        assert runtime.plane.runtime.health().ready
        assert (tmp_path / "node" / "plane.sqlite3").is_file()
    finally:
        runtime.abort()
