"""Tests for src/astralplane/database/revision.py: schema lineage and lock-identity
binding, and repeat-safe canonical migration execution.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import astralplane.database.migrations as migrations_module
import astralplane.database.revision as revision_module
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_DIGEST,
    MIGRATION_REGISTRY,
    PLANE_SCHEMA_067_MIGRATION,
    PLANE_SCHEMA_067_REGISTRY_DIGEST,
    PLANE_SCHEMA_067_STATEMENTS,
    PLANE_SCHEMA_074_001_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_002_MIGRATION,
    PLANE_SCHEMA_074_002_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_002_STATEMENTS,
    PLANE_SCHEMA_074_003_MIGRATION,
    PLANE_SCHEMA_074_003_REGISTRY_DIGEST,
    PLANE_SCHEMA_074_003_STATEMENTS,
    PLANE_SCHEMA_074_004_MIGRATION,
    PLANE_SCHEMA_074_004_STATEMENTS,
    PLANE_SCHEMA_074_MIGRATION,
    PLANE_SCHEMA_074_STATEMENTS,
)
from astralplane.database.revision import (
    ADVISORY_LOCK_IDS,
    READ_COMPATIBLE_FROM,
    SCHEMA_PREDECESSOR_REVISION,
    SCHEMA_REVISION,
    DataPlaneRevision,
    validate_revision,
)
from astralplane.errors import SchemaRevisionError


class _RecordingTransaction:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []

    def execute(self, statement: str, parameters: object = ()) -> object:
        self.executions.append((statement, parameters))
        return object()


def test_schema_lineage_and_lock_identities_bind_the_canonical_registry() -> None:
    plane_schema_075_migration = migrations_module.PLANE_SCHEMA_075_MIGRATION
    plane_schema_075_statements = migrations_module.PLANE_SCHEMA_075_STATEMENTS
    plane_schema_074_004_registry_digest = migrations_module.PLANE_SCHEMA_074_004_REGISTRY_DIGEST
    assert SCHEMA_PREDECESSOR_REVISION == "089.021"
    assert SCHEMA_REVISION == "089.022"
    assert READ_COMPATIBLE_FROM == "066.001"
    assert ADVISORY_LOCK_IDS == ((1095980114, 60001), (1095980114, 60002))
    assert not hasattr(revision_module, "MIGRATION_DIGEST")
    assert MIGRATION_REGISTRY.digest == MIGRATION_DIGEST
    assert CURRENT_DATA_PLANE_REVISION.schema_revision == SCHEMA_REVISION
    assert CURRENT_DATA_PLANE_REVISION.read_compatible_from == (
        READ_COMPATIBLE_FROM,
        "067.001",
        "074.001",
        "074.002",
        "074.003",
        "074.004",
        "075.001",
        "079.001",
        "088.001",
        "088.002",
        "088.003",
        "088.004",
        "088.005",
        "088.006",
        "088.007",
        "088.008",
        "089.001",
        "089.002",
        "089.003",
        "089.004",
        "089.005",
        "089.006",
        "089.007",
        "089.008",
        "089.009",
        "089.010",
        "089.011",
        "089.012",
        "089.013",
        "089.014",
        "089.015",
        "089.016",
        "089.017",
        "089.018",
        "089.019",
        "089.020",
        "089.021",
    )
    assert CURRENT_DATA_PLANE_REVISION.migration_digest == MIGRATION_DIGEST
    assert CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests == (
        ("067.001", PLANE_SCHEMA_067_REGISTRY_DIGEST),
        ("074.001", PLANE_SCHEMA_074_001_REGISTRY_DIGEST),
        ("074.002", PLANE_SCHEMA_074_002_REGISTRY_DIGEST),
        ("074.003", PLANE_SCHEMA_074_003_REGISTRY_DIGEST),
        ("074.004", plane_schema_074_004_registry_digest),
        ("075.001", migrations_module.PLANE_SCHEMA_075_REGISTRY_DIGEST),
        ("079.001", migrations_module.PLANE_SCHEMA_079_REGISTRY_DIGEST),
        ("088.001", migrations_module.PLANE_SCHEMA_088_REGISTRY_DIGEST),
        ("088.002", migrations_module.PLANE_SCHEMA_088_002_REGISTRY_DIGEST),
        ("088.003", migrations_module.PLANE_SCHEMA_088_003_REGISTRY_DIGEST),
        ("088.004", migrations_module.PLANE_SCHEMA_088_004_REGISTRY_DIGEST),
        ("088.005", migrations_module.PLANE_SCHEMA_088_005_REGISTRY_DIGEST),
        ("088.006", migrations_module.PLANE_SCHEMA_088_006_REGISTRY_DIGEST),
        ("088.007", migrations_module.PLANE_SCHEMA_088_007_REGISTRY_DIGEST),
        ("088.008", migrations_module.PLANE_SCHEMA_088_008_REGISTRY_DIGEST),
        ("089.001", migrations_module.PLANE_SCHEMA_089_001_REGISTRY_DIGEST),
        ("089.002", migrations_module.PLANE_SCHEMA_089_002_REGISTRY_DIGEST),
        ("089.003", migrations_module.PLANE_SCHEMA_089_003_REGISTRY_DIGEST),
        ("089.004", migrations_module.PLANE_SCHEMA_089_004_REGISTRY_DIGEST),
        ("089.005", migrations_module.PLANE_SCHEMA_089_005_REGISTRY_DIGEST),
        ("089.006", migrations_module.PLANE_SCHEMA_089_006_REGISTRY_DIGEST),
        ("089.007", migrations_module.PLANE_SCHEMA_089_007_REGISTRY_DIGEST),
        ("089.008", migrations_module.PLANE_SCHEMA_089_008_REGISTRY_DIGEST),
        ("089.009", migrations_module.PLANE_SCHEMA_089_009_REGISTRY_DIGEST),
        ("089.010", migrations_module.PLANE_SCHEMA_089_010_REGISTRY_DIGEST),
        ("089.011", migrations_module.PLANE_SCHEMA_089_011_REGISTRY_DIGEST),
        ("089.012", migrations_module.PLANE_SCHEMA_089_012_REGISTRY_DIGEST),
        ("089.013", migrations_module.PLANE_SCHEMA_089_013_REGISTRY_DIGEST),
        ("089.014", migrations_module.PLANE_SCHEMA_089_014_REGISTRY_DIGEST),
        ("089.015", migrations_module.PLANE_SCHEMA_089_015_REGISTRY_DIGEST),
        ("089.016", migrations_module.PLANE_SCHEMA_089_016_REGISTRY_DIGEST),
        ("089.017", migrations_module.PLANE_SCHEMA_089_017_REGISTRY_DIGEST),
        ("089.018", migrations_module.PLANE_SCHEMA_089_018_REGISTRY_DIGEST),
        ("089.019", migrations_module.PLANE_SCHEMA_089_019_REGISTRY_DIGEST),
        ("089.020", migrations_module.PLANE_SCHEMA_089_020_REGISTRY_DIGEST),
        ("089.021", migrations_module.PLANE_SCHEMA_089_021_REGISTRY_DIGEST),
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.008")
        == migrations_module.PLANE_SCHEMA_089_008_REGISTRY_DIGEST
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.009")
        == migrations_module.PLANE_SCHEMA_089_009_REGISTRY_DIGEST
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.010")
        == migrations_module.PLANE_SCHEMA_089_010_REGISTRY_DIGEST
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.011")
        == migrations_module.PLANE_SCHEMA_089_011_REGISTRY_DIGEST
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.015")
        == migrations_module.PLANE_SCHEMA_089_015_REGISTRY_DIGEST
    )
    assert (
        CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.019")
        == migrations_module.PLANE_SCHEMA_089_019_REGISTRY_DIGEST
    )
    assert CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("067.001") == (
        PLANE_SCHEMA_067_REGISTRY_DIGEST
    )
    assert CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.001") == (
        migrations_module.PLANE_SCHEMA_089_001_REGISTRY_DIGEST
    )
    assert PLANE_SCHEMA_067_MIGRATION.source_revisions == ("066.001",)
    assert PLANE_SCHEMA_067_MIGRATION.target_revision == "067.001"
    assert PLANE_SCHEMA_067_REGISTRY_DIGEST == (
        "ae2285e6764cf084eeaf6099443d85fb9b27ae930fcb0684e4e0f458d17bb4f9"
    )
    assert PLANE_SCHEMA_074_MIGRATION.source_revisions == ("067.001",)
    assert PLANE_SCHEMA_074_MIGRATION.target_revision == "074.001"
    assert PLANE_SCHEMA_074_001_REGISTRY_DIGEST == (
        "02ee01830e51c97edbeb384eb05f25b5101efa6c0f564383bab5b7b90a7e80cf"
    )
    assert PLANE_SCHEMA_074_002_MIGRATION.source_revisions == ("074.001",)
    assert PLANE_SCHEMA_074_002_MIGRATION.target_revision == "074.002"
    assert PLANE_SCHEMA_074_003_MIGRATION.source_revisions == ("074.002",)
    assert PLANE_SCHEMA_074_003_MIGRATION.target_revision == "074.003"
    assert PLANE_SCHEMA_074_004_MIGRATION.source_revisions == ("074.003",)
    assert PLANE_SCHEMA_074_004_MIGRATION.target_revision == "074.004"
    assert plane_schema_075_migration.source_revisions == ("074.004",)
    assert plane_schema_075_migration.target_revision == "075.001"
    assert migrations_module.PLANE_SCHEMA_079_MIGRATION.source_revisions == ("075.001",)
    assert migrations_module.PLANE_SCHEMA_079_MIGRATION.target_revision == "079.001"
    assert migrations_module.PLANE_SCHEMA_088_MIGRATION.source_revisions == ("079.001",)
    assert migrations_module.PLANE_SCHEMA_088_MIGRATION.target_revision == "088.001"
    assert migrations_module.PLANE_SCHEMA_088_002_MIGRATION.source_revisions == ("088.001",)
    assert migrations_module.PLANE_SCHEMA_088_002_MIGRATION.target_revision == "088.002"
    assert migrations_module.PLANE_SCHEMA_088_003_MIGRATION.source_revisions == ("088.002",)
    assert migrations_module.PLANE_SCHEMA_088_003_MIGRATION.target_revision == "088.003"
    assert migrations_module.PLANE_SCHEMA_088_004_MIGRATION.source_revisions == ("088.003",)
    assert migrations_module.PLANE_SCHEMA_088_004_MIGRATION.target_revision == "088.004"
    assert migrations_module.PLANE_SCHEMA_088_005_MIGRATION.source_revisions == ("088.004",)
    assert migrations_module.PLANE_SCHEMA_088_005_MIGRATION.target_revision == "088.005"
    assert migrations_module.PLANE_SCHEMA_088_006_MIGRATION.source_revisions == ("088.005",)
    assert migrations_module.PLANE_SCHEMA_088_006_MIGRATION.target_revision == "088.006"
    assert migrations_module.PLANE_SCHEMA_088_006_REGISTRY_DIGEST == (
        "4f5783e676a12a4009959da2056c2c769418f8c101d67f533223a608158ed73a"
    )
    assert migrations_module.PLANE_SCHEMA_088_007_MIGRATION.source_revisions == ("088.006",)
    assert migrations_module.PLANE_SCHEMA_088_007_MIGRATION.target_revision == "088.007"
    assert migrations_module.PLANE_SCHEMA_088_007_MIGRATION.name == (
        "astralplane-088-scheduler-policy"
    )
    assert migrations_module.PLANE_SCHEMA_088_007_REGISTRY_DIGEST == (
        "844d9f6629bc422013f84488b10ba6d2862f4805475c63b217639184146fca4d"
    )
    assert migrations_module.PLANE_SCHEMA_088_008_MIGRATION.source_revisions == ("088.007",)
    assert migrations_module.PLANE_SCHEMA_088_008_MIGRATION.target_revision == ("088.008")
    assert migrations_module.PLANE_SCHEMA_088_008_MIGRATION.name == (
        "astralplane-088-framework-credentials"
    )
    assert migrations_module.PLANE_SCHEMA_089_001_MIGRATION.source_revisions == ("088.008",)
    assert migrations_module.PLANE_SCHEMA_089_001_MIGRATION.target_revision == ("089.001")
    assert migrations_module.PLANE_SCHEMA_089_001_MIGRATION.name == (
        "astralplane-089-typesafe-credentials"
    )
    assert migrations_module.PLANE_SCHEMA_089_002_MIGRATION.source_revisions == ("089.001",)
    assert migrations_module.PLANE_SCHEMA_089_002_MIGRATION.target_revision == "089.002"
    assert migrations_module.PLANE_SCHEMA_089_002_MIGRATION.name == (
        "astralplane-089-mesh-enrollment-records"
    )
    assert migrations_module.PLANE_SCHEMA_089_003_MIGRATION.source_revisions == ("089.002",)
    assert migrations_module.PLANE_SCHEMA_089_003_MIGRATION.target_revision == "089.003"
    assert migrations_module.PLANE_SCHEMA_089_004_MIGRATION.source_revisions == ("089.003",)
    assert migrations_module.PLANE_SCHEMA_089_004_MIGRATION.target_revision == "089.004"
    assert migrations_module.PLANE_SCHEMA_089_004_MIGRATION.name == (
        "astralplane-089-completion-wake"
    )
    assert migrations_module.PLANE_SCHEMA_089_006_MIGRATION.source_revisions == ("089.005",)
    assert migrations_module.PLANE_SCHEMA_089_006_MIGRATION.target_revision == "089.006"
    assert migrations_module.PLANE_SCHEMA_089_006_MIGRATION.name == ("astralplane-089-atlas-pages")
    assert migrations_module.PLANE_SCHEMA_089_006_REGISTRY_DIGEST == (
        "1176ee5a65f74e0447abe2e63bb90808acc40289acd1a6a09ec6a10ae1a967b1"
    )
    assert migrations_module.PLANE_SCHEMA_089_007_MIGRATION.source_revisions == ("089.006",)
    assert migrations_module.PLANE_SCHEMA_089_007_MIGRATION.target_revision == "089.007"
    assert migrations_module.PLANE_SCHEMA_089_007_MIGRATION.name == (
        "astralplane-089-atlas-revisions"
    )
    assert migrations_module.PLANE_SCHEMA_089_007_REGISTRY_DIGEST == (
        "fe60420679846e7ea06ca5da3cef250cde5ad75a4ca47b6eb15739893a8ebd09"
    )
    assert migrations_module.PLANE_SCHEMA_089_008_MIGRATION.source_revisions == ("089.007",)
    assert migrations_module.PLANE_SCHEMA_089_008_MIGRATION.target_revision == "089.008"
    assert migrations_module.PLANE_SCHEMA_089_008_MIGRATION.name == ("astralplane-089-atlas-links")
    assert MIGRATION_REGISTRY.migrations == (
        PLANE_SCHEMA_067_MIGRATION,
        PLANE_SCHEMA_074_MIGRATION,
        PLANE_SCHEMA_074_002_MIGRATION,
        PLANE_SCHEMA_074_003_MIGRATION,
        PLANE_SCHEMA_074_004_MIGRATION,
        plane_schema_075_migration,
        migrations_module.PLANE_SCHEMA_079_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_002_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_003_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_004_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_005_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_006_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_007_MIGRATION,
        migrations_module.PLANE_SCHEMA_088_008_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_001_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_002_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_003_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_004_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_005_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_006_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_007_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_008_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_009_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_010_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_011_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_012_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_013_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_014_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_015_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_016_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_017_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_018_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_019_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_020_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_021_MIGRATION,
        migrations_module.PLANE_SCHEMA_089_022_MIGRATION,
    )
    assert len(PLANE_SCHEMA_067_STATEMENTS) == 18
    assert {
        "astralplane_outbox",
        "audit_retention_anchor",
        "astralplane_purge_tombstone",
        "astralplane_reconciliation_marker",
    } <= {word for statement in PLANE_SCHEMA_067_STATEMENTS for word in statement.split()}
    canonical_sql = "\n".join(PLANE_SCHEMA_067_STATEMENTS)
    assert "audit_events ADD COLUMN IF NOT EXISTS chain_sequence BIGINT" in canonical_sql
    assert "SET LOCAL audit.allow_purge = 'true'" in canonical_sql
    assert "audit_events_assign_sequence" in canonical_sql
    assert "astralplane_schema_postcondition" in canonical_sql

    authority_sql = "\n".join(PLANE_SCHEMA_074_STATEMENTS)
    assert {
        "astralplane_authority_binding",
        "astralplane_authority_lifecycle_operation",
        "astralplane_protected_effect_operation",
        "astralplane_receipt_sequence_watermark",
        "astralplane_receipt_claim",
    } <= {word for statement in PLANE_SCHEMA_074_STATEMENTS for word in statement.split()}
    assert "uq_astralplane_authority_binding_nonterminal" in authority_sql
    assert "astralplane_receipt_claim_nonce_key" in authority_sql
    assert "astralplane_receipt_claim_sequence_key" in authority_sql
    assert "astralplane_receipt_watermark_require_advance" in authority_sql
    assert "idx_astralplane_outbox_authority_pending" in authority_sql
    assert "astralplane_outbox_payload_size_check" in authority_sql
    assert "astralplane_authority_binding_remote_state_check" in authority_sql
    assert "^pending:warden:[0-9a-f]{32}$" in authority_sql
    assert "lease_expires_at_ns = 0" in authority_sql
    assert "state IN ('provisioning', 'closed')" in authority_sql
    assert "state <> 'provisioning'" in authority_sql
    assert "astralplane_authority_postcondition" in authority_sql

    quality_sql = "\n".join(PLANE_SCHEMA_074_002_STATEMENTS)
    assert {
        "test_runs",
        "test_case_results",
        "test_evidence",
        "audit_entries",
        "latex_artifacts",
    } <= {word for statement in PLANE_SCHEMA_074_002_STATEMENTS for word in statement.split()}
    assert "system:quality-audit" in quality_sql
    assert "test_case_results_owner_run_fk" in quality_sql
    assert "astralplane_quality_audit_postcondition" in quality_sql

    runtime_contract_sql = "\n".join(PLANE_SCHEMA_074_003_STATEMENTS)
    assert "legacy_runtime_contract" in runtime_contract_sql
    assert "agent_host_session_runtime_contract_version_check" in runtime_contract_sql

    attachment_materialization_sql = "\n".join(PLANE_SCHEMA_074_004_STATEMENTS)
    assert "user_attachments_materialization_state_check" in attachment_materialization_sql
    assert "astralplane_blob_owner_state" in attachment_materialization_sql
    assert "astralplane_purge_tombstone_target_shape_check" in attachment_materialization_sql

    speech_backend_sql = "\n".join(plane_schema_075_statements)
    assert "voice_session_speech_backend_075_check" in speech_backend_sql
    assert "ADD COLUMN IF NOT EXISTS speech_backend TEXT" in speech_backend_sql
    assert "SET speech_backend = 'llm_factory'" in speech_backend_sql
    assert "ALTER COLUMN speech_backend SET NOT NULL" in speech_backend_sql
    assert "transport = 'client_local'" in speech_backend_sql

    canonical = json.dumps(
        PLANE_SCHEMA_067_STATEMENTS,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert PLANE_SCHEMA_067_MIGRATION.checksum == hashlib.sha256(canonical).hexdigest()
    canonical_authority = json.dumps(
        PLANE_SCHEMA_074_STATEMENTS,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert PLANE_SCHEMA_074_MIGRATION.checksum == hashlib.sha256(canonical_authority).hexdigest()
    canonical_quality = json.dumps(
        PLANE_SCHEMA_074_002_STATEMENTS,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert PLANE_SCHEMA_074_002_MIGRATION.checksum == hashlib.sha256(canonical_quality).hexdigest()
    canonical_runtime_contract = json.dumps(
        PLANE_SCHEMA_074_003_STATEMENTS,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert (
        PLANE_SCHEMA_074_003_MIGRATION.checksum
        == hashlib.sha256(canonical_runtime_contract).hexdigest()
    )
    canonical_attachment_materialization = json.dumps(
        PLANE_SCHEMA_074_004_STATEMENTS,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert (
        PLANE_SCHEMA_074_004_MIGRATION.checksum
        == hashlib.sha256(canonical_attachment_materialization).hexdigest()
    )
    canonical_speech_backend = json.dumps(
        plane_schema_075_statements,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert (
        plane_schema_075_migration.checksum == hashlib.sha256(canonical_speech_backend).hexdigest()
    )

    declared = DataPlaneRevision(
        schema_revision="067.001",
        read_compatible_from=("066.001",),
        migration_digest="a" * 64,
    )
    assert declared.migration_lock == ADVISORY_LOCK_IDS[0]
    assert declared.accepts_reader_at("066.001")
    assert declared.accepts_reader_at("067.001")
    assert not declared.accepts_reader_at("065.001")


def test_canonical_migration_executes_every_repeat_safe_statement_without_rewriting() -> None:
    transaction = _RecordingTransaction()
    PLANE_SCHEMA_067_MIGRATION.apply(transaction)  # type: ignore[arg-type]

    assert transaction.executions == [(statement, ()) for statement in PLANE_SCHEMA_067_STATEMENTS]

    transaction = _RecordingTransaction()
    PLANE_SCHEMA_074_MIGRATION.apply(transaction)  # type: ignore[arg-type]
    assert transaction.executions == [(statement, ()) for statement in PLANE_SCHEMA_074_STATEMENTS]

    transaction = _RecordingTransaction()
    PLANE_SCHEMA_074_002_MIGRATION.apply(transaction)  # type: ignore[arg-type]
    assert transaction.executions == [
        (statement, ()) for statement in PLANE_SCHEMA_074_002_STATEMENTS
    ]

    transaction = _RecordingTransaction()
    PLANE_SCHEMA_074_003_MIGRATION.apply(transaction)  # type: ignore[arg-type]
    assert transaction.executions == [
        (statement, ()) for statement in PLANE_SCHEMA_074_003_STATEMENTS
    ]

    transaction = _RecordingTransaction()
    PLANE_SCHEMA_074_004_MIGRATION.apply(transaction)  # type: ignore[arg-type]
    assert transaction.executions == [
        (statement, ()) for statement in PLANE_SCHEMA_074_004_STATEMENTS
    ]

    transaction = _RecordingTransaction()
    migrations_module.PLANE_SCHEMA_075_MIGRATION.apply(transaction)  # type: ignore[arg-type]
    assert transaction.executions == [
        (statement, ()) for statement in migrations_module.PLANE_SCHEMA_075_STATEMENTS
    ]


@pytest.mark.parametrize("value", ["66.1", "066.01", "066.0001", "v066.001", "", 1])
def test_revision_format_is_exact(value: object) -> None:
    with pytest.raises(SchemaRevisionError):
        validate_revision(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "arguments",
    [
        {"read_compatible_from": ()},
        {"read_compatible_from": ["065.001"]},
        {"read_compatible_from": ("065.001", "065.001")},
        {"migration_digest": "A" * 64},
        {"advisory_lock_ids": ()},
        {"advisory_lock_ids": [(1, 2)]},
        {"advisory_lock_ids": ((1,),)},
        {"advisory_lock_ids": ((1, "two"),)},
        {"accepted_predecessor_digests": []},
        {"accepted_predecessor_digests": (("065.001",),)},
        {"accepted_predecessor_digests": (("064.001", "a" * 64),)},
        {
            "accepted_predecessor_digests": (
                ("065.001", "a" * 64),
                ("065.001", "b" * 64),
            )
        },
        {"accepted_predecessor_digests": (("065.001", "A" * 64),)},
    ],
)
def test_invalid_revision_metadata_fails_closed(arguments: dict[str, object]) -> None:
    values: dict[str, object] = {
        "schema_revision": "066.001",
        "read_compatible_from": ("065.001",),
        "migration_digest": "a" * 64,
    }
    values.update(arguments)
    with pytest.raises(SchemaRevisionError):
        DataPlaneRevision(**values)  # type: ignore[arg-type]
