"""Validates the composition manifest's declared AstralPlane contract and builds the
initialized Plane runtime on PostgreSQL or, for the personal node profile, SQLite, delegating
connection, migration, and repository mechanics to astralplane. Composed by runtime_composition.py at boot.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from astralplane import (
    BLOB_LAYOUT_VERSION,
    AttachmentMaterializationCoordinator,
    CONTRACT_VERSION,
    GENERATED_AGENT_BUNDLE_CONTRACT,
    ImmutableBundleStore,
    MIGRATION_DIGEST,
    PACKAGE_VERSION,
    PlaneRuntime,
    READ_COMPATIBLE_FROM,
    SCHEMA_REVISION,
    SQLITE_DATA_PLANE_REVISION,
    SQLITE_MIGRATION_DIGEST,
    RepositoryCatalog,
    StreamingBlobStore,
    create_repository_catalog,
    create_postgres_runtime,
    create_sqlite_runtime,
    create_attachment_materialization_coordinator,
    create_streaming_blob_store,
    inspect_compatibility,
)
from astralplane.database.sqlite import is_sqlite_url
from astralplane.database.sqlite_migrations import SQLITE_SCHEMA_STRUCTURE_DIGEST
from astralplane.repositories.agents import AgentPolicyReconciliationResult

from orchestrator.agent_constitution import USER_AGENT_POLICY_REVISION
from orchestrator.attachments.materialization import AttachmentMaterializationService
from orchestrator.attachments.purge import AttachmentPurgeCoordinator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9]{3}\.[0-9]{3}$")
_CONTRACT = re.compile(r"^[a-z][a-z0-9._-]*/v[1-9][0-9]*$")
_PRODUCT_RECONCILER_NAME = "astraldeep-plane-contract"
_PRODUCT_RECONCILER_PROFILE = "astraldeep-runtime/v1"
PROFILE_ENV = "ASTRAL_NODE_PROFILE"
DATA_DIR_ENV = "ASTRAL_NODE_DATA_DIR"
PERSONAL = "personal"
POSTGRESQL = "postgresql"
SQLITE = "sqlite"
SQLITE_DATABASE_FILE = "plane.sqlite3"
_SPLIT_DATABASE_VARIABLES = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT")
logger = logging.getLogger("Orchestrator.Plane")


class PlaneCompositionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AstralDeepPlaneContractReconciler:
    composition_digest: str
    contract_version: str
    schema_revision: str
    repositories: tuple[str, ...]

    @property
    def name(self) -> str:
        return _PRODUCT_RECONCILER_NAME

    @property
    def version(self) -> str:
        return f"contract-{self.composition_digest[:24]}"

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object]:
        if context.get("host") != "astraldeep":
            raise PlaneCompositionError("Plane reconciliation host is not AstralDeep")
        if context.get("composition_digest") != self.composition_digest:
            raise PlaneCompositionError("Plane reconciliation composition drifted")
        return {
            "composition_digest": self.composition_digest,
            "contract_version": self.contract_version,
            "profile": _PRODUCT_RECONCILER_PROFILE,
            "repository_count": len(self.repositories),
            "schema_revision": self.schema_revision,
        }


def _required_text(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PlaneCompositionError(f"data_plane.{field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class SqliteRegistryExpectation:
    schema_revision: str
    migration_sha256: str
    structure_sha256: str

    @classmethod
    def from_mapping(cls, value: object) -> SqliteRegistryExpectation:
        if not isinstance(value, Mapping):
            raise PlaneCompositionError("composition data_plane_sqlite must be an object")
        expected_keys = {"migration_sha256", "schema_revision", "structure_sha256"}
        actual_keys = set(value)
        if actual_keys != expected_keys:
            raise PlaneCompositionError(
                "data_plane_sqlite keys differ: "
                f"missing={sorted(expected_keys - actual_keys)!r}, "
                f"extra={sorted(actual_keys - expected_keys)!r}"
            )
        return cls(
            schema_revision=_required_text(
                value["schema_revision"], "sqlite.schema_revision", _REVISION
            ),
            migration_sha256=_required_text(
                value["migration_sha256"], "sqlite.migration_sha256", _SHA256
            ),
            structure_sha256=_required_text(
                value["structure_sha256"], "sqlite.structure_sha256", _SHA256
            ),
        )


@dataclass(frozen=True, slots=True)
class PlaneContractExpectation:
    contract_version: str
    schema_revision: str
    read_compatible_from: str
    migration_sha256: str
    blob_layout_version: str
    sqlite: SqliteRegistryExpectation | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlaneContractExpectation:
        expected_keys = {
            "blob_layout_version",
            "contract_version",
            "migration_sha256",
            "read_compatible_from",
            "schema_revision",
        }
        actual_keys = set(value)
        if actual_keys != expected_keys:
            raise PlaneCompositionError(
                "data_plane keys differ: "
                f"missing={sorted(expected_keys - actual_keys)!r}, "
                f"extra={sorted(actual_keys - expected_keys)!r}"
            )
        return cls(
            contract_version=_required_text(
                value["contract_version"], "contract_version", _CONTRACT
            ),
            schema_revision=_required_text(
                value["schema_revision"], "schema_revision", _REVISION
            ),
            read_compatible_from=_required_text(
                value["read_compatible_from"], "read_compatible_from", _REVISION
            ),
            migration_sha256=_required_text(
                value["migration_sha256"], "migration_sha256", _SHA256
            ),
            blob_layout_version=_required_text(
                value["blob_layout_version"], "blob_layout_version", _CONTRACT
            ),
        )


@dataclass(frozen=True, slots=True)
class PlaneCompositionReport:
    compatible: bool
    reasons: tuple[str, ...]
    producer_version: str
    repositories: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "producer_version": self.producer_version,
            "reasons": list(self.reasons),
            "repositories": list(self.repositories),
        }


@dataclass(frozen=True, slots=True)
class PlaneComposition:
    expectation: PlaneContractExpectation
    repositories: RepositoryCatalog
    report: PlaneCompositionReport
    backend: str = POSTGRESQL

    @property
    def repository_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self.repositories.as_mapping()))


@dataclass(frozen=True, slots=True)
class InitializedPlaneComposition:
    contract: PlaneComposition
    runtime: PlaneRuntime
    blobs: StreamingBlobStore
    generated_agent_bundles: ImmutableBundleStore
    agent_policy_reconciliation: AgentPolicyReconciliationResult
    attachment_materializations: AttachmentMaterializationCoordinator
    attachment_purges: AttachmentPurgeCoordinator
    attachment_materializer: AttachmentMaterializationService

    @property
    def repositories(self) -> RepositoryCatalog:
        return self.contract.repositories

    def close(self) -> None:
        # Close order matters: cleanup below still needs the pool
        self.attachment_materializer.abort()
        self.attachment_materializations.close()
        self.attachment_purges.abort()
        # Let a busy-store error raise; don't proceed to close the pool
        self.blobs.close()
        self.runtime.close()


def plane_backend(database_url: str) -> str:
    return SQLITE if is_sqlite_url(database_url) else POSTGRESQL


def _registry_reasons(
    expectation: PlaneContractExpectation, backend: str
) -> list[str]:
    if backend == POSTGRESQL:
        return (
            []
            if expectation.migration_sha256 == MIGRATION_DIGEST
            else ["migration_digest_mismatch"]
        )
    pinned = expectation.sqlite
    if pinned is None:
        return ["sqlite_registry_unpinned"]
    observed = (
        SQLITE_DATA_PLANE_REVISION.schema_revision,
        SQLITE_MIGRATION_DIGEST,
        SQLITE_SCHEMA_STRUCTURE_DIGEST,
    )
    declared = (pinned.schema_revision, pinned.migration_sha256, pinned.structure_sha256)
    names = (
        "sqlite_schema_revision_mismatch",
        "sqlite_migration_digest_mismatch",
        "sqlite_structure_digest_mismatch",
    )
    return [name for name, left, right in zip(names, declared, observed) if left != right]


def inspect_plane_composition(
    expectation: PlaneContractExpectation,
    backend: str = POSTGRESQL,
) -> PlaneCompositionReport:
    if backend not in (POSTGRESQL, SQLITE):
        raise PlaneCompositionError("Plane backend is not recognized")
    reasons: list[str] = []
    registry = _registry_reasons(expectation, backend)
    exact_fields = (
        (
            expectation.contract_version,
            CONTRACT_VERSION,
            "contract_version_mismatch",
        ),
        (
            expectation.schema_revision,
            SCHEMA_REVISION,
            "schema_revision_mismatch",
        ),
        (
            expectation.read_compatible_from,
            READ_COMPATIBLE_FROM,
            "read_compatible_from_mismatch",
        ),
        (None, None, None),
        (
            expectation.blob_layout_version,
            BLOB_LAYOUT_VERSION,
            "blob_layout_version_mismatch",
        ),
    )
    for declared, observed, reason in exact_fields:
        if reason is None:
            reasons.extend(registry)
        elif declared != observed:
            reasons.append(reason)
    producer_report = inspect_compatibility(
        expected_contract_version=expectation.contract_version,
        observed_schema_revision=expectation.schema_revision,
        consumer_version=PACKAGE_VERSION,
    )
    reasons.extend(
        reason for reason in producer_report.reasons if reason not in reasons
    )
    repository_names = tuple(sorted(create_repository_catalog().as_mapping()))
    return PlaneCompositionReport(
        compatible=not reasons,
        reasons=tuple(reasons),
        producer_version=PACKAGE_VERSION,
        repositories=repository_names,
    )


def compose_plane_catalog(
    expectation: PlaneContractExpectation,
    backend: str = POSTGRESQL,
) -> PlaneComposition:
    report = inspect_plane_composition(expectation, backend)
    if not report.compatible:
        raise PlaneCompositionError(
            "AstralPlane composition is incompatible: " + ", ".join(report.reasons)
        )
    return PlaneComposition(
        expectation=expectation,
        repositories=create_repository_catalog(),
        report=report,
        backend=backend,
    )


def _product_reconciler(
    contract: PlaneComposition,
) -> AstralDeepPlaneContractReconciler:
    evidence = {
        "blob_layout_version": contract.expectation.blob_layout_version,
        "contract_version": contract.expectation.contract_version,
        "migration_sha256": contract.expectation.migration_sha256,
        "profile": _PRODUCT_RECONCILER_PROFILE,
        "read_compatible_from": contract.expectation.read_compatible_from,
        "repositories": list(contract.report.repositories),
        "schema_revision": contract.expectation.schema_revision,
    }
    if contract.backend == SQLITE:
        pinned = contract.expectation.sqlite
        assert pinned is not None
        evidence["backend"] = SQLITE
        evidence["sqlite_migration_sha256"] = pinned.migration_sha256
        evidence["sqlite_structure_sha256"] = pinned.structure_sha256
    canonical = json.dumps(
        evidence,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return AstralDeepPlaneContractReconciler(
        composition_digest=hashlib.sha256(canonical).hexdigest(),
        contract_version=contract.expectation.contract_version,
        schema_revision=contract.expectation.schema_revision,
        repositories=contract.report.repositories,
    )


def compose_plane_runtime(
    expectation: PlaneContractExpectation,
    *,
    database_url: str,
    blob_root: str | os.PathLike[str],
    personal_agent_artifact_root: str | os.PathLike[str],
    identity: str,
    minimum_connections: int = 2,
    maximum_connections: int = 10,
    acquire_timeout_seconds: float = 30.0,
    connect_timeout_seconds: int = 10,
) -> InitializedPlaneComposition:
    artifact_root = Path(personal_agent_artifact_root)
    if not artifact_root.is_absolute():
        raise PlaneCompositionError(
            "PERSONAL_AGENT_ARTIFACT_ROOT must be an absolute path"
        )
    backend = plane_backend(database_url)
    contract = compose_plane_catalog(expectation, backend)
    if backend == SQLITE:
        _prepare_sqlite_directory(database_url)
    blobs = create_streaming_blob_store(root=blob_root, create_root=True)
    reconciler = _product_reconciler(contract)
    generated_agent_bundles: ImmutableBundleStore | None = None
    runtime: PlaneRuntime | None = None
    attachment_materializations: AttachmentMaterializationCoordinator | None = None
    attachment_purges: AttachmentPurgeCoordinator | None = None
    attachment_materializer: AttachmentMaterializationService | None = None
    try:
        generated_agent_bundles = ImmutableBundleStore(
            artifact_root,
            contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        )
        shared_options: dict[str, Any] = {
            "identity": identity,
            "reconcilers": (reconciler,),
            "reconciliation_context": {
                "host": "astraldeep",
                "composition_digest": reconciler.composition_digest,
            },
            "repositories": contract.repositories,
            "expected_contract_version": expectation.contract_version,
            "observed_schema_revision": expectation.schema_revision,
            "maximum_connections": maximum_connections,
            "acquire_timeout_seconds": acquire_timeout_seconds,
        }
        if backend == SQLITE:
            runtime = create_sqlite_runtime(
                database_url,
                busy_timeout_seconds=float(connect_timeout_seconds),
                **shared_options,
            )
        else:
            runtime = create_postgres_runtime(
                database_url,
                minimum_connections=minimum_connections,
                connect_timeout_seconds=connect_timeout_seconds,
                application_name=f"astralplane:{identity}",
                **shared_options,
            )
        runtime.initialize(expected_revision=expectation.schema_revision)
        if backend == SQLITE:
            _require_private_sqlite_file(database_url)
        health = runtime.health()
        if not health.ready:
            raise PlaneCompositionError("AstralPlane initialization did not become ready")
        policy_reconciliation = _reconcile_agent_validation_policy(
            runtime,
            contract.repositories,
        )
        _report_agent_policy_reconciliation(policy_reconciliation)
        attachment_materializations = create_attachment_materialization_coordinator(
            database=runtime,
            materializations=contract.repositories.artifacts.materializations,
            blobs=blobs,
        )
        attachment_purges = AttachmentPurgeCoordinator(
            plane_runtime=runtime,
            purge_repository=contract.repositories.purge,
            blobs=blobs,
        )
        startup_purges = attachment_purges.reconcile_startup()
        if getattr(attachment_purges, "ready", True):
            logger.info(
                "Attachment purge startup reconciliation complete: attempts=%d",
                len(startup_purges),
            )
        else:
            logger.warning(
                "Attachment purge startup reconciliation remains degraded: "
                "attempts=%d reason=%s",
                len(startup_purges),
                getattr(
                    attachment_purges,
                    "readiness_code",
                    "purge_reconciliation_incomplete",
                ),
            )
        attachment_materializer = AttachmentMaterializationService(
            coordinator=attachment_materializations,
            purge_coordinator=attachment_purges,
        )
    except BaseException:
        try:
            if attachment_materializer is not None:
                attachment_materializer.abort()
        finally:
            try:
                if attachment_materializations is not None:
                    attachment_materializations.close()
            finally:
                try:
                    if attachment_purges is not None:
                        attachment_purges.abort()
                finally:
                    try:
                        blobs.close()
                    finally:
                        if runtime is not None:
                            runtime.close()
        raise
    assert runtime is not None
    assert generated_agent_bundles is not None
    assert attachment_materializations is not None
    assert attachment_purges is not None
    assert attachment_materializer is not None
    return InitializedPlaneComposition(
        contract=contract,
        runtime=runtime,
        blobs=blobs,
        generated_agent_bundles=generated_agent_bundles,
        agent_policy_reconciliation=policy_reconciliation,
        attachment_materializations=attachment_materializations,
        attachment_purges=attachment_purges,
        attachment_materializer=attachment_materializer,
    )


def _sqlite_path(database_url: str) -> Path | None:
    parts = urlsplit(database_url)
    path = unquote(parts.path)
    if parts.netloc or not path.startswith("/") or "\x00" in path:
        return None
    return Path(path)


def _prepare_sqlite_directory(database_url: str) -> None:
    path = _sqlite_path(database_url)
    if path is not None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _require_private_sqlite_file(database_url: str) -> None:
    path = _sqlite_path(database_url)
    if path is None or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PlaneCompositionError("SQLite database file must be private to its owner")


def _reconcile_agent_validation_policy(
    runtime: PlaneRuntime,
    repositories: RepositoryCatalog,
) -> AgentPolicyReconciliationResult:
    with runtime.transaction() as transaction:
        return repositories.agents.reconcile_validation_policy_for_administration(
            transaction,
            policy_revision=USER_AGENT_POLICY_REVISION,
        )


def _report_agent_policy_reconciliation(
    result: AgentPolicyReconciliationResult,
) -> None:
    logger.info(
        "User-agent policy reconciled: revision=%s marker_changed=%s "
        "agents_marked_for_revalidation=%d",
        result.policy_revision,
        str(result.marker_changed).lower(),
        result.agents_marked_for_revalidation,
    )


def _positive_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    value = environ.get(name, str(default))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise PlaneCompositionError(f"{name} must be a positive integer") from None
    if parsed < 1:
        raise PlaneCompositionError(f"{name} must be a positive integer")
    return parsed


def _positive_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = environ.get(name, str(default))
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise PlaneCompositionError(f"{name} must be positive") from None
    if not 0 < parsed < float("inf"):
        raise PlaneCompositionError(f"{name} must be positive")
    return parsed


def _personal_profile(values: Mapping[str, str]) -> bool:
    return values.get(PROFILE_ENV, "").strip().lower() == PERSONAL


def _personal_sqlite_url(values: Mapping[str, str]) -> str:
    configured = values.get(DATA_DIR_ENV, "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[1] / "data" / "node"
    )
    return f"sqlite://{quote(str(root.resolve() / SQLITE_DATABASE_FILE), safe='/')}"


def resolve_plane_database_url(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    direct = values.get("DATABASE_URL")
    if direct is not None:
        if not isinstance(direct, str) or not direct.strip():
            raise PlaneCompositionError("DATABASE_URL must be non-empty")
        if is_sqlite_url(direct) and not _personal_profile(values):
            raise PlaneCompositionError(
                "SQLite storage is available only to the personal node profile"
            )
        return direct
    if _personal_profile(values) and not any(
        name in values for name in _SPLIT_DATABASE_VARIABLES
    ):
        return _personal_sqlite_url(values)

    host = values.get("DB_HOST", "localhost")
    name = values.get("DB_NAME", "astraldeep")
    user = values.get("DB_USER", "astral")
    password = values.get("DB_PASSWORD", "astral_dev")
    if not all(isinstance(item, str) and item for item in (host, name, user, password)):
        raise PlaneCompositionError("split PostgreSQL configuration is incomplete")
    if any(any(ord(character) < 0x20 for character in item) for item in (host, name, user)):
        raise PlaneCompositionError("split PostgreSQL configuration is invalid")
    if host.strip().lower() == "localhost":
        host = "127.0.0.1"
    port = _positive_int(values, "DB_PORT", 5432)
    if port > 65535:
        raise PlaneCompositionError("DB_PORT must be at most 65535")
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host_part}:{port}/{quote(name, safe='')}"
    )


def resolve_plane_blob_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("ATTACHMENT_UPLOAD_ROOT")
    if configured is None:
        return (Path(__file__).resolve().parents[1] / "tmp").resolve()
    if not isinstance(configured, str) or not configured.strip():
        raise PlaneCompositionError("ATTACHMENT_UPLOAD_ROOT must be non-empty")
    root = Path(configured)
    if not root.is_absolute():
        raise PlaneCompositionError("ATTACHMENT_UPLOAD_ROOT must be an absolute path")
    return root


def resolve_personal_agent_artifact_root(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("PERSONAL_AGENT_ARTIFACT_ROOT")
    if configured is None:
        return (
            Path(__file__).resolve().parents[1]
            / "data"
            / "personal-agent-artifacts"
        ).resolve()
    if not isinstance(configured, str) or not configured.strip():
        raise PlaneCompositionError(
            "PERSONAL_AGENT_ARTIFACT_ROOT must be non-empty"
        )
    root = Path(configured)
    if not root.is_absolute():
        raise PlaneCompositionError(
            "PERSONAL_AGENT_ARTIFACT_ROOT must be an absolute path"
        )
    return root


def compose_plane_from_environment(
    manifest_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> InitializedPlaneComposition:
    values = os.environ if environ is None else environ
    minimum = _positive_int(values, "DB_POOL_MIN", 2)
    maximum = _positive_int(values, "DB_POOL_MAX", 10)
    if maximum < minimum:
        raise PlaneCompositionError("DB_POOL_MAX must be no smaller than DB_POOL_MIN")
    identity = (
        values["ASTRALPLANE_IDENTITY"]
        if "ASTRALPLANE_IDENTITY" in values
        else values.get("RUNTIME_METRICS_INSTANCE", "astraldeep")
    )
    if not isinstance(identity, str) or not identity.strip():
        raise PlaneCompositionError("ASTRALPLANE_IDENTITY must be non-empty")
    database_url = resolve_plane_database_url(values)
    expectation = load_plane_expectation(manifest_path)
    return compose_plane_runtime(
        expectation,
        database_url=database_url,
        blob_root=resolve_plane_blob_root(values),
        personal_agent_artifact_root=resolve_personal_agent_artifact_root(values),
        identity=identity,
        minimum_connections=minimum,
        maximum_connections=maximum,
        acquire_timeout_seconds=_positive_float(
            values, "DB_POOL_ACQUIRE_TIMEOUT_SECONDS", 30.0
        ),
        connect_timeout_seconds=_positive_int(
            values, "DB_CONNECT_TIMEOUT_SECONDS", 10
        ),
    )


def load_plane_expectation(path: str | Path) -> PlaneContractExpectation:
    manifest_path = Path(path)
    try:
        document: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaneCompositionError("composition manifest is unreadable") from exc
    if not isinstance(document, dict):
        raise PlaneCompositionError("composition manifest must be an object")
    compatibility = document.get("compatibility")
    if not isinstance(compatibility, dict):
        raise PlaneCompositionError("composition compatibility must be an object")
    data_plane = compatibility.get("data_plane")
    if not isinstance(data_plane, dict):
        raise PlaneCompositionError("composition data_plane must be an object")
    expectation = PlaneContractExpectation.from_mapping(data_plane)
    return replace(
        expectation,
        sqlite=SqliteRegistryExpectation.from_mapping(
            compatibility.get("data_plane_sqlite")
        ),
    )


__all__ = (
    "AstralDeepPlaneContractReconciler",
    "InitializedPlaneComposition",
    "PlaneComposition",
    "PlaneCompositionError",
    "PlaneCompositionReport",
    "PlaneContractExpectation",
    "SqliteRegistryExpectation",
    "compose_plane_catalog",
    "compose_plane_from_environment",
    "compose_plane_runtime",
    "inspect_plane_composition",
    "load_plane_expectation",
    "plane_backend",
    "resolve_plane_blob_root",
    "resolve_plane_database_url",
    "resolve_personal_agent_artifact_root",
)
