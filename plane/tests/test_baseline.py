"""Tests for src/astralplane/database/baseline.py: fresh-install detection, structural
recovery from partial or legacy state, and postcondition-checked baseline runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import astralplane.database.baseline as baseline_module
from astralplane.api import PlaneRuntime
from astralplane.database.baseline import (
    BASELINE_MIGRATION_NAME,
    BASELINE_REQUIRED_TABLES,
    BASELINE_REVISION,
    BaselineCompatibilityState,
    BaselineMigrationRunner,
    _LegacyCursorAdapter,
    initialize_empty_database,
    inspect_baseline_compatibility,
)
from astralplane.database.legacy_baseline_066 import _LegacyBaseline066Builder
from astralplane.database.migrations import MigrationReport
from astralplane.database.transaction import CommandResult, DetachedRecord
from astralplane.errors import SchemaRevisionError

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


class _Transaction:
    def __init__(
        self,
        *,
        tables: set[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.tables = set(tables or ())
        self.metadata = dict(metadata or {})
        self.locks: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: object = ()) -> CommandResult:
        match = _CREATE_TABLE.search(statement)
        if match is not None:
            self.tables.add(match.group(1))
        if statement.lstrip().startswith("INSERT INTO schema_meta"):
            key, value = tuple(parameters)  # type: ignore[arg-type]
            self.metadata[str(key)] = str(value)
        return CommandResult(rowcount=0, status_message="OK")

    def fetch_one(self, statement: str, parameters: object = ()) -> DetachedRecord | None:
        if "pg_advisory_xact_lock" in statement:
            self.locks.append(tuple(parameters))  # type: ignore[arg-type]
            return DetachedRecord({"pg_advisory_xact_lock": None})
        raise AssertionError(f"unexpected fetch_one statement: {statement}")

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[DetachedRecord, ...]:
        del parameters
        if "information_schema.tables" in statement:
            return tuple(
                DetachedRecord({"table_name": table}) for table in sorted(self.tables)
            )
        if "FROM schema_meta" in statement:
            return tuple(
                DetachedRecord({"key": key, "value": value})
                for key, value in sorted(self.metadata.items())
            )
        raise AssertionError(f"unexpected fetch_all statement: {statement}")


class _Database:
    def __init__(self, transaction: _Transaction) -> None:
        self._transaction = transaction

    @contextmanager
    def transaction(self, *, isolation: object = None) -> Iterator[_Transaction]:
        del isolation
        yield self._transaction


class _SyntheticBaselineBuilder:
    def apply(self, cursor: object) -> None:
        for table in sorted(BASELINE_REQUIRED_TABLES):
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT)")  # type: ignore[attr-defined]


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self._next_record: object | None = None
        self.rowcount = 0

    def execute(self, statement: str, parameters: object = ()) -> None:
        self.statements.append((statement, parameters))
        if "to_regclass('operation_admission_class') AS relation_name" in statement:
            self._next_record = {"relation_name": "operation_admission_class"}
        elif "AS admission_class" in statement and "AS messages" in statement:
            self._next_record = {
                "admission_class": "operation_admission_class",
                "admission_slot": "operation_admission_slot",
                "background_task": "background_task",
                "conversation_commit": "conversation_commit",
                "messages": "messages",
                "operation_record": "operation_record",
                "workspace_layout": "workspace_layout",
            }
        else:
            self._next_record = None

    def fetchone(self) -> object | None:
        record = self._next_record
        self._next_record = None
        return record


class _FixedRecordCursor(_RecordingCursor):
    def __init__(self, record: object) -> None:
        super().__init__()
        self._record = record

    def execute(self, statement: str, parameters: object = ()) -> None:
        self.statements.append((statement, parameters))

    def fetchone(self) -> object:
        return self._record


def _compatible_database(*, revision: str = BASELINE_REVISION) -> _Database:
    return _Database(
        _Transaction(
            tables={"schema_meta", *BASELINE_REQUIRED_TABLES},
            metadata={"revision": revision},
        )
    )


def test_empty_database_is_recognized_without_mutation() -> None:
    transaction = _Transaction()

    report = inspect_baseline_compatibility(_Database(transaction))

    assert report.state is BaselineCompatibilityState.EMPTY
    assert report.compatible
    assert report.to_dict()["state"] == "empty"
    assert transaction.tables == set()


def test_legacy_cursor_adapter_detaches_and_consumes_returned_records() -> None:
    class ReturningTransaction(_Transaction):
        def execute(self, statement: str, parameters: object = ()) -> CommandResult:
            del statement, parameters
            return CommandResult(
                rowcount=1,
                status_message="SELECT 1",
                returned_records=(DetachedRecord({"value": "detached"}),),
            )

    cursor = _LegacyCursorAdapter(ReturningTransaction())  # type: ignore[arg-type]
    cursor.execute("SELECT 1")

    assert cursor.rowcount == 1
    assert cursor.fetchone() == {"value": "detached"}
    assert cursor.fetchone() is None


def test_runtime_exposes_structural_baseline_inspection() -> None:
    database = _compatible_database()
    runtime = PlaneRuntime(
        pool=object(),  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        initializer=object(),  # type: ignore[arg-type]
        reconciler=object(),  # type: ignore[arg-type]
    )

    report = runtime.inspect_baseline_compatibility()

    assert report.state is BaselineCompatibilityState.COMPATIBLE
    assert report.observed_revision == BASELINE_REVISION


def test_initialize_empty_database_is_locked_repeat_safe_and_postconditioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(baseline_module, "_LegacyBaseline066Builder", _SyntheticBaselineBuilder)
    transaction = _Transaction()
    database = _Database(transaction)

    first = initialize_empty_database(database)
    second = initialize_empty_database(database)

    assert first.initialized
    assert not second.initialized
    assert first.compatibility.observed_revision == BASELINE_REVISION
    assert transaction.metadata["revision"] == BASELINE_REVISION
    assert transaction.tables >= BASELINE_REQUIRED_TABLES
    assert len(transaction.locks) == 2


def test_schema_meta_only_shell_is_recoverable_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(baseline_module, "_LegacyBaseline066Builder", _SyntheticBaselineBuilder)
    transaction = _Transaction(tables={"schema_meta"})

    report = initialize_empty_database(_Database(transaction))

    assert report.initialized
    assert transaction.metadata == {"revision": BASELINE_REVISION}


@pytest.mark.parametrize(
    "revision",
    ["067.001", "079.001", "088.001", "088.002", "088.003", "088.006", "088.007", "088.008"],
)
def test_existing_structurally_complete_database_is_not_rewritten(revision: str) -> None:
    database = _compatible_database(revision=revision)

    report = initialize_empty_database(database)

    assert not report.initialized
    assert report.compatibility.state is BaselineCompatibilityState.COMPATIBLE
    assert report.compatibility.observed_revision == revision


@pytest.mark.parametrize(
    ("tables", "metadata", "reason"),
    [
        ({"chats"}, {}, "schema_metadata_missing"),
        (
            {"schema_meta", *BASELINE_REQUIRED_TABLES},
            {},
            "schema_revision_missing",
        ),
        (
            {"schema_meta", *BASELINE_REQUIRED_TABLES},
            {"revision": "999.001"},
            "schema_revision_unknown",
        ),
        (
            {"schema_meta", *(BASELINE_REQUIRED_TABLES - {"audit_events"})},
            {"revision": BASELINE_REVISION},
            "baseline_tables_missing",
        ),
    ],
)
def test_partial_or_unknown_database_fails_closed(
    tables: set[str],
    metadata: dict[str, str],
    reason: str,
) -> None:
    database = _Database(_Transaction(tables=tables, metadata=metadata))

    report = inspect_baseline_compatibility(database)

    assert report.state is BaselineCompatibilityState.INCOMPATIBLE
    assert reason in report.reason_codes
    with pytest.raises(SchemaRevisionError):
        initialize_empty_database(database)


def test_baseline_runner_reports_the_fresh_install_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(baseline_module, "_LegacyBaseline066Builder", _SyntheticBaselineBuilder)
    database = _Database(_Transaction())

    class Delegate:
        def run(self, *, expected_revision: str) -> MigrationReport:
            return MigrationReport(
                source_revision=BASELINE_REVISION,
                target_revision=expected_revision,
                applied_steps=("to-067", "to-074"),
                already_current=False,
                migration_digest="a" * 64,
            )

    report = BaselineMigrationRunner(database, Delegate()).run(expected_revision="074.001")

    assert report.source_revision is None
    assert report.applied_steps == (BASELINE_MIGRATION_NAME, "to-067", "to-074")


def test_baseline_runner_validates_delegate_and_preserves_existing_report() -> None:
    database = _compatible_database()
    with pytest.raises(TypeError):
        BaselineMigrationRunner(database, object())

    expected = MigrationReport(
        source_revision=BASELINE_REVISION,
        target_revision="074.001",
        applied_steps=("existing",),
        already_current=False,
        migration_digest="b" * 64,
    )

    class ExistingDelegate:
        def run(self, *, expected_revision: str) -> MigrationReport:
            assert expected_revision == "074.001"
            return expected

    assert (
        BaselineMigrationRunner(database, ExistingDelegate()).run(
            expected_revision="074.001"
        )
        is expected
    )

    class InvalidDelegate:
        def run(self, *, expected_revision: str) -> object:
            return expected_revision

    with pytest.raises(TypeError):
        BaselineMigrationRunner(database, InvalidDelegate()).run(
            expected_revision="074.001"
        )


def test_baseline_postcondition_failure_rolls_back_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteBuilder:
        def apply(self, cursor: object) -> None:
            cursor.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT)")  # type: ignore[attr-defined]

    monkeypatch.setattr(baseline_module, "_LegacyBaseline066Builder", IncompleteBuilder)

    with pytest.raises(SchemaRevisionError, match="postcondition"):
        initialize_empty_database(_Database(_Transaction()))


def test_extracted_baseline_covers_the_declared_table_inventory() -> None:
    source = (
        baseline_module.__file__
        and Path(baseline_module.__file__).with_name("legacy_baseline_066.py")
    )
    assert source is not None
    content = source.read_text(encoding="utf-8")
    observed = frozenset(match.group(1) for match in _CREATE_TABLE.finditer(content))

    assert observed == BASELINE_REQUIRED_TABLES
    assert "orchestrator." not in content
    assert "backend.shared" not in content
    assert "tokens truncated" not in content


def test_extracted_baseline_executes_every_schema_chunk_without_host_state() -> None:
    cursor = _RecordingCursor()

    _LegacyBaseline066Builder().apply(cursor)

    observed = frozenset(
        match.group(1)
        for statement, _ in cursor.statements
        for match in _CREATE_TABLE.finditer(statement)
    )
    assert observed == BASELINE_REQUIRED_TABLES
    assert any("idx_messages_chat_user_ts" in statement for statement, _ in cursor.statements)
    assert any(
        "idx_message_attachment_message" in statement for statement, _ in cursor.statements
    )


def test_extracted_baseline_handles_driver_row_shapes_and_missing_prerequisites() -> None:
    builder = _LegacyBaseline066Builder()
    sequence_row = _FixedRecordCursor(("operation_admission_class",))
    builder._migrate_mcp_admission_064(sequence_row)
    assert any("064-defaults" in statement for statement, _ in sequence_row.statements)

    missing_relation = _FixedRecordCursor((None,))
    builder._migrate_mcp_admission_064(missing_relation)
    assert len(missing_relation.statements) == 1

    missing_voice = _FixedRecordCursor(
        {
            "admission_class": "operation_admission_class",
            "admission_slot": "operation_admission_slot",
            "background_task": "background_task",
            "conversation_commit": "conversation_commit",
            "messages": None,
            "operation_record": "operation_record",
            "workspace_layout": "workspace_layout",
        }
    )
    with pytest.raises(SchemaRevisionError, match=r"required 064\.001 relation"):
        builder._migrate_conversational_voice_065(missing_voice)
