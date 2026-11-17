"""Unit tests for astralplane.endpoint_readiness: input denial before I/O, static composition
gates, ambiguous blob evidence, deterministic fence comparison, and data-free unavailable reports.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from astralplane import (
    BLOB_LAYOUT_VERSION,
    ENDPOINT_READINESS_CONTRACT_VERSION,
    MIGRATION_DIGEST,
    SCHEMA_REVISION,
    EndpointFence,
    EndpointFenceError,
    EndpointReadiness,
    EndpointReadinessError,
    EndpointReadinessState,
    EndpointTopology,
    inspect_endpoint_readiness,
)
from astralplane import endpoint_readiness as module

INVENTORY = "a" * 64
IDENTITY = "b" * 64


class _BadPath:
    def __fspath__(self):
        raise TypeError("not a path")


def _endpoint(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "database": "plane",
        "schema": "public",
        "database_oid": 1,
        "schema_oid": 2,
        "schema_owner": "owner",
        "in_recovery": False,
        "transaction_read_only": "off",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "postmaster_started_at": "synthetic",
        "wal_file": "000000010000000000000001",
    }
    values.update(changes)
    return values


class _ScriptedTransaction:
    def __init__(self, *, target=None, endpoint=None):
        self.target = {"database": "plane", "schema": "public"} if target is None else target
        self.endpoint = endpoint

    def execute(self, *args):
        return None

    def fetch_one(self, statement, parameters=()):
        if statement == "SHOW transaction_read_only":
            return {"transaction_read_only": "off"}
        if "pg_try_advisory_xact_lock_shared" in statement:
            return {"acquired": True}
        if statement == (
            "SELECT pg_catalog.current_database() AS database, "
            "pg_catalog.current_schema() AS schema"
        ):
            return self.target
        if "pg_walfile_name" in statement:
            return self.endpoint
        return None

    def fetch_all(self, *args):
        return ()


def _fence(**changes: object) -> EndpointFence:
    values: dict[str, object] = {
        "topology": EndpointTopology.EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS.value,
        "database_identity": IDENTITY,
        "schema_identity": "c" * 64,
        "blob_identity": "d" * 64,
        "blob_inventory_digest": INVENTORY,
        "blob_roots": ("artifacts", "attachments"),
        "schema_revision": SCHEMA_REVISION,
        "migration_digest": MIGRATION_DIGEST,
        "blob_layout_version": BLOB_LAYOUT_VERSION,
    }
    values.update(changes)
    return EndpointFence(**values)  # type: ignore[arg-type]


def _request(tmp_path, **changes: object) -> dict[str, object]:
    root = tmp_path / "durable-blobs"
    root.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "database_url": "PRIVATE-DSN",
        "blob_roots": {"attachments": root},
        "blob_inventory_digest": INVENTORY,
        "expected_database": "plane",
        "expected_schema": "public",
        "expected_schema_revision": SCHEMA_REVISION,
        "expected_migration_digest": MIGRATION_DIGEST,
    }
    values.update(changes)
    return values


def _report(*, state: EndpointReadinessState, fence: EndpointFence | None) -> EndpointReadiness:
    return EndpointReadiness(
        state=state,
        topology=EndpointTopology.EXTERNALLY_MANAGED_PRIMARY_WITH_PAIRED_BLOB_ROOTS.value,
        expected_database="plane",
        expected_schema="public",
        expected_schema_revision=SCHEMA_REVISION,
        expected_migration_digest=MIGRATION_DIGEST,
        expected_blob_layout_version=BLOB_LAYOUT_VERSION,
        blob_roots=("attachments",),
        blob_inventory_digest=INVENTORY,
        fence=fence,
        expected_fence_digest=None,
        reasons=() if state is EndpointReadinessState.WRITABLE else ("synthetic",),
    )


def test_contract_version_is_stable_and_fence_is_deterministic() -> None:
    fence = _fence()

    assert ENDPOINT_READINESS_CONTRACT_VERSION == "astralplane.endpoint-readiness/v1"
    assert fence.digest == _fence().digest
    assert len(fence.digest) == 64
    assert fence.to_dict()["digest"] == fence.digest
    assert fence.to_dict()["blob_roots"] == ["artifacts", "attachments"]
    assert str(fence.database_identity) not in repr(fence.digest)


@pytest.mark.parametrize(
    "changes",
    [
        {"topology": "unsupported"},
        {"database_identity": "short"},
        {"schema_identity": "G" * 64},
        {"blob_identity": ""},
        {"blob_inventory_digest": "0"},
        {"blob_roots": ()},
        {"blob_roots": ("b", "a")},
        {"blob_roots": ("a", "a")},
        {"blob_roots": ("bad name",)},
        {"schema_revision": "089.003"},
        {"migration_digest": "0" * 64},
        {"blob_layout_version": "astralplane.blob-layout/v2"},
    ],
)
def test_fence_rejects_incomplete_or_noncurrent_evidence(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _fence(**changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_url", None),
        ("database_url", ""),
        ("database_url", "   "),
        ("expected_fence", object()),
        ("connect_timeout_seconds", 0),
        ("connect_timeout_seconds", True),
        ("acquire_timeout_seconds", 0),
        ("acquire_timeout_seconds", True),
        ("acquire_timeout_seconds", float("nan")),
        ("acquire_timeout_seconds", float("inf")),
    ],
)
def test_invalid_runtime_inputs_raise_before_any_driver(
    monkeypatch, tmp_path, field: str, value: object
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("invalid readiness input opened a database driver")

    monkeypatch.setattr(module, "create_postgres_driver_pool", forbidden)
    selected = _request(tmp_path)
    selected[field] = value
    with pytest.raises(EndpointReadinessError):
        inspect_endpoint_readiness(**selected)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"expected_database": ""}, "expected_database_invalid"),
        ({"expected_database": None}, "expected_database_invalid"),
        ({"expected_database": "\ud800"}, "expected_database_invalid"),
        ({"expected_schema": "pg_catalog"}, "expected_schema_invalid"),
        ({"expected_schema": "information_schema"}, "expected_schema_invalid"),
        ({"expected_schema_revision": "089.003"}, "expected_schema_revision_not_current"),
        ({"expected_migration_digest": "0" * 64}, "expected_migration_digest_not_current"),
        (
            {"expected_blob_layout_version": "astralplane.blob-layout/v0"},
            "expected_blob_layout_version_mismatch",
        ),
        ({"topology": "external-sqlite/v1"}, "topology_unsupported"),
    ],
)
def test_static_incompatible_composition_is_non_writable_without_io(
    monkeypatch, tmp_path, changes: dict[str, object], reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("static incompatibility opened a database driver")

    monkeypatch.setattr(module, "create_postgres_driver_pool", forbidden)
    report = inspect_endpoint_readiness(**_request(tmp_path, **changes))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.INCOMPATIBLE
    assert not report.writable and report.fence is None
    assert reason in report.reasons
    with pytest.raises(EndpointReadinessError) as error:
        report.require_writable()
    assert error.value.code == "endpoint_readiness_unavailable"
    assert error.value.metadata


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"blob_roots": {}}, "blob_roots_invalid"),
        ({"blob_roots": {"bad name": "unused"}}, "blob_roots_invalid"),
        ({"blob_roots": {"attachments": object()}}, "blob_roots_invalid"),
        ({"blob_roots": {"attachments": ""}}, "blob_roots_invalid"),
        ({"blob_roots": {"attachments": _BadPath()}}, "blob_roots_invalid"),
        ({"blob_roots": {"attachments": "unused"}}, "blob_root_unavailable"),
        ({"blob_inventory_digest": "not-a-digest"}, "blob_inventory_digest_invalid"),
    ],
)
def test_ambiguous_blob_evidence_is_non_writable_without_a_driver(
    monkeypatch, tmp_path, changes: dict[str, object], reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("ambiguous blob evidence opened a database driver")

    monkeypatch.setattr(module, "create_postgres_driver_pool", forbidden)
    report = inspect_endpoint_readiness(**_request(tmp_path, **changes))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.AMBIGUOUS
    assert not report.writable and report.fence is None
    assert report.reasons == (reason,)


def test_blob_cleanup_failure_is_ambiguous(monkeypatch, tmp_path) -> None:
    class BrokenStore:
        def __init__(self, *args, **kwargs):
            pass

        @property
        def root_identity_digest(self):
            return IDENTITY

        def close(self):
            raise RuntimeError("PRIVATE cleanup")

    def forbidden(*args, **kwargs):
        pytest.fail("blob cleanup failure opened a database driver")

    monkeypatch.setattr(module, "ExplicitRootStreamingBlobStore", BrokenStore)
    monkeypatch.setattr(module, "create_postgres_driver_pool", forbidden)
    report = inspect_endpoint_readiness(**_request(tmp_path))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.AMBIGUOUS
    assert report.reasons == ("blob_root_cleanup_failed",)
    assert "PRIVATE" not in repr(report)


def test_duplicate_blob_root_identity_is_ambiguous(monkeypatch, tmp_path) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("duplicate blob roots opened a database driver")

    monkeypatch.setattr(module, "create_postgres_driver_pool", forbidden)
    selected = _request(tmp_path)
    selected["blob_roots"] = {
        "artifacts": selected["blob_roots"]["attachments"],  # type: ignore[index]
        "attachments": selected["blob_roots"]["attachments"],  # type: ignore[index]
    }
    report = inspect_endpoint_readiness(**selected)  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.AMBIGUOUS
    assert report.reasons == ("blob_root_duplicate",)


def test_unavailable_database_diagnostics_never_escape(monkeypatch, tmp_path) -> None:
    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE-DSN PASSWORD")

    monkeypatch.setattr(module, "create_postgres_driver_pool", unavailable)
    report = inspect_endpoint_readiness(**_request(tmp_path))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.UNAVAILABLE
    assert report.reasons == ("database_unavailable",)
    assert not report.writable and report.fence is None
    assert "PRIVATE" not in repr(report) and "PRIVATE" not in repr(report.to_dict())


@pytest.mark.parametrize(
    "target,endpoint,state,reason",
    [
        (
            {"database": "other", "schema": "public"},
            _endpoint(),
            EndpointReadinessState.INCOMPATIBLE,
            "database_or_schema_target_mismatch",
        ),
        (None, None, EndpointReadinessState.INCOMPATIBLE, "endpoint_observation_unavailable"),
        (
            {"database": "plane", "schema": "public"},
            _endpoint(in_recovery=True),
            EndpointReadinessState.NOT_PRIMARY,
            "postgresql_in_recovery",
        ),
        (
            {"database": "plane", "schema": "public"},
            _endpoint(wal_file=None),
            EndpointReadinessState.AMBIGUOUS,
            "endpoint_timeline_unavailable",
        ),
    ],
)
def test_database_observation_refuses_unqualified_endpoint_shapes(
    target, endpoint, state, reason
) -> None:
    if reason == "endpoint_observation_unavailable":
        transaction = _ScriptedTransaction(
            target={"database": "plane", "schema": "public"}
        )
    elif reason == "database_or_schema_target_mismatch":
        transaction = _ScriptedTransaction(target=target, endpoint=endpoint)
    else:
        transaction = _ScriptedTransaction(
            target={"database": "plane", "schema": "public"}, endpoint=endpoint
        )

    observation = module._observe_database(
        transaction, expected_database="plane", expected_schema="public"
    )

    assert observation.state is state
    assert observation.reasons == (reason,)


def test_pool_cleanup_failure_during_database_failure_is_suppressed(
    monkeypatch, tmp_path
) -> None:
    class Pool:
        def __init__(self, driver):
            self.driver = driver

        def close(self):
            raise RuntimeError("PRIVATE cleanup")

    def observe(*args, **kwargs):
        raise RuntimeError("PRIVATE database failure")

    monkeypatch.setattr(module, "create_postgres_driver_pool", lambda *a, **k: object())
    monkeypatch.setattr(module, "ConnectionPool", Pool)
    monkeypatch.setattr(module, "_inspect_database", observe)
    report = inspect_endpoint_readiness(**_request(tmp_path))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.UNAVAILABLE
    assert report.reasons == ("database_unavailable",)
    assert "PRIVATE" not in repr(report)


def test_cleanup_failure_never_publishes_writable_evidence(monkeypatch, tmp_path) -> None:
    class Pool:
        def __init__(self, driver):
            self.driver = driver

        def close(self):
            raise RuntimeError("PRIVATE cleanup")

    def observe(*args, **kwargs):
        return module._DatabaseObservation(
            EndpointReadinessState.WRITABLE,
            (),
            IDENTITY,
            "c" * 64,
        )

    monkeypatch.setattr(module, "create_postgres_driver_pool", lambda *a, **k: object())
    monkeypatch.setattr(module, "ConnectionPool", Pool)
    monkeypatch.setattr(module, "_inspect_database", observe)
    report = inspect_endpoint_readiness(**_request(tmp_path))  # type: ignore[arg-type]

    assert report.state is EndpointReadinessState.UNAVAILABLE
    assert report.reasons == ("database_cleanup_failed",)
    assert "PRIVATE" not in repr(report)


def test_report_requires_writable_and_exact_current_fence() -> None:
    fence = _fence()
    report = _report(state=EndpointReadinessState.WRITABLE, fence=fence)

    assert report.writable
    assert report.require_writable() == fence
    assert report.require_fence(fence) == fence
    assert report.to_dict()["writable"] is True
    stale = replace(fence, blob_identity="e" * 64)
    with pytest.raises(EndpointFenceError) as error:
        report.require_fence(stale)
    assert error.value.code == "endpoint_fence_stale"
    assert error.value.metadata
    with pytest.raises(TypeError):
        report.require_fence(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"topology": "other"}, "fence_topology_mismatch"),
        ({"database_identity": "e" * 64}, "fence_database_identity_mismatch"),
        ({"schema_identity": "e" * 64}, "fence_schema_identity_mismatch"),
        ({"blob_identity": "e" * 64}, "fence_blob_identity_mismatch"),
        ({"blob_inventory_digest": "e" * 64}, "fence_blob_inventory_mismatch"),
        ({"blob_roots": ("artifacts",)}, "fence_blob_root_set_mismatch"),
    ],
)
def test_fence_comparison_names_each_stale_dimension(
    changes: dict[str, object], reason: str
) -> None:
    observed = _fence()
    if changes.get("topology") == "other":
        expected = _fence()
        object.__setattr__(expected, "topology", "other")
    else:
        expected = _fence(**changes)

    assert reason in module._fence_reasons(expected, observed)


def test_identical_fence_with_a_different_digest_fails_closed() -> None:
    fence = _fence()
    assert module._fence_reasons(fence, fence) == ("fence_mismatch",)