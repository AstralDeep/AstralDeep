"""Guarded fresh-install baseline installer and structural compatibility inspector, used
by database/bootstrap.py and api.py before any migration runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from astralplane.contracts import Parameters, Transaction
from astralplane.contracts import PlaneDatabase as PlaneDatabaseContract
from astralplane.database.dialect import dialect_of
from astralplane.database.legacy_baseline_066 import (
    LEGACY_BASELINE_SOURCE_BLOB,
    _LegacyBaseline066Builder,
)
from astralplane.database.migrations import MigrationReport
from astralplane.database.revision import ADVISORY_LOCK_IDS
from astralplane.errors import SchemaRevisionError

BASELINE_REVISION: Final = "066.001"
BASELINE_MIGRATION_NAME: Final = "astralplane-066-fresh-install-baseline"

_SCHEMA_MIGRATION_LOCK: Final = ADVISORY_LOCK_IDS[0]
_KNOWN_REVISIONS: Final = frozenset(
    {
        BASELINE_REVISION,
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
        "089.022",
    }
)
_SCHEMA_META_TABLE: Final = "schema_meta"
_SCHEMA_META_REVISION: Final = "revision"

BASELINE_REQUIRED_TABLES: Final = frozenset(
    {
        "agent_host_session",
        "agent_ownership",
        "agent_runtime_instance",
        "agent_runtime_request",
        "agent_scopes",
        "agent_trust",
        "attachment_parser",
        "audit_events",
        "auth_revocation_queue",
        "background_task",
        "chat_files",
        "chat_steps",
        "chats",
        "component_feedback",
        "component_version",
        "consolidation_sweep",
        "conversation_commit",
        "draft_agents",
        "draft_artifact_publication",
        "draft_transition",
        "effect_ledger",
        "interaction_log",
        "job_run",
        "knowledge_update_proposal",
        "logs",
        "machine_credential",
        "maintenance_unit",
        "maintenance_unit_input",
        "memory_item",
        "memory_link",
        "message_attachment",
        "messages",
        "onboarding_state",
        "operation_admission_class",
        "operation_admission_slot",
        "operation_record",
        "operation_submission_result",
        "quarantine_entry",
        "remote_machine",
        "remote_operation_proposal",
        "saved_components",
        "scheduled_job",
        "scheduled_occurrence",
        "share_grant",
        "short_term_signal",
        "system_llm_config",
        "tool_overrides",
        "tool_permissions",
        "tool_quality_signal",
        "tracked_job",
        "tutorial_step",
        "tutorial_step_revision",
        "user_agent",
        "user_agent_revision",
        "user_attachments",
        "user_credentials",
        "user_llm_config",
        "user_offline_grant",
        "user_persona",
        "user_personalization",
        "user_preferences",
        "users",
        "voice_session",
        "voice_turn",
        "web_session",
        "workspace_layout",
        "workspace_snapshot",
    }
)

_ADVISORY_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(%s, %s)"
_LIST_TABLES_SQL: Final = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = current_schema()
  AND table_type = 'BASE TABLE'
ORDER BY table_name
""".strip()
_LIST_TABLES_SQLITE_SQL: Final = """
SELECT name AS table_name
FROM sqlite_master
WHERE type = 'table' AND substr(name, 1, 7) <> 'sqlite_'
ORDER BY name
""".strip()
_READ_METADATA_SQL: Final = """
SELECT key, value
FROM schema_meta
ORDER BY key
""".strip()
_CREATE_SCHEMA_META_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""".strip()
_WRITE_REVISION_SQL: Final = """
INSERT INTO schema_meta (key, value)
VALUES (%s, %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
""".strip()


class BaselineCompatibilityState(StrEnum):
    EMPTY = "empty"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class BaselineCompatibilityReport:
    state: BaselineCompatibilityState
    observed_revision: str | None
    table_count: int
    missing_required_tables: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        return self.state is not BaselineCompatibilityState.INCOMPATIBLE

    @property
    def empty(self) -> bool:
        return self.state is BaselineCompatibilityState.EMPTY

    def to_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "empty": self.empty,
            "missing_required_tables": list(self.missing_required_tables),
            "observed_revision": self.observed_revision,
            "reason_codes": list(self.reason_codes),
            "state": self.state.value,
            "table_count": self.table_count,
        }


@dataclass(frozen=True, slots=True)
class BaselineInitializationReport:
    initialized: bool
    compatibility: BaselineCompatibilityReport
    source_blob: str = LEGACY_BASELINE_SOURCE_BLOB


class _LegacyCursorAdapter:
    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction
        self._records: tuple[object, ...] = ()
        self._offset = 0
        self.rowcount = -1

    def execute(self, statement: str, parameters: Parameters = ()) -> None:
        result = self._transaction.execute(statement, parameters)
        self._records = result.returned_records
        self._offset = 0
        self.rowcount = result.rowcount

    def fetchone(self) -> object | None:
        if self._offset >= len(self._records):
            return None
        record = self._records[self._offset]
        self._offset += 1
        return record


def _inspect_transaction(transaction: Transaction) -> BaselineCompatibilityReport:
    table_rows = transaction.fetch_all(
        dialect_of(transaction).choose(postgres=_LIST_TABLES_SQL, sqlite=_LIST_TABLES_SQLITE_SQL)
    )
    tables = frozenset(str(row["table_name"]) for row in table_rows)
    if not tables:
        return BaselineCompatibilityReport(
            state=BaselineCompatibilityState.EMPTY,
            observed_revision=None,
            table_count=0,
        )

    if _SCHEMA_META_TABLE not in tables:
        return BaselineCompatibilityReport(
            state=BaselineCompatibilityState.INCOMPATIBLE,
            observed_revision=None,
            table_count=len(tables),
            missing_required_tables=(_SCHEMA_META_TABLE,),
            reason_codes=("schema_metadata_missing",),
        )

    metadata_rows = transaction.fetch_all(_READ_METADATA_SQL)
    metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
    revision = metadata.get(_SCHEMA_META_REVISION)
    application_tables = tables - {_SCHEMA_META_TABLE}
    if not application_tables and not metadata:
        # Metadata-only table counts as empty (partial prior run)
        return BaselineCompatibilityReport(
            state=BaselineCompatibilityState.EMPTY,
            observed_revision=None,
            table_count=1,
        )

    reasons: list[str] = []
    if revision is None:
        reasons.append("schema_revision_missing")
    elif revision not in _KNOWN_REVISIONS:
        reasons.append("schema_revision_unknown")
    missing = tuple(sorted(BASELINE_REQUIRED_TABLES - application_tables))
    if missing:
        reasons.append("baseline_tables_missing")
    state = (
        BaselineCompatibilityState.INCOMPATIBLE
        if reasons
        else BaselineCompatibilityState.COMPATIBLE
    )
    return BaselineCompatibilityReport(
        state=state,
        observed_revision=revision,
        table_count=len(tables),
        missing_required_tables=missing,
        reason_codes=tuple(reasons),
    )


def inspect_baseline_compatibility(
    database: PlaneDatabaseContract,
) -> BaselineCompatibilityReport:
    with database.transaction() as transaction:
        return _inspect_transaction(transaction)


def initialize_empty_database(
    database: PlaneDatabaseContract,
) -> BaselineInitializationReport:
    with database.transaction() as transaction:
        transaction.fetch_one(_ADVISORY_LOCK_SQL, _SCHEMA_MIGRATION_LOCK)
        before = _inspect_transaction(transaction)
        if not before.compatible:
            raise SchemaRevisionError(
                "database is neither empty nor a structurally compatible Astral baseline",
                metadata={
                    "observed_revision": before.observed_revision or "<empty>",
                    "reason_codes": ",".join(before.reason_codes),
                },
            )
        if not before.empty:
            return BaselineInitializationReport(initialized=False, compatibility=before)

        cursor = _LegacyCursorAdapter(transaction)
        _LegacyBaseline066Builder().apply(cursor)
        transaction.execute(_CREATE_SCHEMA_META_SQL)
        transaction.execute(_WRITE_REVISION_SQL, (_SCHEMA_META_REVISION, BASELINE_REVISION))
        after = _inspect_transaction(transaction)
        if (
            after.state is not BaselineCompatibilityState.COMPATIBLE
            or after.observed_revision != BASELINE_REVISION
        ):
            raise SchemaRevisionError("fresh-install baseline failed its locked postcondition")
        return BaselineInitializationReport(initialized=True, compatibility=after)


class BaselineMigrationRunner:
    def __init__(self, database: PlaneDatabaseContract, runner: object) -> None:
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner must expose run(expected_revision=...)")
        self._database = database
        self._runner = runner

    def run(self, *, expected_revision: str) -> MigrationReport:
        baseline = initialize_empty_database(self._database)
        report = self._runner.run(expected_revision=expected_revision)
        if not isinstance(report, MigrationReport):
            raise TypeError("delegate migration runner returned an invalid report")
        if not baseline.initialized:
            return report
        return MigrationReport(
            source_revision=None,
            target_revision=report.target_revision,
            applied_steps=(BASELINE_MIGRATION_NAME, *report.applied_steps),
            already_current=False,
            migration_digest=report.migration_digest,
        )


__all__ = (
    "BASELINE_MIGRATION_NAME",
    "BASELINE_REQUIRED_TABLES",
    "BASELINE_REVISION",
    "BaselineCompatibilityReport",
    "BaselineCompatibilityState",
    "BaselineInitializationReport",
    "BaselineMigrationRunner",
    "initialize_empty_database",
    "inspect_baseline_compatibility",
)
