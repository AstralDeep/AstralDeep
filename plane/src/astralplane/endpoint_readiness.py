"""Read-only qualification of an externally managed PostgreSQL primary and its paired blob
roots, returning deterministic fence evidence for host-owned election, admission, and stale
writer refusal across endpoint loss, reconnect, and failover.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from astralplane.blob_store import ExplicitRootStreamingBlobStore
from astralplane.compatibility import BLOB_LAYOUT_VERSION
from astralplane.contracts import IsolationLevel
from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION, MIGRATION_REGISTRY
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import Transaction
from astralplane.errors import PlaneError

ENDPOINT_READINESS_CONTRACT_VERSION: Final = "astralplane.endpoint-readiness/v1"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_TIMELINE: Final = re.compile(r"^[0-9a-f]{8}$")
_ROOT_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_METADATA_IDENTITY_QUERY: Final = """
SELECT relation.oid AS relation_oid, namespace.oid AS namespace_oid
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = %s AND relation.relname = 'schema_meta'
  AND relation.relkind = 'r' AND NOT relation.relrowsecurity
  AND NOT relation.relforcerowsecurity
"""
_ENDPOINT_QUERY: Final = """
SELECT
    pg_catalog.current_database() AS database,
    pg_catalog.current_schema() AS schema,
    (SELECT oid FROM pg_catalog.pg_database
     WHERE datname = pg_catalog.current_database()) AS database_oid,
    pg_catalog.current_schema()::regnamespace::oid AS schema_oid,
    (SELECT pg_catalog.pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace
     WHERE oid = pg_catalog.current_schema()::regnamespace) AS schema_owner,
    pg_catalog.pg_is_in_recovery() AS in_recovery,
    pg_catalog.current_setting('transaction_read_only') AS transaction_read_only,
    pg_catalog.inet_server_addr()::text AS server_address,
    pg_catalog.inet_server_port() AS server_port,
    pg_catalog.pg_postmaster_start_time() AS postmaster_started_at,
    CASE WHEN NOT pg_catalog.pg_is_in_recovery()
         THEN pg_catalog.pg_walfile_name(pg_catalog.pg_current_wal_lsn())
    END AS wal_file
"""


class EndpointTopology(StrEnum):
    EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS = (
        "astralplane.external-primary-paired-blobs/v1"
    )


class EndpointReadinessState(StrEnum):
    WRITABLE = "writable"
    UNAVAILABLE = "unavailable"
    NOT_PRIMARY = "not_primary"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"


class EndpointReadinessError(PlaneError):
    default_code = "endpoint_readiness_unavailable"


class EndpointFenceError(EndpointReadinessError):
    default_code = "endpoint_fence_stale"


@dataclass(frozen=True, slots=True)
class EndpointFence:
    topology: str
    database_identity: str
    schema_identity: str
    blob_identity: str
    blob_inventory_digest: str
    blob_roots: tuple[str, ...]
    schema_revision: str
    migration_digest: str
    blob_layout_version: str

    def __post_init__(self) -> None:
        if self.topology != EndpointTopology.EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS:
            raise ValueError("endpoint fence topology is unsupported")
        for name in ("database_identity", "schema_identity", "blob_identity"):
            if _SHA256.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"endpoint fence {name} must be lowercase SHA-256")
        if _SHA256.fullmatch(self.blob_inventory_digest) is None:
            raise ValueError("endpoint fence blob inventory digest must be lowercase SHA-256")
        if (
            not isinstance(self.blob_roots, tuple)
            or not self.blob_roots
            or self.blob_roots != tuple(sorted(self.blob_roots))
            or len(set(self.blob_roots)) != len(self.blob_roots)
            or any(_ROOT_NAME.fullmatch(name) is None for name in self.blob_roots)
        ):
            raise ValueError("endpoint fence blob roots must be unique sorted identifiers")
        if self.migration_digest != CURRENT_DATA_PLANE_REVISION.migration_digest:
            raise ValueError("endpoint fence migration digest is not current")
        if self.schema_revision != CURRENT_DATA_PLANE_REVISION.schema_revision:
            raise ValueError("endpoint fence schema revision is not current")
        if self.blob_layout_version != BLOB_LAYOUT_VERSION:
            raise ValueError("endpoint fence blob layout is unsupported")

    @property
    def digest(self) -> str:
        return _digest(
            {
                "blob_identity": self.blob_identity,
                "blob_inventory_digest": self.blob_inventory_digest,
                "blob_layout_version": self.blob_layout_version,
                "blob_roots": list(self.blob_roots),
                "database_identity": self.database_identity,
                "migration_digest": self.migration_digest,
                "schema_identity": self.schema_identity,
                "schema_revision": self.schema_revision,
                "topology": self.topology,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "blob_identity": self.blob_identity,
            "blob_inventory_digest": self.blob_inventory_digest,
            "blob_layout_version": self.blob_layout_version,
            "blob_roots": list(self.blob_roots),
            "database_identity": self.database_identity,
            "digest": self.digest,
            "migration_digest": self.migration_digest,
            "schema_identity": self.schema_identity,
            "schema_revision": self.schema_revision,
            "topology": self.topology,
        }


@dataclass(frozen=True, slots=True)
class EndpointReadiness:
    state: EndpointReadinessState
    topology: str
    expected_database: str
    expected_schema: str
    expected_schema_revision: str
    expected_migration_digest: str
    expected_blob_layout_version: str
    blob_roots: tuple[str, ...]
    blob_inventory_digest: str | None
    fence: EndpointFence | None
    expected_fence_digest: str | None
    reasons: tuple[str, ...]

    @property
    def writable(self) -> bool:
        return self.state is EndpointReadinessState.WRITABLE and self.fence is not None

    def require_writable(self) -> EndpointFence:
        if not self.writable or self.fence is None:
            raise EndpointReadinessError(
                "endpoint is not qualified for writable admission",
                metadata={"reasons": ",".join(self.reasons), "state": self.state.value},
            )
        return self.fence

    def require_fence(self, expected: EndpointFence) -> EndpointFence:
        if not isinstance(expected, EndpointFence):
            raise TypeError("expected endpoint fence must be an EndpointFence")
        fence = self.require_writable()
        if fence.digest != expected.digest:
            raise EndpointFenceError(
                "endpoint fence is stale",
                metadata={"expected": expected.digest, "observed": fence.digest},
            )
        return fence

    def to_dict(self) -> dict[str, object]:
        return {
            "blob_inventory_digest": self.blob_inventory_digest,
            "blob_roots": list(self.blob_roots),
            "expected_blob_layout_version": self.expected_blob_layout_version,
            "expected_database": self.expected_database,
            "expected_fence_digest": self.expected_fence_digest,
            "expected_migration_digest": self.expected_migration_digest,
            "expected_schema": self.expected_schema,
            "expected_schema_revision": self.expected_schema_revision,
            "fence": None if self.fence is None else self.fence.to_dict(),
            "reasons": list(self.reasons),
            "state": self.state.value,
            "topology": self.topology,
            "writable": self.writable,
        }


@dataclass(frozen=True, slots=True)
class _DatabaseObservation:
    state: EndpointReadinessState
    reasons: tuple[str, ...]
    database_identity: str | None = None
    schema_identity: str | None = None


def _digest(document: object) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _identifier(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 63
    except UnicodeEncodeError:
        return False


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalized_roots(
    value: object,
) -> dict[str, str | os.PathLike[str]] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    normalized: dict[str, str | os.PathLike[str]] = {}
    for name, path in value.items():
        if not isinstance(name, str) or _ROOT_NAME.fullmatch(name) is None:
            return None
        if not isinstance(path, (str, os.PathLike)):
            return None
        try:
            if not os.fspath(path):
                return None
        except TypeError:
            return None
        normalized[name] = path
    return normalized


def _blob_observation(
    roots: Mapping[str, str | os.PathLike[str]],
) -> tuple[str | None, str | None]:
    identities: list[tuple[str, str]] = []
    for name, path in sorted(roots.items()):
        store: ExplicitRootStreamingBlobStore | None = None
        try:
            store = ExplicitRootStreamingBlobStore(path, create_root=False)
            identities.append((name, store.root_identity_digest))
        except Exception:
            return None, "blob_root_unavailable"
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    return None, "blob_root_cleanup_failed"
    observed = tuple(identity for _, identity in identities)
    if len(set(observed)) != len(observed):
        return None, "blob_root_duplicate"
    return _digest(identities), None


def _observe_database(
    transaction: Transaction,
    *,
    expected_database: str,
    expected_schema: str,
) -> _DatabaseObservation:
    transaction.execute("SET LOCAL statement_timeout = '5s'")
    transaction.execute("SET LOCAL lock_timeout = '1s'")
    posture = transaction.fetch_one("SHOW transaction_read_only")
    transaction.execute("SET TRANSACTION READ ONLY")
    transaction.execute("SET LOCAL row_security = off")
    transaction.execute(
        "SELECT pg_catalog.set_config('search_path', pg_catalog.concat("
        "pg_catalog.quote_ident(pg_catalog.current_schema()), ',pg_temp'), true)"
    )
    target = transaction.fetch_one(
        "SELECT pg_catalog.current_database() AS database, "
        "pg_catalog.current_schema() AS schema"
    )
    if (
        target is None
        or target.get("database") != expected_database
        or target.get("schema") != expected_schema
    ):
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("database_or_schema_target_mismatch",),
        )
    search_path = '"' + expected_schema.replace('"', '""') + '",pg_temp'
    transaction.execute(
        "SELECT pg_catalog.set_config('search_path', %s, true)", (search_path,)
    )
    migration_lock = transaction.fetch_one(
        "SELECT pg_catalog.pg_try_advisory_xact_lock_shared(%s, %s) AS acquired",
        CURRENT_DATA_PLANE_REVISION.migration_lock,
    )
    if migration_lock is None or migration_lock.get("acquired") is not True:
        return _DatabaseObservation(
            EndpointReadinessState.UNAVAILABLE,
            ("migration_in_progress",),
        )
    endpoint = transaction.fetch_one(_ENDPOINT_QUERY)
    if endpoint is None:
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("endpoint_observation_unavailable",),
        )
    if endpoint.get("in_recovery") is not False:
        return _DatabaseObservation(
            EndpointReadinessState.NOT_PRIMARY,
            ("postgresql_in_recovery",),
        )
    if posture is None or posture.get("transaction_read_only") != "off":
        return _DatabaseObservation(
            EndpointReadinessState.NOT_PRIMARY,
            ("postgresql_read_only",),
        )
    wal_file = endpoint.get("wal_file")
    timeline = wal_file[:8].lower() if isinstance(wal_file, str) else ""
    if _TIMELINE.fullmatch(timeline) is None:
        return _DatabaseObservation(
            EndpointReadinessState.AMBIGUOUS,
            ("endpoint_timeline_unavailable",),
        )
    identity = transaction.fetch_one(_METADATA_IDENTITY_QUERY, (expected_schema,))
    if identity is None or identity["namespace_oid"] != endpoint.get("schema_oid"):
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("schema_catalog_mismatch",),
        )
    relation = '"' + expected_schema.replace('"', '""') + '".schema_meta'
    try:
        transaction.execute(f"LOCK TABLE ONLY {relation} IN ACCESS SHARE MODE NOWAIT")
        if transaction.fetch_one(_METADATA_IDENTITY_QUERY, (expected_schema,)) != identity:
            return _DatabaseObservation(
                EndpointReadinessState.INCOMPATIBLE,
                ("schema_identity_changed",),
            )
        MIGRATION_REGISTRY.verify_current(transaction)
    except Exception as exc:
        if getattr(exc, "pgcode", None) in {"55P03", "57014"}:
            raise
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("schema_catalog_mismatch",),
        )
    metadata = transaction.fetch_all(
        "WITH metadata AS MATERIALIZED ("
        "SELECT CASE WHEN tableoid = %s AND pg_catalog.pg_typeof(key) = "
        "'pg_catalog.text'::pg_catalog.regtype AND pg_catalog.pg_typeof(value) = "
        "'pg_catalog.text'::pg_catalog.regtype "
        "THEN pg_catalog.format('%%s'::pg_catalog.text, key) END AS key, "
        "CASE WHEN tableoid = %s AND pg_catalog.pg_typeof(key) = "
        "'pg_catalog.text'::pg_catalog.regtype AND pg_catalog.pg_typeof(value) = "
        "'pg_catalog.text'::pg_catalog.regtype "
        "THEN pg_catalog.format('%%s'::pg_catalog.text, value) END AS value "
        f"FROM ONLY {relation} TABLESAMPLE pg_catalog.system(100)) "
        "SELECT key,value FROM metadata WHERE key IN (%s,%s) LIMIT 3",
        (
            identity["relation_oid"],
            identity["relation_oid"],
            "revision",
            "astralplane_migration_digest",
        ),
    )
    expected_metadata = {
        "revision": CURRENT_DATA_PLANE_REVISION.schema_revision,
        "astralplane_migration_digest": CURRENT_DATA_PLANE_REVISION.migration_digest,
    }
    if len(metadata) != 2 or {row["key"]: row["value"] for row in metadata} != expected_metadata:
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("schema_metadata_mismatch",),
        )
    if transaction.fetch_one(_METADATA_IDENTITY_QUERY, (expected_schema,)) != identity:
        return _DatabaseObservation(
            EndpointReadinessState.INCOMPATIBLE,
            ("schema_identity_changed",),
        )
    database_identity = _digest(
        {
            "database": endpoint.get("database"),
            "database_oid": str(endpoint.get("database_oid")),
            "postmaster_started_at": str(endpoint.get("postmaster_started_at")),
            "server_address": endpoint.get("server_address") or "",
            "server_port": endpoint.get("server_port"),
            "timeline": timeline,
        }
    )
    schema_identity = _digest(
        {
            "database_identity": database_identity,
            "schema": endpoint.get("schema"),
            "schema_oid": str(endpoint.get("schema_oid")),
            "schema_owner": endpoint.get("schema_owner"),
        }
    )
    return _DatabaseObservation(
        EndpointReadinessState.WRITABLE,
        (),
        database_identity,
        schema_identity,
    )


def _inspect_database(
    pool: ConnectionPool, *, expected_database: str, expected_schema: str
) -> _DatabaseObservation:
    with pool.connection() as connection:
        transaction = Transaction(connection, isolation=IsolationLevel.READ_COMMITTED)
        try:
            transaction._begin()
            try:
                return _observe_database(
                    transaction,
                    expected_database=expected_database,
                    expected_schema=expected_schema,
                )
            finally:
                transaction._finish(failed=True)
        except BaseException:
            if transaction.discard_required:
                pool._discard_on_return(connection)
            raise


def _fence_reasons(expected: EndpointFence, observed: EndpointFence) -> tuple[str, ...]:
    checks = (
        ("topology", "fence_topology_mismatch"),
        ("database_identity", "fence_database_identity_mismatch"),
        ("schema_identity", "fence_schema_identity_mismatch"),
        ("blob_identity", "fence_blob_identity_mismatch"),
        ("blob_inventory_digest", "fence_blob_inventory_mismatch"),
        ("blob_roots", "fence_blob_root_set_mismatch"),
        ("schema_revision", "fence_schema_revision_mismatch"),
        ("migration_digest", "fence_migration_digest_mismatch"),
        ("blob_layout_version", "fence_blob_layout_mismatch"),
    )
    reasons = tuple(
        reason for field, reason in checks if getattr(expected, field) != getattr(observed, field)
    )
    return reasons or ("fence_mismatch",)


def _report(
    *,
    state: EndpointReadinessState,
    topology: str,
    expected_database: object,
    expected_schema: object,
    expected_schema_revision: object,
    expected_migration_digest: object,
    expected_blob_layout_version: object,
    blob_roots: tuple[str, ...],
    blob_inventory_digest: str | None,
    fence: EndpointFence | None,
    expected_fence: EndpointFence | None,
    reasons: tuple[str, ...],
) -> EndpointReadiness:
    return EndpointReadiness(
        state=state,
        topology=topology,
        expected_database=_text(expected_database),
        expected_schema=_text(expected_schema),
        expected_schema_revision=_text(expected_schema_revision),
        expected_migration_digest=_text(expected_migration_digest),
        expected_blob_layout_version=_text(expected_blob_layout_version),
        blob_roots=blob_roots,
        blob_inventory_digest=blob_inventory_digest,
        fence=fence,
        expected_fence_digest=None if expected_fence is None else expected_fence.digest,
        reasons=reasons,
    )


def inspect_endpoint_readiness(
    *,
    database_url: str,
    blob_roots: Mapping[str, str | os.PathLike[str]],
    blob_inventory_digest: str,
    expected_database: str,
    expected_schema: str,
    expected_schema_revision: str,
    expected_migration_digest: str,
    expected_blob_layout_version: str = BLOB_LAYOUT_VERSION,
    topology: EndpointTopology | str = (
        EndpointTopology.EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS
    ),
    expected_fence: EndpointFence | None = None,
    connect_timeout_seconds: int = 5,
    acquire_timeout_seconds: float = 1.0,
) -> EndpointReadiness:
    if not isinstance(database_url, str) or not database_url.strip():
        raise EndpointReadinessError("database_url must be a non-empty PostgreSQL DSN")
    if expected_fence is not None and not isinstance(expected_fence, EndpointFence):
        raise EndpointReadinessError("expected fence must be an EndpointFence")
    if (
        not isinstance(connect_timeout_seconds, int)
        or isinstance(connect_timeout_seconds, bool)
        or connect_timeout_seconds < 1
    ):
        raise EndpointReadinessError("connect timeout must be a positive integer")
    if (
        isinstance(acquire_timeout_seconds, bool)
        or not isinstance(acquire_timeout_seconds, (int, float))
        or not math.isfinite(float(acquire_timeout_seconds))
        or acquire_timeout_seconds <= 0
    ):
        raise EndpointReadinessError("acquire timeout must be finite and positive")
    try:
        selected_topology = EndpointTopology(topology).value
    except (TypeError, ValueError):
        selected_topology = str(topology)
    roots = _normalized_roots(blob_roots)
    root_names = () if roots is None else tuple(sorted(roots))
    reasons: list[str] = []
    if not _identifier(expected_database):
        reasons.append("expected_database_invalid")
    if (
        not _identifier(expected_schema)
        or expected_schema.startswith("pg_")
        or expected_schema == "information_schema"
    ):
        reasons.append("expected_schema_invalid")
    if expected_schema_revision != CURRENT_DATA_PLANE_REVISION.schema_revision:
        reasons.append("expected_schema_revision_not_current")
    if expected_migration_digest != CURRENT_DATA_PLANE_REVISION.migration_digest:
        reasons.append("expected_migration_digest_not_current")
    if expected_blob_layout_version != BLOB_LAYOUT_VERSION:
        reasons.append("expected_blob_layout_version_mismatch")
    if selected_topology != EndpointTopology.EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS:
        reasons.append("topology_unsupported")
    if reasons:
        return _report(
            state=EndpointReadinessState.INCOMPATIBLE,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=(
                blob_inventory_digest
                if isinstance(blob_inventory_digest, str)
                and _SHA256.fullmatch(blob_inventory_digest) is not None
                else None
            ),
            fence=None,
            expected_fence=expected_fence,
            reasons=tuple(reasons),
        )
    if roots is None:
        return _report(
            state=EndpointReadinessState.AMBIGUOUS,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=None,
            fence=None,
            expected_fence=expected_fence,
            reasons=("blob_roots_invalid",),
        )
    if not isinstance(blob_inventory_digest, str) or _SHA256.fullmatch(
        blob_inventory_digest
    ) is None:
        return _report(
            state=EndpointReadinessState.AMBIGUOUS,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=None,
            fence=None,
            expected_fence=expected_fence,
            reasons=("blob_inventory_digest_invalid",),
        )
    blob_identity, blob_reason = _blob_observation(roots)
    if blob_identity is None:
        return _report(
            state=EndpointReadinessState.AMBIGUOUS,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=blob_inventory_digest,
            fence=None,
            expected_fence=expected_fence,
            reasons=(blob_reason or "blob_root_unavailable",),
        )
    pool: ConnectionPool | None = None
    try:
        driver_pool = create_postgres_driver_pool(
            database_url,
            minimum_connections=1,
            maximum_connections=1,
            acquire_timeout_seconds=float(acquire_timeout_seconds),
            connect_timeout_seconds=connect_timeout_seconds,
            application_name="astralplane:endpoint-readiness",
        )
        pool = ConnectionPool(driver_pool)
        observation = _inspect_database(
            pool,
            expected_database=expected_database,
            expected_schema=expected_schema,
        )
    except Exception:
        if pool is not None:
            with suppress(Exception):
                pool.close()
        return _report(
            state=EndpointReadinessState.UNAVAILABLE,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=blob_inventory_digest,
            fence=None,
            expected_fence=expected_fence,
            reasons=("database_unavailable",),
        )
    close_failed = False
    if pool is not None:
        try:
            pool.close()
        except Exception:
            close_failed = True
    if close_failed:
        return _report(
            state=EndpointReadinessState.UNAVAILABLE,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=blob_inventory_digest,
            fence=None,
            expected_fence=expected_fence,
            reasons=("database_cleanup_failed",),
        )
    if observation.state is not EndpointReadinessState.WRITABLE:
        return _report(
            state=observation.state,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=blob_inventory_digest,
            fence=None,
            expected_fence=expected_fence,
            reasons=observation.reasons,
        )
    assert observation.database_identity is not None
    assert observation.schema_identity is not None
    fence = EndpointFence(
        topology=selected_topology,
        database_identity=observation.database_identity,
        schema_identity=observation.schema_identity,
        blob_identity=blob_identity,
        blob_inventory_digest=blob_inventory_digest,
        blob_roots=root_names,
        schema_revision=CURRENT_DATA_PLANE_REVISION.schema_revision,
        migration_digest=CURRENT_DATA_PLANE_REVISION.migration_digest,
        blob_layout_version=expected_blob_layout_version,
    )
    if expected_fence is not None and expected_fence.digest != fence.digest:
        return _report(
            state=EndpointReadinessState.STALE,
            topology=selected_topology,
            expected_database=expected_database,
            expected_schema=expected_schema,
            expected_schema_revision=expected_schema_revision,
            expected_migration_digest=expected_migration_digest,
            expected_blob_layout_version=expected_blob_layout_version,
            blob_roots=root_names,
            blob_inventory_digest=blob_inventory_digest,
            fence=fence,
            expected_fence=expected_fence,
            reasons=_fence_reasons(expected_fence, fence),
        )
    return _report(
        state=EndpointReadinessState.WRITABLE,
        topology=selected_topology,
        expected_database=expected_database,
        expected_schema=expected_schema,
        expected_schema_revision=expected_schema_revision,
        expected_migration_digest=expected_migration_digest,
        expected_blob_layout_version=expected_blob_layout_version,
        blob_roots=root_names,
        blob_inventory_digest=blob_inventory_digest,
        fence=fence,
        expected_fence=expected_fence,
        reasons=(),
    )


__all__ = (
    "ENDPOINT_READINESS_CONTRACT_VERSION",
    "EndpointFence",
    "EndpointFenceError",
    "EndpointReadiness",
    "EndpointReadinessError",
    "EndpointReadinessState",
    "EndpointTopology",
    "inspect_endpoint_readiness",
)