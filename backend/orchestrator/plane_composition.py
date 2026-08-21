"""AstralDeep's narrow composition boundary for the embedded data plane.

This module contains no connection, migration, or repository implementation.
Those mechanics belong to :mod:`astralplane`. It validates the exact component
contract declared by the AstralDeep composition manifest and delegates runtime
construction, empty-database initialization, migrations, pooling, and
transactions to Plane's stable public facade.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

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
    RepositoryCatalog,
    StreamingBlobStore,
    create_repository_catalog,
    create_postgres_runtime,
    create_attachment_materialization_coordinator,
    create_streaming_blob_store,
    inspect_compatibility,
)
from astralplane.repositories.agents import AgentPolicyReconciliationResult

from orchestrator.agent_constitution import USER_AGENT_POLICY_REVISION
from orchestrator.attachments.materialization import AttachmentMaterializationService
from orchestrator.attachments.purge import AttachmentPurgeCoordinator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9]{3}\.[0-9]{3}$")
_CONTRACT = re.compile(r"^[a-z][a-z0-9._-]*/v[1-9][0-9]*$")
_PRODUCT_RECONCILER_NAME = "astraldeep-plane-contract"
_PRODUCT_RECONCILER_PROFILE = "astraldeep-runtime/v1"
logger = logging.getLogger("Orchestrator.Plane")


class PlaneCompositionError(RuntimeError):
    """The declared AstralPlane component cannot be admitted safely."""


@dataclass(frozen=True, slots=True)
class AstralDeepPlaneContractReconciler:
    """Durably attest the exact Plane contract admitted by this product.

    This is a real product reconciliation gate, not a placeholder migration:
    Plane records the digest-derived hook version only after the installed
    schema/contract, repository surface, and Deep runtime profile have all
    matched the immutable composition supplied at construction.
    """

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
class PlaneContractExpectation:
    """Exact Plane metadata pinned by one AstralDeep composition."""

    contract_version: str
    schema_revision: str
    read_compatible_from: str
    migration_sha256: str
    blob_layout_version: str

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
    """Detached, non-sensitive evidence for the component admission check."""

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
    """An exact compatible Plane repository catalog."""

    expectation: PlaneContractExpectation
    repositories: RepositoryCatalog
    report: PlaneCompositionReport

    @property
    def repository_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self.repositories.as_mapping()))


@dataclass(frozen=True, slots=True)
class InitializedPlaneComposition:
    """A compatible Plane runtime and its application-scoped storage boundaries."""

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
        # The higher-level runtime joins request/upload state machines and the
        # purge loop before reaching this final synchronous boundary.  An
        # unstarted partial graph is also safe to abort here.  Blob staging
        # workers must close before the Plane pool they may still need for
        # fenced cleanup.
        self.attachment_materializer.abort()
        self.attachment_materializations.close()
        self.attachment_purges.abort()
        # A busy blob boundary means a staging capability was not joined.  Do
        # not half-close the Plane pool needed to fence/abandon that durable
        # intent; surface the error so the higher-level shared close task can
        # be retried after the capability converges.
        self.blobs.close()
        self.runtime.close()


def inspect_plane_composition(
    expectation: PlaneContractExpectation,
) -> PlaneCompositionReport:
    """Compare every declared compatibility field to the installed producer."""

    reasons: list[str] = []
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
        (
            expectation.migration_sha256,
            MIGRATION_DIGEST,
            "migration_digest_mismatch",
        ),
        (
            expectation.blob_layout_version,
            BLOB_LAYOUT_VERSION,
            "blob_layout_version_mismatch",
        ),
    )
    reasons.extend(
        reason for declared, observed, reason in exact_fields if declared != observed
    )
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
) -> PlaneComposition:
    """Return the public repository catalog only after an exact contract match."""

    report = inspect_plane_composition(expectation)
    if not report.compatible:
        raise PlaneCompositionError(
            "AstralPlane composition is incompatible: " + ", ".join(report.reasons)
        )
    return PlaneComposition(
        expectation=expectation,
        repositories=create_repository_catalog(),
        report=report,
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
    """Create and initialize the exact declared Plane runtime or fail closed.

    The database URL is passed through without being retained in detached
    composition evidence or exception messages. Any construction or startup
    failure closes the Plane-owned pool before it escapes.
    """

    artifact_root = Path(personal_agent_artifact_root)
    if not artifact_root.is_absolute():
        raise PlaneCompositionError(
            "PERSONAL_AGENT_ARTIFACT_ROOT must be an absolute path"
        )
    contract = compose_plane_catalog(expectation)
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
        runtime = create_postgres_runtime(
            database_url,
            identity=identity,
            reconcilers=(reconciler,),
            reconciliation_context={
                "host": "astraldeep",
                "composition_digest": reconciler.composition_digest,
            },
            repositories=contract.repositories,
            expected_contract_version=expectation.contract_version,
            observed_schema_revision=expectation.schema_revision,
            minimum_connections=minimum_connections,
            maximum_connections=maximum_connections,
            acquire_timeout_seconds=acquire_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
            application_name=f"astralplane:{identity}",
        )
        runtime.initialize(expected_revision=expectation.schema_revision)
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


def _reconcile_agent_validation_policy(
    runtime: PlaneRuntime,
    repositories: RepositoryCatalog,
) -> AgentPolicyReconciliationResult:
    """Enforce the current Deep policy revision before traffic admission."""

    with runtime.transaction() as transaction:
        return repositories.agents.reconcile_validation_policy_for_administration(
            transaction,
            policy_revision=USER_AGENT_POLICY_REVISION,
        )


def _report_agent_policy_reconciliation(
    result: AgentPolicyReconciliationResult,
) -> None:
    """Emit only the bounded, non-owner policy-reconciliation evidence."""

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


def resolve_plane_database_url(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the product's PostgreSQL endpoint without retaining secrets.

    Plane owns the driver and pool, while Deep owns deployment configuration.
    Supplying one resolved URL to both Plane and the temporary legacy adapters
    also prevents subtle drift for escaped credentials or IPv6 hosts.
    """

    values = os.environ if environ is None else environ
    direct = values.get("DATABASE_URL")
    if direct is not None:
        if not isinstance(direct, str) or not direct.strip():
            raise PlaneCompositionError("DATABASE_URL must be non-empty")
        return direct

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
    """Resolve one explicit absolute blob root for the application composition."""

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
    """Resolve Deep's one absolute root for immutable generated-agent bundles."""

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
    """Initialize the one application-scoped Plane runtime from host config."""

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
    """Load only the declared data-plane block from a composition manifest."""

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
    return PlaneContractExpectation.from_mapping(data_plane)


__all__ = (
    "AstralDeepPlaneContractReconciler",
    "InitializedPlaneComposition",
    "PlaneComposition",
    "PlaneCompositionError",
    "PlaneCompositionReport",
    "PlaneContractExpectation",
    "compose_plane_catalog",
    "compose_plane_from_environment",
    "compose_plane_runtime",
    "inspect_plane_composition",
    "load_plane_expectation",
    "resolve_plane_blob_root",
    "resolve_plane_database_url",
    "resolve_personal_agent_artifact_root",
)
