"""Real-PostgreSQL failover-readiness cases: a qualified primary keeps its fence across loss
and reconnect, a promoted endpoint requires fresh evidence, stale writers and read-only,
renamed-root, incompatible, and damaged states never become writable.
"""

from __future__ import annotations

import hashlib
import socket

import pytest
from psycopg2.extensions import make_dsn

from astralplane import (
    MIGRATION_DIGEST,
    SCHEMA_REVISION,
    EndpointFenceError,
    EndpointReadinessState,
    inspect_endpoint_readiness,
)

INVENTORY = hashlib.sha256(b"synthetic complete blob inventory").hexdigest()
CHANGED_INVENTORY = hashlib.sha256(b"synthetic renamed blob inventory").hexdigest()


def _dsn(database, *, read_only: bool = False) -> str:
    options = f"-csearch_path={database.schema},pg_catalog"
    if read_only:
        options += " -cdefault_transaction_read_only=on"
    return make_dsn(database.dsn, options=options)


def _lost_dsn(database) -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return make_dsn(
        database.dsn,
        host="127.0.0.1",
        port=port,
        options=f"-csearch_path={database.schema},pg_catalog",
    )


def _request(database, roots, *, inventory: str = INVENTORY) -> dict[str, object]:
    return {
        "database_url": _dsn(database),
        "blob_roots": roots,
        "blob_inventory_digest": inventory,
        "expected_database": database.name,
        "expected_schema": database.schema,
        "expected_schema_revision": SCHEMA_REVISION,
        "expected_migration_digest": MIGRATION_DIGEST,
    }


def test_primary_loss_reconnect_and_stale_writer_are_explicit(
    migrated_clone, migrated_template, tmp_path
):
    root = tmp_path / "durable-blobs"
    root.mkdir()
    roots = {"attachments": root}
    request = _request(migrated_clone, roots)

    first = inspect_endpoint_readiness(**request)
    fence = first.require_writable()
    assert first.state is EndpointReadinessState.WRITABLE
    assert first.fence == fence and first.fence.to_dict()["blob_roots"] == ["attachments"]
    assert first.expected_fence_digest is None
    assert str(root) not in repr(first.to_dict())
    assert "PRIVATE" not in repr(first.to_dict())

    lost = inspect_endpoint_readiness(
        **{
            **request,
            "database_url": _lost_dsn(migrated_clone),
            "expected_fence": fence,
            "connect_timeout_seconds": 1,
        }
    )
    assert lost.state is EndpointReadinessState.UNAVAILABLE
    assert not lost.writable and lost.fence is None
    assert lost.reasons == ("database_unavailable",)
    assert lost.expected_fence_digest == fence.digest

    reconnected = inspect_endpoint_readiness(**{**request, "expected_fence": fence})
    assert reconnected.state is EndpointReadinessState.WRITABLE
    assert reconnected.fence == fence
    assert reconnected.require_fence(fence) == fence

    with migrated_template.clone() as promoted:
        promoted_request = _request(promoted, roots)
        stale = inspect_endpoint_readiness(**{**promoted_request, "expected_fence": fence})
        assert stale.state is EndpointReadinessState.STALE
        assert not stale.writable and stale.fence is not None
        assert stale.fence.digest != fence.digest
        assert "fence_database_identity_mismatch" in stale.reasons

        fresh = inspect_endpoint_readiness(**promoted_request)
        assert fresh.state is EndpointReadinessState.WRITABLE
        assert fresh.fence is not None and fresh.fence.digest != fence.digest
        with pytest.raises(EndpointFenceError) as error:
            fresh.require_fence(fence)
        assert error.value.code == "endpoint_fence_stale"


def test_renamed_root_and_changed_inventory_require_a_new_fence(migrated_clone, tmp_path):
    original = tmp_path / "attachments"
    original.mkdir()
    request = _request(migrated_clone, {"attachments": original})
    fence = inspect_endpoint_readiness(**request).require_writable()

    relocated = tmp_path / "relocated-attachments"
    original.rename(relocated)
    moved = inspect_endpoint_readiness(
        **{
            **request,
            "blob_roots": {"attachments": relocated},
            "expected_fence": fence,
        }
    )
    assert moved.state is EndpointReadinessState.STALE
    assert not moved.writable
    assert "fence_blob_identity_mismatch" in moved.reasons

    fresh = inspect_endpoint_readiness(
        **{**request, "blob_roots": {"attachments": relocated}}
    )
    fresh_fence = fresh.require_writable()
    assert fresh_fence.digest != fence.digest

    changed = inspect_endpoint_readiness(
        **{
            **request,
            "blob_roots": {"attachments": relocated},
            "blob_inventory_digest": CHANGED_INVENTORY,
            "expected_fence": fresh_fence,
        }
    )
    assert changed.state is EndpointReadinessState.STALE
    assert not changed.writable
    assert "fence_blob_inventory_mismatch" in changed.reasons
    assert changed.blob_inventory_digest == CHANGED_INVENTORY


def test_externally_read_only_endpoint_is_never_writable(migrated_clone, tmp_path):
    root = tmp_path / "durable-blobs"
    root.mkdir()
    request = _request(migrated_clone, {"attachments": root})

    report = inspect_endpoint_readiness(
        **{**request, "database_url": _dsn(migrated_clone, read_only=True)}
    )

    assert report.state is EndpointReadinessState.NOT_PRIMARY
    assert not report.writable and report.fence is None
    assert report.reasons == ("postgresql_read_only",)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("wrong-digest", "schema_metadata_mismatch"),
        ("missing-digest", "schema_metadata_mismatch"),
        ("catalog", "schema_catalog_mismatch"),
        ("wrong-database", "database_or_schema_target_mismatch"),
        ("wrong-schema", "database_or_schema_target_mismatch"),
    ],
)
def test_damaged_or_mistargeted_state_stays_non_writable(
    migrated_clone, tmp_path, damage: str, reason: str
) -> None:
    root = tmp_path / "durable-blobs"
    root.mkdir()
    request = _request(migrated_clone, {"attachments": root})
    with migrated_clone.database.transaction() as transaction:
        before = transaction.fetch_all("SELECT key,value FROM schema_meta ORDER BY key")
        if damage == "wrong-digest":
            transaction.execute(
                "UPDATE schema_meta SET value=%s "
                "WHERE key='astralplane_migration_digest'",
                ("0" * 64,),
            )
        elif damage == "missing-digest":
            transaction.execute(
                "DELETE FROM schema_meta WHERE key='astralplane_migration_digest'"
            )
        elif damage == "catalog":
            transaction.execute(
                "ALTER TABLE web_session ADD COLUMN unexpected_failover_field TEXT"
            )
    if damage == "wrong-database":
        request["expected_database"] = "unselected-database"
    elif damage == "wrong-schema":
        request["expected_schema"] = "unselected_schema"

    report = inspect_endpoint_readiness(**request)

    assert report.state is EndpointReadinessState.INCOMPATIBLE
    assert not report.writable and report.fence is None
    assert report.reasons == (reason,)
    with migrated_clone.database.transaction() as transaction:
        after = transaction.fetch_all("SELECT key,value FROM schema_meta ORDER BY key")
        if damage in {"wrong-digest", "missing-digest"}:
            assert after != before
        else:
            assert after == before
        if damage == "catalog":
            column = transaction.fetch_one(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='web_session' AND column_name='unexpected_failover_field'"
            )
            assert column is not None
            assert column["column_name"] == "unexpected_failover_field"